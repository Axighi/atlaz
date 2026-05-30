"""Shared auxiliary client router for side tasks.

Provides a single resolution chain so every consumer (context compression,
session search, web extraction, vision analysis, browser vision) picks up
the best available backend without duplicating fallback logic.

Resolution order for text tasks (auto mode):
  1. User's main provider + main model (used regardless of provider type —
     aggregators, direct API-key providers, native Anthropic, Codex, etc.)
  2. OpenRouter  (OPENROUTER_API_KEY)
  3. Nous Portal (~/.hermes/auth.json active provider)
  4. Custom endpoint (config.yaml model.base_url + OPENAI_API_KEY)
  5. Native Anthropic
  6. Direct API-key providers (z.ai/GLM, Kimi/Moonshot, MiniMax, MiniMax-CN)
  7. None

Resolution order for vision/multimodal tasks (auto mode):
  1. Selected main provider, if it is one of the supported vision backends below
  2. OpenRouter
  3. Nous Portal
  4. Native Anthropic
  5. Custom endpoint (for local vision models: Qwen-VL, LLaVA, Pixtral, etc.)
  6. None

Codex OAuth (ChatGPT-account auth) is intentionally NOT in either
fallback chain: OpenAI gates this endpoint behind an undocumented,
shifting model allow-list, so "just try Codex with a hardcoded model"
rots on its own.  Codex is used only when the user's main provider *is*
openai-codex (Step 1 above) or when a caller explicitly requests it with
a model (auxiliary.<task>.provider + auxiliary.<task>.model).

Per-task overrides are configured in config.yaml under the ``auxiliary:`` section
(e.g. ``auxiliary.vision.provider``, ``auxiliary.compression.model``).
Default "auto" follows the chains above.

Payment / credit exhaustion fallback:
  When a resolved provider returns HTTP 402 or a credit-related error,
  call_llm() automatically retries with the next available provider in the
  auto-detection chain.  This handles the common case where a user depletes
  their OpenRouter balance but has Codex OAuth or another provider available.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path  # noqa: F401 — used by test mocks
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from urllib.parse import urlparse, parse_qs, urlunparse

# NOTE: `from openai import OpenAI` is deliberately NOT at module top — the
# openai SDK pulls a large type tree (~240 ms cold, including responses/*,
# graders/*). We expose `OpenAI` here as a thin proxy that imports the SDK on
# first call and forwards, so:
#   (a) the 15+ in-module `OpenAI(...)` construction sites work unchanged
#       (Python's function-scope name lookup resolves `OpenAI` to the proxy
#       object bound in module globals here, without triggering any import);
#   (b) external code can still do `auxiliary_client.OpenAI` or
#       `patch("agent.auxiliary_client.OpenAI", ...)` — tests see the proxy,
#       and patch replaces the module attribute as usual;
#   (c) `OpenAI` as a type annotation resolves at runtime to the proxy class
#       (which is harmless — annotations aren't type-checked at runtime).
# See tests/agent/test_auxiliary_client.py for patch patterns this supports.
if TYPE_CHECKING:
    from openai import OpenAI  # noqa: F401 — type hints only

_OPENAI_CLS_CACHE: Optional[type] = None


def _load_openai_cls() -> type:
    """Import and cache ``openai.OpenAI``."""
    global _OPENAI_CLS_CACHE
    if _OPENAI_CLS_CACHE is None:
        from openai import OpenAI as _cls
        _OPENAI_CLS_CACHE = _cls
    return _OPENAI_CLS_CACHE


class _OpenAIProxy:
    """Module-level proxy that looks like the ``openai.OpenAI`` class.

    Forwards ``OpenAI(...)`` calls and ``isinstance(x, OpenAI)`` checks to the
    real SDK class, importing the SDK lazily on first use.
    """

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        return _load_openai_cls()(*args, **kwargs)

    def __instancecheck__(self, obj):
        return isinstance(obj, _load_openai_cls())

    def __repr__(self):
        return "<lazy openai.OpenAI proxy>"


OpenAI = _OpenAIProxy()  # module-level name, resolves lazily on call/isinstance

from agent.credential_pool import load_pool
from atlaz_cli.config import get_hermes_home
from atlaz_constants import OPENROUTER_BASE_URL
from atlaz_cli.config import get_hermes_home
from utils import base_url_host_matches, base_url_hostname, normalize_proxy_env_vars

logger = logging.getLogger(__name__)


def _safe_isinstance(obj: Any, maybe_type: Any) -> bool:
    """Return False instead of raising when a patched symbol is not a type."""
    try:
        return isinstance(obj, maybe_type)
    except TypeError:
        return False


def _extract_url_query_params(url: str):
    """Extract query params from URL, return (clean_url, default_query dict or None)."""
    parsed = urlparse(url)
    if parsed.query:
        clean = urlunparse(parsed._replace(query=""))
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return clean, params
    return url, None


# Module-level flag: only warn once per process about stale OPENAI_BASE_URL.
_stale_base_url_warned = False

_PROVIDER_ALIASES = {
    "google": "gemini",
    "google-gemini": "gemini",
    "google-ai-studio": "gemini",
    "x-ai": "xai",
    "x.ai": "xai",
    "grok": "xai",
    "glm": "zai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",
    "kimi": "kimi-coding",
    "moonshot": "kimi-coding",
    "kimi-cn": "kimi-coding-cn",
    "moonshot-cn": "kimi-coding-cn",
    "gmi-cloud": "gmi",
    "gmicloud": "gmi",
    "minimax-china": "minimax-cn",
    "minimax_cn": "minimax-cn",
    "claude": "anthropic",
    "claude-code": "anthropic",
    "github": "copilot",
    "github-copilot": "copilot",
    "github-model": "copilot",
    "github-models": "copilot",
    "github-copilot-acp": "copilot-acp",
    "copilot-acp-agent": "copilot-acp",
    "tencent": "tencent-tokenhub",
    "tokenhub": "tencent-tokenhub",
    "tencent-cloud": "tencent-tokenhub",
    "tencentmaas": "tencent-tokenhub",
}


def _normalize_aux_provider(provider: Optional[str]) -> str:
    normalized = (provider or "auto").strip().lower()
    if normalized.startswith("custom:"):
        suffix = normalized.split(":", 1)[1].strip()
        if not suffix:
            return "custom"
        normalized = suffix
    if normalized == "codex":
        return "openai-codex"
    if normalized == "main":
        # Resolve to the user's actual main provider so named custom providers
        # and non-aggregator providers (DeepSeek, Alibaba, etc.) work correctly.
        main_prov = (_read_main_provider() or "").strip().lower()
        if main_prov and main_prov not in {"auto", "main", ""}:
            normalized = main_prov
        else:
            return "custom"
    return _PROVIDER_ALIASES.get(normalized, normalized)


# Sentinel: when returned by _fixed_temperature_for_model(), callers must
# strip the ``temperature`` key from API kwargs entirely so the provider's
# server-side default applies.  Kimi/Moonshot models manage temperature
# internally — sending *any* value (even the "correct" one) can conflict
# with gateway-side mode selection (thinking → 1.0, non-thinking → 0.6).
OMIT_TEMPERATURE: object = object()


def _is_kimi_model(model: Optional[str]) -> bool:
    """True for any Kimi / Moonshot model that manages temperature server-side."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare.startswith("kimi-") or bare == "kimi"


