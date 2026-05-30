"""
Multi-provider authentication system for Hermes Agent.

Supports OAuth device code flows (Nous Portal, future: OpenAI Codex) and
traditional API key providers (OpenRouter, custom endpoints). Auth state
is persisted in ~/.hermes/auth.json with cross-process file locking.

Architecture:
- ProviderConfig registry defines known OAuth providers
- Auth store (auth.json) holds per-provider credential state
- resolve_provider() picks the active provider via priority chain
- resolve_*_runtime_credentials() handles token refresh and runtime keys
- logout_command() is the CLI entry point for clearing auth

Nous authentication paths:
- Invoke JWT (preferred): use a scoped access_token directly for inference.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import shlex
import ssl
import stat
import sys
import base64
import hashlib
import subprocess
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from atlaz_cli.config import get_hermes_home, get_config_path, read_raw_config
from atlaz_constants import OPENROUTER_BASE_URL, secure_parent_dir
from atlaz_cli.config import get_hermes_home, get_config_path, read_raw_config
from atlaz_constants import OPENROUTER_BASE_URL, secure_parent_dir
from agent.credential_persistence import sanitize_borrowed_credential_payload
from utils import atomic_replace, atomic_yaml_write, is_truthy_value

logger = logging.getLogger(__name__)

try:
    import fcntl
except Exception:
    fcntl = None
try:
    import msvcrt
except Exception:
    msvcrt = None

# =============================================================================
# Constants
# =============================================================================

AUTH_STORE_VERSION=***
AUTH_LOCK_TIMEOUT_SECONDS=***

# Nous Portal defaults
DEFAULT_NOUS_PORTAL_URL = "https://portal.nousresearch.com"
DEFAULT_NOUS_INFERENCE_URL = "https://inference-api.nousresearch.com/v1"
DEFAULT_NOUS_CLIENT_ID = "hermes-cli"
NOUS_INFERENCE_INVOKE_SCOPE = "inference:invoke"
DEFAULT_NOUS_SCOPE = NOUS_INFERENCE_INVOKE_SCOPE
NOUS_DEVICE_CODE_SOURCE = "device_code"
NOUS_AUTH_PATH_INVOKE_JWT="***"
ACCESS_TOKEN_REFRESH_SKEW_SECONDS=***       # refresh 2 min before expiry
NOUS_INVOKE_JWT_MIN_TTL_SECONDS = ACCESS_TOKEN_REFRESH_SKEW_SECONDS
DEVICE_AUTH_POLL_INTERVAL_CAP_SECONDS=***     # poll at most every 1s
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_XAI_OAUTH_BASE_URL="https:...i/v1"
MINIMAX_OAUTH_CLIENT_ID="782570...9113"
MINIMAX_OAUTH_SCOPE=*** profile model.completion"
MINIMAX_OAUTH_GRANT_TYPE="urn:ie...code"
MINIMAX_OAUTH_GLOBAL_BASE="https:...x.io"
MINIMAX_OAUTH_CN_BASE="https:....com"
MINIMAX_OAUTH_GLOBAL_INFERENCE="https:...opic"
MINIMAX_OAUTH_CN_INFERENCE="https:...opic"
MINIMAX_OAUTH_REFRESH_SKEW_SECONDS=***
DEFAULT_QWEN_BASE_URL = "https://portal.qwen.ai/v1"
DEFAULT_GITHUB_MODELS_BASE_URL = "https://api.githubcopilot.com"
DEFAULT_COPILOT_ACP_BASE_URL = "acp://copilot"
DEFAULT_OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
STEPFUN_STEP_PLAN_INTL_BASE_URL = "https://api.stepfun.ai/step_plan/v1"
STEPFUN_STEP_PLAN_CN_BASE_URL = "https://api.stepfun.com/step_plan/v1"
CODEX_OAUTH_CLIENT_ID="app_EM...rann"
CODEX_OAUTH_TOKEN_URL="https:...oken"
CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS=***
XAI_OAUTH_ISSUER="***"
XAI_OAUTH_DISCOVERY_URL=f"{XAI...ion"
XAI_OAUTH_CLIENT_ID="b1a004...a828"
XAI_OAUTH_SCOPE=*** profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_HOST="***"
XAI_OAUTH_REDIRECT_PORT=***
XAI_OAUTH_REDIRECT_PATH="***"
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS=***
QWEN_OAUTH_CLIENT_ID="f03043...9e56"
QWEN_OAUTH_TOKEN_URL="https:...oken"
QWEN_ACCESS_TOKEN_REFRESH_SKEW_SECONDS=***
DEFAULT_SPOTIFY_ACCOUNTS_BASE_URL = "https://accounts.spotify.com"
DEFAULT_SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"
DEFAULT_SPOTIFY_REDIRECT_URI = "http://127.0.0.1:43827/spotify/callback"
SPOTIFY_DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/spotify"
SPOTIFY_DASHBOARD_URL = "https://developer.spotify.com/dashboard"
SPOTIFY_ACCESS_TOKEN_REFRESH_SKEW_SECONDS=***

XAI_OAUTH_DOCS_URL="https:...auth"
OAUTH_OVER_SSH_DOCS_URL="https:...-ssh"
DEFAULT_SPOTIFY_SCOPE = " ".join((
    "user-modify-playback-state",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
    "user-library-modify",
))
SERVICE_PROVIDER_NAMES: Dict[str, str] = {
    "spotify": "Spotify",
}

# Google Gemini OAuth (google-gemini-cli provider, Cloud Code Assist backend)
DEFAULT_GEMINI_CLOUDCODE_BASE_URL = "cloudcode-pa://google"
GEMINI_OAUTH_ACCESS_TOKEN_REFRESH_SKEW_SECONDS=***  # refresh 60s before expiry

# LM Studio's default no-auth mode still requires *some* non-empty bearer for
# the API-key code paths (auxiliary_client, runtime resolver) to treat the
# provider as configured. This sentinel is sent only to LM Studio, never to
# any remote service.
LMSTUDIO_NOAUTH_PLACEHOLDER="***"


# =============================================================================
# Provider Registry
# =============================================================================

@dataclass
class ProviderConfig:
    """Describes a known inference provider."""
    id: str
    name: str
    auth_type: str  # "oauth_device_code", "oauth_external", "oauth_minimax", or "api_key"
    portal_base_url: str = ""
    inference_base_url: str = ""
    client_id: str = ""
    scope: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    # For API-key providers: env vars to check (in priority order)
    api_key_env_vars: tuple = ()
    # Optional env var for base URL override
    base_url_env_var: str = ""


PROVIDER_REGISTRY: Dict[str, ProviderConfig] = {
    "nous": ProviderConfig(
        id="nous",
        name="Nous Portal",
        auth_type="oauth_device_code",
        portal_base_url=DEFAULT_NOUS_PORTAL_URL,
        inference_base_url=DEFAULT_NOUS_INFERENCE_URL,
        client_id=DEFAULT_NOUS_CLIENT_ID,
        scope=DEFAULT_NOUS_SCOPE,
    ),
    "openai-codex": ProviderConfig(
        id="openai-codex",
        name="OpenAI Codex",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_CODEX_BASE_URL,
    ),
    "openai-api": ProviderConfig(
        id="openai-api",
        name="OpenAI API",
        auth_type="api_key",
        inference_base_url="https://api.openai.com/v1",
        api_key_env_vars=("OPENAI_API_KEY",),
        base_url_env_var="OPENAI_BASE_URL",
    ),
    "xai-oauth": ProviderConfig(
        id="xai-oauth",
        name="xAI Grok OAuth (SuperGrok / Premium+)",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_XAI_OAUTH_BASE_URL,
    ),
    "qwen-oauth": ProviderConfig(
        id="qwen-oauth",
        name="Qwen OAuth",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_QWEN_BASE_URL,
    ),
    "google-gemini-cli": ProviderConfig(
        id="google-gemini-cli",
        name="Google Gemini (OAuth)",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_GEMINI_CLOUDCODE_BASE_URL,
    ),
    "lmstudio": ProviderConfig(
        id="lmstudio",
        name="LM Studio",
        auth_type="api_key",
        inference_base_url="http://127.0.0.1:1234/v1",
        api_key_env_vars=("LM_API_KEY",),
        base_url_env_var="LM_BASE_URL",
    ),
    "copilot": ProviderConfig(
        id="copilot",
        name="GitHub Copilot",
        auth_type="api_key",
        inference_base_url=DEFAULT_GITHUB_MODELS_BASE_URL,
        api_key_env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        base_url_env_var="COPILOT_API_BASE_URL",
    ),
    "copilot-acp": ProviderConfig(
        id="copilot-acp",
        name="GitHub Copilot ACP",
        auth_type="external_process",
        inference_base_url=DEFAULT_COPILOT_ACP_BASE_URL,
        base_url_env_var="COPILOT_ACP_BASE_URL",
    ),
    "gemini": ProviderConfig(
        id="gemini",
        name="Google AI Studio",
        auth_type="api_key",
        inference_base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        base_url_env_var="GEMINI_BASE_URL",
    ),
    "zai": ProviderConfig(
        id="zai",
        name="Z.AI / GLM",
        auth_type="api_key",
        inference_base_url="https://api.z.ai/api/paas/v4",
        api_key_env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url_env_var="GLM_BASE_URL",
    ),
    "kimi-coding": ProviderConfig(
        id="kimi-coding",
        name="Kimi / Moonshot",
        auth_type="api_key",
        # Legacy platform.moonshot.ai keys use this endpoint (OpenAI-compat).
        # sk-kimi- (Kimi Code) keys are auto-redirected to api.kimi.com/coding
        # by _resolve_kimi_base_url() below.
        inference_base_url="https://api.moonshot.ai/v1",
        api_key_env_vars=("KIMI_API_KEY", "KIMI_CODING_API_KEY"),
        base_url_env_var="KIMI_BASE_URL",
    ),
    "kimi-coding-cn": ProviderConfig(
        id="kimi-coding-cn",
        name="Kimi / Moonshot (China)",
        auth_type="api_key",
        inference_base_url="https://api.moonshot.cn/v1",
        api_key_env_vars=("KIMI_CN_API_KEY",),
    ),
    "stepfun": ProviderConfig(
        id="stepfun",
        name="StepFun Step Plan",
        auth_type="api_key",
        inference_base_url=STEPFUN_STEP_PLAN_INTL_BASE_URL,
        api_key_env_vars=("STEPFUN_API_KEY",),
        base_url_env_var="STEPFUN_BASE_URL",
    ),
    "arcee": ProviderConfig(
        id="arcee",
        name="Arcee AI",
        auth_type="api_key",
        inference_base_url="https://api.arcee.ai/api/v1",
        api_key_env_vars=("ARCEEAI_API_KEY",),
        base_url_env_var="ARCEE_BASE_URL",
    ),
    "gmi": ProviderConfig(
        id="gmi",
        name="GMI Cloud",
        auth_type="api_key",
        inference_base_url="https://api.gmi-serving.com/v1",
        api_key_env_vars=("GMI_API_KEY",),
        base_url_env_var="GMI_BASE_URL",
    ),
    "minimax": ProviderConfig(
        id="minimax",
        name="MiniMax",
        auth_type="api_key",
        inference_base_url="https://api.minimax.io/anthropic",
        api_key_env_vars=("MINIMAX_API_KEY",),
        base_url_env_var="MINIMAX_BASE_URL",
    ),
    "minimax-oauth": ProviderConfig(
        id="minimax-oauth",
        name="MiniMax (OAuth \u00b7 minimax.io)",
        auth_type="oauth_minimax",
        portal_base_url=MINIMAX_OAUTH_GLOBAL_BASE,
        inference_base_url=MINIMAX_OAUTH_GLOBAL_INFERENCE,
        client_id=MINIMAX_OAUTH_CLIENT_ID,
        scope=MINIMAX_OAUTH_SCOPE,
        extra={"region": "global", "cn_portal_base_url": MINIMAX_OAUTH_CN_BASE,
               "cn_inference_base_url": MINIMAX_OAUTH_CN_INFERENCE},
    ),
    "anthropic": ProviderConfig(
        id="anthropic",
        name="Anthropic",
        auth_type="api_key",
        inference_base_url="https://api.anthropic.com",
        api_key_env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        base_url_env_var="ANTHROPIC_BASE_URL",
    ),
    "alibaba": ProviderConfig(
        id="alibaba",
        name="Qwen Cloud",
        auth_type="api_key",
        inference_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_env_vars=("DASHSCOPE_API_KEY",),
        base_url_env_var="DASHSCOPE_BASE_URL",
    ),
    "alibaba-coding-plan": ProviderConfig(
        id="alibaba-coding-plan",
        name="Alibaba Cloud (Coding Plan)",
        auth_type="api_key",
        inference_base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key_env_vars=("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_env_var="ALIBABA_CODING_PLAN_BASE_URL",
    ),
    "minimax-cn": ProviderConfig(
        id="minimax-cn",
        name="MiniMax (China)",
        auth_type="api_key",
        inference_base_url="https://api.minimaxi.com/anthropic",
        api_key_env_vars=("MINIMAX_CN_API_KEY",),
        base_url_env_var="MINIMAX_CN_BASE_URL",
    ),
    "deepseek": ProviderConfig(
        id="deepseek",
        name="DeepSeek",
        auth_type="api_key",
        inference_base_url="https://api.deepseek.com/v1",
        api_key_env_vars=("DEEPSEEK_API_KEY",),
        base_url_env_var="DEEPSEEK_BASE_URL",
    ),
    "xai": ProviderConfig(
        id="xai",
        name="xAI",
        auth_type="api_key",
        inference_base_url="https://api.x.ai/v1",
        api_key_env_vars=("XAI_API_KEY",),
        base_url_env_var="XAI_BASE_URL",
    ),
    "nvidia": ProviderConfig(
        id="nvidia",
        name="NVIDIA NIM",
        auth_type="api_key",
        inference_base_url="https://integrate.api.nvidia.com/v1",
        api_key_env_vars=("NVIDIA_API_KEY",),
        base_url_env_var="NVIDIA_BASE_URL",
    ),
    "opencode-zen": ProviderConfig(
        id="opencode-zen",
        name="OpenCode Zen",
        auth_type="api_key",
        inference_base_url="https://opencode.ai/zen/v1",
        api_key_env_vars=("OPENCODE_ZEN_API_KEY",),
        base_url_env_var="OPENCODE_ZEN_BASE_URL",
    ),
    "opencode-go": ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        auth_type="api_key",
        # OpenCode Go mixes API surfaces by model:
        # - GLM / Kimi use OpenAI-compatible chat completions under /v1
        # - MiniMax models use Anthropic Messages under /v1/messages
        # - Qwen 3.7 uses Anthropic Messages under /v1/messages
        # Keep the provider base at /v1 and select api_mode per-model.
        inference_base_url="https://opencode.ai/zen/go/v1",
        api_key_env_vars=("OPENCODE_GO_API_KEY",),
        base_url_env_var="OPENCODE_GO_BASE_URL",
    ),
    "kilocode": ProviderConfig(
        id="kilocode",
        name="Kilo Code",
        auth_type="api_key",
        inference_base_url="https://api.kilo.ai/api/gateway",
        api_key_env_vars=("KILOCODE_API_KEY",),
        base_url_env_var="KILOCODE_BASE_URL",
    ),
    "huggingface": ProviderConfig(
        id="huggingface",
        name="Hugging Face",
        auth_type="api_key",
        inference_base_url="https://router.huggingface.co/v1",
        api_key_env_vars=("HF_TOKEN",),
        base_url_env_var="HF_BASE_URL",
    ),
    "xiaomi": ProviderConfig(
        id="xiaomi",
        name="Xiaomi MiMo",
        auth_type="api_key",
        inference_base_url="https://api.xiaomimimo.com/v1",
        api_key_env_vars=("XIAOMI_API_KEY",),
        base_url_env_var="XIAOMI_BASE_URL",
    ),
    "tencent-tokenhub": ProviderConfig(
        id="tencent-tokenhub",
        name="Tencent TokenHub",
        auth_type="api_key",
        inference_base_url="https://tokenhub.tencentmaas.com/v1",
        api_key_env_vars=("TOKENHUB_API_KEY",),
        base_url_env_var="TOKENHUB_BASE_URL",
    ),
    "ollama-cloud": ProviderConfig(
        id="ollama-cloud",
        name="Ollama Cloud",
        auth_type="api_key",
        inference_base_url=DEFAULT_OLLAMA_CLOUD_BASE_URL,
        api_key_env_vars=("OLLAMA_API_KEY",),
        base_url_env_var="OLLAMA_BASE_URL",
    ),
    "bedrock": ProviderConfig(
        id="bedrock",
        name="AWS Bedrock",
        auth_type="aws_sdk",
        inference_base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_key_env_vars=(),
        base_url_env_var="BEDROCK_BASE_URL",
    ),
    "azure-foundry": ProviderConfig(
        id="azure-foundry",
        name="Azure Foundry",
        auth_type="api_key",
        inference_base_url="",  # User-provided endpoint
        api_key_env_vars=("AZURE_FOUNDRY_API_KEY",),
        base_url_env_var="AZURE_FOUNDRY_BASE_URL",
    ),
}

# Auto-extend PROVIDER_REGISTRY with any api-key provider registered in
# providers/ that is not already declared above.  New providers only need a
# plugins/model-providers/<name>/ plugin — no edits to this file required.
try:
    from providers import list_providers as _list_providers_for_registry
    for _pp in _list_providers_for_registry():
        if _pp.name in PROVIDER_REGISTRY:
            continue
        if _pp.auth_type != "api_key" or not _pp.env_vars:
            continue
        # Skip providers that need custom token resolution or are special-cased
        # in resolve_provider() (copilot/kimi/zai have bespoke token refresh;
        # openrouter/custom are aggregator/user-supplied and handled outside
        # the registry — adding them here breaks runtime_provider resolution
        # that relies on `openrouter not in PROVIDER_REGISTRY`).
        if _pp.name in {"copilot", "kimi-coding", "kimi-coding-cn", "zai", "openrouter", "custom"}:
            continue
        _api_key_vars = tuple(v for v in _pp.env_vars if not v.endswith("_BASE_URL") and not v.endswith("_URL"))
        _base_url_var = next((v for v in _pp.env_vars if v.endswith("_BASE_URL") or v.endswith("_URL")), None)
        PROVIDER_REGISTRY[_pp.name] = ProviderConfig(
            id=_pp.name,
            name=_pp.display_name or _pp.name,
            auth_type="api_key",
            inference_base_url=_pp.base_url,
            api_key_env_vars=_api_key_vars or _pp.env_vars,
            base_url_env_var=_base_url_var or "",
        )
        # Also register aliases so resolve_provider() resolves them
        for _alias in _pp.aliases:
            if _alias not in PROVIDER_REGISTRY:
                PROVIDER_REGISTRY[_alias] = PROVIDER_REGISTRY[_pp.name]
except Exception:
    pass


# =============================================================================
# Anthropic Key Helper
# =============================================================================

def get_anthropic_key() -> str:
    """Return the first usable Anthropic credential, or ``""``.

    Checks both the ``.env`` file (via ``get_env_value``) and the process
    environment (``os.getenv``).  The fallback order mirrors the
    ``PROVIDER_REGISTRY["anthropic"].api_key_env_vars`` tuple:

        ANTHROPIC_API_KEY -> ANTHROPIC_TOKEN -> CLAUDE_CODE_OAUTH_TOKEN
    """
    from atlaz_cli.config import get_env_value

    for var in PROVIDER_REGISTRY["anthropic"].api_key_env_vars:
        value = get_env_value(var) or os.getenv(var, "")
        if value:
            return value
    return ""


# =============================================================================
# Kimi Code Endpoint Detection
# =============================================================================

# Kimi Code (kimi.com/code) issues keys prefixed "sk-kimi-" that only work
# on api.kimi.com/coding.  Legacy keys from platform.moonshot.ai work on
# api.moonshot.ai/v1 (the old default).  Auto-detect when user hasn't set
# KIMI_BASE_URL explicitly.
#
# Note: the base URL intentionally has NO /v1 suffix.  The /coding endpoint
# speaks the Anthropic Messages protocol, and the anthropic SDK appends
# "/v1/messages" internally — so "/coding" + SDK suffix → "/coding/v1/messages

... [OUTPUT TRUNCATED - 252742 chars omitted out of 302742 total] ...

code_verifier: str, expired_in: int, interval_ms: Optional[int],
) -> Dict[str, Any]:
    # OpenClaw treats expired_in as a unix-ms timestamp (Date.now() < expireTimeMs).
    # Defensive parsing: if it's small enough to be a duration, treat as seconds.
    import time as _time
    now_ms = int(_time.time() * 1000)
    raw = int(expired_in)
    if _minimax_expired_in_looks_like_unix_ms(raw, now_ms=now_ms):
        deadline = raw / 1000.0
    else:
        deadline = _time.time() + max(1, raw)
    interval = max(2.0, (interval_ms or 2000) / 1000.0)

    while _time.time() < deadline:
        response = client.post(
            f"{portal_base_url}/oauth/token",
            data={
                "grant_type": MINIMAX_OAUTH_GRANT_TYPE,
                "client_id": client_id,
                "user_code": user_code,
                "code_verifier": code_verifier,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            payload = response.json() if response.text else {}
        except Exception:
            payload = {}

        if response.status_code != 200:
            msg = (payload.get("base_resp", {}) or {}).get("status_msg") or response.text
            raise AuthError(
                f"MiniMax OAuth error: {msg or 'unknown'}",
                provider="minimax-oauth", code="token_exchange_failed",
            )

        status = payload.get("status")
        if status == "error":
            raise AuthError(
                "MiniMax OAuth reported an error. Please try again later.",
                provider="minimax-oauth", code="authorization_denied",
            )
        if status == "success":
            if not all(payload.get(k) for k in ("access_token", "refresh_token", "expired_in")):
                raise AuthError(
                    "MiniMax OAuth success payload missing required token fields.",
                    provider="minimax-oauth", code="token_incomplete",
                )
            return payload
        # "pending" or any other status -> keep polling
        _time.sleep(interval)

    raise AuthError(
        "MiniMax OAuth timed out before authorization completed.",
        provider="minimax-oauth", code="timeout",
    )


def _minimax_save_auth_state(auth_state: Dict[str, Any]) -> None:
    """Persist MiniMax OAuth state to Hermes auth store (~/.hermes/auth.json)."""
    with _auth_store_lock():
        auth_store = _load_auth_store()
        _save_provider_state(auth_store, "minimax-oauth", auth_state)
        _save_auth_store(auth_store)


def _minimax_oauth_login(
    *, region: str = "global", open_browser: bool = True,
    timeout_seconds: float = 15.0,
) -> Dict[str, Any]:
    """Run MiniMax OAuth flow, persist tokens, return auth state dict."""
    pconfig = PROVIDER_REGISTRY["minimax-oauth"]
    if region == "cn":
        portal_base_url = pconfig.extra["cn_portal_base_url"]
        inference_base_url = pconfig.extra["cn_inference_base_url"]
    else:
        portal_base_url = pconfig.portal_base_url
        inference_base_url = pconfig.inference_base_url

    verifier, challenge, state = _minimax_pkce_pair()

    if _is_remote_session():
        open_browser = False

    print(f"Starting Hermes login via MiniMax ({region}) OAuth...")
    print(f"Portal: {portal_base_url}")

    with httpx.Client(timeout=httpx.Timeout(timeout_seconds),
                      headers={"Accept": "application/json"},
                      follow_redirects=True) as client:
        code_data = _minimax_request_user_code(
            client, portal_base_url=portal_base_url,
            client_id=pconfig.client_id,
            code_challenge=challenge, state=state,
        )
        verification_url = str(code_data["verification_uri"])
        user_code = str(code_data["user_code"])

        print()
        print("To continue:")
        print(f"  1. Open: {verification_url}")
        print(f"  2. If prompted, enter code: {user_code}")
        if open_browser and _can_open_graphical_browser():
            if webbrowser.open(verification_url):
                print("  (Opened browser for verification)")
            else:
                print("  Could not open browser automatically -- use the URL above.")

        interval_raw = code_data.get("interval")
        interval_ms = int(interval_raw) if interval_raw is not None else None
        print("Waiting for approval...")

        token_data = _minimax_poll_token(
            client, portal_base_url=portal_base_url,
            client_id=pconfig.client_id,
            user_code=user_code, code_verifier=verifier,
            expired_in=int(code_data["expired_in"]),
            interval_ms=interval_ms,
        )

    now = datetime.now(timezone.utc)
    expires_at_unix = _minimax_resolve_token_expiry_unix(
        int(token_data["expired_in"]), now=now,
    )
    expires_in_s = max(0, int(expires_at_unix - now.timestamp()))

    auth_state = {
        "provider": "minimax-oauth",
        "region": region,
        "portal_base_url": portal_base_url,
        "inference_base_url": inference_base_url,
        "client_id": pconfig.client_id,
        "scope": MINIMAX_OAUTH_SCOPE,
        "token_type": token_data.get("token_type", "Bearer"),
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "resource_url": token_data.get("resource_url"),
        "obtained_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_unix, tz=timezone.utc).isoformat(),
        "expires_in": expires_in_s,
    }

    _minimax_save_auth_state(auth_state)
    print("\u2713 MiniMax OAuth login successful.")
    if msg := token_data.get("notification_message"):
        print(f"Note from MiniMax: {msg}")
    return auth_state


def _refresh_minimax_oauth_state(
    state: Dict[str, Any], *, timeout_seconds: float = 15.0,
    force: bool = False,
) -> Dict[str, Any]:
    """Refresh MiniMax OAuth access token if close to expiry (or forced)."""
    if not state.get("refresh_token"):
        raise AuthError(
            "MiniMax OAuth state has no refresh_token; please re-login.",
            provider="minimax-oauth", code="no_refresh_token", relogin_required=True,
        )
    try:
        expires_at = datetime.fromisoformat(state.get("expires_at", "")).timestamp()
    except Exception:
        expires_at = 0.0
    now = time.time()
    if not force and (expires_at - now) > MINIMAX_OAUTH_REFRESH_SKEW_SECONDS:
        return state

    portal_base_url = state["portal_base_url"]
    with httpx.Client(timeout=httpx.Timeout(timeout_seconds),
                      follow_redirects=True) as client:
        response = client.post(
            f"{portal_base_url}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": state["client_id"],
                "refresh_token": state["refresh_token"],
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    if response.status_code != 200:
        body = response.text.lower()
        relogin = any(m in body for m in
                      ("invalid_grant", "refresh_token_reused", "invalid_refresh_token"))
        raise AuthError(
            f"MiniMax OAuth refresh failed: {response.text or response.reason_phrase}",
            provider="minimax-oauth", code="refresh_failed",
            relogin_required=relogin,
        )
    payload = response.json()
    if payload.get("status") != "success":
        raise AuthError(
            "MiniMax OAuth refresh did not return success.",
            provider="minimax-oauth", code="refresh_failed",
            relogin_required=True,
        )
    now_dt = datetime.now(timezone.utc)
    expires_at_unix = _minimax_resolve_token_expiry_unix(
        int(payload["expired_in"]), now=now_dt,
    )
    expires_in_s = max(0, int(expires_at_unix - now_dt.timestamp()))
    new_state = dict(state)
    new_state.update({
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", state["refresh_token"]),
        "obtained_at": now_dt.isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_unix, tz=timezone.utc).isoformat(),
        "expires_in": expires_in_s,
    })
    _minimax_save_auth_state(new_state)
    return new_state


def _minimax_oauth_quarantine_on_terminal_refresh(state: Dict[str, Any], exc: AuthError) -> None:
    """Wipe dead tokens from auth.json after a terminal refresh failure.

    Shared by both the eager-resolve path and the lazy per-request token
    provider. Mirrors the Nous / xAI-OAuth / Codex-OAuth quarantine pattern
    so subsequent calls fail fast without a network retry.
    """
    if not (exc.relogin_required and state.get("refresh_token")):
        return
    for _k in ("access_token", "refresh_token", "expires_at", "expires_in", "obtained_at"):
        state.pop(_k, None)
    state["last_auth_error"] = {
        "provider": "minimax-oauth",
        "code": exc.code or "refresh_failed",
        "message": str(exc),
        "reason": "runtime_refresh_failure",
        "relogin_required": True,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _minimax_save_auth_state(state)
    except Exception as _save_exc:
        logger.debug("MiniMax OAuth: failed to persist quarantined state: %s", _save_exc)


def build_minimax_oauth_token_provider() -> Callable[[], str]:
    """Return a zero-arg callable that yields a fresh MiniMax access token.

    The Anthropic SDK caches ``api_key`` as a static string at construction
    time, so a session that resolves credentials once at startup will keep
    sending the same bearer until MiniMax's server returns 401 — typically
    ~15 minutes in, because MiniMax issues short-lived access tokens.

    Returning a *callable* instead of a string lets us hook into the
    existing Entra-ID bearer infrastructure in
    :mod:`agent.anthropic_adapter`: ``build_anthropic_client`` detects a
    callable and routes through ``_build_anthropic_client_with_bearer_hook``,
    which mints a fresh ``Authorization`` header on every outbound request.
    Each invocation re-reads the persisted state from ``auth.json`` and
    calls :func:`_refresh_minimax_oauth_state` — that helper is a no-op
    when the token still has more than ``MINIMAX_OAUTH_REFRESH_SKEW_SECONDS``
    of life left, so the steady-state cost is one file read + one
    timestamp compare per request.

    Reading state fresh each time also means a refresh persisted by one
    process (CLI, gateway, cron) is immediately visible to every other
    process sharing the same ``auth.json``.
    """
    def _provide() -> str:
        state = get_provider_auth_state("minimax-oauth")
        if not state or not state.get("access_token"):
            raise AuthError(
                "Not logged into MiniMax OAuth. Run `hermes model` and select "
                "MiniMax (OAuth).",
                provider="minimax-oauth", code="not_logged_in", relogin_required=True,
            )
        try:
            state = _refresh_minimax_oauth_state(state)
        except AuthError as exc:
            _minimax_oauth_quarantine_on_terminal_refresh(state, exc)
            raise
        token = state.get("access_token")
        if not token:
            raise AuthError(
                "MiniMax OAuth state has no access_token after refresh.",
                provider="minimax-oauth", code="no_access_token", relogin_required=True,
            )
        return token

    return _provide


def resolve_minimax_oauth_runtime_credentials(
    *, min_token_ttl_seconds: int = MINIMAX_OAUTH_REFRESH_SKEW_SECONDS,
    as_token_provider: bool = False,
) -> Dict[str, Any]:
    """Return {provider, api_key, base_url, source} for minimax-oauth.

    When ``as_token_provider`` is True, ``api_key`` is a zero-arg callable
    that mints a fresh access token per call (proactively refreshing if
    the cached token is within ``MINIMAX_OAUTH_REFRESH_SKEW_SECONDS`` of
    expiry). This is what the runtime provider path uses so that long
    sessions survive MiniMax's short access-token lifetime — see
    :func:`build_minimax_oauth_token_provider` for the rationale.

    The default (string ``api_key``) preserves the historical contract for
    diagnostic call sites like ``hermes status`` that just want to know
    whether a valid token exists right now.
    """
    state = get_provider_auth_state("minimax-oauth")
    if not state or not state.get("access_token"):
        raise AuthError(
            "Not logged into MiniMax OAuth. Run `hermes model` and select "
            "MiniMax (OAuth).",
            provider="minimax-oauth", code="not_logged_in", relogin_required=True,
        )
    try:
        state = _refresh_minimax_oauth_state(state)
    except AuthError as exc:
        _minimax_oauth_quarantine_on_terminal_refresh(state, exc)
        raise
    if as_token_provider:
        api_key: Any = build_minimax_oauth_token_provider()
    else:
        api_key = state["access_token"]
    return {
        "provider": "minimax-oauth",
        "api_key": api_key,
        "base_url": state["inference_base_url"].rstrip("/"),
        "source": "oauth",
    }


def get_minimax_oauth_auth_status() -> Dict[str, Any]:
    """Return auth status dict for MiniMax OAuth provider."""
    state = get_provider_auth_state("minimax-oauth")
    if not state or not state.get("access_token"):
        return {"logged_in": False, "provider": "minimax-oauth"}
    try:
        expires_at = datetime.fromisoformat(state.get("expires_at", "")).timestamp()
        token_valid = (expires_at - time.time()) > 0
    except Exception:
        token_valid = bool(state.get("access_token"))
    return {
        "logged_in": token_valid,
        "provider": "minimax-oauth",
        "region": state.get("region", "global"),
        "expires_at": state.get("expires_at"),
    }


def _login_minimax_oauth(args, pconfig: ProviderConfig) -> None:
    """CLI entry for MiniMax OAuth login."""
    region = getattr(args, "region", None) or "global"
    open_browser = not getattr(args, "no_browser", False)
    timeout = getattr(args, "timeout", None) or 15.0
    try:
        _minimax_oauth_login(
            region=region, open_browser=open_browser, timeout_seconds=timeout,
        )
    except AuthError as exc:
        print(format_auth_error(exc))
        raise SystemExit(1)


def _nous_device_code_login(
    *,
    portal_base_url: Optional[str] = None,
    inference_base_url: Optional[str] = None,
    client_id: Optional[str] = None,
    scope: Optional[str] = None,
    open_browser: bool = True,
    timeout_seconds: float = 15.0,
    insecure: bool = False,
    ca_bundle: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the Nous device-code flow and return full OAuth state without persisting."""
    pconfig = PROVIDER_REGISTRY["nous"]
    portal_base_url = (
        portal_base_url
        or os.getenv("HERMES_PORTAL_BASE_URL")
        or os.getenv("NOUS_PORTAL_BASE_URL")
        or pconfig.portal_base_url
    ).rstrip("/")
    requested_inference_url = (
        inference_base_url
        or os.getenv("NOUS_INFERENCE_BASE_URL")
        or pconfig.inference_base_url
    ).rstrip("/")
    client_id = client_id or pconfig.client_id
    scope = scope or pconfig.scope
    timeout = httpx.Timeout(timeout_seconds)
    verify: bool | str = False if insecure else (ca_bundle if ca_bundle else True)

    if _is_remote_session():
        open_browser = False

    print(f"Starting Hermes login via {pconfig.name}...")
    print(f"Portal: {portal_base_url}")
    if insecure:
        print("TLS verification: disabled (--insecure)")
    elif ca_bundle:
        print(f"TLS verification: custom CA bundle ({ca_bundle})")

    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}, verify=verify) as client:
        device_data = _request_device_code(
            client=client,
            portal_base_url=portal_base_url,
            client_id=client_id,
            scope=scope,
        )

        verification_url = str(device_data["verification_uri_complete"])
        user_code = str(device_data["user_code"])
        expires_in = int(device_data["expires_in"])
        interval = int(device_data["interval"])

        print()
        print("To continue:")
        print(f"  1. Open: {verification_url}")
        print(f"  2. If prompted, enter code: {user_code}")

        if open_browser:
            opened = webbrowser.open(verification_url)
            if opened:
                print("  (Opened browser for verification)")
            else:
                print("  Could not open browser automatically — use the URL above.")

        effective_interval = max(1, min(interval, DEVICE_AUTH_POLL_INTERVAL_CAP_SECONDS))
        print(f"Waiting for approval (polling every {effective_interval}s)...")

        token_data = _poll_for_token(
            client=client,
            portal_base_url=portal_base_url,
            client_id=client_id,
            device_code=str(device_data["device_code"]),
            expires_in=expires_in,
            poll_interval=interval,
        )

    now = datetime.now(timezone.utc)
    token_expires_in = _coerce_ttl_seconds(token_data.get("expires_in", 0))
    expires_at = now.timestamp() + token_expires_in
    resolved_inference_url = (
        _optional_base_url(token_data.get("inference_base_url"))
        or requested_inference_url
    )
    if resolved_inference_url != requested_inference_url:
        print(f"Using portal-provided inference URL: {resolved_inference_url}")

    auth_state = {
        "portal_base_url": portal_base_url,
        "inference_base_url": resolved_inference_url,
        "client_id": client_id,
        "scope": token_data.get("scope") or scope,
        "token_type": token_data.get("token_type", "Bearer"),
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "obtained_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        "expires_in": token_expires_in,
        "tls": {
            "insecure": verify is False,
            "ca_bundle": verify if isinstance(verify, str) else None,
        },
        "agent_key": None,
        "agent_key_id": None,
        "agent_key_expires_at": None,
        "agent_key_expires_in": None,
        "agent_key_reused": None,
        "agent_key_obtained_at": None,
    }
    try:
        return refresh_nous_oauth_from_state(
            auth_state,
            timeout_seconds=timeout_seconds,
            force_refresh=False,
        )
    except AuthError as exc:
        if exc.code == "subscription_required":
            portal_url = auth_state.get(
                "portal_base_url", DEFAULT_NOUS_PORTAL_URL
            ).rstrip("/")
            message = format_auth_error(exc)
            print()
            print(message)
            print(f"  Subscribe here: {portal_url}/billing")
            print()
            print("After subscribing, run `hermes model` again to finish setup.")
            raise SystemExit(1)
        raise


