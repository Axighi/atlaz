# Backward-compat shim: hermes_logging → atlaz_logging
# Deprecated — will be removed in v1.0
import warnings
warnings.warn(
    "Import from 'hermes_logging' is deprecated. Use 'atlaz_logging' instead.",
    DeprecationWarning, stacklevel=2
)
# ---------------------------------------------------------------------------
# Public session context API
def set_session_context(session_id: str) -> None:
    """Set the session ID for the current thread.
    All subsequent log records on this thread will include ``[session_id]``
    in the formatted output.  Call at the start of ``run_conversation()``.
    """
    _session_context.session_id = session_id
def clear_session_context() -> None:
    """Clear the session ID for the current thread."""
    _session_context.session_id = None
# Record factory — injects session_tag into every LogRecord at creation
def _install_session_record_factory() -> None:
    """Replace the global LogRecord factory with one that adds ``session_tag``.
    Unlike a ``logging.Filter`` on a handler or logger, the record factory
    runs for EVERY record in the process — including records that propagate
    from child loggers and records handled by third-party handlers.  This
    guarantees ``%(session_tag)s`` is always available in format strings,
    eliminating the KeyError that would occur if a handler used our format
    without having a ``_SessionFilter`` attached.
    Idempotent — checks for a marker attribute to avoid double-wrapping if
    the module is reloaded.
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_hermes_session_injector", False):
        return  # already installed
    def _session_record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        sid = getattr(_session_context, "session_id", None)
        record.session_tag = f" [{sid}]" if sid else ""  # type: ignore[attr-defined]
        return record
    _session_record_factory._hermes_session_injector = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_session_record_factory)
# Install immediately on import — session_tag is available on all records
# from this point forward, even before setup_logging() is called.
_install_session_record_factory()
# Filters
class _ComponentFilter(logging.Filter):
    """Only pass records whose logger name starts with one of *prefixes*.
    Used to route gateway-specific records to ``gateway.log`` while
    keeping ``agent.log`` as the catch-all.
    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes)