def _is_arcee_trinity_thinking(model: Optional[str]) -> bool:
    """True for Arcee Trinity Large Thinking (direct or via OpenRouter)."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare == "trinity-large-thinking"


def _fixed_temperature_for_model(
    model: Optional[str],
    base_url: Optional[str] = None,
) -> "Optional[float] | object":
    """Return a temperature directive for models with strict contracts.

    Returns:
        ``OMIT_TEMPERATURE`` — caller must remove the ``temperature`` key so the
            provider chooses its own default.  Used for all Kimi / Moonshot
            models whose gateway selects temperature server-side.
        ``float`` — a specific value the caller must use (reserved for future
            models with fixed-temperature contracts).
        ``None`` — no override; caller should use its own default.
    """
    if _is_kimi_model(model):
        logger.debug("Omitting temperature for Kimi model %r (server-managed)", model)
        return OMIT_TEMPERATURE
    if _is_arcee_trinity_thinking(model):
        return 0.5
    return None


def _compression_threshold_for_model(model: Optional[str]) -> Optional[float]:
    """Return a context-compression threshold override for specific models.

    The threshold is the fraction of the model's context window that must be
    consumed before Hermes triggers summarization.  Higher values delay
    compression and preserve more raw context.

    Returns a float in (0, 1] to override the global ``compression.threshold``
    config value, or ``None`` to leave the user's config value unchanged.
    """
    if _is_arcee_trinity_thinking(model):
        return 0.75
    return None

# Default auxiliary models for direct API-key providers (cheap/fast for side tasks)
def _get_aux_model_for_provider(provider_id: str) -> str:
    """Return the cheap auxiliary model for a provider.

    Reads from ProviderProfile.default_aux_model first, falling back to the
    legacy hardcoded dict for providers that predate the profiles system.
    """
    try:
        from providers import get_provider_profile
        _p = get_provider_profile(provider_id)
        if _p and _p.default_aux_model:
            return _p.default_aux_model
    except Exception:
        pass
    return _API_KEY_PROVIDER_AUX_MODELS_FALLBACK.get(provider_id, "")


# Fallback for providers not yet migrated to ProviderProfile.default_aux_model,
# plus providers we intentionally keep pinned here (e.g. Anthropic predates
# profiles). New providers should set default_aux_model on their profile instead.
_API_KEY_PROVIDER_AUX_MODELS_FALLBACK: Dict[str, str] = {
    "gemini": "gemini-3-flash-preview",
    "zai": "glm-4.5-flash",
    "kimi-coding": "kimi-k2-turbo-preview",
    "stepfun": "step-3.5-flash",
    "kimi-coding-cn": "kimi-k2-turbo-preview",
    "gmi": "google/gemini-3.1-flash-lite-preview",
    "minimax": "MiniMax-M2.7",
    "minimax-oauth": "MiniMax-M2.7-highspeed",
    "minimax-cn": "MiniMax-M2.7",
    "anthropic": "claude-haiku-4-5-20251001",
    "opencode-zen": "gemini-3-flash",
    "opencode-go": "glm-5",
    "kilocode": "google/gemini-3-flash-preview",
    "ollama-cloud": "nemotron-3-nano:30b",
    "tencent-tokenhub": "hy3-preview",
}

# Legacy alias — callers that haven't been updated to _get_aux_model_for_provider()
# can still use this dict directly. Kept in sync with _FALLBACK above.
_API_KEY_PROVIDER_AUX_MODELS: Dict[str, str] = _API_KEY_PROVIDER_AUX_MODELS_FALLBACK

# Vision-specific model overrides for direct providers.
# When the user's main provider has a dedicated vision/multimodal model that
# differs from their main chat model, map it here.  The vision auto-detect
# "exotic provider" branch checks this before falling back to the main model.
_PROVIDER_VISION_MODELS: Dict[str, str] = {
    "xiaomi": "mimo-v2.5",
    "zai": "glm-5v-turbo",
}

# Providers whose endpoint does not accept image input, even though the
# provider's broader ecosystem has vision models available elsewhere.  When
# `auxiliary.vision.provider: auto` sees one of these as the main provider,
# it must skip straight to the aggregator chain instead of returning a client
# that will 404 on every vision request.
#
# kimi-coding / kimi-coding-cn: the Kimi Coding Plan routes through
# api.kimi.com/coding (Anthropic Messages wire) which Kimi's own docs
# describe as having no image_in capability. Vision lives on the separate
# Kimi Platform (api.moonshot.ai, OpenAI-wire, pay-as-you-go).  See #17076.
_PROVIDERS_WITHOUT_VISION: frozenset = frozenset({
    "kimi-coding",
    "kimi-coding-cn",
})

# OpenRouter app attribution headers (base — always sent).
# `X-Title` is the canonical attribution header OpenRouter's dashboard
# reads; the previous `X-OpenRouter-Title` label was not recognized there.
_OR_HEADERS_BASE = {
    "HTTP-Referer": "https://atlaz.nousresearch.com",
    "X-Title": "Hermes Agent",
    "X-OpenRouter-Categories": "productivity,cli-agent",
}

# Truthy values for boolean env-var parsing.
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def build_or_headers(or_config: dict | None = None) -> dict:
    """Build OpenRouter headers, optionally including response-cache headers.

    Precedence for response cache: env var > config.yaml > default (enabled).

    Environment variables:
        ``HERMES_OPENROUTER_CACHE`` — truthy (``1``/``true``/``yes``/``on``)
            enables caching; ``0``/``false``/``no``/``off`` disables.
            Overrides ``openrouter.response_cache`` in config.yaml.
        ``HERMES_OPENROUTER_CACHE_TTL`` — integer seconds (1-86400).
            Overrides ``openrouter.response_cache_ttl`` in config.yaml.

    *or_config* is the ``openrouter`` section from config.yaml.  When *None*,
    falls back to reading config from disk via ``load_config()``.
    """
    headers = dict(_OR_HEADERS_BASE)

    # Resolve config from disk if not provided.
    if or_config is None:
        try:
            from atlaz_cli.config import load_config
            or_config = load_config().get("openrouter", {})
        except Exception:
            or_config = {}

    # Determine cache enabled: env var overrides config.
    env_cache = os.environ.get("HERMES_OPENROUTER_CACHE", "").strip().lower()
    if env_cache:
        cache_enabled = env_cache in _TRUTHY_ENV_VALUES
    else:
        cache_enabled = or_config.get("response_cache", False)

    if not cache_enabled:
        return headers

    headers["X-OpenRouter-Cache"] = "true"

    # Determine TTL: env var overrides config.
    env_ttl = os.environ.get("HERMES_OPENROUTER_CACHE_TTL", "").strip()
    if env_ttl:
        if env_ttl.isdigit():
            ttl = int(env_ttl)
            if 1 <= ttl <= 86400:
                headers["X-OpenRouter-Cache-TTL"] = str(ttl)
    else:
        ttl = or_config.get("response_cache_ttl", 300)
        if isinstance(ttl, (int, float)) and 1 <= ttl <= 86400:
            headers["X-OpenRouter-Cache-TTL"] = str(int(ttl))

    return headers


# NVIDIA NIM cloud billing attribution.  Keep this host-gated because the
# nvidia provider also supports local/on-prem NIM endpoints via NVIDIA_BASE_URL.
_NVIDIA_NIM_CLOUD_HEADERS = {
    "X-BILLING-INVOKE-ORIGIN": "HermesAgent",
}


def build_nvidia_nim_headers(base_url: str | None) -> dict:
    """Return NVIDIA NIM cloud attribution headers for build.nvidia.com traffic."""
    if base_url_host_matches(str(base_url or ""), "integrate.api.nvidia.com"):
        return dict(_NVIDIA_NIM_CLOUD_HEADERS)
    return {}



# Nous Portal extra_body for product attribution.
# Callers should pass this as extra_body in chat.completions.create()
# when the auxiliary client is backed by Nous Portal.
#
# The tags are computed from agent.portal_tags so the client= marker stays
# in lockstep with atlaz_cli.__version__ across every Portal call site
# (main loop, aux, compression, web_extract). Do not inline a literal here;
# see agent/portal_tags.py for the rationale.
from agent.portal_tags import nous_portal_tags as _nous_portal_tags


def _nous_extra_body() -> dict:
    """Return a fresh Nous Portal ``extra_body`` dict.

    Computed at call time so a hot-reloaded ``atlaz_cli.__version__`` is
    reflected without restarting long-running processes.
    """
    return {"tags": _nous_portal_tags()}


# Backwards-compatible module attribute. Some callers (tests, third-party
# plugins) read ``NOUS_EXTRA_BODY`` directly; keep it as a snapshot of the
# current tags. Callers that need the freshest value should call
# ``_nous_extra_body()`` or import ``nous_portal_tags`` directly.
NOUS_EXTRA_BODY = _nous_extra_body()

# Set at resolve time — True if the auxiliary client points to Nous Portal
auxiliary_is_nous: bool = False

# Default auxiliary models per provider
_OPENROUTER_MODEL = "google/gemini-3-flash-preview"
_NOUS_MODEL = "google/gemini-3-flash-preview"
_NOUS_DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
_AUTH_JSON_PATH=*** / "auth.json"

# Codex OAuth endpoint used when a caller explicitly requests
# provider="openai-codex".  There is deliberately no hardcoded default
# model: the set of models OpenAI accepts on this endpoint for
# ChatGPT-account auth is an undocumented, shifting allow-list, and
# pinning one here has drifted silently twice (gpt-5.3-codex → gpt-5.2-codex
# → gpt-5.4 over 6 weeks in early 2026).  Callers must pass the model
# they want explicitly (from config.yaml model.model, auxiliary.<task>.model,
# or the user's active Codex model selection).
_CODEX_AUX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _codex_cloudflare_headers(access_token: str) -> Dict[str, str]:
    """Headers required to avoid Cloudflare 403s on chatgpt.com/backend-api/codex.

    The Cloudflare layer in front of the Codex endpoint whitelists a small set of
    first-party originators (``codex_cli_rs``, ``codex_vscode``, ``codex_sdk_ts``,
    anything starting with ``Codex``). Requests from non-residential IPs (VPS,
    server-hosted agents) that don't advertise an allowed originator are served
    a 403 with ``cf-mitigated: challenge`` regardless of auth correctness.

    We pin ``originator: codex_cli_rs`` to match the upstream codex-rs CLI, set
    ``User-Agent`` to a codex_cli_rs-shaped string (beats SDK fingerprinting),
    and extract ``ChatGPT-Account-ID`` (canonical casing, from codex-rs
    ``auth.rs``) out of the OAuth JWT's ``chatgpt_account_id`` claim.

    Malformed tokens are tolerated — we drop the account-ID header rather than
    raise, so a bad token still surfaces as an auth error (401) instead of a
    crash at client construction.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
        "originator": "codex_cli_rs",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    try:
        import base64
        parts = access_token.split(".")
        if len(parts) < 2:
            return headers
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(acct_id, str) and acct_id:
            headers["ChatGPT-Account-ID"] = acct_id
    except Exception:
        pass
    return headers


