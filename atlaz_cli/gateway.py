"""
Gateway subcommand for hermes CLI.

Handles: hermes gateway [run|start|stop|restart|status|install|uninstall|setup]
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from gateway.status import terminate_pid
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    parse_restart_drain_timeout,
)
from atlaz_cli.config import (
    get_env_value,
    get_hermes_home,
    is_managed,
    managed_error,
    read_raw_config,
    save_env_value,
)
# display_hermes_home is imported lazily at call sites to avoid ImportError
# when atlaz_constants is cached from a pre-update version during `hermes update`.
from atlaz_cli.setup import (
# when atlaz_constants is cached from a pre-update version during `hermes update`.
from atlaz_cli.setup import (
    print_header, print_info, print_success, print_warning, print_error,
    prompt, prompt_choice, prompt_yes_no,
)
from atlaz_cli.colors import Colors, color

logger = logging.getLogger(__name__)

# =============================================================================
# Process Management (for manual gateway runs)
# =============================================================================


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str
    service_installed: bool = False
    service_running: bool = False
    gateway_pids: tuple[int, ...] = ()
    service_scope: str | None = None

    @property
    def running(self) -> bool:
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        return self.service_installed and self.running and not self.service_running


@dataclass(frozen=True)
class ProfileGatewayProcess:
    profile: str
    path: Path
    pid: int

def _get_service_pids() -> set:
    """Return PIDs currently managed by systemd or launchd gateway services.

    Used to avoid killing freshly-restarted service processes when sweeping
    for stale manual gateway processes after a service restart.  Relies on the
    service manager having committed the new PID before the restart command
    returns (true for both systemd and launchd in practice).
    """
    pids: set = set()

    # --- systemd (Linux): user and system scopes ---
    if supports_systemd_services():
        for scope_args in [["systemctl", "--user"], ["systemctl"]]:
            try:
                result = subprocess.run(
                    scope_args + ["list-units", "hermes-gateway*",
                                  "--plain", "--no-legend", "--no-pager"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if not parts or not parts[0].endswith(".service"):
                        continue
                    svc = parts[0]
                    try:
                        show = subprocess.run(
                            scope_args + ["show", svc,
                                          "--property=MainPID", "--value"],
                            capture_output=True, text=True, timeout=5,
                        )
                        pid = int(show.stdout.strip())
                        if pid > 0:
                            pids.add(pid)
                    except (ValueError, subprocess.TimeoutExpired):
                        pass
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    # --- launchd (macOS) ---
    if is_macos():
        try:
            label = get_launchd_label()
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                # Output: "PID\tStatus\tLabel" header, then one data line
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] == label:
                        try:
                            pid = int(parts[0])
                            if pid > 0:
                                pids.add(pid)
                        except ValueError:
                            pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return pids


def _get_parent_pid(pid: int) -> int | None:
    """Return the parent PID for ``pid``, or ``None`` when unavailable.

    Uses psutil (core dependency) which works on every platform.  The
    older implementation shelled out to ``ps -o ppid= -p <pid>``, which
    silently fails on Windows (no ``ps``) so the ancestor walk terminated
    at self — the caller's dedup / exclude logic then couldn't distinguish
    "hermes CLI that invoked this scan" from "real gateway process".
    """
    if pid <= 1:
        return None
    try:
        import psutil  # type: ignore
        return psutil.Process(pid).ppid() or None
    except ImportError:
        pass
    except Exception:
        return None
    # Fallback: shell out to ps (POSIX only — bare ``ps`` doesn't exist on Windows).
    if not shutil.which("ps"):
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        parent_pid = int(raw.splitlines()[-1].strip())
    except ValueError:
        return None
    return parent_pid if parent_pid > 0 else None


def _is_pid_ancestor_of_current_process(target_pid: int) -> bool:
    """Return True when ``target_pid`` is this process or one of its ancestors."""
    if target_pid <= 0:
        return False

    pid = os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen:
        if pid == target_pid:
            return True
        seen.add(pid)
        pid = _get_parent_pid(pid) or 0
    return False


def _request_gateway_self_restart(pid: int) -> bool:
    """Ask a running gateway ancestor to restart itself asynchronously."""
    if not hasattr(signal, "SIGUSR1"):
        return False
    if not _is_pid_ancestor_of_current_process(pid):
        return False
    try:
        os.kill(pid, signal.SIGUSR1)  # windows-footgun: ok — POSIX signal, guarded by hasattr(signal, 'SIGUSR1') above
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _graceful_restart_via_sigusr1(pid: int, drain_timeout: float) -> bool:
    """Send SIGUSR1 to a gateway PID and wait for it to exit gracefully.

    SIGUSR1 is wired in gateway/run.py to ``request_restart(via_service=True)``
    which drains in-flight agent runs (up to ``agent.restart_drain_timeout``
    seconds), then exits with code 75.  Both systemd (``Restart=always``
    + ``RestartForceExitStatus=75``) and launchd (``KeepAlive.SuccessfulExit
    = false``) relaunch the process after the graceful exit.

    This is the drain-aware alternative to ``systemctl restart`` / ``SIGTERM``,
    which SIGKILL in-flight agents after a short timeout.

    Args:
        pid: Gateway process PID (systemd MainPID, launchd PID, or bare
            process PID).
        drain_timeout: Seconds to wait for the process to exit after sending
            SIGUSR1.  Should be slightly larger than the gateway's
            ``agent.restart_drain_timeout`` to allow the drain loop to
            finish cleanly.

    Returns:
        True if the PID was signalled and exited within the timeout.
        False if SIGUSR1 couldn't be sent or the process didn't exit in
        time (caller should fall back to a harder restart path).
    """
    if not hasattr(signal, "SIGUSR1"):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)  # windows-footgun: ok — POSIX signal, guarded by hasattr(signal, 'SIGUSR1') above
    except ProcessLookupError:
        # Already gone — nothing to drain.
        return True
    except (PermissionError, OSError):
        return False

    import time as _time

    deadline = _time.monotonic() + max(drain_timeout, 1.0)
    # IMPORTANT Windows note: ``os.kill(pid, 0)`` is NOT a no-op on
    # Windows — Python's implementation calls ``TerminateProcess(handle, 0)``
    # for sig=0, hard-killing the target. Use the cross-platform
    # ``_pid_exists`` helper in gateway.status which does OpenProcess +
    # WaitForSingleObject on Windows.
    from gateway.status import _pid_exists

    while _time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        _time.sleep(0.5)
    # Drain didn't finish in time.
    return False


def _get_ancestor_pids() -> set[int]:
    """Return the set of PIDs in the current process's ancestor chain.

    Walks from the current PID up to PID 1 (init) so that process-table scans
    never match the calling CLI process or any of its parents.  This prevents
    ``hermes gateway status`` from falsely counting the ``hermes`` CLI that
    invoked it as a running gateway instance (see #13242).
    """
    ancestors: set[int] = set()
    pid = os.getpid()
    # Cap iterations to avoid infinite loops on exotic platforms.
    for _ in range(64):
        ancestors.add(pid)
        parent = _get_parent_pid(pid)
        if parent is None or parent <= 0 or parent in ancestors:
            break
        pid = parent
    return ancestors


def _append_unique_pid(pids: list[int], pid: int | None, exclude_pids: set[int]) -> None:
    if pid is None or pid <= 0:
        return
    if pid == os.getpid() or pid in exclude_pids or pid in pids:
        return
    pids.append(pid)


def _scan_gateway_pids(exclude_pids: set[int], all_profiles: bool = False) -> list[int]:
    """Best-effort process-table scan for gateway PIDs.

    This supplements the profile-scoped PID file so status views can still spot
    a live gateway when the PID file is stale/missing, and ``--all`` sweeps can
    discover gateways outside the current profile.
    """
    # Exclude the entire ancestor chain so the CLI process that invoked this
    # scan (e.g. ``hermes gateway status``) is never mistaken for a running
    # gateway.  See #13242.
    exclude_pids = exclude_pids | _get_ancestor_pids()
    pids: list[int] = []
    patterns = [
        "atlaz_cli.main gateway",
        "atlaz_cli.main --profile",
        "atlaz_cli.main -p",
        "atlaz_cli/main.py gateway",
        "atlaz_cli/main.py --profile",
        "atlaz_cli/main.py -p",
        "hermes gateway",
        "gateway/run.py",
    ]
    current_home = str(get_hermes_home().resolve())
    current_profile_arg = _profile_arg(current_home)
    current_profile_name = current_profile_arg.split()[-1] if current_profile_arg else ""

    def _matches_current_profile(command: str) -> bool:
        if current_profile_name:
            return (
                f"--profile {current_profile_name}" in command
                or f"-p {current_profile_name}" in command
                or f"HERMES_HOME={current_home}" in command
            )

        # Default-profile case: no profile flag in argv. Accept as long as
        # the command doesn't advertise *some other* profile. HERMES_HOME
        # may be passed via env (not visible in wmic/CIM command line) so
        # its absence is NOT disqualifying — only a non-matching explicit
        # HERMES_HOME= in argv is.
        if "--profile " in command or " -p " in command:
            return False
        if "HERMES_HOME=" in command and f"HERMES_HOME={current_home}" not in command:
            return False
        return True

    try:
        if is_windows():
            # Prefer wmic when present (fast, stable output format).  On
            # modern Windows 11 / Win 10 late builds, wmic has been
            # removed as part of the WMIC deprecation — fall back to
            # PowerShell's Get-CimInstance.  Any OSError here (FileNotFoundError
            # on missing wmic) trips the fallback.
            wmic_path = shutil.which("wmic")
            used_fallback = False
            result = None
            if wmic_path is not None:
                try:
                    result = subprocess.run(
                        [wmic_path, "process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                        timeout=10,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    result = None
            if result is None or result.returncode != 0 or not (result.stdout or ""):
                # Fallback: PowerShell Get-CimInstance, emit LIST-style output
                # so the downstream parser below doesn't need to branch.
                powershell = shutil.which("powershell") or shutil.which("pwsh")
                if powershell is None:
                    return []
                ps_cmd = (
                    "Get-CimInstance Win32_Process | "
                    "ForEach-Object { "
                    "  'CommandLine=' + ($_.CommandLine -replace \"`r`n\",' ' -replace \"`n\",' '); "
                    "  'ProcessId=' + $_.ProcessId; "
                    "  '' "
                    "}"
                )
                try:
                    result = subprocess.run(
                        [powershell, "-NoProfile", "-Command", ps_cmd],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                        timeout=15,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    return []
                used_fallback = True
            if result.returncode != 0 or result.stdout is None:
                return []
            current_cmd = ""
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("CommandLine="):
                    current_cmd = line[len("CommandLine="):]
                elif line.startswith("ProcessId="):
                    pid_str = line[len("ProcessId="):]
                    if any(p in current_cmd for p in patterns) and (
                        all_profiles or _matches_current_profile(current_cmd)
                    ):
                        try:
                            _append_unique_pid(pids, int(pid_str), exclude_pids)
                        except ValueError:
                            pass
                    current_cmd = ""
        else:
            # Try /proc first (works in Docker without procps installed),
            # fall back to ps -A eww.
            _found_via_proc = False
            if os.path.isdir("/proc"):
                try:
                    my_pid = os.getpid()
                    for entry in os.listdir("/proc"):
                        if not entry.isdigit():
                            continue
                        pid = int(entry)
                        if pid == my_pid or pid in exclude_pids:
                            continue
                        try:
                            cmdline = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", errors="replace")
                            cmdline = cmdline.replace("\x00", " ")
                            if any(p in cmdline for p in patterns) and (
                                all_profiles or _matches_current_profile(cmdline)
                            ):
                                _append_unique_pid(pids, pid, exclude_pids)
                        except (OSError, PermissionError):
                            continue
                    _found_via_proc = True
                except Exception:
                    pass

            if not _found_via_proc:
                result = subprocess.run(
                    ["ps", "-A", "eww", "-o", "pid=,command="],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    return []
                for line in result.stdout.split("\n"):
                    stripped = line.strip()
                    if not stripped or "grep" in stripped:
                        continue

                    pid = None
                    command = ""

                    parts = stripped.split(None, 1)
                    if len(parts) == 2:
                        try:
                            pid = int(parts[0])
                            command = parts[1]
                        except ValueError:
                            pid = None

                    if pid is None:
                        aux_parts = stripped.split()
                        if len(aux_parts) > 10 and aux_parts[1].isdigit():
                            pid = int(aux_parts[1])
                            command = " ".join(aux_parts[10:])

                    if pid is None:
                        continue
                    if any(pattern in command for pattern in patterns) and (
                        all_profiles or _matches_current_profile(command)
                    ):
                        _append_unique_pid(pids, pid, exclude_pids)
    except (OSError, subprocess.TimeoutExpired):
        return []

    # Windows-specific: collapse venv launcher stubs.  A venv-built
    # ``pythonw.exe`` in ``<venv>/Scripts/`` is a ~100 KB launcher exe
    # that spawns the base Python (e.g. ``C:\Program Files\Python311\
    # pythonw.exe``) with the same command line, preserving the venv's
    # ``pyvenv.cfg`` context.  This is standard Windows CPython venv
    # behaviour — BUT it means every gateway run produces two pythonw
    # PIDs with identical command lines (one launcher stub, one actual
    # interpreter) which is confusing in ``gateway status`` output.
    # Filter the stub: if a PID in our result is the PARENT of another
    # PID in our result, and both are pythonw.exe, the parent is the
    # launcher stub — drop it, keep the child.
    if is_windows() and len(pids) > 1:
        pids = _filter_venv_launcher_stubs(pids)

    return pids


def _filter_venv_launcher_stubs(pids: list[int]) -> list[int]:
    """Drop venv-launcher ``pythonw.exe`` stubs that are parents of the real
    interpreter process.  See comment at the tail of ``_scan_gateway_pids``.

    Uses ``psutil`` (core dependency).  Safe on any platform; only invoked
    on Windows by the caller because the stub pattern is Windows-specific.
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        return pids

    pid_set = set(pids)
    # Collect each PID's parent so we can flag "child of another matched PID".
    parent_of: dict[int, int | None] = {}
    for pid in pids:
        try:
            parent_of[pid] = psutil.Process(pid).ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent_of[pid] = None

    # For each child whose parent is also in our set, drop the parent.
    drop: set[int] = set()
    for pid, ppid in parent_of.items():
        if ppid is not None and ppid in pid_set:
            drop.add(ppid)

    return [p for p in pids if p not in drop]


def find_gateway_pids(exclude_pids: set | None = None, all_profiles: bool = False) -> list:
    """Find PIDs of running gateway processes.

   

... [OUTPUT TRUNCATED - 184808 chars omitted out of 234808 total] ...

:
            mgr.stop(service_name)
        elif action == "restart":
            mgr.restart(service_name)
        else:
            return False
    except GatewayNotRegisteredError as exc:
        print(f"✗ {exc}")
        sys.exit(1)
    except S6CommandError as exc:
        print(f"✗ {exc}")
        sys.exit(1)
    return True


def _dispatch_all_via_service_manager_if_s6(action: str) -> bool:
    """Inside a container with s6, dispatch ``--all`` lifecycle to every
    registered profile gateway.

    Returns True iff dispatched (caller should ``return``); False
    otherwise — caller continues with the host-side code path.

    Without this, ``hermes gateway stop --all`` and ``... restart --all``
    fall through to ``kill_gateway_processes(all_profiles=True)``, which
    just ``pkill``s every gateway process. s6-supervise observes the
    crash and restarts each one ~1s later — so ``--all`` ends up
    *kicking* every gateway instead of *stopping* it. By iterating
    ``list_profile_gateways()`` and sending the lifecycle command
    through the service manager we get the intended semantics (s6's
    ``want up``/``want down`` flips correctly so supervise stays down
    after a stop).

    ``action`` is one of ``stop`` / ``restart`` (``start --all`` isn't
    a supported CLI surface).
    """
    from atlaz_cli.service_manager import (
        detect_service_manager,
        get_service_manager,
    )

    if detect_service_manager() != "s6":
        return False
    if action not in ("stop", "restart"):
        return False
    mgr = get_service_manager()
    profiles = mgr.list_profile_gateways()
    if not profiles:
        print("✗ No profile gateways registered under s6")
        return True
    fn = mgr.stop if action == "stop" else mgr.restart
    errors: list[tuple[str, Exception]] = []
    for profile in profiles:
        service_name = f"gateway-{profile}"
        try:
            fn(service_name)
        except Exception as exc:  # noqa: BLE001 — report and continue
            errors.append((profile, exc))
    succeeded = len(profiles) - len(errors)
    verb = "stopped" if action == "stop" else "restarted"
    if succeeded:
        print(f"✓ {verb.capitalize()} {succeeded} profile gateway(s) under s6")
    for profile, exc in errors:
        print(f"✗ Could not {action} gateway-{profile}: {exc}")
    return True


def gateway_command(args):
    """Handle gateway subcommands."""
    try:
        return _gateway_command_inner(args)
    except UserSystemdUnavailableError as e:
        # Clean, actionable message instead of a traceback when the user D-Bus
        # session is unreachable (fresh SSH shell, no linger, container, etc.).
        print_error("User systemd not reachable:")
        for line in str(e).splitlines():
            print(f"  {line}")
        sys.exit(1)
    except SystemScopeRequiresRootError as e:
        # The direct ``hermes gateway install|uninstall|start|stop|restart``
        # path lands here when the user typed a system-scope action without
        # sudo. Same exit code as before — just gives the wizard a way to
        # intercept the same condition with friendlier guidance before the
        # error is raised.
        print(str(e))
        sys.exit(1)


def _maybe_redirect_run_to_s6_supervision(args) -> bool:
    """Inside an s6 container, redirect bare ``gateway run`` to the
    supervised path.

    Background. Before the s6 image landed, ``docker run <image> gateway
    run`` was the standard way to start a containerized gateway: the
    gateway was the container's main process, tini reaped zombies, and
    container exit code == gateway exit code. With s6-overlay as PID 1,
    we'd much rather have the gateway run as a supervised s6 longrun
    (auto-restart on crash, dashboard supervised alongside, multiple
    profile gateways under the same /init). This redirect upgrades the
    old invocation transparently — the user gets the new behavior
    without changing their docker run command.

    Three gates make this a no-op outside the intended scope:

      1. ``_dispatch_via_service_manager_if_s6`` returns False unless
         we're in a container with s6 as PID 1. Host runs of
         ``hermes gateway run`` are unaffected.
      2. ``HERMES_S6_SUPERVISED_CHILD`` is exported by
         ``S6ServiceManager._render_run_script`` for the supervised
         process itself — i.e. when s6-supervise execs ``hermes gateway
         run --replace`` as a longrun, this guard short-circuits the
         redirect so the supervised gateway actually runs in
         foreground (otherwise we'd recurse: run → start → run → start
         → ...).
      3. ``--no-supervise`` (or ``HERMES_GATEWAY_NO_SUPERVISE=1``) opts
         out for users who genuinely want pre-s6 semantics — CI smoke
         tests, debugging the foreground startup path, etc.

    Returns True iff dispatched (caller should ``return``).
    """
    no_supervise = getattr(args, "no_supervise", False) or \
        os.environ.get("HERMES_GATEWAY_NO_SUPERVISE", "").lower() in ("1", "true", "yes")
    if no_supervise:
        return False
    if os.environ.get("HERMES_S6_SUPERVISED_CHILD"):
        # We ARE the supervised child s6-supervise is running. Fall
        # through to the foreground code path so the gateway actually
        # starts.
        return False
    if not _dispatch_via_service_manager_if_s6("start"):
        return False
    # Loud breadcrumb: explain the upgrade and how to opt out. Print to
    # stderr so it doesn't pollute stdout-parsing scripts. The
    # supervised gateway's own logs are routed by s6-log to both
    # `docker logs` and ${HERMES_HOME}/logs/gateways/<profile>/current,
    # so the user sees a clear sequence: this banner first, then the
    # gateway's own stdout/stderr from the supervisor.
    print(
        "→ gateway is now running under s6 supervision (auto-restart on crash,\n"
        "  dashboard supervised alongside if HERMES_DASHBOARD is set).\n"
        "  This is the recommended setup for the s6 container image — the\n"
        "  gateway will keep running even if it crashes.\n"
        "  Use `--no-supervise` (or HERMES_GATEWAY_NO_SUPERVISE=1) to opt out\n"
        "  and get the pre-s6 foreground behavior instead.",
        file=sys.stderr,
        flush=True,
    )
    # Block until the container is signalled. The supervised gateway's
    # lifetime is independent of this process — s6-supervise restarts
    # it on crash, and we don't want the container to exit when the
    # gateway flaps. `sleep infinity` matches the static main-hermes
    # service's pattern (see docker/s6-rc.d/main-hermes/run): the CMD
    # process is a no-op heartbeat that keeps /init alive until
    # `docker stop` sends SIGTERM, at which point /init runs stage 3
    # shutdown (which tears down the supervised gateway cleanly).
    os.execvp("sleep", ["sleep", "infinity"])


def _gateway_command_inner(args):
    subcmd = getattr(args, 'gateway_command', None)
    
    # Default to run if no subcommand
    if subcmd is None or subcmd == "run":
        if _maybe_redirect_run_to_s6_supervision(args):
            return  # unreachable; execvp doesn't return
        verbose = getattr(args, 'verbose', 0)
        quiet = getattr(args, 'quiet', False)
        replace = getattr(args, 'replace', False)
        run_gateway(verbose, quiet=quiet, replace=replace)
        return

    if subcmd == "setup":
        gateway_setup()
        return

    # Service management commands
    if subcmd == "install":
        if is_managed():
            managed_error("install gateway service (managed by NixOS)")
            return
        force = getattr(args, 'force', False)
        system = getattr(args, 'system', False)
        run_as_user = getattr(args, 'run_as_user', None)
        if is_termux():
            print("Gateway service installation is not supported on Termux.")
            print("Run manually: hermes gateway")
            sys.exit(1)
        if supports_systemd_services():
            if is_wsl():
                print_warning("WSL detected — systemd services may not survive WSL restarts.")
                print_info("  Consider running in foreground instead: hermes gateway run")
                print_info("  Or use tmux/screen for persistence: tmux new -s hermes 'hermes gateway run'")
                print()
            start_now = prompt_yes_no("Start the gateway now after installing the service?", True)
            start_on_login = prompt_yes_no("Start the gateway automatically on login/boot with systemd?", True)
            systemd_install(
                force=force,
                system=system,
                run_as_user=run_as_user,
                enable_on_startup=start_on_login,
            )
            if start_now:
                systemd_start(system=system)
        elif is_macos():
            launchd_install(force)
        elif is_windows():
            from atlaz_cli import gateway_windows
            gateway_windows.install(
                force=force,
                start_now=getattr(args, 'start_now', None),
                start_on_login=getattr(args, 'start_on_login', None),
                elevated_handoff=getattr(args, 'elevated_handoff', False),
            )
        elif is_wsl():
            print("WSL detected but systemd is not running.")
            print("Either enable systemd (add systemd=true to /etc/wsl.conf and restart WSL)")
            print("or run the gateway in foreground mode:")
            print()
            print("  hermes gateway run                              # direct foreground")
            print("  tmux new -s hermes 'hermes gateway run'         # persistent via tmux")
            print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background")
            sys.exit(1)
        elif is_container():
            # Phase 4: inside a container with s6 the gateway service is
            # auto-registered when the profile is created (and reconciled
            # at every container boot). `install` is therefore informational.
            from atlaz_cli.service_manager import detect_service_manager
            if detect_service_manager() == "s6":
                print("Per-profile gateways are auto-registered when you create a profile.")
                print()
                print("  hermes profile create <name>     # creates the s6 service slot")
                print("  hermes -p <name> gateway start   # bring it up via s6")
                print("  hermes status                    # see currently-supervised gateways")
                return
            # Fallback for pre-s6 containers or other container runtimes
            # we haven't taught about supervision (Podman without our
            # /init, k8s plain runs, etc.) — the historical guidance still
            # applies.
            print("Service installation is not needed inside a Docker container.")
            print("The container runtime is your service manager — use Docker restart policies instead:")
            print()
            print("  docker run --restart unless-stopped ...   # auto-restart on crash/reboot")
            print("  docker restart <container>                # manual restart")
            print()
            print("To run the gateway: hermes gateway run")
            sys.exit(0)
        else:
            print("Service installation not supported on this platform.")
            print("Run manually: hermes gateway run")
            sys.exit(1)
    
    elif subcmd == "uninstall":
        if is_managed():
            managed_error("uninstall gateway service (managed by NixOS)")
            return
        system = getattr(args, 'system', False)
        if is_termux():
            print("Gateway service uninstall is not supported on Termux because there is no managed service to remove.")
            print("Stop manual runs with: hermes gateway stop")
            sys.exit(1)
        if supports_systemd_services():
            systemd_uninstall(system=system)
        elif is_macos():
            launchd_uninstall()
        elif is_windows():
            from atlaz_cli import gateway_windows
            gateway_windows.uninstall()
        elif is_container():
            from atlaz_cli.service_manager import detect_service_manager
            if detect_service_manager() == "s6":
                print("Per-profile gateways are auto-unregistered when you delete the profile.")
                print()
                print("  hermes profile delete <name>     # tears down the s6 service slot")
                print("  hermes -p <name> gateway stop    # stop without deleting the profile")
                return
            print("Service uninstall is not applicable inside a Docker container.")
            print("To stop the gateway, stop or remove the container:")
            print()
            print("  docker stop <container>")
            print("  docker rm <container>")
            sys.exit(0)
        else:
            print("Not supported on this platform.")
            sys.exit(1)

    elif subcmd == "start":
        system = getattr(args, 'system', False)
        start_all = getattr(args, 'all', False)

        # Phase 4: inside a container with s6, dispatch via the service
        # manager instead of falling through to systemd/launchd/windows.
        # `--all` isn't meaningful here (each profile has its own service
        # slot — start them individually via `hermes -p <name> gateway
        # start`), so just bring up the current profile's slot.
        if not start_all and _dispatch_via_service_manager_if_s6("start"):
            return

        if start_all:
            # Kill all stale gateway processes across all profiles before starting
            killed = kill_gateway_processes(all_profiles=True)
            if killed:
                print(f"✓ Killed {killed} stale gateway process(es) across all profiles")
                _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

        if is_termux():
            print("Gateway service start is not supported on Termux because there is no system service manager.")
            print("Run manually: hermes gateway")
            sys.exit(1)
        if supports_systemd_services():
            systemd_start(system=system)
        elif is_macos():
            launchd_start()
        elif is_windows():
            from atlaz_cli import gateway_windows
            gateway_windows.start()
        elif is_wsl():
            print("WSL detected but systemd is not available.")
            print("Run the gateway in foreground mode instead:")
            print()
            print("  hermes gateway run                              # direct foreground")
            print("  tmux new -s hermes 'hermes gateway run'         # persistent via tmux")
            print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background")
            print()
            print("To enable systemd: add systemd=true to /etc/wsl.conf and run 'wsl --shutdown' from PowerShell.")
            sys.exit(1)
        elif is_container():
            # Reached only when s6 ISN'T running (the early dispatch
            # above handles the s6 case). Pre-s6 containers or other
            # container runtimes that don't ship our /init get the
            # historical guidance: the gateway is the container's main
            # process, so use docker lifecycle commands.
            print("Service start is not applicable inside a Docker container.")
            print("The gateway runs as the container's main process.")
            print()
            print("  docker start <container>     # start a stopped container")
            print("  docker restart <container>   # restart a running container")
            print()
            print("Or run the gateway directly: hermes gateway run")
            sys.exit(0)
        else:
            print("Not supported on this platform.")
            sys.exit(1)

    elif subcmd == "stop":
        stop_all = getattr(args, 'all', False)
        system = getattr(args, 'system', False)

        # Phase 4: inside a container with s6, dispatch via the service
        # manager. ``--all`` iterates every registered profile gateway
        # through s6 (otherwise it would fall through to ``pkill``,
        # which s6-supervise observes as a crash and immediately restarts).
        if stop_all and _dispatch_all_via_service_manager_if_s6("stop"):
            return
        if not stop_all and _dispatch_via_service_manager_if_s6("stop"):
            return

        if stop_all:
            # --all: kill every gateway process on the machine
            service_available = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from atlaz_cli import gateway_windows
                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_available = True
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass
            killed = kill_gateway_processes(all_profiles=True)
            total = killed + (1 if service_available else 0)
            if total:
                print(f"✓ Stopped {total} gateway process(es) across all profiles")
            else:
                print("✗ No gateway processes found")
        else:
            # Default: stop only the current profile's gateway
            service_available = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from atlaz_cli import gateway_windows
                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_available = True
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass

            if not service_available:
                # No systemd/launchd/schtasks service — use profile-scoped PID file
                if stop_profile_gateway():
                    print("✓ Stopped gateway for this profile")
                else:
                    print("✗ No gateway running for this profile")
            else:
                print(f"✓ Stopped {get_service_name()} service")
    
    elif subcmd == "restart":
        # Try service first, fall back to killing and restarting
        service_available = False
        system = getattr(args, 'system', False)
        restart_all = getattr(args, 'all', False)
        service_configured = False

        # Phase 4: inside a container with s6, dispatch via the service
        # manager (s6-svc -t restarts the supervised process). ``--all``
        # iterates every registered profile gateway through s6; without
        # this it would fall through to ``pkill``, which s6-supervise
        # would observe as a crash and immediately restart anyway.
        if restart_all and _dispatch_all_via_service_manager_if_s6("restart"):
            return
        if not restart_all and _dispatch_via_service_manager_if_s6("restart"):
            return

        if restart_all:
            # --all: stop every gateway process across all profiles, then start fresh
            service_stopped = False
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                try:
                    systemd_stop(system=system)
                    service_stopped = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_stopped = True
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from atlaz_cli import gateway_windows
                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_stopped = True
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass
            killed = kill_gateway_processes(all_profiles=True)
            total = killed + (1 if service_stopped else 0)
            if total:
                print(f"✓ Stopped {total} gateway process(es) across all profiles")
            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

            # Start the current profile's service fresh
            print("Starting gateway...")
            if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
                systemd_start(system=system)
            elif is_macos() and get_launchd_plist_path().exists():
                launchd_start()
            elif is_windows():
                from atlaz_cli import gateway_windows
                # On Windows, even without a registered Scheduled Task / Startup
                # entry, gateway_windows.start() uses the safe detached
                # pythonw.exe launcher.  Do not fall back to run_gateway() here:
                # when invoked from a gateway-hosted agent/tool call, foreground
                # run_gateway() is tied to the very gateway process we just
                # stopped and can die before the replacement is stable.
                gateway_windows.start()
            else:
                run_gateway(verbose=0)
            return
        
        if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            service_configured = True
            try:
                systemd_restart(system=system)
                service_available = True
            except subprocess.CalledProcessError:
                pass
        elif is_macos() and get_launchd_plist_path().exists():
            service_configured = True
            try:
                launchd_restart()
                service_available = True
            except subprocess.CalledProcessError:
                pass
        elif is_windows():
            from atlaz_cli import gateway_windows
            # Prefer the Windows-specific restart path: it supports both
            # registered Scheduled Task / Startup installs and no-service
            # detached restarts.  In the normal successful Telegram-triggered
            # restart flow, this avoids the generic foreground run_gateway()
            # path that can be reaped with the old gateway process.  If the
            # Windows backend raises, intentionally preserve the existing
            # generic failure fallback below.
            service_configured = gateway_windows.is_installed()
            try:
                gateway_windows.restart()
                return
            except (subprocess.CalledProcessError, RuntimeError, OSError):
                pass
        
        if not service_available:
            # systemd/launchd restart failed — check if linger is the issue
            if supports_systemd_services():
                linger_ok, _detail = get_systemd_linger_status()
                if linger_ok is not True:
                    import getpass
                    _username = getpass.getuser()
                    print()
                    print("⚠ Cannot restart gateway as a service — linger is not enabled.")
                    print("  The gateway user service requires linger to function on headless servers.")
                    print()
                    print(f"  Run:  sudo loginctl enable-linger {_username}")
                    print()
                    print("  Then restart the gateway:")
                    print("    hermes gateway restart")
                    return

            if service_configured:
                print()
                print("✗ Gateway service restart failed.")
                print("  The service definition exists, but the service manager did not recover it.")
                print("  Fix the service, then retry: hermes gateway start")
                sys.exit(1)

            # Manual restart: stop only this profile's gateway
            if stop_profile_gateway():
                print("✓ Stopped gateway for this profile")

            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

            # Start fresh
            print("Starting gateway...")
            run_gateway(verbose=0)
    
    elif subcmd == "status":
        deep = getattr(args, 'deep', False)
        full = getattr(args, 'full', False)
        system = getattr(args, 'system', False)
        snapshot = get_gateway_runtime_snapshot(system=system)
        
        # Check for service first
        _windows_service_installed = False
        if is_windows():
            from atlaz_cli import gateway_windows
            _windows_service_installed = gateway_windows.is_installed()
        if supports_systemd_services() and (get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()):
            systemd_status(deep, system=system, full=full)
            _print_gateway_process_mismatch(snapshot)
        elif is_macos() and get_launchd_plist_path().exists():
            launchd_status(deep)
            _print_gateway_process_mismatch(snapshot)
        elif _windows_service_installed:
            from atlaz_cli import gateway_windows
            gateway_windows.status(deep=deep)
            _print_gateway_process_mismatch(snapshot)
        else:
            # Check for manually running processes
            pids = list(snapshot.gateway_pids)
            if pids:
                print(f"✓ Gateway is running (PID: {', '.join(map(str, pids))})")
                print("  (Running manually, not as a system service)")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                if is_termux():
                    print("Termux note:")
                    print("  Android may stop background jobs when Termux is suspended")
                elif is_wsl():
                    print("WSL note:")
                    print("  The gateway is running in foreground/manual mode (recommended for WSL).")
                    print("  Use tmux or screen for persistence across terminal closes.")
                elif is_windows():
                    print("To install as a Windows Scheduled Task (auto-start on login):")
                    print("  hermes gateway install")
                else:
                    print("To install as a service:")
                    print("  hermes gateway install")
                    print("  sudo hermes gateway install --system")
            else:
                print("✗ Gateway is not running")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                print("To start:")
                print("  hermes gateway run      # Run in foreground")
                if is_termux():
                    print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # Best-effort background start")
                elif is_wsl():
                    print("  tmux new -s hermes 'hermes gateway run'         # persistent via tmux")
                    print("  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background")
                elif is_windows():
                    print("  hermes gateway install  # Install as Windows Scheduled Task (auto-start on login)")
                else:
                    print("  hermes gateway install  # Install as user service")
                    print("  sudo hermes gateway install --system  # Install as boot-time system service")

        # Show other profiles' gateway status for multi-profile awareness
        _print_other_profiles_gateway_status()

    elif subcmd == "list":
        _gateway_list()

    elif subcmd == "migrate-legacy":
        # Stop, disable, and remove legacy Hermes gateway unit files from
        # pre-rename installs (e.g. hermes.service). Profile units and
        # unrelated third-party services are never touched.
        dry_run = getattr(args, 'dry_run', False)
        yes = getattr(args, 'yes', False)
        if not supports_systemd_services() and not is_macos():
            print("Legacy unit migration only applies to systemd-based Linux hosts.")
            return
        remove_legacy_hermes_units(interactive=not yes, dry_run=dry_run)