# Logger name prefixes that belong to each component.
# Used by _ComponentFilter and exposed for ``hermes logs --component``.
COMPONENT_PREFIXES = {
    "gateway": ("gateway", "hermes_plugins"),
    "agent": ("agent", "run_agent", "model_tools", "batch_runner"),
    "tools": ("tools",),
    "cli": ("atlaz_cli", "cli"),
    "cron": ("cron",),
}
# Main setup
def setup_logging(
    *,
    hermes_home: Optional[Path] = None,
    log_level: Optional[str] = None,
    max_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Configure the Hermes logging subsystem.
    Safe to call multiple times — the second call is a no-op unless
    *force* is ``True``.
    Parameters
    ----------
    hermes_home
        Override for the Hermes home directory.  Falls back to
        ``get_hermes_home()`` (profile-aware).
    log_level
        Minimum level for the ``agent.log`` file handler.  Accepts any
        standard Python level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
        Defaults to ``"INFO"`` or the value from config.yaml ``logging.level``.
    max_size_mb
        Maximum size of each log file in megabytes before rotation.
        Defaults to 5 or the value from config.yaml ``logging.max_size_mb``.
    backup_count
        Number of rotated backup files to keep.
        Defaults to 3 or the value from config.yaml ``logging.backup_count``.
    mode
        Caller context: ``"cli"``, ``"gateway"``, ``"cron"``.
        When ``"gateway"``, an additional ``gateway.log`` file is created
        that receives only gateway-component records.
    force
        Re-run setup even if it has already been called.
    Returns
    -------
    Path
        The ``logs/`` directory where files are written.
    global _logging_initialized
    home = hermes_home or get_hermes_home()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Read config defaults (best-effort — config may not be loaded yet).
    cfg_level, cfg_max_size, cfg_backup = _read_logging_config()
    level_name = (log_level or cfg_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    max_bytes = (max_size_mb or cfg_max_size or 5) * 1024 * 1024
    backups = backup_count or cfg_backup or 3
    # Lazy import to avoid circular dependency at module load time.
    from agent.redact import RedactingFormatter
    root = logging.getLogger()
    # --- agent.log (INFO+) — the main activity log -------------------------
    _add_rotating_handler(
        root,
        log_dir / "agent.log",
        level=level,
        max_bytes=max_bytes,
        backup_count=backups,
        formatter=RedactingFormatter(_LOG_FORMAT),
    )
    # --- errors.log (WARNING+) — quick triage log --------------------------
        log_dir / "errors.log",
        level=logging.WARNING,
        max_bytes=2 * 1024 * 1024,
        backup_count=2,
    # --- gateway.log (INFO+, gateway component only) ------------------------
    if mode == "gateway":
            log_dir / "gateway.log",
            level=logging.INFO,
            max_bytes=5 * 1024 * 1024,
            backup_count=3,
            log_filter=_ComponentFilter(COMPONENT_PREFIXES["gateway"]),
    if _logging_initialized and not force:
        return log_dir
    # Ensure root logger level is low enough for the handlers to fire.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    # Suppress noisy third-party loggers.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _logging_initialized = True
def setup_verbose_logging() -> None:
    """Enable DEBUG-level console logging for ``--verbose`` / ``-v`` mode.
    Called by ``AIAgent.__init__()`` when ``verbose_logging=True``.
    # Avoid adding duplicate stream handlers.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            if getattr(h, "_hermes_verbose", False):
                return
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT_VERBOSE, datefmt="%H:%M:%S"))
    handler._hermes_verbose = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    # Lower root logger level so DEBUG records reach all handlers.
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    # Keep third-party libraries at WARNING to reduce noise.
    # rex-deploy at INFO for sandbox status.
    logging.getLogger("rex-deploy").setLevel(logging.INFO)
# Internal helpers
class _ManagedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that ensures group-writable perms in managed mode
    AND survives external rotation.
    Two responsibilities:
    1.  In managed mode (NixOS), the stateDir uses setgid (2770) so new files
        inherit the hermes group. However, both ``_open()`` (initial creation)
        and ``doRollover()`` create files via ``open()``, which uses the
        process umask — typically 0022, producing 0644. This subclass applies
        ``chmod 0660`` after both operations so the gateway and interactive
        users can share log files.
    2.  ``RotatingFileHandler`` keeps an open file descriptor.  If anything
        rotates the file *externally* (``logrotate``, manual ``mv``,
        another process rotating under us, a transient unlink), our fd
        keeps pointing at the renamed/unlinked inode and every subsequent
        write goes to ``gateway.log.1`` instead of ``gateway.log`` — silent
        log loss for the file every operator expects to read.  Before each
        emit we ``stat`` ``baseFilename`` and compare it against the open
        stream's inode; on mismatch we reopen.  This is the same pattern
        as stdlib ``WatchedFileHandler.reopenIfNeeded()``, adapted for
        rotating handlers.
    def __init__(self, *args, **kwargs):
        from atlaz_cli.config import is_managed
        self._managed = is_managed()
        super().__init__(*args, **kwargs)
        # Snapshot the inode of the currently open stream so emit() can
        # detect external rotation without an extra fstat per write.
        self._stat_dev: Optional[int] = None
        self._stat_ino: Optional[int] = None
        self._record_stream_stat()
    def _chmod_if_managed(self):
        if self._managed:
            try:
                os.chmod(self.baseFilename, 0o660)
            except OSError:
                pass
    def _record_stream_stat(self) -> None:
        """Snapshot dev/ino of ``baseFilename`` so we can detect external rotation."""
            st = os.stat(self.baseFilename)
            self._stat_dev, self._stat_ino = st.st_dev, st.st_ino
            self._stat_dev, self._stat_ino = None, None
    def _reopen_if_externally_rotated(self) -> None:
        """Reopen the stream when ``baseFilename`` no longer matches our fd.
        Triggered when ``baseFilename`` was renamed (logrotate), unlinked,
        or replaced by a different inode.  Silent + best-effort: any error
        falls back to the existing (possibly stale) stream so logging keeps
        working instead of dying on a stat failure.
        except FileNotFoundError:
            # File was rotated/unlinked underneath us.  Close + reopen so a
            # fresh inode is created at the expected path.
                if self.stream is not None:
                    self.stream.close()
            except Exception:
            self.stream = None  # type: ignore[assignment]
                self.stream = self._open()
                # Couldn't reopen — leave stream=None; next emit will
                # bail rather than write to a stale inode.
            return  # transient — try again on the next emit
        if self._stat_dev is None or self._stat_ino is None:
        if (st.st_dev, st.st_ino) != (self._stat_dev, self._stat_ino):
            # baseFilename now points at a DIFFERENT inode than the one we
            # hold open.  Close the old stream and open the new file.
    def emit(self, record: logging.LogRecord) -> None:
        # Cheap-ish stat-per-record check; the kernel caches inode metadata
        # so the syscall is sub-microsecond on a hot file.
        if self.stream is not None or os.path.exists(self.baseFilename):
            self._reopen_if_externally_rotated()
        super().emit(record)
    def _open(self):
        stream = super()._open()
        self._chmod_if_managed()
        return stream
    def doRollover(self):
        super().doRollover()
        # Our own rollover writes a new baseFilename; refresh the snapshot
        # so the next emit doesn't mistake it for external rotation.
def _add_rotating_handler(
    logger: logging.Logger,
    path: Path,
    level: int,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
    log_filter: Optional[logging.Filter] = None,
) -> None:
    """Add a ``RotatingFileHandler`` to *logger*, skipping if one already
    exists for the same resolved file path (idempotent).
    log_filter
        Optional filter to attach to the handler (e.g. ``_ComponentFilter``
        for gateway.log).
    resolved = path.resolve()
    for existing in logger.handlers:
        if (
            isinstance(existing, RotatingFileHandler)
            and Path(getattr(existing, "baseFilename", "")).resolve() == resolved
        ):
            return  # already attached
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _ManagedRotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if log_filter is not None:
        handler.addFilter(log_filter)
    logger.addHandler(handler)
def _read_logging_config():
    """Best-effort read of ``logging.*`` from config.yaml.
    Returns ``(level, max_size_mb, backup_count)`` — any may be ``None``.
        import yaml
        config_path = get_config_path()
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            log_cfg = cfg.get("logging", {})
            if isinstance(log_cfg, dict):
                return (
                    log_cfg.get("level"),
                    log_cfg.get("max_size_mb"),
                    log_cfg.get("backup_count"),
    return (None, None, None)
from atlaz_logging import *  # noqa: F401, F403