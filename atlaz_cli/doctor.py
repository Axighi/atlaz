"""
Doctor command for hermes CLI.

Diagnoses issues with Hermes Agent setup.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

from atlaz_cli.config import get_project_root, get_hermes_home, get_env_path
from atlaz_cli.env_loader import load_hermes_dotenv
from atlaz_constants import display_hermes_home
from atlaz_cli.config import get_project_root, get_hermes_home, get_env_path
from atlaz_cli.env_loader import load_hermes_dotenv
from atlaz_constants import display_hermes_home

PROJECT_ROOT = get_project_root()
HERMES_HOME = get_hermes_home()
_DHH = display_hermes_home()  # user-facing display path (e.g. ~/.hermes or ~/.hermes/profiles/coder)

# Load environment variables from ~/.hermes/.env so API key checks work
_env_path = get_env_path()
load_hermes_dotenv(hermes_home=_env_path.parent, project_env=PROJECT_ROOT / ".env")

from atlaz_cli.colors import Colors, color
from atlaz_cli.models import _HERMES_USER_AGENT
from atlaz_constants import OPENROUTER_MODELS_URL
from atlaz_cli.colors import Colors, color
from atlaz_cli.models import _HERMES_USER_AGENT
from atlaz_constants import OPENROUTER_MODELS_URL
from utils import base_url_host_matches


_PROVIDER_ENV_HINTS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "OPENAI_BASE_URL",
    "NOUS_API_KEY",
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "Z_AI_API_KEY",
    "KIMI_API_KEY",
    "KIMI_CN_API_KEY",
    "GMI_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "KILOCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "HF_TOKEN",
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_GO_API_KEY",
    "XIAOMI_API_KEY",
    "TOKENHUB_API_KEY",
)


from atlaz_constants import is_termux as _is_termux


def _python_install_cmd() -> str:
    return "python -m pip install" if _is_termux() else "uv pip install"


def _system_package_install_cmd(pkg: str) -> str:
    if _is_termux():
        return f"pkg install {pkg}"
    if sys.platform == "darwin":
        return f"brew install {pkg}"
    return f"sudo apt install {pkg}"


def _safe_which(cmd: str) -> str | None:
    """shutil.which wrapper resilient to platform monkeypatching in tests."""
    try:
        return shutil.which(cmd)
    except Exception:
        return None


def _termux_browser_setup_steps(node_installed: bool) -> list[str]:
    steps: list[str] = []
    step = 1
    if not node_installed:
        steps.append(f"{step}) pkg install nodejs")
        step += 1
    steps.append(f"{step}) npm install -g agent-browser")
    steps.append(f"{step + 1}) agent-browser install")
    return steps


def _termux_install_all_fallback_notes() -> list[str]:
    return [
        "Termux install profile: use .[termux-all] for broad compatibility (installer default on Termux).",
        "Matrix E2EE extra is excluded on Termux (python-olm currently fails to build).",
        "Local faster-whisper extra is excluded on Termux (ctranslate2/av build path unavailable).",
        "STT fallback: use Groq Whisper (set GROQ_API_KEY) or OpenAI Whisper (set VOICE_TOOLS_OPENAI_KEY).",
    ]


def _has_provider_env_config(content: str) -> bool:
    """Return True when ~/.hermes/.env contains provider auth/base URL settings."""
    return any(key in content for key in _PROVIDER_ENV_HINTS)


def _honcho_is_configured_for_doctor() -> bool:
    """Return True when Honcho is configured, even if this process has no active session."""
    try:
        from plugins.memory.honcho.client import HonchoClientConfig

        cfg = HonchoClientConfig.from_global_config()
        return bool(cfg.enabled and (cfg.api_key or cfg.base_url))
    except Exception:
        return False


def _is_kanban_worker_env_gate(item: dict) -> bool:
    """Return True when Kanban is unavailable only because this is not a worker process."""
    if item.get("name") != "kanban":
        return False
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False

    tools = item.get("tools") or []
    return bool(tools) and all(str(tool).startswith("kanban_") for tool in tools)


def _doctor_tool_availability_detail(toolset: str) -> str:
    """Optional explanatory suffix for toolsets whose doctor status needs context."""
    if toolset == "kanban" and not os.environ.get("HERMES_KANBAN_TASK"):
        return "(runtime-gated; loaded only for dispatcher-spawned workers)"
    return ""


def _apply_doctor_tool_availability_overrides(available: list[str], unavailable: list[dict]) -> tuple[list[str], list[dict]]:
    """Adjust runtime-gated tool availability for doctor diagnostics."""
    updated_available = list(available)
    updated_unavailable = []
    for item in unavailable:
        name = item.get("name")
        if _is_kanban_worker_env_gate(item):
            if "kanban" not in updated_available:
                updated_available.append("kanban")
            continue
        if name == "honcho" and _honcho_is_configured_for_doctor():
            if "honcho" not in updated_available:
                updated_available.append("honcho")
            continue
        updated_unavailable.append(item)
    return updated_available, updated_unavailable


def _has_healthy_oauth_fallback_for_apikey_provider(provider_label: str) -> bool:
    """Return True when a direct API-key probe failure is non-blocking.

    Some provider families support both a direct API-key path and a separate
    OAuth runtime path. When the OAuth path is already healthy, doctor should
    still show a failed API-key connectivity row, but it should not promote
    that direct-key problem into the final blocking summary.
    """
    normalized = (provider_label or "").strip().lower()
    if normalized in {"google / gemini", "gemini"}:
        try:
            from atlaz_cli.auth import get_gemini_oauth_auth_status
            return bool((get_gemini_oauth_auth_status() or {}).get("logged_in"))
        except Exception:
            return False
    if normalized == "minimax":
        try:
            from atlaz_cli.auth import get_minimax_oauth_auth_status
            return bool((get_minimax_oauth_auth_status() or {}).get("logged_in"))
        except Exception:
            return False
    if normalized == "xai":
        try:
            from atlaz_cli.auth import get_xai_oauth_auth_status
            return bool((get_xai_oauth_auth_status() or {}).get("logged_in"))
        except Exception:
            return False
    return False


def check_ok(text: str, detail: str = ""):
    print(f"  {color('✓', Colors.GREEN)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_warn(text: str, detail: str = ""):
    print(f"  {color('⚠', Colors.YELLOW)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_fail(text: str, detail: str = ""):
    print(f"  {color('✗', Colors.RED)} {text}" + (f" {color(detail, Colors.DIM)}" if detail else ""))

def check_info(text: str):
    print(f"    {color('→', Colors.CYAN)} {text}")


def _section(title: str) -> None:
    """Print a doctor section banner: blank line + bold cyan ◆ title."""
    print()
    print(color(f"◆ {title}", Colors.CYAN, Colors.BOLD))


def _fail_and_issue(text: str, detail: str, fix: str, issues: list[str]) -> None:
    """Emit a check_fail and append the corresponding fix instruction."""
    check_fail(text, detail)
    issues.append(fix)


def _check_s6_supervision(issues: list[str]) -> None:
    """Inside a container under our s6 /init, surface what s6 sees.

    Runs as a counterpart to :func:`_check_gateway_service_linger` for
    the systemd-on-host case. No-op everywhere except in the s6
    container so host runs aren't cluttered with irrelevant output.

    Reports:
      - Whether the main-hermes and dashboard static services are up
      - How many per-profile gateway slots are registered (via
        ``S6ServiceManager.list_profile_gateways()``) and how many are
        currently supervised as ``up``
    """
    try:
        from atlaz_cli.service_manager import (
            S6ServiceManager,
            detect_service_manager,
        )
    except Exception:
        return

    if detect_service_manager() != "s6":
        return

    _section("s6 Supervision")

    mgr = S6ServiceManager()

    # Static services. They live under /run/service/ via s6-rc symlinks,
    # so the same s6-svstat probe works.
    for static in ("main-hermes", "dashboard"):
        if mgr.is_running(static):
            check_ok(f"{static}: up")
        else:
            check_info(f"{static}: down (expected if not enabled via env)")

    profiles = mgr.list_profile_gateways()
    if not profiles:
        check_info("No per-profile gateways registered yet — create one with `hermes profile create <name>`")
        return

    up_count = sum(1 for p in profiles if mgr.is_running(f"gateway-{p}"))
    check_ok(
        f"Per-profile gateways: {up_count}/{len(profiles)} supervised up"
        + (f" ({', '.join(sorted(profiles))})" if len(profiles) <= 8 else "")
    )


def _check_gateway_service_linger(issues: list[str]) -> None:
    """Warn when a systemd user gateway service will stop after logout.

    Skipped inside a container running under s6 — the linger concept
    (user-systemd surviving SSH logout) doesn't apply there, and the
    s6 supervision state is surfaced separately by
    ``_check_s6_supervision``.
    """
    try:
        from atlaz_cli.gateway import (
            get_systemd_linger_status,
            get_systemd_unit_path,
            is_linux,
        )
        from atlaz_cli.service_manager import detect_service_manager
    except Exception as e:
        check_warn("Gateway service linger", f"(could not import gateway helpers: {e})")
        return

    if not is_linux():
        return

    # Inside a container under our s6 /init, _check_s6_supervision
    # reports the live supervision state; the linger warning would be
    # confusing here (no systemd, no logout, no "lingering" concept).
    if detect_service_manager() == "s6":
        return

    unit_path = get_systemd_unit_path()
    if not unit_path.exists():
        return

    _section("Gateway Service")
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        check_ok("Systemd linger enabled", "(gateway service survives logout)")
    elif linger_enabled is False:
        check_warn("Systemd linger disabled", "(gateway may stop after logout)")
        check_info("Run: sudo loginctl enable-linger $USER")
        issues.append("Enable linger for the gateway user service: sudo loginctl enable-linger $USER")
    else:
        check_warn("Could not verify systemd linger", f"({linger_detail})")


_APIKEY_PROVIDERS_CACHE: list | None = None


def _build_apikey_providers_list() -> list:
    """Build the API-key provider health-check list once and cache it.

    Tuple format: (name, env_vars, default_url, base_env, supports_models_endpoint)
    Base list augmented with any ProviderProfile with auth_type="api_key" not
    already present — adding plugins/model-providers/<name>/ is sufficient to get into doctor.
    """
    _static = [
        ("Z.AI / GLM",      ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"), "https://api.z.ai/api/paas/v4/models", "GLM_BASE_URL", True),
        ("Kimi / Moonshot",  ("KIMI_API_KEY",),                              "https://api.moonshot.ai/v1/models",   "KIMI_BASE_URL", True),
        ("StepFun Step Plan", ("STEPFUN_API_KEY",),                          "https://api.stepfun.ai/step_plan/v1/models", "STEPFUN_BASE_URL", True),
        ("Kimi / Moonshot (China)", ("KIMI_CN_API_KEY",),                    "https://api.moonshot.cn/v1/models",   None, True),
        ("Arcee AI",         ("ARCEEAI_API_KEY",),                           "https://api.arcee.ai/api/v1/models",  "ARCEE_BASE_URL", True),
        ("GMI Cloud",        ("GMI_API_KEY",),                               "https://api.gmi-serving.com/v1/models", "GMI_BASE_URL", True),
        ("DeepSeek",         ("DEEPSEEK_API_KEY",),                          "https://api.deepseek.com/v1/models",  "DEEPSEEK_BASE_URL", True),
        ("Hugging Face",     ("HF_TOKEN",),                                  "https://router.huggingface.co/v1/models", "HF_BASE_URL", True),
        ("NVIDIA NIM",       ("NVIDIA_API_KEY",),                            "https://integrate.api.nvidia.com/v1/models", "NVIDIA_BASE_URL", True),
        ("Alibaba/DashScope", ("DASHSCOPE_API_KEY",),                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models", "DASHSCOPE_BASE_URL", True),
        # MiniMax global: /v1 endpoint supports /models.
        ("MiniMax",          ("MINIMAX_API_KEY",),                           "https://api.minimax.io/v1/models",    "MINIMAX_BASE_URL", True),
        # MiniMax CN: /v1 endpoint does NOT support /models (returns 404).
        ("MiniMax (China)",  ("MINIMAX_CN_API_KEY",),                        "https://api.minimaxi.com/v1/models",  "MINIMAX_CN_BASE_URL", False),
        ("Kilo Code",        ("KILOCODE_API_KEY",),                          "https://api.kilo.ai/api/gateway/models", "KILOCODE_BASE_URL", True),
        ("OpenCode Zen",     ("OPENCODE_ZEN_API_KEY",),                      "https://opencode.ai/zen/v1/models",  "OPENCODE_ZEN_BASE_URL", True),
        # OpenCode Go has no shared /models endpoint; skip the health check.
        ("OpenCode Go",      ("OPENCODE_GO_API_KEY",),                       None,                                  "OPENCODE_GO_BASE_URL", False),
    ]
    _known_names = {t[0] for t in _static}
    # Also index by profile canonical name so profiles without display_name
    # don't create duplicate entries for providers already in the static list.
    _known_canonical: set[str] = set()
    _name_to_canonical = {
        "Z.AI / GLM": "zai", "Kimi / Moonshot": "kimi-coding",
        "StepFun Step Plan": "stepfun", "Kimi / Moonshot (China)": "kimi-coding-cn",
        "Arcee AI": "arcee", "GMI Cloud": "gmi", "DeepSeek": "deepseek",
        "Hugging Face": "huggingface", "NVIDIA NIM": "nvidia",
        "Alibaba/DashScope": "alibaba", "MiniMax": "minimax",
        "MiniMax (China)": "minimax-cn",
        "Kilo Code": "kilocode", "OpenCode Zen": "opencode-zen",
        "OpenCode Go": "opencode-go",
    }
    for _label, _canonical in _name_to_canonical.items():
        _known_canonical.add(_canonical)
    # Providers that already have a dedicated health check above the generic
    # API-key loop (with custom headers/auth). Skip their pluggable profiles
    # here so the generic Bearer-auth loop doesn't run a duplicate, broken
    # check (e.g. Anthropic native API requires x-api-key, not Bearer).
    _dedicated_canonical = {"anthropic", "openrouter", "bedrock"}
    _known_canonical.update(_dedicated_canonical)
    try:
        from providers import list_providers
        from providers.base import ProviderProfile as _PP
        try:
            from atlaz_cli.providers import normalize_provider as _normalize_provider
        except Exception:  # pragma: no cover - normalization is best-effort
            def _normalize_provider(_name: str) -> str:
                return (_name or "").strip().lower()
        for _pp in list_providers():
            if not isinstance(_pp, _PP) or _pp.auth_type != "api_key" or not _pp.env_vars:
                continue
            _label = _pp.display_name or _pp.name
            if _label in _known_names or _pp.name in _known_canonical:
                continue
            _candidates = {_normalize_provider(_pp.name)}
            for _alias in (_pp.aliases or ()):
                _candidates.add(_normalize_provider(_alias))
            if _candidates & _dedicated_canonical:
                continue
            # Separate API-key vars from base-URL override vars — the health-check
            # loop sends the first found value as Authorization: Bearer, so a URL
            # string must never be picked.
            _key_vars = tuple(
                v for v in _pp.env_vars
                if not v.endswith("_BASE_URL") and not v.endswith("_URL")
            )
            _base_var = next(
                (v for v in _pp.env_vars if v.endswith("_BASE_URL") or v.endswith("_URL")),
                None,
            )
            if not _key_vars:
                continue
            _models_url = (
                (_pp.models_url or (_pp.base_url.rstrip("/") + "/models"))
                if _pp.base_url else None
            )
            _hc = getattr(_pp, "supports_health_check", True)
            _static.append((_label, _key_vars, _models_url, _base_var, _hc))
    except Exception:
        pass
    return _static


def run_doctor(args):
    """Run diagnostic checks."""
    should_fix = getattr(args, 'fix', False)
    ack_target = getattr(args, 'ack', None)

    # Doctor runs from the interactive CLI, so CLI-gated tool availability
    # checks (like cronjob management) should see the same context as `hermes`.
    os.environ.setdefault("HERMES_INTERACTIVE", "1")

    # Handle `hermes doctor --ack <id>` as a fast path. Persist the ack and
    # return without running the rest of the diagnostics — the user has
    # already seen the advisory and just wants to silence it.
    if ack_target:
        from atlaz_cli.security_advisories import (
            ADVISORIES,
            ack_advisory,
        )
        valid_ids = {a.id for a in ADVISORIES}
        if ack_target not in valid_ids:
            print(color(
                f"Unknown advisory ID: {ack_target!r}. Known IDs: "
                f"{', '.join(sorted(valid_ids)) or '(none)'}",
                Colors.RED,
            ))
            sys.exit(2)
        if ack_advisory(ack_target):
            print(color(
                f"  ✓ Acknowledged advisory {ack_target}. "
                f"It will no longer trigger startup banners.",
                Colors.GREEN,
            ))
        else:
            print(color(
                f"  ✗ Failed to persist ack for {ack_target}. "
                f"Check ~/.hermes/config.yaml is writable.",
                Colors.RED,
            ))
            sys.exit(1)
        return

    issues = []
    manual_issues = []  # issues that can't be auto-fixed
    fixed_count = 0

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                 🩺 Hermes Doctor                        │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))

    _section("Security Advisories")
    try:
        from atlaz_cli.security_advisories import (
            detect_compromised,
            filter_unacked,
            full_remediation_text,
            get_acked_ids,
        )
        all_hits = detect_compromised()
        fresh_hits = filter_unacked(all_hits)
        if fresh_hits:
            for hit in fresh_hits:
                check_fail(
                    f"{hit.advisory.title}",
                    f"({hit.package}=={hit.installed_version})",
                )
                # Print the full remediation block, indented under the
                # check_fail header so it reads as a single section.
                for line in full_remediation_text(hit):
                    if line:
                        print(f"    {color(line, Colors.YELLOW)}")
                    else:
                        print()
                # Funnel into the action list so the summary block surfaces it
                # for users who scroll past the section.
                manual_issues.append(
                    f"Resolve security advisory {hit.advisory.id}: "
                    f"uninstall {hit.package}=={hit.installed_version} and "
                    f"rotate credentials, then run "
                    

... [OUTPUT TRUNCATED - 40252 chars omitted out of 90252 total] ...

       )
                    issues.append(
                        f"{label} has {total} npm "
                        f"{'vulnerability' if total == 1 else 'vulnerabilities'}"
                    )
                else:
                    check_ok(
                        f"{label} deps",
                        f"({moderate} moderate "
                        f"{'vulnerability' if moderate == 1 else 'vulnerabilities'})",
                    )
            except Exception:
                pass

    if _is_termux():
        check_info("Termux compatibility fallbacks:")
        for note in _termux_install_all_fallback_notes():
            check_info(note)

    _section("API Connectivity")
    # Refactor: every connectivity probe below is HTTP-bound and fully
    # independent. Running them in series spent ~5s wall on a typical
    # workstation (2s of that was boto3's IMDS lookup for AWS credentials,
    # which times out unless you're actually on EC2). Threading them with
    # a small executor pool collapses the section to roughly the slowest
    # single probe — about 2s — without changing the output format.
    #
    # Each ``_probe_*`` helper is a pure function: takes its inputs,
    # makes one HTTP/SDK call, returns a ``_ConnectivityResult`` carrying
    # the line(s) to print and any issue strings to append. No globals,
    # no shared mutable state, no printing inside the workers.
    import concurrent.futures as _futures
    from collections import namedtuple as _namedtuple

    _ConnectivityResult = _namedtuple(
        "_ConnectivityResult", ["label", "lines", "issues"]
    )
    _probes: list = []  # list of (label, callable) submitted in display order

    def _probe_openrouter() -> _ConnectivityResult:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            return _ConnectivityResult(
                "OpenRouter API",
                [(color("⚠", Colors.YELLOW), "OpenRouter API",
                  color("(not configured)", Colors.DIM))],
                [],
            )
        try:
            import httpx
            r = httpx.get(
                OPENROUTER_MODELS_URL,
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if r.status_code == 200:
                return _ConnectivityResult(
                    "OpenRouter API",
                    [(color("✓", Colors.GREEN), "OpenRouter API", "")],
                    [],
                )
            if r.status_code == 401:
                return _ConnectivityResult(
                    "OpenRouter API",
                    [(color("✗", Colors.RED), "OpenRouter API",
                      color("(invalid API key)", Colors.DIM))],
                    ["Check OPENROUTER_API_KEY in .env"],
                )
            if r.status_code == 402:
                return _ConnectivityResult(
                    "OpenRouter API",
                    [(color("✗", Colors.RED), "OpenRouter API",
                      color("(out of credits — payment required)", Colors.DIM))],
                    ["OpenRouter account has insufficient credits. "
                     "Fix: run 'hermes config set model.provider <provider>' "
                     "to switch providers, or fund your OpenRouter account "
                     "at https://openrouter.ai/settings/credits"],
                )
            if r.status_code == 429:
                return _ConnectivityResult(
                    "OpenRouter API",
                    [(color("✗", Colors.RED), "OpenRouter API",
                      color("(rate limited)", Colors.DIM))],
                    ["OpenRouter rate limit hit — consider switching to "
                     "a different provider or waiting"],
                )
            return _ConnectivityResult(
                "OpenRouter API",
                [(color("✗", Colors.RED), "OpenRouter API",
                  color(f"(HTTP {r.status_code})", Colors.DIM))],
                [],
            )
        except Exception as e:
            return _ConnectivityResult(
                "OpenRouter API",
                [(color("✗", Colors.RED), "OpenRouter API",
                  color(f"({e})", Colors.DIM))],
                ["Check network connectivity"],
            )

    def _probe_anthropic() -> _ConnectivityResult:
        from atlaz_cli.auth import get_anthropic_key
        key = get_anthropic_key()
        if not key:
            return _ConnectivityResult("Anthropic API", [], [])
        try:
            import httpx
            from agent.anthropic_adapter import (
                _is_oauth_token,
                _COMMON_BETAS,
                _OAUTH_ONLY_BETAS,
                _CONTEXT_1M_BETA,
            )
            headers = {"anthropic-version": "2023-06-01"}
            is_oauth = _is_oauth_token(key)
            if is_oauth:
                headers["Authorization"] = f"Bearer {key}"
                headers["anthropic-beta"] = ",".join(_COMMON_BETAS + _OAUTH_ONLY_BETAS)
            else:
                headers["x-api-key"] = key
            r = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers=headers, timeout=10,
            )
            # Reactive recovery: OAuth subscriptions without 1M context reject the
            # request with 400 "long context beta is not yet available for this
            # subscription". Retry once with that beta stripped so the doctor
            # check doesn't falsely report Anthropic as unreachable.
            if (
                is_oauth
                and r.status_code == 400
                and "long context beta" in r.text.lower()
                and "not yet available" in r.text.lower()
            ):
                headers["anthropic-beta"] = ",".join(
                    [b for b in _COMMON_BETAS if b != _CONTEXT_1M_BETA]
                    + list(_OAUTH_ONLY_BETAS)
                )
                r = httpx.get(
                    "https://api.anthropic.com/v1/models",
                    headers=headers, timeout=10,
                )
            if r.status_code == 200:
                return _ConnectivityResult(
                    "Anthropic API",
                    [(color("✓", Colors.GREEN), "Anthropic API", "")],
                    [],
                )
            if r.status_code == 401:
                return _ConnectivityResult(
                    "Anthropic API",
                    [(color("✗", Colors.RED), "Anthropic API",
                      color("(invalid API key)", Colors.DIM))],
                    [],
                )
            return _ConnectivityResult(
                "Anthropic API",
                [(color("⚠", Colors.YELLOW), "Anthropic API",
                  color("(couldn't verify)", Colors.DIM))],
                [],
            )
        except Exception as e:
            return _ConnectivityResult(
                "Anthropic API",
                [(color("⚠", Colors.YELLOW), "Anthropic API",
                  color(f"({e})", Colors.DIM))],
                [],
            )

    def _probe_apikey_provider(pname, env_vars, default_url, base_env,
                               supports_health_check) -> _ConnectivityResult:
        key = ""
        for ev in env_vars:
            key = os.getenv(ev, "")
            if key:
                break
        if not key:
            return _ConnectivityResult(pname, [], [])
        label = pname.ljust(20)
        if not supports_health_check:
            return _ConnectivityResult(
                pname,
                [(color("✓", Colors.GREEN), label,
                  color("(key configured)", Colors.DIM))],
                [],
            )
        try:
            import httpx
            base = os.getenv(base_env, "") if base_env else ""
            # Auto-detect Kimi Code keys (sk-kimi-) → api.kimi.com/coding/v1
            # (OpenAI-compat surface, which exposes /models for health check).
            if not base and key.startswith("sk-kimi-"):
                base = "https://api.kimi.com/coding/v1"
            # Anthropic-compat endpoints (/anthropic, api.kimi.com/coding
            # with no /v1) don't support /models. Rewrite to OpenAI-compat
            # /v1 surface for health checks.
            if base and base.rstrip("/").endswith("/anthropic"):
                from agent.auxiliary_client import _to_openai_base_url
                base = _to_openai_base_url(base)
            if base_url_host_matches(base, "api.kimi.com") and base.rstrip("/").endswith("/coding"):
                base = base.rstrip("/") + "/v1"
            url = (base.rstrip("/") + "/models") if base else default_url
            headers = {
                "Authorization": f"Bearer {key}",
                "User-Agent": _HERMES_USER_AGENT,
            }
            if base_url_host_matches(base, "api.kimi.com"):
                headers["User-Agent"] = "claude-code/0.1.0"
            # Google's Generative Language API (generativelanguage.googleapis.com)
            # rejects ``Authorization: Bearer *** with 401
            # ``ACCESS_TOKEN_TYPE_UNSUPPORTED`` — that header is reserved for
            # OAuth 2 access tokens, not plain API keys. Plain keys use
            # ``x-goog-api-key`` (or ``?key=``). Without this, a perfectly valid
            # GOOGLE_API_KEY/GEMINI_API_KEY always shows red in ``hermes doctor``.
            if url and base_url_host_matches(url, "generativelanguage.googleapis.com"):
                headers.pop("Authorization", None)
                headers["x-goog-api-key"] = key
            r = httpx.get(url, headers=headers, timeout=10)
            if (
                pname == "Alibaba/DashScope"
                and not base
                and r.status_code == 401
            ):
                r = httpx.get(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
                    headers=headers, timeout=10,
                )
            if r.status_code == 200:
                return _ConnectivityResult(
                    pname,
                    [(color("✓", Colors.GREEN), label, "")],
                    [],
                )
            if r.status_code == 401:
                return _ConnectivityResult(
                    pname,
                    [(color("✗", Colors.RED), label,
                      color("(invalid API key)", Colors.DIM))],
                    [f"Check {env_vars[0]} in .env"],
                )
            return _ConnectivityResult(
                pname,
                [(color("⚠", Colors.YELLOW), label,
                  color(f"(HTTP {r.status_code})", Colors.DIM))],
                [],
            )
        except Exception as e:
            return _ConnectivityResult(
                pname,
                [(color("⚠", Colors.YELLOW), label,
                  color(f"({e})", Colors.DIM))],
                [],
            )

    def _probe_bedrock() -> _ConnectivityResult:
        try:
            from agent.bedrock_adapter import (
                has_aws_credentials,
                resolve_aws_auth_env_var,
                resolve_bedrock_region,
            )
        except ImportError:
            return _ConnectivityResult("AWS Bedrock", [], [])
        if not has_aws_credentials():
            return _ConnectivityResult("AWS Bedrock", [], [])
        auth_var = resolve_aws_auth_env_var()
        region = resolve_bedrock_region()
        label = "AWS Bedrock".ljust(20)
        try:
            import boto3
            from botocore.config import Config as _BotoConfig
            # Trim retries on the actual Bedrock API call so a transient
            # failure doesn't pad the doctor run by 30+ seconds.
            cfg = _BotoConfig(
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 1},
            )
            client = boto3.client("bedrock", region_name=region, config=cfg)
            resp = client.list_foundation_models()
            n = len(resp.get("modelSummaries", []))
            return _ConnectivityResult(
                "AWS Bedrock",
                [(color("✓", Colors.GREEN), label,
                  color(f"({auth_var}, {region}, {n} models)", Colors.DIM))],
                [],
            )
        except ImportError:
            return _ConnectivityResult(
                "AWS Bedrock",
                [(color("⚠", Colors.YELLOW), label,
                  color(f"(boto3 not installed — {sys.executable} -m pip install boto3)",
                        Colors.DIM))],
                [f"Install boto3 for Bedrock: {sys.executable} -m pip install boto3"],
            )
        except Exception as e:
            err_name = type(e).__name__
            return _ConnectivityResult(
                "AWS Bedrock",
                [(color("⚠", Colors.YELLOW), label,
                  color(f"({err_name}: {e})", Colors.DIM))],
                [f"AWS Bedrock: {err_name} — check IAM permissions for "
                 f"bedrock:ListFoundationModels"],
            )

    def _probe_azure_entra() -> _ConnectivityResult:
        """Probe Azure Foundry Entra ID auth, parallel to ``_probe_bedrock``.

        Skipped unless the active config has ``model.provider:
        azure-foundry`` AND ``model.auth_mode: entra_id`` — we don't probe
        the token-service / CLI chain for users on plain API-key Azure.

        Bounded by a 10s timeout (via
        :func:`agent.azure_identity_adapter.describe_active_credential`)
        so a slow token service can't pad the doctor run.
        """
        label = "Azure Foundry (Entra ID)".ljust(28)
        try:
            from atlaz_cli.config import load_config
            cfg = load_config()
            model_cfg = cfg.get("model") if isinstance(cfg, dict) else {}
            if not isinstance(model_cfg, dict):
                return _ConnectivityResult("Azure Foundry (Entra ID)", [], [])
            cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
            auth_mode = str(model_cfg.get("auth_mode") or "").strip().lower()
            if cfg_provider != "azure-foundry" or auth_mode != "entra_id":
                return _ConnectivityResult("Azure Foundry (Entra ID)", [], [])
        except Exception:
            return _ConnectivityResult("Azure Foundry (Entra ID)", [], [])

        try:
            from agent.azure_identity_adapter import (
                EntraIdentityConfig,
                SCOPE_AI_AZURE_DEFAULT,
                describe_active_credential,
                has_azure_identity_installed,
            )
        except Exception as exc:
            return _ConnectivityResult(
                "Azure Foundry (Entra ID)",
                [(color("⚠", Colors.YELLOW), label,
                  color(f"(adapter import failed: {exc})", Colors.DIM))],
                [f"Azure Foundry adapter import failed: {exc}"],
            )

        if not has_azure_identity_installed():
            return _ConnectivityResult(
                "Azure Foundry (Entra ID)",
                [(color("⚠", Colors.YELLOW), label,
                  color("(azure-identity not installed)", Colors.DIM))],
                [f"Install azure-identity: {sys.executable} -m pip install azure-identity"],
            )

        base_url = str(model_cfg.get("base_url") or "").strip()
        entra_cfg = model_cfg.get("entra") or {}
        if not isinstance(entra_cfg, dict):
            entra_cfg = {}
        scope = (
            str(entra_cfg.get("scope") or "").strip()
            or SCOPE_AI_AZURE_DEFAULT
        )
        config = EntraIdentityConfig(
            scope=scope,
        )
        info = describe_active_credential(config=config, timeout_seconds=10.0)
        if info.get("ok"):
            env_sources = info.get("env_sources") or []
            tag = ", ".join(env_sources) if env_sources else "default credential chain"
            return _ConnectivityResult(
                "Azure Foundry (Entra ID)",
                [(color("✓", Colors.GREEN), label,
                  color(f"({tag}, scope={scope})", Colors.DIM))],
                [],
            )
        err = info.get("error") or "credential chain exhausted"
        hint = info.get("hint") or (
            "Run `az login`, set AZURE_TENANT_ID/AZURE_CLIENT_ID/"
            "AZURE_CLIENT_SECRET, or attach a managed identity to this VM."
        )
        return _ConnectivityResult(
            "Azure Foundry (Entra ID)",
            [(color("⚠", Colors.YELLOW), label,
              color(f"({err})", Colors.DIM))],
            [f"Azure Foundry Entra: {err}. {hint}"],
        )

    # Build the probe submission list in display order
    _probes.append(("OpenRouter API", _probe_openrouter))
    _probes.append(("Anthropic API", _probe_anthropic))

    global _APIKEY_PROVIDERS_CACHE
    if _APIKEY_PROVIDERS_CACHE is None:
        _APIKEY_PROVIDERS_CACHE=_build...st()
    for _entry in _APIKEY_PROVIDERS_CACHE:
        _pname, _env_vars, _default_url, _base_env, _supports = _entry
        # Capture loop vars by binding default args — without this, all closures
        # would share the final iteration's values and every probe would hit
        # the last provider's URL.
        _probes.append((_pname, lambda p=_pname, e=_env_vars, u=_default_url,
                                       b=_base_env, s=_supports:
                                _probe_apikey_provider(p, e, u, b, s)))

    _probes.append(("AWS Bedrock", _probe_bedrock))
    _probes.append(("Azure Foundry (Entra ID)", _probe_azure_entra))

    # Print a single status line so users see something happening, then
    # fan out. ``\r`` clears it once the first real result line lands.
    print(f"  {color(f'Running {len(_probes)} connectivity checks in parallel…', Colors.DIM)}",
          end="", flush=True)

    # Disable boto3's EC2 instance-metadata-service probe for the duration
    # of the parallel block. boto's default credential chain tries
    # 169.254.169.254 with a multi-second timeout when we're not on EC2,
    # which dominated the section's wall time before this fix
    # (~2s on a developer laptop, even with the rest parallelized).
    # Set on the parent thread before submitting work so the env-var
    # mutation never races with another worker. has_aws_credentials() in
    # the bedrock probe already gates on real env-var creds, so IMDS is
    # never the legitimate source for `hermes doctor`.
    _imds_prev = os.environ.get("AWS_EC2_METADATA_DISABLED")
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    try:
        # 8 workers is plenty — each probe is a single HTTP call plus a TLS
        # handshake. More than that wastes thread-startup cost and risks
        # noisy output if anything ever printed from inside a worker.
        with _futures.ThreadPoolExecutor(max_workers=8,
                                         thread_name_prefix="doctor-probe") as _ex:
            _futures_in_order = [_ex.submit(_fn) for _, _fn in _probes]
            _results = [_f.result() for _f in _futures_in_order]
    finally:
        if _imds_prev is None:
            os.environ.pop("AWS_EC2_METADATA_DISABLED", None)
        else:
            os.environ["AWS_EC2_METADATA_DISABLED"] = _imds_prev

    # Clear the "Running …" line and print all results in submission order.
    print("\r" + " " * 70 + "\r", end="")
    for _r in _results:
        for _glyph, _label, _detail in _r.lines:
            if _detail:
                print(f"  {_glyph} {_label} {_detail}")
            else:
                print(f"  {_glyph} {_label}")
        _issues_to_add = list(_r.issues)
        if _issues_to_add and _has_healthy_oauth_fallback_for_apikey_provider(_r.label):
            _issues_to_add = []
        for _issue in _issues_to_add:
            issues.append(_issue)

    _section("Tool Availability")
    try:
        # Add project root to path for imports
        sys.path.insert(0, str(PROJECT_ROOT))
        from model_tools import check_tool_availability, TOOLSET_REQUIREMENTS
        
        available, unavailable = check_tool_availability()
        available, unavailable = _apply_doctor_tool_availability_overrides(available, unavailable)
        
        for tid in available:
            info = TOOLSET_REQUIREMENTS.get(tid, {})
            check_ok(info.get("name", tid), _doctor_tool_availability_detail(tid))
        
        for item in unavailable:
            env_vars = item.get("missing_vars") or item.get("env_vars") or []
            if env_vars:
                vars_str = ", ".join(env_vars)
                check_warn(item["name"], f"(missing {vars_str})")
            else:
                check_warn(item["name"], "(system dependency not met)")

        # Count disabled tools with API key requirements
        api_disabled = [u for u in unavailable if (u.get("missing_vars") or u.get("env_vars"))]
        if api_disabled:
            issues.append("Run 'hermes setup' to configure missing API keys for full tool access")
    except Exception as e:
        check_warn("Could not check tool availability", f"({e})")
    
    _section("Skills Hub")
    hub_dir = HERMES_HOME / "skills" / ".hub"
    if hub_dir.exists():
        check_ok("Skills Hub directory exists")
        lock_file = hub_dir / "lock.json"
        if lock_file.exists():
            try:
                import json
                lock_data = json.loads(lock_file.read_text())
                count = len(lock_data.get("installed", {}))
                check_ok(f"Lock file OK ({count} hub-installed skill(s))")
            except Exception:
                check_warn("Lock file", "(corrupted or unreadable)")
        quarantine = hub_dir / "quarantine"
        q_count = sum(1 for d in quarantine.iterdir() if d.is_dir()) if quarantine.exists() else 0
        if q_count > 0:
            check_warn(f"{q_count} skill(s) in quarantine", "(pending review)")
    else:
        check_warn("Skills Hub directory not initialized", "(run: hermes skills list)")

    from atlaz_cli.config import get_env_value

    def _gh_authenticated() -> bool:
        """Check if gh CLI is authenticated via token file or device flow."""
        try:
            result = subprocess.run(
                ["gh", "auth", "status", "--json", "authenticated"],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    github_token = get_env_value("GITHUB_TOKEN") or get_env_value("GH_TOKEN")
    if github_token:
        check_ok("GitHub token configured (authenticated API access)")
    elif _gh_authenticated():
        check_ok("GitHub authenticated via gh CLI", "(full API access — no GITHUB_TOKEN needed)")
    else:
        check_warn("No GITHUB_TOKEN", f"(60 req/hr rate limit — set in {_DHH}/.env for better rates)")

    _section("Memory Provider")
    _active_memory_provider = ""
    try:
        import yaml as _yaml
        _mem_cfg_path = HERMES_HOME / "config.yaml"
        if _mem_cfg_path.exists():
            with open(_mem_cfg_path, encoding="utf-8") as _f:
                _raw_cfg = _yaml.safe_load(_f) or {}
            _active_memory_provider = (_raw_cfg.get("memory") or {}).get("provider", "")
    except Exception:
        pass

    if not _active_memory_provider:
        check_ok("Built-in memory active", "(no external provider configured — this is fine)")
    elif _active_memory_provider == "honcho":
        try:
            from plugins.memory.honcho.client import HonchoClientConfig, resolve_config_path
            hcfg = HonchoClientConfig.from_global_config()
            _honcho_cfg_path = resolve_config_path()

            if not _honcho_cfg_path.exists():
                check_warn("Honcho config not found", "run: hermes memory setup")
            elif not hcfg.enabled:
                check_info(f"Honcho disabled (set enabled: true in {_honcho_cfg_path} to activate)")
            elif not (hcfg.api_key or hcfg.base_url):
                _fail_and_issue(
                    "Honcho API key or base URL not set",
                    "run: hermes memory setup",
                    "No Honcho API key — run 'hermes memory setup'",
                    issues,
                )
            else:
                from plugins.memory.honcho.client import get_honcho_client, reset_honcho_client
                reset_honcho_client()
                try:
                    get_honcho_client(hcfg)
                    check_ok(
                        "Honcho connected",
                        f"workspace={hcfg.workspace_id} mode={hcfg.recall_mode} freq={hcfg.write_frequency}",
                    )
                except Exception as _e:
                    _fail_and_issue("Honcho connection failed", str(_e), f"Honcho unreachable: {_e}", issues)
        except ImportError:
            _fail_and_issue(
                "honcho-ai not installed",
                "pip install honcho-ai",
                "Honcho is set as memory provider but honcho-ai is not installed",
                issues,
            )
        except Exception as _e:
            check_warn("Honcho check failed", str(_e))
    elif _active_memory_provider == "mem0":
        try:
            from plugins.memory.mem0 import _load_config as _load_mem0_config
            mem0_cfg = _load_mem0_config()
            mem0_key = mem0_cfg.get("api_key", "")
            if mem0_key:
                check_ok("Mem0 API key configured")
                check_info(f"user_id={mem0_cfg.get('user_id', '?')}  agent_id={mem0_cfg.get('agent_id', '?')}")
            else:
                _fail_and_issue(
                    "Mem0 API key not set",
                    "(set MEM0_API_KEY in .env or run hermes memory setup)",
                    "Mem0 is set as memory provider but API key is missing",
                    issues,
                )
        except ImportError:
            _fail_and_issue(
                "Mem0 plugin not loadable",
                "pip install mem0ai",
                "Mem0 is set as memory provider but mem0ai is not installed",
                issues,
            )
        except Exception as _e:
            check_warn("Mem0 check failed", str(_e))
    else:
        # Generic check for other memory providers (openviking, hindsight, etc.)
        try:
            from plugins.memory import load_memory_provider
            _provider = load_memory_provider(_active_memory_provider)
            if _provider and _provider.is_available():
                check_ok(f"{_active_memory_provider} provider active")
            elif _provider:
                check_warn(f"{_active_memory_provider} configured but not available", "run: hermes memory status")
            else:
                check_warn(f"{_active_memory_provider} plugin not found", "run: hermes memory setup")
        except Exception as _e:
            check_warn(f"{_active_memory_provider} check failed", str(_e))

    try:
        from atlaz_cli.profiles import list_profiles, _get_wrapper_dir, profile_exists
        import re as _re

        named_profiles = [p for p in list_profiles() if not p.is_default]
        if named_profiles:
            _section("Profiles")
            check_ok(f"{len(named_profiles)} profile(s) found")
            wrapper_dir = _get_wrapper_dir()
            for p in named_profiles:
                parts = []
                if p.gateway_running:
                    parts.append("gateway running")
                if p.model:
                    parts.append(p.model[:30])
                if not (p.path / "config.yaml").exists():
                    parts.append("⚠ missing config")
                if not (p.path / ".env").exists():
                    parts.append("no .env")
                wrapper = wrapper_dir / p.name
                if not wrapper.exists():
                    parts.append("no alias")
                status = ", ".join(parts) if parts else "configured"
                check_ok(f"  {p.name}: {status}")

            # Check for orphan wrappers
            if wrapper_dir.is_dir():
                for wrapper in wrapper_dir.iterdir():
                    if not wrapper.is_file():
                        continue
                    try:
                        content = wrapper.read_text()
                        if "hermes -p" in content:
                            _m = _re.search(r"hermes -p (\S+)", content)
                            if _m and not profile_exists(_m.group(1)):
                                check_warn(f"Orphan alias: {wrapper.name} → profile '{_m.group(1)}' no longer exists")
                    except Exception:
                        pass
    except ImportError:
        pass
    except Exception:
        pass

    print()
    remaining_issues = issues + manual_issues
    if should_fix and fixed_count > 0:
        print(color("─" * 60, Colors.GREEN))
        print(color(f"  Fixed {fixed_count} issue(s).", Colors.GREEN, Colors.BOLD), end="")
        if remaining_issues:
            print(color(f" {len(remaining_issues)} issue(s) require manual intervention.", Colors.YELLOW, Colors.BOLD))
        else:
            print()
        print()
        if remaining_issues:
            for i, issue in enumerate(remaining_issues, 1):
                print(f"  {i}. {issue}")
            print()
    elif remaining_issues:
        print(color("─" * 60, Colors.YELLOW))
        print(color(f"  Found {len(remaining_issues)} issue(s) to address:", Colors.YELLOW, Colors.BOLD))
        print()
        for i, issue in enumerate(remaining_issues, 1):
            print(f"  {i}. {issue}")
        print()
        if not should_fix:
            print(color("  Tip: run 'hermes doctor --fix' to auto-fix what's possible.", Colors.DIM))
    else:
        print(color("─" * 60, Colors.GREEN))
        print(color("  All checks passed! 🎉", Colors.GREEN, Colors.BOLD))
    
    print()