def _to_openai_base_url(base_url: str) -> str:
    """Normalize an Anthropic-style base URL to OpenAI-compatible format.

    Some providers (MiniMax, MiniMax-CN) expose an ``/anthropic`` endpoint for
    the Anthropic Messages API and a separate ``/v1`` endpoint for OpenAI chat
    completions.  The auxiliary client uses the OpenAI SDK, so it must hit the
    ``/v1`` surface.  Passing the raw ``inference_base_url`` causes requests to
    land on ``/anthropic/chat/completions`` — a 404.
    """
    url = str(base_url or "").strip().rstrip("/")
    if url.endswith("/anthropic"):
        # ZAI (open.bigmodel.cn) uses /api/anthropic for Anthropic wire
        # but /api/paas/v4 for OpenAI wire — the generic /v1 rewrite is wrong.
        if "open.bigmodel.cn" in url or "bigmodel" in url:
            rewritten = url[: -len("/anthropic")] + "/paas/v4"
            logger.debug("Auxiliary client: rewrote ZAI base URL %s → %s", url, rewritten)
            return rewritten
        

... [OUTPUT TRUNCATED - 196115 chars omitted out of 246115 total] ...

xcept Exception as retry_err:
                    if not (
                        _is_auth_error(retry_err)
                        or _is_payment_error(retry_err)
                        or _is_connection_error(retry_err)
                        or _is_rate_limit_error(retry_err)
                    ):
                        raise
                    first_err = retry_err

        if _is_auth_error(first_err) and client_is_nous:
            refreshed_client, refreshed_model = _refresh_nous_auxiliary_client(
                cache_provider=resolved_provider or "nous",
                model=final_model,
                async_mode=False,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                main_runtime=main_runtime,
                is_vision=(task == "vision"),
            )
            if refreshed_client is not None:
                logger.info("Auxiliary %s: refreshed Nous runtime credentials after 401, retrying",
                            task or "call")
                if refreshed_model and refreshed_model != kwargs.get("model"):
                    kwargs["model"] = refreshed_model
                return _validate_llm_response(
                    refreshed_client.chat.completions.create(**kwargs), task)

        # ── Auth refresh retry ───────────────────────────────────────
        if (_is_auth_error(first_err)
                and resolved_provider not in {"auto", "", None}
                and not client_is_nous):
            if _refresh_provider_credentials(resolved_provider):
                logger.info(
                    "Auxiliary %s: refreshed %s credentials after auth error, retrying",
                    task or "call", resolved_provider,
                )
                return _retry_same_provider_sync(
                    task=task,
                    resolved_provider=resolved_provider,
                    resolved_model=resolved_model,
                    resolved_base_url=resolved_base_url,
                    resolved_api_key=resolved_api_key,
                    resolved_api_mode=resolved_api_mode,
                    main_runtime=main_runtime,
                    final_model=final_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    effective_timeout=effective_timeout,
                    effective_extra_body=effective_extra_body,
                )

        # ── Same-provider credential-pool recovery ─────────────────────
        pool_provider = _recoverable_pool_provider(resolved_provider, client, main_runtime=main_runtime)
        # Capture the exact API key used so mark_exhausted_and_rotate can find
        # the correct pool entry even when another process rotated the pool
        # between this call and recovery (which leaves current()=None and makes
        # _select_unlocked() return the NEXT key by mistake).
        _client_api_key = str(getattr(client, "api_key", "") or "")
        if pool_provider and (_is_auth_error(first_err) or _is_payment_error(first_err) or _is_rate_limit_error(first_err)):
            recovery_err = first_err
            # Skip the extra retry for clear payment/quota errors — the endpoint
            # won't accept another request with the same exhausted key.
            if _is_rate_limit_error(first_err) and not _is_payment_error(first_err):
                try:
                    return _validate_llm_response(
                        client.chat.completions.create(**kwargs), task)
                except Exception as retry_err:
                    if not (_is_auth_error(retry_err) or _is_payment_error(retry_err) or _is_rate_limit_error(retry_err)):
                        raise
                    recovery_err = retry_err
            if _recover_provider_pool(pool_provider, recovery_err, failed_api_key=_client_api_key):
                logger.info(
                    "Auxiliary %s: recovered %s via credential-pool rotation after %s",
                    task or "call", pool_provider, type(recovery_err).__name__,
                )
                try:
                    return _retry_same_provider_sync(
                        task=task,
                        resolved_provider=resolved_provider,
                        resolved_model=resolved_model,
                        resolved_base_url=resolved_base_url,
                        resolved_api_key=resolved_api_key,
                        resolved_api_mode=resolved_api_mode,
                        main_runtime=main_runtime,
                        final_model=final_model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                        effective_timeout=effective_timeout,
                        effective_extra_body=effective_extra_body,
                    )
                except Exception as retry2_err:
                    # The rotated key also hit a quota/auth wall.  Mark it
                    # immediately so concurrent processes don't make a
                    # redundant API call to discover it's exhausted too.
                    # Then fall through to the payment fallback below so
                    # alternative providers can still serve the request.
                    if (_is_payment_error(retry2_err) or _is_auth_error(retry2_err)
                            or _is_rate_limit_error(retry2_err)):
                        _recover_provider_pool(pool_provider, retry2_err)
                        first_err = retry2_err
                    else:
                        raise

        # ── Payment / credit exhaustion fallback ──────────────────────
        # When the resolved provider returns 402 or a credit-related error,
        # try alternative providers instead of giving up.  This handles the
        # common case where a user runs out of OpenRouter credits but has
        # Codex OAuth or another provider available.
        #
        # ── Connection error fallback ────────────────────────────────
        # When a provider endpoint is unreachable (DNS failure, connection
        # refused, timeout), try alternative providers.  This handles stale
        # Codex/OAuth tokens that authenticate but whose endpoint is down,
        # and providers the user never configured that got picked up by
        # the auto-detection chain.
        #
        # ── Rate-limit fallback (#13579) ─────────────────────────────
        # When the provider returns a 429 rate-limit (not billing), fall
        # back to an alternative provider instead of exhausting retries
        # against the same rate-limited endpoint.
        should_fallback = (
            _is_payment_error(first_err)
            or _is_connection_error(first_err)
            or _is_rate_limit_error(first_err)
        )
        # Respect explicit provider choice for transient errors (auth, request
        # validation, etc.) but allow fallback when the provider clearly cannot
        # serve the request due to capacity: payment/quota exhaustion and
        # connection failures are capacity problems, not request constraints.
        # See #26803: daily token quota (429 + "too many tokens per day") must
        # fall back just like a 402 credit error.
        is_auto = resolved_provider in {"auto", "", None}
        # Capacity errors bypass the explicit-provider gate: the provider
        # literally cannot serve this request regardless of user intent.
        is_capacity_error = _is_payment_error(first_err) or _is_connection_error(first_err)
        if should_fallback and (is_auto or is_capacity_error):
            if _is_payment_error(first_err):
                reason = "payment error"
                # Resolve the actual provider label (resolved_provider may be
                # "auto"; the client's base_url tells us which backend got the
                # 402). Mark THAT label unhealthy so subsequent aux calls
                # skip it instead of paying another doomed RTT.
                _mark_provider_unhealthy(
                    _recoverable_pool_provider(resolved_provider, client, main_runtime=main_runtime) or resolved_provider
                )
            elif _is_rate_limit_error(first_err):
                reason = "rate limit"
            else:
                reason = "connection error"
            logger.info("Auxiliary %s: %s on %s (%s), trying fallback",
                        task or "call", reason, resolved_provider, first_err)

            # Fallback order (#26882, #26803):
            #   1. User-configured fallback_chain (per-task) if set
            #   2. Main agent model (last-resort safety net)
            # For auto users (no explicit aux provider), use the full
            # auto-detection chain instead — its Step 1 IS the main agent
            # model, so users on `auto` already get main-model fallback.
            fb_client, fb_model, fb_label = (None, None, "")
            if is_auto:
                fb_client, fb_model, fb_label = _try_payment_fallback(
                    resolved_provider, task, reason=reason)
            else:
                fb_client, fb_model, fb_label = _try_configured_fallback_chain(
                    task, resolved_provider or "auto", reason=reason)
                if fb_client is None:
                    fb_client, fb_model, fb_label = _try_main_agent_model_fallback(
                        resolved_provider, task, reason=reason)

            if fb_client is not None:
                fb_kwargs = _build_call_kwargs(
                    fb_label, fb_model, messages,
                    temperature=temperature, max_tokens=max_tokens,
                    tools=tools, timeout=effective_timeout,
                    extra_body=effective_extra_body,
                    base_url=str(getattr(fb_client, "base_url", "") or ""))
                return _validate_llm_response(
                    fb_client.chat.completions.create(**fb_kwargs), task)
            # All fallback layers exhausted — emit a single user-visible
            # warning so the operator knows aux task is about to fail.
            # (#26882) The error itself is re-raised below.
            logger.warning(
                "Auxiliary %s: %s on %s and all fallbacks exhausted "
                "(fallback_chain + main agent model). Raising original error.",
                task or "call", reason, resolved_provider,
            )
        # Connection/timeout errors leave the cached client poisoned (closed
        # httpx transport, half-read stream, dead async loop).  Drop it from
        # the cache regardless of whether we found a fallback above so the
        # next auxiliary call rebuilds a fresh client instead of reusing the
        # dead one.  See issue #23432.
        if _is_connection_error(first_err):
            try:
                _evict_cached_client_instance(client)
            except Exception:
                logger.debug("Auxiliary: cache eviction after connection error failed",
                             exc_info=True)
        raise


