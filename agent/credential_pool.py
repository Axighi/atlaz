"""Persistent multi-credential pool for same-provider failover."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
import uuid
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from atlaz_constants import OPENROUTER_BASE_URL
from atlaz_cli.config import load_env
from atlaz_cli.config import load_env
from agent.credential_persistence import (
    is_borrowed_credential_source,
    sanitize_borrowed_credential_payload,
)
import atlaz_cli.auth as auth_mod
from atlaz_cli.auth import (
    CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    PROVIDER_REGISTRY,
    _auth_store_lock,
    _codex_access_token_is_expiring,
    _decode_jwt_claims,
    _load_auth_store,
    _load_provider_state,
    _resolve_kimi_base_url,
    _resolve_zai_base_url,
    _save_auth_store,
    _save_provider_state,
    _store_provider_state,
    read_credential_pool,
    write_credential_pool,
)

logger = logging.getLogger(__name__)


def _load_config_safe() -> Optional[dict]:
    """Load config.yaml, returning None on any error."""
    try:
        from atlaz_cli.config import load_config

        return load_config()
    except Exception:
        return None


# --- Status and type constants ---

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
# Terminal failure — the credential will never recover on its own.  Used for
# upstream-permanent OAuth states like ``token_invalidated`` / ``token_revoked``
# where retrying after a TTL cooldown is guaranteed to fail.  ``DEAD`` entries
# are excluded from rotation unconditionally and only clear when an explicit
# write-side sync (e.g. ``_save_codex_tokens`` after a fresh device-code
# login) rewrites the tokens.
STATUS_DEAD = "dead"

# OAuth error reasons that indicate the credential is permanently invalid
# server-side and cannot be recovered by retry/refresh.  Sourced from
# OpenAI Codex Responses API, Anthropic, xAI, and Google OAuth spec.
_TERMINAL_AUTH_REASONS=***
    "token_invalidated",   # OpenAI Codex: "Your authentication token has been invalidated."
    "token_revoked",        # OAuth 2.0 RFC 7009: token explicitly revoked
    "invalid_token",        # RFC 6750: bearer token is malformed/expired/revoked
    "invalid_grant",        # RFC 6749: refresh_token rejected during refresh
    "unauthorized_client",  # RFC 6749: client no longer authorized
    "refresh_token_reused", # Single-use refresh token consumed by another process
})

# How long a DEAD manual credential is preserved before being pruned.
# Manual entries (``manual:*``) are independent credentials with no singleton
# to re-seed from, so pruning them after a quiet window cleans up dead state
# without losing recoverability — the user always has the option to re-add
# via ``hermes auth add``.
#
# Singleton-seeded entries (``device_code``, ``loopback_pkce``, ``claude_code``)
# are NOT pruned because ``_seed_from_singletons`` would just re-create them
# on the next ``load_pool()`` with the same stale singleton tokens, defeating
# the cleanup.  They remain in the pool marked DEAD until an explicit re-auth
# write-side sync (``_save_codex_tokens`` etc.) clears the status.
DEAD_MANUAL_PRUNE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

AUTH_TYPE_OAUTH="***"
AUTH_TYPE_API_KEY="***"

SOURCE_MANUAL = "manual"

STRATEGY_FILL_FIRST = "fill_first"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_RANDOM = "random"
STRATEGY_LEAST_USED = "least_used"
SUPPORTED_POOL_STRATEGIES = {
    STRATEGY_FILL_FIRST,
    STRATEGY_ROUND_ROBIN,
    STRATEGY_RANDOM,
    STRATEGY_LEAST_USED,
}

# Cooldown before retrying an exhausted credential.
# Transient 401 auth failures cool down briefly so single-key setups can recover.
# 429 (rate-limited), 402 (billing/quota), and other failures cool down after 1 hour.
# Provider-supplied reset_at timestamps override these defaults.
EXHAUSTED_TTL_401_SECONDS = 5 * 60           # 5 minutes
EXHAUSTED_TTL_429_SECONDS = 60 * 60          # 1 hour
EXHAUSTED_TTL_DEFAULT_SECONDS = 60 * 60      # 1 hour

# Pool key prefix for custom OpenAI-compatible endpoints.
# Custom endpoints all share provider='custom' but are keyed by their
# custom_providers name: 'custom:<normalized_name>'.
CUSTOM_POOL_PREFIX = "custom:"


# Fields that are only round-tripped through JSON — never used for logic as attributes.
_EXTRA_KEYS = frozenset({
    "token_type", "scope", "client_id", "portal_base_url", "obtained_at",
    "expires_in", "agent_key_id", "agent_key_expires_in", "agent_key_reused",
    "agent_key_obtained_at", "tls", "secret_source", "secret_fingerprint",
})


@dataclass
class PooledCredential:
    provider: str
    id: str
    label: str
    auth_type: str
    priority: int
    source: str
    access_token: str
    refresh_token: Optional[str] = None
    last_status: Optional[str] = None
    last_status_at: Optional[float] = None
    last_error_code: Optional[int] = None
    last_error_reason: Optional[str] = None
    last_error_message: Optional[str] = None
    last_error_reset_at: Optional[float] = None
    base_url: Optional[str] = None
    expires_at: Optional[str] = None
    expires_at_ms: Optional[int] = None
    last_refresh: Optional[str] = None
    inference_base_url: Optional[str] = None
    agent_key: Optional[str] = None
    agent_key_expires_at: Optional[str] = None
    request_count: int = 0
    extra: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}

    def __getattr__(self, name: str):
        if name in _EXTRA_KEYS:
            return self.extra.get(name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    @classmethod
    def from_dict(cls, provider: str, payload: Dict[str, Any]) -> "PooledCredential":
        field_names = {f.name for f in fields(cls) if f.name != "provider"}
        data = {k: payload.get(k) for k in field_names if k in payload}
        # Rehydrated last_status_at may be an ISO string from to_dict() — normalize to float epoch
        if "last_status_at" in data and isinstance(data["last_status_at"], str):
            data["last_status_at"] = _parse_absolute_timestamp(data["last_status_at"])
        extra = {k: payload[k] for k in _EXTRA_KEYS if k in payload and payload[k] is not None}
        data["extra"] = extra
        data.setdefault("id", uuid.uuid4().hex[:6])
        data.setdefault("label", payload.get("source", provider))
        data.setdefault("auth_type", AUTH_TYPE_API_KEY)
        data.setdefault("priority", 0)
        data.setdefault("source", SOURCE_MANUAL)
        data.setdefault("access_token", "")
        return cls(provider=provider, **data)

    def to_dict(self) -> Dict[str, Any]:
        _ALWAYS_EMIT = {
            "last_status",
            "last_status_at",
            "last_error_code",
            "last_error_reason",
            "last_error_message",
            "last_error_reset_at",
        }
        result: Dict[str, Any] = {}
        for field_def in fields(self):
            if field_def.name in {"provider", "extra"}:
                continue
            value = getattr(self, field_def.name)
            if value is not None or field_def.name in _ALWAYS_EMIT:
                result[field_def.name] = value
        for k, v in self.extra.items():
            if v is not None:
                result[k] = v
        return sanitize_borrowed_credential_payload(result, self.provider)

    @property
    def runtime_api_key(self) -> str:
        if self.provider == "nous":
            # Nous stores the runtime inference credential in agent_key for
            # compatibility. It must be a NAS invoke JWT.
            for token, expires_at in (
                (self.agent_key, self.agent_key_expires_at),
                (self.access_token, self.expires_at),
            ):
                if (
                    isinstance(token, str)
                    and token.strip()
                    and auth_mod._nous_invoke_jwt_is_usable(
                        token,
                        scope=getattr(self, "scope", None),
                        expires_at=expires_at,
                    )
                ):
                    return token.strip()
            return ""
        return str(self.access_token or "")

    @property
    def runtime_base_url(self) -> Optional[str]:
        if self.provider == "nous":
            return self.inference_base_url or self.base_url
        return self.base_url


def label_from_token(token: str, fallback: str) -> str:
    claims = _decode_jwt_claims(token)
    for key in ("email", "preferred_username", "upn"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _next_priority(entries: List[PooledCredential]) -> int:
    return max((entry.priority for entry in entries), default=-1) + 1


def _is_manual_source(source: str) -> bool:
    normalized = (source or "").strip().lower()
    return normalized == SOURCE_MANUAL or normalized.startswith(f"{SOURCE_MANUAL}:")


def _exhausted_ttl(error_code: Optional[int]) -> int:
    """Return cooldown seconds based on the HTTP status that caused exhaustion."""
    if error_code == 401:
        return EXHAUSTED_TTL_401_SECONDS
    if error_code == 429:
        return EXHAUSTED_TTL_429_SECONDS
    return EXHAUSTED_TTL_DEFAULT_SECONDS


def _parse_absolute_timestamp(value: Any) -> Optional[float]:
    """Best-effort parse for provider reset timestamps.

    Accepts epoch seconds, epoch milliseconds, and ISO-8601 strings.
    Returns seconds since epoch.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            numeric = float(raw)
        except ValueError:
            numeric = None
        if numeric is not None:
            return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _extract_retry_delay_seconds(message: str) -> Optional[float]:
    if not message:
        return None
    delay_match = re.search(r"quotaResetDelay[:\s\"]+(\d+(?:\.\d+)?)(ms|s)", message, re.IGNORECASE)
    if delay_match:
        value = float(delay_match.group(1))
        return value / 1000.0 if delay_match.group(2).lower() == "ms" else value
    sec_match = re.search(r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)", message, re.IGNORECASE)
    if sec_match:
        return float(sec_match.group(1))
    # "Resets in 4hr 5min" format used by OpenCode Go weekly usage limits
    hr_min_match = re.search(r"resets?\s+in\s+(\d+)\s*hr\s+(\d+)\s*min", message, re.IGNORECASE)
    if hr_min_match:
        return int(hr_min_match.group(1)) * 3600 + int(hr_min_match.group(2)) * 60
    hr_only_match = re.search(r"resets?\s+in\s+(\d+)\s*hr\b", message, re.IGNORECASE)
    if hr_only_match:
        return int(hr_only_match.group(1)) * 3600
    min_only_match = re.search(r"resets?\s+in\s+(\d+)\s*min\b", message, re.IGNORECASE)
    if min_only_match:
        return int(min_only_match.group(1)) * 60
    return None