def _login_nous(args, pconfig: ProviderConfig) -> None:
    """Nous Portal device authorization flow."""
    timeout_seconds = getattr(args, "timeout", None) or 15.0
    insecure = bool(getattr(args, "insecure", False))
    ca_bundle = (
        getattr(args, "ca_bundle", None)
        or os.getenv("HERMES_CA_BUNDLE")
        or os.getenv("SSL_CERT_FILE")
    )

    try:
        auth_state = None

        # Codex-style auto-import: before launching a fresh device-code
        # flow, check the shared store for an existing Nous credential
        # from any other profile. If present, offer to rehydrate it.
        shared = _read_shared_nous_state()
        if shared:
            try:
                shared_path = _nous_shared_store_path()
            except RuntimeError:
                shared_path = None
            print()
            if shared_path:
                print(f"Found existing Nous OAuth credentials at {shared_path}")
            else:
                print("Found existing shared Nous OAuth credentials")
            try:
                do_import = input("Import these credentials? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                do_import = "y"
            if do_import in {"", "y", "yes"}:
                print("Rehydrating Nous session from shared credentials...")
                auth_state = _try_import_shared_nous_state(
                    timeout_seconds=timeout_seconds,
                )
                if auth_state is None:
                    print("Could not refresh shared credentials — falling back to device-code login.")

        if auth_state is None:
            auth_state = _nous_device_code_login(
                portal_base_url=getattr(args, "portal_url", None),
                inference_base_url=getattr(args, "inference_url", None),
                client_id=getattr(args, "client_id", None) or pconfig.client_id,
                scope=getattr(args, "scope", None),
                open_browser=not getattr(args, "no_browser", False),
                timeout_seconds=timeout_seconds,
                insecure=insecure,
                ca_bundle=ca_bundle,
            )

        inference_base_url = auth_state["inference_base_url"]

        # Snapshot the prior active_provider BEFORE _save_provider_state
        # overwrites it to "nous".  If the user picks "Skip (keep current)"
        # during model selection below, we restore this so the user's previous
        # provider (e.g. openrouter) is preserved.
        with _auth_store_lock():
            _prior_store = _load_auth_store()
            prior_active_provider = _prior_store.get("active_provider")

        with _auth_store_lock():
            auth_store = _load_auth_store()
            _save_provider_state(auth_store, "nous", auth_state)
            saved_to = _save_auth_store(auth_store)

        # Mirror to the shared store so other profiles can one-tap import
        # these credentials. Best-effort: any I/O failure is logged and
        # swallowed inside the helper.
        _write_shared_nous_state(auth_state)
        _sync_nous_pool_from_auth_store()

        print()
        print("Login successful!")
        print(f"  Auth state: {saved_to}")

        # Resolve model BEFORE writing provider to config.yaml so we never
        # leave the config in a half-updated state (provider=nous but model
        # still set to the previous provider's model, e.g. opus from
        # OpenRouter).  The auth.json active_provider was already set above.
        selected_model = None
        try:
            runtime_key = auth_state.get("agent_key") or auth_state.get("access_token")
            if not isinstance(runtime_key, str) or not runtime_key:
                raise AuthError(
                    "No runtime API key available to fetch models",
                    provider="nous",
                    code="invalid_token",
                )

            from atlaz_cli.models import (
                get_curated_nous_model_ids, get_pricing_for_provider,
                check_nous_free_tier, partition_nous_models_by_tier,
                union_with_portal_free_recommendations,
                union_with_portal_paid_recommendations,
            )
            model_ids = get_curated_nous_model_ids()

            print()
            unavailable_models: list = []
            unavailable_message = ""
            if model_ids:
                pricing = get_pricing_for_provider("nous")
                # Force fresh account data for model selection so recent credit
                # purchases are reflected immediately.
                free_tier = check_nous_free_tier(force_fresh=True)
                _portal_for_recs = auth_state.get("portal_base_url", "")
                if free_tier:
                    try:
                        from atlaz_cli.nous_account import (
                            format_nous_portal_entitlement_message,
                            get_nous_portal_account_info,
                        )

                        _account_info = get_nous_portal_account_info(force_fresh=True)
                        unavailable_message = (
                            format_nous_portal_entitlement_message(
                                _account_info,
                                capability="paid Nous models",
                            )
                            or ""
                        )
                    except Exception:
                        unavailable_message = ""
                    # The Portal's freeRecommendedModels endpoint is the
                    # source of truth for what's free *right now*. Augment
                    # the curated list with anything new the Portal flags
                    # as free so users on older Hermes builds still see
                    # newly-launched free models without a CLI release.
                    model_ids, pricing = union_with_portal_free_recommendations(
                        model_ids, pricing, _portal_for_recs,
                    )
                    model_ids, unavailable_models = partition_nous_models_by_tier(
                        model_ids, pricing, free_tier=True,
                    )
                else:
                    # Paid-tier mirror: pull paidRecommendedModels so newly
                    # launched paid models surface in the picker even if
                    # the in-repo curated list and docs-hosted manifest
                    # haven't caught up yet.
                    model_ids, pricing = union_with_portal_paid_recommendations(
                        model_ids, pricing, _portal_for_recs,
                    )
            _portal = auth_state.get("portal_base_url", "")
            if model_ids:
                print(f"Showing {len(model_ids)} curated models — use \"Enter custom model name\" for others.")
                selected_model = _prompt_model_selection(
                    model_ids, pricing=pricing,
                    unavailable_models=unavailable_models,
                    portal_url=_portal,
                    unavailable_message=unavailable_message,
                )
            elif unavailable_models:
                _url = (_portal or DEFAULT_NOUS_PORTAL_URL).rstrip("/")
                print("No free models currently available.")
                print(unavailable_message or f"Upgrade at {_url} to access paid models.")
            else:
                print("No curated models available for Nous Portal.")
        except Exception as exc:
            message = format_auth_error(exc) if isinstance(exc, AuthError) else str(exc)
            print()
            print(f"Login succeeded, but could not fetch available models. Reason: {message}")

        # Write provider + model atomically so config is never mismatched.
        # If no model was selected (user picked "Skip (keep current)",
        # model list fetch failed, or no curated models were available),
        # preserve the user's previous provider — don't silently switch
        # them to Nous with a mismatched model.  The Nous OAuth tokens
        # stay saved for future use.
        if not selected_model:
            # Restore the prior active_provider that _save_provider_state
            # overwrote to "nous".  config.yaml model.provider is left
            # untouched, so the user's previous provider is fully preserved.
            with _auth_store_lock():
                auth_store = _load_auth_store()
                if prior_active_provider:
                    auth_store["active_provider"] = prior_active_provider
                else:
                    auth_store.pop("active_provider", None)
                _save_auth_store(auth_store)
            print()
            print("No provider change. Nous credentials saved for future use.")
            print("  Run `hermes model` again to switch to Nous Portal.")
            return

        config_path = _update_config_for_provider(
            "nous", inference_base_url, default_model=selected_model,
        )
        if selected_model:
            _save_model_choice(selected_model)
            print(f"Default model set to: {selected_model}")
        print(f"  Config updated: {config_path} (model.provider=nous)")

    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Login failed: {exc}")
        raise SystemExit(1)


def logout_command(args) -> None:
    """Clear auth state for a provider."""
    provider_id = getattr(args, "provider", None)

    if provider_id and not is_known_auth_provider(provider_id):
        print(f"Unknown provider: {provider_id}")
        raise SystemExit(1)

    active = get_active_provider()
    target = provider_id or active or _logout_default_provider_from_config()

    if not target:
        print("No provider is currently logged in.")
        return

    should_reset_config = _should_reset_config_provider_on_logout(target)
    provider_name = get_auth_provider_display_name(target)

    if clear_provider_auth(target) or should_reset_config:
        if should_reset_config:
            _reset_config_provider()
        print(f"Logged out of {provider_name}.")
        if should_reset_config and os.getenv("OPENROUTER_API_KEY"):
            print("Hermes will use OpenRouter for inference.")
        elif should_reset_config:
            print("Run `hermes model` or configure an API key to use Hermes.")
        else:
            print("Model provider configuration was unchanged.")
    else:
        print(f"No auth state found for {provider_name}.")