def extract_content_or_reasoning(response) -> str:
    """Extract content from an LLM response, falling back to reasoning fields.

    Mirrors the main agent loop's behavior when a reasoning model (DeepSeek-R1,
    Qwen-QwQ, etc.) returns ``content=None`` with reasoning in structured fields.

    Resolution order:
      1. ``message.content`` — strip inline think/reasoning blocks, check for
         remaining non-whitespace text.
      2. ``message.reasoning`` / ``message.reasoning_content`` — direct
         structured reasoning fields (DeepSeek, Moonshot, NovitaAI, etc.).
      3. ``message.reasoning_details`` — OpenRouter unified array format.

    Returns the best available text, or ``""`` if nothing found.
    """
    import re

    msg = response.choices[0].message
    content = (msg.content or "").strip()

    if content:
        # Strip inline think/reasoning blocks (mirrors _strip_think_blocks)
        cleaned = re.sub(
            r"<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>"
            r".*?"
            r"</(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>",
            "", content, flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        if cleaned:
            return cleaned

    # Content is empty or reasoning-only — try structured reasoning fields
    reasoning_parts: list[str] = []
    for field in ("reasoning", "reasoning_content"):
        val = getattr(msg, field, None)
        if val and isinstance(val, str) and val.strip() and val not in reasoning_parts:
            reasoning_parts.append(val.strip())

    details = getattr(msg, "reasoning_details", None)
    if details and isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                summary = (
                    detail.get("summary")
                    or detail.get("content")
                    or detail.get("text")
                )
                if summary and summary not in reasoning_parts:
                    reasoning_parts.append(summary.strip() if isinstance(summary, str) else str(summary))

    if reasoning_parts:
        return "\n\n".join(reasoning_parts)

    return ""


async def async_call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    main_runtime: Optional[Dict[str, Any]] = None,
    messages: list,
    temperature: float = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = None,
    extra_body: dict = None,
) -> Any:
    """Centralized asynchronous LLM call.

    Same as call_llm() but async. See call_llm() for full documentation.
    """
    resolved_provider, resolved_model, resolved_base_url, resolved_api_key, resolved_api_mode = _resolve_task_provider_model(
        task, provider, model, base_url, api_key)
    effective_extra_body = _get_task_extra_body(task)
    effective_extra_body.update(extra_body or {})

    if task == "vision":
        effective_provider, client, final_model = resolve_vision_provider_client(
            provider=resolved_provider if resolved_provider != "auto" else provider,
            model=resolved_model or model,
            base_url=resolved_base_url or base_url,
            api_key=resolved_api_key or api_key,
            async_mode=True,
        )
        if client is None and resolved_provider != "auto" and not resolved_base_url:
            logger.warning(
                "Vision provider %s unavailable, falling back to auto vision backends",
                resolved_provider,
            )
            effective_provider, client, final_model = resolve_vision_provider_client(
                provider="auto",
                model=resolved_model,
                async_mode=True,
            )
        if client is None:
            raise RuntimeError(
                f"No LLM provider configured for task={task} provider={resolved_provider}. "
                f"Run: hermes setup"
            )
        resolved_provider = effective_provider or resolved_provider
    else:
        client, final_model = _get_cached_client(
            resolved_provider,
            resolved_model,
            async_mode=True,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            api_mode=resolved_api_mode,
        )
        if client is None:
            _explicit = (resolved_provider or "").strip().lower()
            if _explicit and _explicit not in {"auto", "openrouter", "custom"}:
                raise RuntimeError(
                    f"Provider '{_explicit}' is set in config.yaml but no API key "
                    f"was found. Set the {_explicit.upper()}_API_KEY environment "
                    f"variable, or switch to a different provider with `hermes model`."
                )
            if not resolved_base_url:
                logger.info("Auxiliary %s: provider %s unavailable, trying auto-detection chain",
                            task or "call", resolved_provider)
                client, final_model = _get_cached_client("auto", async_mode=True)
        if client is None:
            raise RuntimeError(
                f"No LLM provider configured for task={task} provider={resolved_provider}. "
                f"Run: hermes setup")

    effective_timeout = timeout if timeout is not None else _get_task_timeout(task)

    # Pass the client's actual base_url (not just resolved_base_url) so
    # endpoint-specific temperature overrides can distinguish
    # api.moonshot.ai vs api.kimi.com/coding even on auto-detected routes.
    _client_base = str(getattr(client, "base_url", "") or "")
    kwargs = _build_call_kwargs(
        resolved_provider, final_model, messages,
        temperature=temperature, max_tokens=max_tokens,
        tools=tools, timeout=effective_timeout, extra_body=effective_extra_body,
        base_url=_client_base or resolved_base_url)

    # Convert image blocks for Anthropic-compatible endpoints (e.g. MiniMax)
    if _is_anthropic_compat_endpoint(resolved_provider, _client_base):
        kwargs["messages"] = _convert_openai_images_to_anthropic(kwargs["messages"])

    try:
        return _validate_llm_response(
            await client.chat.completions.create(**kwargs), task)
    except Exception as first_err:
        if "temperature" in kwargs and _is_unsupported_temperature_error(first_err):
            retry_kwargs = dict(kwargs)
            retry_kwargs.pop("temperature", None)
            logger.info(
                "Auxiliary %s (async): provider rejected temperature; retrying once without it",
                task or "call",
            )
            try:
                return _validate_llm_response(
                    await client.chat.completions.create(**retry_kwargs), task)
            except Exception as retry_err:
                retry_err_str = str(retry_err)
                if not (
                    _is_payment_error(retry_err)
                    or _is_connection_error(retry_err)
                    or _is_auth_error(retry_err)
                    or "max_tokens" in retry_err_str
                    or "unsupported_parameter" in retry_err_str
                ):
                    raise
                first_err = retry_err
                kwargs = retry_kwargs

        err_str = str(first_err)
        # ZAI vision models (glm-4v-flash etc.) return error code 1210
        # ("API 调用参数有误") when max_tokens is passed on multimodal
        # calls.  The error message does NOT contain "max_tokens" so the
        # generic retry below never fires.  Detect the ZAI-specific error
        # and strip max_tokens before retrying.
        _is_zai_param_error = (
            "1210" in err_str
            and "bigmodel" in str(getattr(client, "base_url", ""))
        )
        if max_tokens is not None and (
            "max_tokens" in err_str
            or "unsupported_parameter" in err_str
            or _is_unsupported_parameter_error(first_err, "max_tokens")
            or _is_zai_param_error
        ):
            kwargs.pop("max_tokens", None)
            kwargs.pop("max_completion_tokens", None)
            try:
                return _validate_llm_response(
                    await client.chat.completions.create(**kwargs), task)
            except Exception as retry_err:
                # If the max_tokens retry also hits a payment or connection
                # error, fall through to the fallback chain below.
                if not (_is_payment_error(retry_err) or _is_connection_error(retry_err) or _is_rate_limit_error(retry_err)):
                    raise
                first_err = retry_err

        # ── Nous auth refresh parity with main agent ──────────────────
        client_is_nous = (
            resolved_provider == "nous"
            or base_url_host_matches(_client_base, "inference-api.nousresearch.com")
        )
        if (
            _is_payment_error(first_err)
            and client_is_nous
            and _nous_portal_account_has_fresh_paid_access()
        ):
            refreshed_client, refreshed_model = _refresh_nous_auxiliary_client(
                cache_provider=resolved_provider or "nous",
                model=final_model,
                async_mode=True,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                is_vision=(task == "vision"),
            )
            if refreshed_client is not None:
                logger.info(
                    "Auxiliary %s (async): refreshed Nous runtime credentials after paid account check, retrying",
                    task or "call",
                )
                if refreshed_model and refreshed_model != kwargs.get("model"):
                    kwargs["model"] = refreshed_model
                try:
                    return _validate_llm_response(
                        await refreshed_client.chat.completions.create(**kwargs), task)
                except Exception as retry_err:
                    if not (
                        _is_auth_error(retry_err)
                        or _is_payment_error(retry_err)
                        or _is_connection_error(retry_err)
                        or _is_rate_limit_error(retry_err)
                    ):
                        raise
                    first_err = retry_err

        if _is_auth_error(first_err) and client_is_nous:
            refreshed_client, refreshed_model = _refresh_nous_auxiliary_client(
                cache_provider=resolved_provider or "nous",
                model=final_model,
                async_mode=True,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                is_vision=(task == "vision"),
            )
            if refreshed_client is not None:
                logger.info("Auxiliary %s (async): refreshed Nous runtime credentials after 401, retrying",
                            task or "call")
                if refreshed_model and refreshed_model != kwargs.get("model"):
                    kwargs["model"] = refreshed_model
                return _validate_llm_response(
                    await refreshed_client.chat.completions.create(**kwargs), task)

        # ── Auth refresh retry (mirrors sync call_llm) ───────────────
        if (_is_auth_error(first_err)
                and resolved_provider not in {"auto", "", None}
                and not client_is_nous):
            if _refresh_provider_credentials(resolved_provider):
                logger.info(
                    "Auxiliary %s (async): refreshed %s credentials after auth error, retrying",
                    task or "call", resolved_provider,
                )
                return await _retry_same_provider_async(
                    task=task,
                    resolved_provider=resolved_provider,
                    resolved_model=resolved_model,
                    resolved_base_url=resolved_base_url,
                    resolved_api_key=resolved_api_key,
                    resolved_api_mode=resolved_api_mode,
                    final_model=final_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    effective_timeout=effective_timeout,
                    effective_extra_body=effective_extra_body,
                )

        # ── Same-provider credential-pool recovery (mirrors sync) ─────
        pool_provider = _recoverable_pool_provider(resolved_provider, client, main_runtime=main_runtime)
        _client_api_key = str(getattr(client, "api_key", "") or "")
        if pool_provider and (_is_auth_error(first_err) or _is_payment_error(first_err) or _is_rate_limit_error(first_err)):
            recovery_err = first_err
            # Skip the extra retry for clear payment/quota errors — the endpoint
            # won't accept another request with the same exhausted key.
            if _is_rate_limit_error(first_err) and not _is_payment_error(first_err):
                try:
                    return _validate_llm_response(
                        await client.chat.completions.create(**kwargs), task)
                except Exception as retry_err:
                    if not (_is_auth_error(retry_err) or _is_payment_error(retry_err) or _is_rate_limit_error(retry_err)):
                        raise
                    recovery_err = retry_err
            if _recover_provider_pool(pool_provider, recovery_err, failed_api_key=_client_api_key):
                logger.info(
                    "Auxiliary %s (async): recovered %s via credential-pool rotation after %s",
                    task or "call", pool_provider, type(recovery_err).__name__,
                )
                try:
                    return await _retry_same_provider_async(
                        task=task,
                        resolved_provider=resolved_provider,
                        resolved_model=resolved_model,
                        resolved_base_url=resolved_base_url,
                        resolved_api_key=resolved_api_key,
                        resolved_api_mode=resolved_api_mode,
                        final_model=final_model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                        effective_timeout=effective_timeout,
                        effective_extra_body=effective_extra_body,
                    )
                except Exception as retry2_err:
                    if (_is_payment_error(retry2_err) or _is_auth_error(retry2_err)
                            or _is_rate_limit_error(retry2_err)):
                        _recover_provider_pool(pool_provider, retry2_err)
                        first_err = retry2_err
                    else:
                        raise

        # ── Payment / connection / rate-limit fallback (mirrors sync call_llm) ──
        should_fallback = (
            _is_payment_error(first_err)
            or _is_connection_error(first_err)
            or _is_rate_limit_error(first_err)
        )
        # Capacity errors (payment/quota/connection) bypass the explicit-provider
        # gate — the provider cannot serve the request regardless of user intent.
        # See #26803: daily token quota must fall back like a 402 credit error.
        is_auto = resolved_provider in {"auto", "", None}
        is_capacity_error = _is_payment_error(first_err) or _is_connection_error(first_err)
        if should_fallback and (is_auto or is_capacity_error):
            if _is_payment_error(first_err):
                reason = "payment error"
                _mark_provider_unhealthy(
                    _recoverable_pool_provider(resolved_provider, client) or resolved_provider
                )
            elif _is_rate_limit_error(first_err):
                reason = "rate limit"
            else:
                reason = "connection error"
            logger.info("Auxiliary %s (async): %s on %s (%s), trying fallback",
                        task or "call", reason, resolved_provider, first_err)

            # Fallback order (#26882, #26803):
            #   1. User-configured fallback_chain (per-task) if set
            #   2. Main agent model (last-resort safety net)
            # Auto users get the full auto-detection chain instead — its
            # Step 1 IS the main agent model.
            fb_client, fb_model, fb_label = (None, None, "")
            if is_auto:
                fb_client, fb_model, fb_label = _try_payment_fallback(
                    resolved_provider, task, reason=reason)
            else:
                fb_client, fb_model, fb_label = _try_configured_fallback_chain(
                    task, resolved_provider or "auto", reason=reason)
                if fb_client is None:
                    fb_client, fb_model, fb_label = _try_main_agent_model_fallback(
                        resolved_provider, task, reason=reason)

            if fb_client is not None:
                fb_kwargs = _build_call_kwargs(
                    fb_label, fb_model, messages,
                    temperature=temperature, max_tokens=max_tokens,
                    tools=tools, timeout=effective_timeout,
                    extra_body=effective_extra_body,
                    base_url=str(getattr(fb_client, "base_url", "") or ""))
                # Convert sync fallback client to async
                async_fb, async_fb_model = _to_async_client(
                    fb_client, fb_model or "", is_vision=(task == "vision")
                )
                if async_fb_model and async_fb_model != fb_kwargs.get("model"):
                    fb_kwargs["model"] = async_fb_model
                return _validate_llm_response(
                    await async_fb.chat.completions.create(**fb_kwargs), task)
            # All fallback layers exhausted — warn before re-raising. (#26882)
            logger.warning(
                "Auxiliary %s (async): %s on %s and all fallbacks exhausted "
                "(fallback_chain + main agent model). Raising original error.",
                task or "call", reason, resolved_provider,
            )
        # Mirror the sync path: drop poisoned clients on connection/timeout
        # so the next aux call rebuilds.  See issue #23432.
        if _is_connection_error(first_err):
            try:
                _evict_cached_client_instance(client)
            except Exception:
                logger.debug("Auxiliary (async): cache eviction after connection error failed",
                             exc_info=True)
        raise