def _normalize_error_context(error_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(error_context, dict):
        return {}
    normalized: Dict[str, Any] = {}
    reason = error_context.get("reason")
    if isinstance(reason, str) and reason.strip():
        normalized["reason"] = reason.strip()
    message = error_context.get("message")
    if isinstance(message, str) and message.strip():
        normalized["message"] = message.strip()
    reset_at = (
        error_context.get("reset_at")
        or error_context.get("resets_at")
        or error_context.get("retry_until")
    )
    parsed_reset_at = _parse_absolute_timestamp(reset_at)
    if parsed_reset_at is None and isinstance(message, str):
        retry_delay_seconds = _extract_retry_delay_seconds(message)
        if retry_delay_seconds is not None:
            parsed_reset_at = time.time() + retry_delay_seconds
    if parsed_reset_at is not None:
        normalized["reset_at"] = parsed_reset_at
    return normalized


def _exhausted_until(entry: PooledCredential) -> Optional[float]:
    if entry.last_status != STATUS_EXHAUSTED:
        return None
    reset_at = _parse_absolute_timestamp(getattr(entry, "last_error_reset_at", None))
    if reset_at is not None:
        return reset_at
    if entry.last_status_at:
        return entry.last_status_at + _exhausted_ttl(entry.last_error_code)
    return None


def _normalize_custom_pool_name(name: str) -> str:
    """Normalize a custom provider name for use as a pool key suffix."""
    return name.strip().lower().replace(" ", "-")


def _iter_custom_providers(config: Optional[dict] = None):
    """Yield (normalized_name, entry_dict) for each valid custom_providers entry."""
    if config is None:
        config = _load_config_safe()
    if config is None:
        return
    custom_providers = config.get("custom_providers")
    if not isinstance(custom_providers, list):
        # Fall back to the v12+ providers dict via the compatibility layer
        try:
            from atlaz_cli.config import get_compatible_custom_providers

            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            return
    if not custom_providers:
        return
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        yield _normalize_custom_pool_name(name), entry


def get_custom_provider_pool_key(base_url: str, provider_name: Optional[str] = None) -> Optional[str]:
    """Look up the custom_providers list in config.yaml and return 'custom:<name>' for a matching base_url.

    When provider_name is given, prefer matching by name first (solving the case where
    multiple custom providers share the same base_url but have different API keys).
    Falls back to base_url matching when no name match is found.

    Returns None if no match is found.
    """
    if not base_url:
        return None
    normalized_url = base_url.strip().rstrip("/")

    # When a provider name is given, try to match by name first.
    # This fixes the P1 bug where two custom providers sharing the same
    # base_url always resolve to the first one's credentials.
    if provider_name:
        normalized_name = _normalize_custom_pool_name(provider_name)
        for norm_name, entry in _iter_custom_providers():
            if norm_name == normalized_name:
                return f"{CUSTOM_POOL_PREFIX}{norm_name}"

    # Fall back to base_url matching (original behavior)
    for norm_name, entry in _iter_custom_providers():
        entry_url = str(entry.get("base_url") or "").strip().rstrip("/")
        if entry_url and entry_url == normalized_url:
            return f"{CUSTOM_POOL_PREFIX}{norm_name}"
    return None


def list_custom_pool_providers() -> List[str]:
    """Return all 'custom:*' pool keys that have entries in auth.json."""
    pool_data = read_credential_pool(None)
    return sorted(
        key for key in pool_data
        if key.startswith(CUSTOM_POOL_PREFIX)
        and isinstance(pool_data.get(key), list)
        and pool_data[key]
    )


def _get_custom_provider_config(pool_key: str) -> Optional[Dict[str, Any]]:
    """Return the custom_providers config entry matching a pool key like 'custom:together.ai'."""
    if not pool_key.startswith(CUSTOM_POOL_PREFIX):
        return None
    suffix = pool_key[len(CUSTOM_POOL_PREFIX):]
    for norm_name, entry in _iter_custom_providers():
        if norm_name == suffix:
            return entry
    return None


def get_pool_strategy(provider: str) -> str:
    """Return the configured selection strategy for a provider."""
    config = _load_config_safe()
    if config is None:
        return STRATEGY_FILL_FIRST

    strategies = config.get("credential_pool_strategies")
    if not isinstance(strategies, dict):
        return STRATEGY_FILL_FIRST

    strategy = str(strategies.get(provider, "") or "").strip().lower()
    if strategy in SUPPORTED_POOL_STRATEGIES:
        return strategy
    return STRATEGY_FILL_FIRST


DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL=***


class CredentialPool:
    def __init__(self, provider: str, entries: List[PooledCredential]):
        self.provider = provider
        self._entries = sorted(entries, key=lambda entry: entry.priority)
        self._current_id: Optional[str] = None
        self._strategy = get_pool_strategy(provider)
        self._lock = threading.Lock()
        self._active_leases: Dict[str, int] = {}
        self._max_concurrent = DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL

    def has_credentials(self) -> bool:
        return bool(self._entries)

    def has_available(self) -> bool:
        """True if at least one entry is not currently in exhaustion cooldown."""
        return bool(self._available_entries())

    def entries(self) -> List[PooledCredential]:
        return list(self._entries)

    def current(self) -> Optional[PooledCredential]:
        if not self._current_id:
            return None
        return next((entry for entry in self._entries if entry.id == self._current_id), None)

    def _replace_entry(self, old: PooledCredential, new: PooledCredential) -> None:
        """Swap an entry in-place by id, preserving sort order."""
        for idx, entry in enumerate(self._entries):
            if entry.id == old.id:
                self._entries[idx] = new
                return

    def _persist(self) -> None:
        write_credential_pool(
            self.provider,
            [entry.to_dict() for entry in self._entries],
        )

    def _is_terminal_auth_failure(
        self,
        status_code: Optional[int],
        normalized_error: Dict[str, Any],
    ) -> bool:
        """Detect upstream-permanent OAuth failures that won't recover on TTL.

        Only fires for 401 responses whose error code/reason matches a known
        terminal OAuth state (token_invalidated, token_revoked, invalid_grant,
        etc.).  Distinguishes permanent failures from transient ones like
        token_expired (refreshable) or generic 401 without a specific reason
        (could be a server-side glitch worth retrying).

        Returns False for non-401 status codes — 429 rate limits and 402
        billing failures are transient by nature and should keep TTL semantics.
        """
        if status_code != 401:
            return False
        reason = normalized_error.get("reason")
        if not isinstance(reason, str):
            return False
        return reason.strip().lower() in _TERMINAL_AUTH_REASONS

    def _mark_exhausted(
        self,
        entry: PooledCredential,
        status_code: Optional[int],
        error_context: Optional[Dict[str, Any]] = None,
    ) -> PooledCredential:
        normalized_error = _normalize_error_context(error_context)
        # Permanent OAuth failures (token_invalidated, token_revoked, etc.)
        # transition to STATUS_DEAD instead of STATUS_EXHAUSTED.  Without this,
        # a revoked credential gets a 1-hour TTL cooldown and then re-enters
        # rotation, failing immediately every hour until the user manually
        # removes it (issue #32849).  DEAD entries are excluded from rotation
        # unconditionally and only clear

... [OUTPUT TRUNCATED - 49605 chars omitted out of 99605 total] ...

ew_entries
            self._persist()
        return count

    def remove_index(self, index: int) -> Optional[PooledCredential]:
        if index < 1 or index > len(self._entries):
            return None
        removed = self._entries.pop(index - 1)
        self._entries = [
            replace(entry, priority=new_priority)
            for new_priority, entry in enumerate(self._entries)
        ]
        self._persist()
        if self._current_id == removed.id:
            self._current_id = None
        return removed

    def resolve_target(self, target: Any) -> Tuple[Optional[int], Optional[PooledCredential], Optional[str]]:
        raw = str(target or "").strip()
        if not raw:
            return None, None, "No credential target provided."

        for idx, entry in enumerate(self._entries, start=1):
            if entry.id == raw:
                return idx, entry, None

        label_matches = [
            (idx, entry)
            for idx, entry in enumerate(self._entries, start=1)
            if entry.label.strip().lower() == raw.lower()
        ]
        if len(label_matches) == 1:
            return label_matches[0][0], label_matches[0][1], None
        if len(label_matches) > 1:
            return None, None, f'Ambiguous credential label "{raw}". Use the numeric index or entry id instead.'
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(self._entries):
                return index, self._entries[index - 1], None
            return None, None, f"No credential #{index}."
        return None, None, f'No credential matching "{raw}".'

    def add_entry(self, entry: PooledCredential) -> PooledCredential:
        entry = replace(entry, priority=_next_priority(self._entries))
        self._entries.append(entry)
        self._persist()
        return entry


def _upsert_entry(entries: List[PooledCredential], provider: str, source: str, payload: Dict[str, Any]) -> bool:
    existing_idx = None
    for idx, entry in enumerate(entries):
        if entry.source == source:
            existing_idx = idx
            break

    if existing_idx is None:
        payload.setdefault("id", uuid.uuid4().hex[:6])
        payload.setdefault("priority", _next_priority(entries))
        payload.setdefault("label", payload.get("label") or source)
        entries.append(PooledCredential.from_dict(provider, payload))
        return True

    existing = entries[existing_idx]
    field_updates = {}
    extra_updates = {}
    _field_names = {f.name for f in fields(existing)}
    for key, value in payload.items():
        if key in {"id", "priority"} or value is None:
            continue
        if key == "label" and existing.label:
            continue
        if key in _field_names:
            if getattr(existing, key) != value:
                field_updates[key] = value
        elif key in _EXTRA_KEYS:
            if existing.extra.get(key) != value:
                extra_updates[key] = value
    if field_updates or extra_updates:
        if extra_updates:
            field_updates["extra"] = {**existing.extra, **extra_updates}
        updated = replace(existing, **field_updates)
        entries[existing_idx] = updated
        # Runtime-only borrowed secret updates should refresh the in-memory
        # entry without forcing auth.json churn when the disk-safe payload is
        # unchanged (for example env keys with the same fingerprint).
        return existing.to_dict() != updated.to_dict()
    return False


def _normalize_pool_priorities(provider: str, entries: List[PooledCredential]) -> bool:
    if provider != "anthropic":
        return False

    source_rank = {
        "env:ANTHROPIC_TOKEN": 0,
        "env:CLAUDE_CODE_OAUTH_TOKEN": 1,
        "hermes_pkce": 2,
        "claude_code": 3,
        "env:ANTHROPIC_API_KEY": 4,
    }
    manual_entries = sorted(
        (entry for entry in entries if _is_manual_source(entry.source)),
        key=lambda entry: entry.priority,
    )
    seeded_entries = sorted(
        (entry for entry in entries if not _is_manual_source(entry.source)),
        key=lambda entry: (
            source_rank.get(entry.source, len(source_rank)),
            entry.priority,
            entry.label,
        ),
    )

    ordered = [*manual_entries, *seeded_entries]
    id_to_idx = {entry.id: idx for idx, entry in enumerate(entries)}
    changed = False
    for new_priority, entry in enumerate(ordered):
        if entry.priority != new_priority:
            entries[id_to_idx[entry.id]] = replace(entry, priority=new_priority)
            changed = True
    return changed


def _seed_from_singletons(provider: str, entries: List[PooledCredential]) -> Tuple[bool, Set[str]]:
    changed = False
    active_sources: Set[str] = set()
    auth_store = _load_auth_store()

    # Shared suppression gate — used at every upsert site so
    # `hermes auth remove <provider> <N>` is stable across all source types.
    try:
        from atlaz_cli.auth import is_source_suppressed as _is_suppressed
    except ImportError:
        def _is_suppressed(_p, _s):  # type: ignore[misc]
            return False

    if provider == "anthropic":
        # Only auto-discover external credentials (Claude Code, Hermes PKCE)
        # when the user has explicitly configured anthropic as their provider.
        # Without this gate, auxiliary client fallback chains silently read
        # ~/.claude/.credentials.json without user consent.  See PR #4210.
        try:
            from atlaz_cli.auth import is_provider_explicitly_configured
            if not is_provider_explicitly_configured("anthropic"):
                return changed, active_sources
        except ImportError:
            pass

        # API-key vs OAuth is a user-visible choice at `hermes setup` ("Claude
        # Pro/Max subscription" vs "Anthropic API key").  The signal that the
        # user picked the API-key path is: ANTHROPIC_API_KEY set in the env,
        # AND no OAuth env vars set — `save_anthropic_api_key()` writes the
        # API key and zeros ANTHROPIC_TOKEN; `save_anthropic_oauth_token()`
        # does the inverse.  When that signal is present we MUST NOT seed
        # autodiscovered OAuth tokens (~/.claude/.credentials.json from the
        # Claude Code CLI, hermes_pkce creds from a previous OAuth login)
        # into the anthropic pool — otherwise rotation on a 401/429 silently
        # flips the session onto an OAuth credential, which forces the Claude
        # Code identity injection, `mcp_` tool-name rewrite, and claude-cli
        # User-Agent header (`agent/anthropic_adapter.py:2128`).  Users who
        # explicitly opted into the API-key path are explicitly opting OUT of
        # that masquerade.  Prefer ~/.hermes/.env over os.environ for the
        # same reason `_seed_from_env` does — that's the authoritative file
        # that `hermes setup` writes.
        _env_file = load_env()

        def _env_val(key: str) -> str:
            return (_env_file.get(key) or os.environ.get(key) or "").strip()

        anthropic_api_key = _env_val("ANTHROPIC_API_KEY")
        anthropic_oauth_env = (
            _env_val("ANTHROPIC_TOKEN") or _env_val("CLAUDE_CODE_OAUTH_TOKEN")
        )
        api_key_path_explicit = bool(anthropic_api_key and not anthropic_oauth_env)

        if api_key_path_explicit:
            # Prune any stale autodiscovered OAuth entries that may have been
            # seeded into the on-disk pool during a previous OAuth session.
            # Without this, switching OAuth -> API key at setup leaves the
            # OAuth entries dormant in auth.json forever and rotation on a
            # transient 401 could revive them.
            retained = [
                entry for entry in entries
                if entry.source not in {"hermes_pkce", "claude_code"}
            ]
            if len(retained) != len(entries):
                entries[:] = retained
                changed = True
            return changed, active_sources

        from agent.anthropic_adapter import read_claude_code_credentials, read_hermes_oauth_credentials

        for source_name, creds in (
            ("hermes_pkce", read_hermes_oauth_credentials()),
            ("claude_code", read_claude_code_credentials()),
        ):
            if creds and creds.get("accessToken"):
                if _is_suppressed(provider, source_name):
                    continue
                active_sources.add(source_name)
                changed |= _upsert_entry(
                    entries,
                    provider,
                    source_name,
                    {
                        "source": source_name,
                        "auth_type": AUTH_TYPE_OAUTH,
                        "access_token": creds.get("accessToken", ""),
                        "refresh_token": creds.get("refreshToken"),
                        "expires_at_ms": creds.get("expiresAt"),
                        "label": label_from_token(creds.get("accessToken", ""), source_name),
                    },
                )

    elif provider == "nous":
        state = _load_provider_state(auth_store, "nous")
        has_runtime_material = bool(
            isinstance(state, dict)
            and (
                str(state.get("access_token") or "").strip()
                or str(state.get("agent_key") or "").strip()
            )
        )
        if state and not has_runtime_material:
            retained = [
                entry for entry in entries
                if entry.source not in {"device_code", "manual:device_code"}
            ]
            if len(retained) != len(entries):
                entries[:] = retained
                changed = True
        if state and has_runtime_material and not _is_suppressed(provider, "device_code"):
            active_sources.add("device_code")
            # Prefer a user-supplied label embedded in the singleton state
            # (set by persist_nous_credentials(label=...) when the user ran
            # `hermes auth add nous --label <name>`).  Fall back to the
            # auto-derived token fingerprint for logins that didn't supply one.
            custom_label = str(state.get("label") or "").strip()
            seeded_label = custom_label or label_from_token(
                state.get("access_token", ""), "device_code"
            )
            changed |= _upsert_entry(
                entries,
                provider,
                "device_code",
                {
                    "source": "device_code",
                    "auth_type": AUTH_TYPE_OAUTH,
                    "access_token": state.get("access_token", ""),
                    "refresh_token": state.get("refresh_token"),
                    "expires_at": state.get("expires_at"),
                    "token_type": state.get("token_type"),
                    "scope": state.get("scope"),
                    "client_id": state.get("client_id"),
                    "portal_base_url": state.get("portal_base_url"),
                    "inference_base_url": state.get("inference_base_url"),
                    "agent_key": state.get("agent_key"),
                    "agent_key_expires_at": state.get("agent_key_expires_at"),
                    # Carry the refresh timestamps into the pool so
                    # freshness-sensitive consumers (self-heal hooks, pool
                    # pruning by age) can distinguish just-refreshed credentials
                    # from stale ones.  Without these, fresh device_code
                    # entries get obtained_at=None and look older than they
                    # are (#15099).
                    "obtained_at": state.get("obtained_at"),
                    "expires_in": state.get("expires_in"),
                    "agent_key_id": state.get("agent_key_id"),
                    "agent_key_expires_in": state.get("agent_key_expires_in"),
                    "agent_key_reused": state.get("agent_key_reused"),
                    "agent_key_obtained_at": state.get("agent_key_obtained_at"),
                    "tls": state.get("tls") if isinstance(state.get("tls"), dict) else None,
                    "label": seeded_label,
                },
            )

    elif provider == "copilot":
        # Copilot tokens are resolved dynamically via `gh auth token` or
        # env vars (COPILOT_GITHUB_TOKEN / GH_TOKEN).  They don't live in
        # the auth store or credential pool, so we resolve them here.
        try:
            from atlaz_cli.copilot_auth import resolve_copilot_token, get_copilot_api_token
            token, source = resolve_copilot_token()
            if token:
                api_token = get_copilot_api_token(token)
                source_name = "gh_cli" if "gh" in source.lower() else f"env:{source}"
                if not _is_suppressed(provider, source_name):
                    active_sources.add(source_name)
                    pconfig = PROVIDER_REGISTRY.get(provider)
                    changed |= _upsert_entry(
                        entries,
                        provider,
                        source_name,
                        {
                            "source": source_name,
                            "auth_type": AUTH_TYPE_API_KEY,
                            "access_token": api_token,
                            "base_url": pconfig.inference_base_url if pconfig else "",
                            "label": source,
                        },
                    )
        except Exception as exc:
            logger.debug("Copilot token seed failed: %s", exc)

    elif provider == "qwen-oauth":
        # Qwen OAuth tokens live in ~/.qwen/oauth_creds.json, written by
        # the Qwen CLI (`qwen auth qwen-oauth`).  They aren't in the
        # Hermes auth store or env vars, so resolve them here.
        # Use refresh_if_expiring=False to avoid network calls during
        # pool loading / provider discovery.
        try:
            from atlaz_cli.auth import resolve_qwen_runtime_credentials
            creds = resolve_qwen_runtime_credentials(refresh_if_expiring=False)
            token = creds.get("api_key", "")
            if token:
                source_name = creds.get("source", "qwen-cli")
                if not _is_suppressed(provider, source_name):
                    active_sources.add(source_name)
                    changed |= _upsert_entry(
                        entries,
                        provider,
                        source_name,
                        {
                            "source": source_name,
                            "auth_type": AUTH_TYPE_OAUTH,
                            "access_token": token,
                            "expires_at_ms": creds.get("expires_at_ms"),
                            "base_url": creds.get("base_url", ""),
                            "label": creds.get("auth_file", source_name),
                        },
                    )
        except Exception as exc:
            logger.debug("Qwen OAuth token seed failed: %s", exc)

    elif provider == "minimax-oauth":
        # MiniMax OAuth tokens live in ~/.hermes/auth.json providers.minimax-oauth.
        # Seed the pool so `/auth list` reflects the logged-in state and the
        # standard `hermes auth remove minimax-oauth <N>` flow works.
        # Use refresh_if_expiring=False equivalent: resolve_minimax_oauth_runtime_credentials
        # always refreshes on expiry, so instead read raw state here to avoid
        # surprise network calls during provider discovery.
        try:
            from atlaz_cli.auth import get_provider_auth_state
            state = get_provider_auth_state("minimax-oauth")
            if state and state.get("access_token"):
                source_name = "oauth"
                if not _is_suppressed(provider, source_name):
                    active_sources.add(source_name)
                    expires_at_ms = None
                    try:
                        from datetime import datetime as _dt
                        raw = state.get("expires_at", "")
                        if raw:
                            expires_at_ms = int(_dt.fromisoformat(raw).timestamp() * 1000)
                    except Exception:
                        expires_at_ms = None
                    base_url = str(state.get("inference_base_url", "") or "").rstrip("/")
                    changed |= _upsert_entry(
                        entries,
                        provider,
                        source_name,
                        {
                            "source": source_name,
                            "auth_type": AUTH_TYPE_OAUTH,
                            "access_token": state["access_token"],
                            "refresh_token": state.get("refresh_token"),
                            "expires_at_ms": expires_at_ms,
                            "base_url": base_url,
                            "label": state.get("label", "") or label_from_token(
                                state.get("access_token", ""), source_name
                            ),
                        },
                    )
        except Exception as exc:
            logger.debug("MiniMax OAuth token seed failed: %s", exc)

    elif provider == "openai-codex":
        # Respect user suppression — `hermes auth remove openai-codex` marks
        # the device_code source as suppressed so it won't be re-seeded from
        # the Hermes auth store.  Without this gate the removal is instantly
        # undone on the next load_pool() call.
        if _is_suppressed(provider, "device_code"):
            return changed, active_sources

        state = _load_provider_state(auth_store, "openai-codex")
        tokens = state.get("tokens") if isinstance(state, dict) else None
        # Hermes owns its own Codex auth state — we do NOT auto-import from
        # ~/.codex/auth.json at pool-load time.  OAuth refresh tokens are
        # single-use, so sharing them with Codex CLI / VS Code causes
        # refresh_token_reused race failures.  Users who want to adopt
        # existing Codex CLI credentials get a one-time, explicit prompt
        # via `hermes auth openai-codex`.
        if isinstance(tokens, dict) and tokens.get("access_token"):
            active_sources.add("device_code")
            changed |= _upsert_entry(
                entries,
                provider,
                "device_code",
                {
                    "source": "device_code",
                    "auth_type": AUTH_TYPE_OAUTH,
                    "access_token": tokens.get("access_token", ""),
                    "refresh_token": tokens.get("refresh_token"),
                    "base_url": "https://chatgpt.com/backend-api/codex",
                    "last_refresh": state.get("last_refresh"),
                    "label": label_from_token(tokens.get("access_token", ""), "device_code"),
                },
            )

    elif provider == "xai-oauth":
        # When the user logs in via ``hermes model`` -> xAI Grok OAuth,
        # tokens are written to the auth.json singleton
        # (``providers["xai-oauth"]``).  Surface them in the pool too so
        # ``hermes auth list`` reflects the logged-in state and so the pool
        # is the single source of truth for refresh during runtime resolution.
        if _is_suppressed(provider, "loopback_pkce"):
            return changed, active_sources

        state = _load_provider_state(auth_store, "xai-oauth")
        tokens = state.get("tokens") if isinstance(state, dict) else None
        if isinstance(tokens, dict) and tokens.get("access_token"):
            active_sources.add("loopback_pkce")
            from atlaz_cli.auth import DEFAULT_XAI_OAUTH_BASE_URL

            base_url = DEFAULT_XAI_OAUTH_BASE_URL
            changed |= _upsert_entry(
                entries,
                provider,
                "loopback_pkce",
                {
                    "source": "loopback_pkce",
                    "auth_type": AUTH_TYPE_OAUTH,
                    "access_token": tokens.get("access_token", ""),
                    "refresh_token": tokens.get("refresh_token"),
                    "base_url": base_url,
                    "last_refresh": state.get("last_refresh"),
                    "label": label_from_token(tokens.get("access_token", ""), "loopback_pkce"),
                },
            )

    return changed, active_sources


def _seed_from_env(provider: str, entries: List[PooledCredential]) -> Tuple[bool, Set[str]]:
    changed = False
    active_sources: Set[str] = set()

    # Prefer ~/.hermes/.env over os.environ — the user's config file is the
    # authoritative source for Hermes credentials. Stale env vars from parent
    # processes (Codex CLI, test scripts, etc.) should not override deliberate
    # changes to the .env file.
    def _get_env_prefer_dotenv(key: str) -> str:
        env_file = load_env()
        val = env_file.get(key) or os.environ.get(key) or ""
        return val.strip()

    # Honour user suppression — `hermes auth remove <provider> <N>` for an
    # env-seeded credential marks the env:<VAR> source as suppressed so it
    # won't be re-seeded from the user's shell environment or ~/.hermes/.env.
    # Without this gate the removal is silently undone on the next
    # load_pool() call whenever the var is still exported by the shell.
    try:
        from atlaz_cli.auth import is_source_suppressed as _is_source_suppressed
    except ImportError:
        def _is_source_suppressed(_p, _s):  # type: ignore[misc]
            return False

    def _secret_source_for_env(env_var: str) -> Optional[str]:
        try:
            from atlaz_cli.env_loader import get_secret_source
            source_label = get_secret_source(env_var)
        except Exception:
            source_label = None
        return str(source_label).strip() if source_label else None

    def _env_payload(
        *,
        source: str,
        env_var: str,
        token: str,
        base_url: str,
        auth_type: str = AUTH_TYPE_API_KEY,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source": source,
            "auth_type": auth_type,
            "access_token": token,
            "base_url": base_url,
            "label": env_var,
        }
        secret_source = _secret_source_for_env(env_var)
        if secret_source:
            payload["secret_source"] = secret_source
        return payload

    if provider == "openrouter":
        # Prefer ~/.hermes/.env over os.environ
        token = _get_env_prefer_dotenv("OPENROUTER_API_KEY")
        if token:
            source = "env:OPENROUTER_API_KEY"
            if _is_source_suppressed(provider, source):
                return changed, active_sources
            active_sources.add(source)
            changed |= _upsert_entry(
                entries,
                provider,
                source,
                _env_payload(
                    source=source,
                    env_var="OPENROUTER_API_KEY",
                    token=token,
                    base_url=OPENROUTER_BASE_URL,
                ),
            )
        return changed, active_sources

    pconfig = PROVIDER_REGISTRY.get(provider)
    if not pconfig or pconfig.auth_type != AUTH_TYPE_API_KEY:
        return changed, active_sources

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = _get_env_prefer_dotenv(pconfig.base_url_env_var).rstrip("/")

    env_vars = list(pconfig.api_key_env_vars)
    if provider == "anthropic":
        env_vars = [
            "ANTHROPIC_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
        ]

    for env_var in env_vars:
        # Prefer ~/.hermes/.env over os.environ
        token = _get_env_prefer_dotenv(env_var)
        if not token:
            continue
        source = f"env:{env_var}"
        if _is_source_suppressed(provider, source):
            continue
        active_sources.add(source)
        auth_type = AUTH_TYPE_OAUTH if provider == "anthropic" and not token.startswith("sk-ant-api") else AUTH_TYPE_API_KEY
        base_url = env_url or pconfig.inference_base_url
        if provider == "kimi-coding":
            base_url = _resolve_kimi_base_url(token, pconfig.inference_base_url, env_url)
        elif provider == "zai":
            base_url = _resolve_zai_base_url(token, pconfig.inference_base_url, env_url)
        changed |= _upsert_entry(
            entries,
            provider,
            source,
            _env_payload(
                source=source,
                env_var=env_var,
                token=token,
                base_url=base_url,
                auth_type=auth_type,
            ),
        )
    return changed, active_sources


def _prune_stale_seeded_entries(entries: List[PooledCredential], active_sources: Set[str]) -> bool:
    retained = [
        entry
        for entry in entries
        if _is_manual_source(entry.source)
        or entry.source in active_sources
        or not (
            is_borrowed_credential_source(entry.source, entry.provider)
            # Hermes PKCE is Hermes-owned/persistable while present, but it is
            # still a file-backed singleton and should disappear from the pool
            # when the backing OAuth file is gone.
            or entry.source == "hermes_pkce"
        )
    ]
    if len(retained) == len(entries):
        return False
    entries[:] = retained
    return True


def _seed_custom_pool(pool_key: str, entries: List[PooledCredential]) -> Tuple[bool, Set[str]]:
    """Seed a custom endpoint pool from custom_providers config and model config."""
    changed = False
    active_sources: Set[str] = set()

    # Shared suppression gate — same pattern as _seed_from_env/_seed_from_singletons.
    try:
        from atlaz_cli.auth import is_source_suppressed as _is_suppressed
    except ImportError:
        def _is_suppressed(_p, _s):  # type: ignore[misc]
            return False

    # Seed from the custom_providers config entry's api_key field
    cp_config = _get_custom_provider_config(pool_key)
    if cp_config:
        api_key = str(cp_config.get("api_key") or "").strip()
        base_url = str(cp_config.get("base_url") or "").strip().rstrip("/")
        name = str(cp_config.get("name") or "").strip()
        if api_key:
            source = f"config:{name}"
            if not _is_suppressed(pool_key, source):
                active_sources.add(source)
                changed |= _upsert_entry(
                    entries,
                    pool_key,
                    source,
                    {
                        "source": source,
                        "auth_type": AUTH_TYPE_API_KEY,
                        "access_token": api_key,
                        "base_url": base_url,
                        "label": name or source,
                    },
                )

    # Seed from model.api_key if model.provider=='custom' and model.base_url matches
    try:
        config = _load_config_safe()
        model_cfg = config.get("model") if config else None
        if isinstance(model_cfg, dict):
            model_provider = str(model_cfg.get("provider") or "").strip().lower()
            model_base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
            model_api_key = ""
            for k in ("api_key", "api"):
                v = model_cfg.get(k)
                if isinstance(v, str) and v.strip():
                    model_api_key = v.strip()
                    break
            if model_provider == "custom" and model_base_url and model_api_key:
                # Check if this model's base_url matches our custom provider
                matched_key = get_custom_provider_pool_key(model_base_url)
                if matched_key == pool_key:
                    source = "model_config"
                    if not _is_suppressed(pool_key, source):
                        active_sources.add(source)
                        changed |= _upsert_entry(
                            entries,
                            pool_key,
                            source,
                            {
                                "source": source,
                                "auth_type": AUTH_TYPE_API_KEY,
                                "access_token": model_api_key,
                                "base_url": model_base_url,
                                "label": "model_config",
                            },
                        )
    except Exception:
        pass

    return changed, active_sources


def load_pool(provider: str) -> CredentialPool:
    provider = (provider or "").strip().lower()
    raw_entries = read_credential_pool(provider)
    raw_needs_sanitization = any(
        isinstance(payload, dict)
        and sanitize_borrowed_credential_payload(payload, provider) != payload
        for payload in raw_entries
    )
    entries = [PooledCredential.from_dict(provider, payload) for payload in raw_entries]

    if provider.startswith(CUSTOM_POOL_PREFIX):
        # Custom endpoint pool — seed from custom_providers config and model config
        custom_changed, custom_sources = _seed_custom_pool(provider, entries)
        changed = raw_needs_sanitization or custom_changed
        changed |= _prune_stale_seeded_entries(entries, custom_sources)
    else:
        singleton_changed, singleton_sources = _seed_from_singletons(provider, entries)
        env_changed, env_sources = _seed_from_env(provider, entries)
        changed = raw_needs_sanitization or singleton_changed or env_changed
        changed |= _prune_stale_seeded_entries(entries, singleton_sources | env_sources)
        changed |= _normalize_pool_priorities(provider, entries)

    if changed:
        write_credential_pool(
            provider,
            [entry.to_dict() for entry in sorted(entries, key=lambda item: item.priority)],
        )
    return CredentialPool(provider, entries)