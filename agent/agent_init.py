"""Implementation of :meth:`AIAgent.__init__` — extracted as a module function.

``AIAgent.__init__`` is one of the longest methods in the codebase (60+
parameters, ~1,400 lines of attribute initialization, provider
auto-detection, credential resolution, context-engine bootstrap, etc.).
Keeping it in ``run_agent.py`` bloats that file with code that's mostly
"setup state, then forget".

After this extraction the body lives here as ``init_agent(agent, ...)``
and :meth:`AIAgent.__init__` is a thin wrapper that calls
``init_agent(self, ...)``.  All imports the body needs at module-load
time are listed below; the body also performs many lazy imports inside
its own scope that come along unchanged.

Symbols that tests patch on ``run_agent.*`` (``OpenAI``, ``cleanup_vm``,
etc.) are resolved through :func:`_ra` so the patch contract is
preserved.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs, urlunparse

from agent.context_compressor import ContextCompressor
from agent.iteration_budget import IterationBudget
from agent.memory_manager import StreamingContextScrubber
from agent.model_metadata import (
    MINIMUM_CONTEXT_LENGTH,
    fetch_model_metadata,
    is_local_endpoint,
    query_ollama_num_ctx,
)
from agent.process_bootstrap import _install_safe_stdio
from agent.subdirectory_hints import SubdirectoryHintTracker
from agent.think_scrubber import StreamingThinkScrubber
from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolGuardrailDecision,
)
from atlaz_cli.config import cfg_get
from atlaz_cli.timeouts import get_provider_request_timeout
from atlaz_constants import get_hermes_home
from atlaz_cli.config import cfg_get
from atlaz_cli.timeouts import get_provider_request_timeout
from utils import base_url_host_matches

# Use the same logger name as run_agent so tests patching ``run_agent.logger``
# capture our warnings.  (run_agent.py also does
# ``logger = logging.getLogger(__name__)``, which resolves to "run_agent"
# from inside that module.)
logger = logging.getLogger("run_agent")


def _ra():
    """Lazy reference to ``run_agent`` so callers can patch
    ``run_agent.OpenAI`` / ``run_agent.cleanup_vm`` / ... and have those
    patches reach this code path.
    """
    import run_agent
    return run_agent


def _normalized_custom_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def _custom_provider_model_matches(agent_model: str, entry: Dict[str, Any]) -> bool:
    provider_model = str(entry.get("model", "") or "").strip().lower()
    if not provider_model:
        return True
    return provider_model == str(agent_model or "").strip().lower()


def _custom_provider_extra_body_for_agent(
    *,
    provider: str,
    model: str,
    base_url: str,
    custom_providers: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if (provider or "").strip().lower() != "custom":
        return None

    target_url = _normalized_custom_base_url(base_url)
    if not target_url:
        return None

    fallback: Optional[Dict[str, Any]] = None
    for entry in custom_providers or []:
        if not isinstance(entry, dict):
            continue
        if _normalized_custom_base_url(entry.get("base_url")) != target_url:
            continue
        extra_body = entry.get("extra_body")
        if not isinstance(extra_body, dict) or not extra_body:
            continue
        provider_model = str(entry.get("model", "") or "").strip()
        if provider_model:
            if _custom_provider_model_matches(model, entry):
                return dict(extra_body)
        elif fallback is None:
            fallback = dict(extra_body)

    return fallback


def _merge_custom_provider_extra_body(agent, custom_providers: List[Dict[str, Any]]) -> None:
    extra_body = _custom_provider_extra_body_for_agent(
        provider=agent.provider,
        model=agent.model,
        base_url=agent.base_url,
        custom_providers=custom_providers,
    )
    if not extra_body:
        return

    overrides = dict(getattr(agent, "request_overrides", {}) or {})
    merged_extra_body = dict(extra_body)
    existing_extra_body = overrides.get("extra_body")
    if isinstance(existing_extra_body, dict):
        merged_extra_body.update(existing_extra_body)
    overrides["extra_body"] = merged_extra_body
    agent.request_overrides = overrides


def init_agent(
    agent,
    base_url: str = None,
    api_key: str = None,
    provider: str = None,
    api_mode: str = None,
    acp_command: str = None,
    acp_args: list[str] | None = None,
    command: str = None,
    args: list[str] | None = None,
    model: str = "",
    max_iterations: int = 90,  # Default tool-calling iterations (shared with subagents)
    tool_delay: float = 1.0,
    enabled_toolsets: List[str] = None,
    disabled_toolsets: List[str] = None,
    save_trajectories: bool = False,
    verbose_logging: bool = False,
    quiet_mode: bool = False,
    ephemeral_system_prompt: str = None,
    log_prefix_chars: int = 100,
    log_prefix: str = "",
    providers_allowed: List[str] = None,
    providers_ignored: List[str] = None,
    providers_order: List[str] = None,
    provider_sort: str = None,
    provider_require_parameters: bool = False,
    provider_data_collection: str = None,
    openrouter_min_coding_score: Optional[float] = None,
    session_id: str = None,
    tool_progress_callback: callable = None,
    tool_start_callback: callable = None,
    tool_complete_callback: callable = None,
    thinking_callback: callable = None,
    reasoning_callback: callable = None,
    clarify_callback: callable = None,
    step_callback: callable = None,
    stream_delta_callback: callable = None,
    interim_assistant_callback: callable = None,
    tool_gen_callback: callable = None,
    status_callback: callable = None,
    max_tokens: int = None,
    reasoning_config: Dict[str, Any] = None,
    service_tier: str = None,
    request_overrides: Dict[str, Any] = None,
    prefill_messages: List[Dict[str, Any]] = None,
    platform: str = None,
    user_id: str = None,
    user_id_alt: str = None,
    user_name: str = None,
    chat_id: str = None,
    chat_name: str = None,
    chat_type: str = None,
    thread_id: str = None,
    gateway_session_key: str = None,
    skip_context_files: bool = False,
    load_soul_identity: bool = False,
    skip_memory: bool = False,
    session_db=None,
    parent_session_id: str = None,
    iteration_budget: "IterationBudget" = None,
    fallback_model: Dict[str, Any] = None,
    credential_pool=None,
    checkpoints_enabled: bool = False,
    checkpoint_max_snapshots: int = 20,
    checkpoint_max_total_size_mb: int = 500,
    checkpoint_max_file_size_mb: int = 10,
    pass_session_id: bool = False,
):
    """
    Initialize the AI Agent.

    Args:
        base_url (str): Base URL for the model API (optional)
        api_key (str): API key for authentication (optional, uses env var if not provided)
        provider (str): Provider identifier (optional; used for telemetry/routing hints)
        api_mode (str): API mode override: "chat_completions" or "codex_responses"
        model (str): Model name to use (default: "anthropic/claude-opus-4.6")
        max_iterations (int): Maximum number of tool calling iterations (default: 90)
        tool_delay (float): Delay between tool calls in seconds (default: 1.0)
        enabled_toolsets (List[str]): Only enable tools from these toolsets (optional)
        disabled_toolsets (List[str]): Disable tools from these toolsets (optional)
        save_trajectories (bool): Whether to save conversation trajectories to JSONL files (default: False)
        verbose_logging (bool): Enable verbose logging for debugging (default: False)
        quiet_mode (bool): Suppress progress output for clean CLI experience (default: False)
        ephemeral_system_prompt (str): System prompt used during agent execution but NOT saved to trajectories (optional)
        log_prefix_chars (int): Number of characters to show in log previews for tool calls/responses (default: 100)
        log_prefix (str): Prefix to add to all log messages for identification in parallel processing (default: "")
        providers_allowed (List[str]): OpenRouter providers to allow (optional)
        providers_ignored (List[str]): OpenRouter providers to ignore (optional)
        providers_order (List[str]): OpenRouter providers to try in order (optional)
        provider_sort (str): Sort providers by price/throughput/latency (optional)
        openrouter_min_coding_score (float): Coding-score floor (0.0-1.0) for the
            openrouter/pareto-code router. Only applied when model == "openrouter/pareto-code".
            None or empty = let OpenRouter pick the strongest available coder.
        session_id (str): Pre-generated session ID for logging (optional, auto-generated if not provided)
        tool_progress_callback (callable): Callback function(tool_name, args_preview) for progress notifications
        clarify_callback (callable): Callback function(question, choices) -> str for interactive user questions.
            Provided by the platform layer (CLI or gateway). If None, the clarify tool returns an error.
        max_tokens (int): Maximum tokens for model responses (optional, uses model default if not set)
        reasoning_config (Dict): OpenRouter reasoning configuration override (e.g. {"effort": "none"} to disable thinking).
            If None, defaults to {"enabled": True, "effort": "medium"} for OpenRouter. Set to disable/customize reasoning.
        prefill_messages (List[Dict]): Messages to prepend to conversation history as prefilled context.
            Useful for injecting a few-shot example or priming the model's response style.
            Example: [{"role": "user", "content": "Hi!"}, {"role": "assistant", "content": "Hello!"}]
            NOTE: Anthropic Sonnet 4.6+ and Opus 4.6+ reject a conversation that ends on an
            assistant-role message (400 error).  For those models use structured outputs or
            output_config.format instead of a trailing-assistant prefill.
        platform (str): The interface platform the user is on (e.g. "cli", "telegram", "discord", "whatsapp").
            Used to inject platform-specific formatting hints into the system prompt.
        skip_context_files (bool): If True, skip auto-injection of SOUL.md, AGENTS.md, and .cursorrules
            into the system prompt. Use this for batch processing and data generation to avoid
            polluting trajectories with user-specific persona or project instructions.
        load_soul_identity (bool): If True, still use ~/.hermes/SOUL.md as the primary
            identity even when skip_context_files=True. Project context files from the cwd
            remain skipped.
    """
    _install_safe_stdio()

    agent.model = model
    agent.max_iterations = max_iterations
    # Shared iteration budget — parent creates, children inherit.
    # Consumed by every LLM turn across parent + all subagents.
    agent.iteration_budget = iteration_budget or IterationBudget(max_iterations)
    agent.tool_delay = tool_delay
    agent.save_trajectories = save_trajectories
    agent.verbose_logging = verbose_logging
    agent.quiet_mode = quiet_mode
    agent.ephemeral_system_prompt = ephemeral_system_prompt
    agent.platform = platform  # "cli", "telegram", "discord", "whatsapp", etc.
    agent._user_id = user_id  # Platform user identifier (gateway sessions)
    agent._user_id_alt = user_id_alt  # Optional stable alternate platform identifier
    agent._user_name = user_name
    agent._chat_id = chat_id
    agent._chat_name = chat_name
    agent._chat_type = chat_type
    agent._thread_id = thread_id
    agent._gateway_session_key = gateway_session_key  # Stable per-chat key (e.g. agent:main:telegram:dm:123)
    # Pluggable print function — CLI replaces this with _cprint so that
    # raw ANSI status lines are routed through prompt_toolkit's renderer
    # instead of going directly to stdout where patch_stdout's StdoutProxy
    # would mangle the escape sequences.  None = use builtins.print.
    agent._print_fn = None
    agent.background_review_callback = None  # Optional sync callback for gateway delivery
    agent.skip_context_files = skip_context_files
    agent.load_soul_identity = load_soul_identity
    agent.pass_session_id = pass_session_id
    agent._credential_pool = credential_pool
    agent.log_prefix_chars = log_prefix_chars
    agent.log_prefix = f"{log_prefix} " if log_prefix else ""
    # Store effective base URL for feature detection (prompt caching, reasoning, etc.)
    agent.base_url = base_url or ""
    provider_name = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    agent.provider = provider_name or ""
    agent.acp_command = acp_command or command
    agent.acp_args = list(acp_args or args or [])
    if api_mode in {"chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse", "codex_app_server"}:
        agent.api_mode = api_mode
    elif agent.provider == "openai-codex":
        agent.api_mode = "codex_responses"
    elif agent.provider in {"xai", "xai-oauth"}:
        agent.api_mode = "codex_responses"
    elif (provider_name is None) and (
        agent._base_url_hostname == "chatgpt.com"
        and "/backend-api/codex" in agent._base_url_lower
    ):
        agent.api_mode = "codex_responses"
        agent.provider = "openai-codex"
    elif (provider_name is None) and agent._base_url_hostname == "api.x.ai":
        agent.api_mode = "codex_responses"
        agent.provider = "xai"
    elif agent.provider == "anthropic" or (provider_name is None and agent._base_url_hostname == "api.anthropic.com"):
        agent.api_mode = "anthropic_messages"
        agent.provider = "anthropic"
    elif agent._base_url_lower.rstrip("/").endswith("/anthropic"):
        # Third-party Anthropic-compatible endpoints (e.g. MiniMax, DashScope)
        # use a URL convention ending in /anthropic. Auto-detect these so the
        # Anthropic Messages API adapter is used instead of chat completions.
        agent.api_mode = "anthropic_messages"
    elif agent.provider == "bedrock" or (
        agent._base_url_hostname.startswith("bedrock-runtime.")
        and base_url_host_matches(agent._base_url_lower, "amazonaws.com")
    ):
        # AWS Bedrock — auto-detect from provider name or base URL
        # (bedrock-runtime.<region>.amazonaws.com).
        agent.api_mode = "bedrock_converse"
    else:
        agent.api_mode = "chat_completions"

    # Eagerly warm the transport cache so import errors surface at init,
    # not mid-conversation.  Also validates the api_mode is registered.
    try:
        agent._get_transport()
    except Exception:
        pass  # Non-fatal — transport may not exist for all modes yet

    try:
        from atlaz_cli.model_normalize import (
            _AGGREGATOR_PROVIDERS,
            normalize_model_for_provider,
        )

        if agent.provider not in _AGGREGATOR_PROVIDERS:
            agent.model = normalize_model_for_provider(agent.model, agent.provider)
    except Exception:
        pass

    # GPT-5.x models usually require the Responses API path, but some
    # providers have exceptions (for example Copilot's gpt-5-mini still
    # uses chat completions). Also auto-upgrade for direct OpenAI URLs
    # (api.openai.com) since all newer tool-calling models prefer
    # Responses there. ACP runtimes are excluded: CopilotACPClient
    # handles its own routing and does not implement the Responses API
    # surface.
    # When api_mode was explicitly provided, respect it — the user
    # knows what their endpoint supports (#10473).
    # Exception: Azure OpenAI serves gpt-5.x on /chat/completions and
    # does NOT support the Responses API — skip the upgrade for Azure
    # (openai.azure.com), even though it looks OpenAI-compatible.
    if (
        api_mode is None
        and agent.api_mode == "chat_completions"
        and agent.provider != "copilot-acp"
        and not str(agent.base_url or "").lower().startswith("acp://copilot")
        and not str(agent.base_url or "").lower().startswith("acp+tcp://")
        and not agent._is_azure_openai_url()
        and (
            agent._is_direct_openai_url()
            or agent._provider_model_requires_responses_api(
                agent.model,
                provider=agent.provider,
            )
        )
    ):
        agent.api_mode = "codex_responses"
        # Invalidate the eager-warmed transport cache — api_mode changed
        # from chat_completions to codex_responses after the warm at __init__.
        if hasattr(agent, "_transport_cache"):
            agent._transport_cache.clear()

    # Pre-warm OpenRouter model metadata cache in a background thread.
    # fetch_model_metadata() is cached for 1 hour; this avoids a blocking
    # HTTP request on the first API response when pricing is estimated.
    # Use a process-level Event so this thread is only spawned once — a new
    # AIAgent is created for every gateway request, so without the guard
    # each message leaks one OS thread and the process eventually exhausts
    # the system thread limit (RuntimeError: can't start new thread).
    if (agent.provider == "openrouter" or agent._is_openrouter_url()) and \
            not _ra()._openrouter_prewarm_done.is_set():
        _ra()._openrouter_prewarm_done.set()
        threading.Thread(
            target=fetch_model_metadata,
            daemon=True,
            name="openrouter-prewarm",
        ).start()

    agent.tool_progress_callback = tool_progress_callback
    agent.tool_start_callback = tool_start_callback
    agent.tool_complete_callback = tool_complete_callback
    agent.suppress_status_output = False
    agent.thinking_callback = thinking_callback
    agent.reasoning_callback = reasoning_callback
    agent.clarify_callback = clarify_callback
    agent.step_callback = step_callback
    agent.stream_delta_callback = stream_delta_callback
    agent.interim_assistant_callback = interim_assistant_callback
    agent.status_callback = status_callback
    agent.tool_gen_callback = tool_gen_callback

    
    # Tool execution state — allows _vprint during tool execution
    # even when stream consumers are registered (no tokens streaming then)
    agent._executing_tools = False
    agent._tool_guardrails = ToolCallGuardrailController()
    agent._tool_guardrail_halt_decision: ToolGuardrailDecision | None = None

    # Interrupt mechanism for breaking out of tool loops
    agent._interrupt_requested = False
    agent._interrupt_message = None  # Optional message that triggered interrupt
    agent._execution_thread_id: int | None = None  # Set at run_conversation() start
    agent._interrupt_thread_signal_pending = False
    agent._client_lock = threading.RLock()

    # /steer mechanism — inject a user note into the next tool result
    # without interrupting the agent. Unlike interrupt(), steer() does
    # NOT set _interrupt_requested; it waits for the current tool batch
    # to finish naturally, then the drain hook appends the text to the
    # last tool result's content so the model sees it on its next
    # iteration. Message-role alternation is preserved (we modify an
    # existing tool message rather than inserting a new user turn).
    agent._pending_steer: Optional[str] = None
    agent._pending_steer_lock = threading.Lock()

    # Concurrent-tool worker thread tracking.  `_execute_tool_calls_concurrent`
    # runs each tool on its own ThreadPoolExecutor worker — those worker
    # threads hav

... [OUTPUT TRUNCATED - 33712 chars omitted out of 83712 total] ...

Store
    agent._todo_store = TodoStore()
    
    # Load config once for memory, skills, and compression sections
    try:
        from atlaz_cli.config import load_config as _load_agent_config
        _agent_cfg = _load_agent_config()
    except Exception:
        _agent_cfg = {}
    try:
        agent._tool_guardrails = ToolCallGuardrailController(
            ToolCallGuardrailConfig.from_mapping(
                _agent_cfg.get("tool_loop_guardrails", {})
            )
        )
    except Exception as _tlg_err:
        _ra().logger.warning("Tool loop guardrail config ignored: %s", _tlg_err)
    # Cache only the derived auxiliary compression context override that is
    # needed later by the startup feasibility check.  Avoid exposing a
    # broad pseudo-public config object on the agent instance.
    agent._aux_compression_context_length_config = None

    # Persistent memory (MEMORY.md + USER.md) -- loaded from disk
    agent._memory_store = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 10
    agent._turns_since_memory = 0
    agent._iters_since_skill = 0
    if not skip_memory:
        try:
            mem_config = _agent_cfg.get("memory", {})
            agent._memory_enabled = mem_config.get("memory_enabled", False)
            agent._user_profile_enabled = mem_config.get("user_profile_enabled", False)
            agent._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
            if agent._memory_enabled or agent._user_profile_enabled:
                from tools.memory_tool import MemoryStore
                agent._memory_store = MemoryStore(
                    memory_char_limit=mem_config.get("memory_char_limit", 2200),
                    user_char_limit=mem_config.get("user_char_limit", 1375),
                )
                agent._memory_store.load_from_disk()
        except Exception:
            pass  # Memory is optional -- don't break agent init
    


    # Memory provider plugin (external — one at a time, alongside built-in)
    # Reads memory.provider from config to select which plugin to activate.
    agent._memory_manager = None
    if not skip_memory:
        try:
            _mem_provider_name = mem_config.get("provider", "") if mem_config else ""

            if _mem_provider_name and _mem_provider_name.strip():
                from agent.memory_manager import MemoryManager as _MemoryManager
                from plugins.memory import load_memory_provider as _load_mem
                agent._memory_manager = _MemoryManager()
                _mp = _load_mem(_mem_provider_name)
                if _mp and _mp.is_available():
                    agent._memory_manager.add_provider(_mp)
                if agent._memory_manager.providers:
                    _init_kwargs = {
                        "session_id": agent.session_id,
                        "platform": platform or "cli",
                        "hermes_home": str(get_hermes_home()),
                        "agent_context": "primary",
                    }
                    # Thread session title for memory provider scoping
                    # (e.g. honcho uses this to derive chat-scoped session keys)
                    if agent._session_db:
                        try:
                            _st = agent._session_db.get_session_title(agent.session_id)
                            if _st:
                                _init_kwargs["session_title"] = _st
                        except Exception:
                            pass
                    # Thread gateway user identity for per-user memory scoping
                    if agent._user_id:
                        _init_kwargs["user_id"] = agent._user_id
                    if agent._user_id_alt:
                        _init_kwargs["user_id_alt"] = agent._user_id_alt
                    if agent._user_name:
                        _init_kwargs["user_name"] = agent._user_name
                    if agent._chat_id:
                        _init_kwargs["chat_id"] = agent._chat_id
                    if agent._chat_name:
                        _init_kwargs["chat_name"] = agent._chat_name
                    if agent._chat_type:
                        _init_kwargs["chat_type"] = agent._chat_type
                    if agent._thread_id:
                        _init_kwargs["thread_id"] = agent._thread_id
                    # Thread gateway session key for stable per-chat Honcho session isolation
                    if agent._gateway_session_key:
                        _init_kwargs["gateway_session_key"] = agent._gateway_session_key
                    # Profile identity for per-profile provider scoping
                    try:
                        from atlaz_cli.profiles import get_active_profile_name
                        _profile = get_active_profile_name()
                        _init_kwargs["agent_identity"] = _profile
                        _init_kwargs["agent_workspace"] = "hermes"
                    except Exception:
                        pass
                    agent._memory_manager.initialize_all(**_init_kwargs)
                    _ra().logger.info("Memory provider '%s' activated", _mem_provider_name)
                else:
                    _ra().logger.debug("Memory provider '%s' not found or not available", _mem_provider_name)
                    agent._memory_manager = None
        except Exception as _mpe:
            _ra().logger.warning("Memory provider plugin init failed: %s", _mpe)
            agent._memory_manager = None

    # Inject memory provider tool schemas into the tool surface.
    # Skip tools whose names already exist (plugins may register the
    # same tools via ctx.register_tool(), which lands in agent.tools
    # through _ra().get_tool_definitions()).  Duplicate function names cause
    # 400 errors on providers that enforce unique names (e.g. Xiaomi
    # MiMo via Nous Portal).
    #
    # Respect the platform's enabled_toolsets configuration (#5544):
    #   enabled_toolsets is None        → no filter, inject (backward compat)
    #   "memory" in enabled_toolsets    → user opted in, inject
    #   otherwise (incl. [])            → user excluded memory, skip injection
    #
    # Without this gate, `platform_toolsets: telegram: []` still leaks memory
    # provider tools (fact_store, etc.) into the tool surface — a 10x latency
    # penalty on local models and a frequent trigger of tool-call loops.
    if agent._memory_manager and agent.tools is not None and (
        agent.enabled_toolsets is None or "memory" in agent.enabled_toolsets
    ):
        _existing_tool_names = {
            t.get("function", {}).get("name")
            for t in agent.tools
            if isinstance(t, dict)
        }
        for _schema in agent._memory_manager.get_all_tool_schemas():
            _tname = _schema.get("name", "")
            if _tname and _tname in _existing_tool_names:
                continue  # already registered via plugin path
            _wrapped = {"type": "function", "function": _schema}
            agent.tools.append(_wrapped)
            if _tname:
                agent.valid_tool_names.add(_tname)
                _existing_tool_names.add(_tname)

    # Skills config: nudge interval for skill creation reminders
    agent._skill_nudge_interval = 10
    try:
        skills_config = _agent_cfg.get("skills", {})
        agent._skill_nudge_interval = int(skills_config.get("creation_nudge_interval", 10))
    except Exception:
        pass

    # Tool-use enforcement config: "auto" (default — matches hardcoded
    # model list), true (always), false (never), or list of substrings.
    _agent_section = _agent_cfg.get("agent", {})
    if not isinstance(_agent_section, dict):
        _agent_section = {}
    agent._tool_use_enforcement = _agent_section.get("tool_use_enforcement", "auto")

    # Universal task-completion guidance toggle.  Default True.  Surfaced
    # as a separate flag from tool_use_enforcement because the guidance
    # applies to ALL models, not just the model families enforcement
    # targets.
    agent._task_completion_guidance = bool(_agent_section.get("task_completion_guidance", True))

    # Local Python toolchain probe toggle.  Default True.  When False,
    # the probe is skipped entirely (no subprocess calls, no system-prompt
    # line).  Useful for users on exotic setups where the probe heuristics
    # are noisy.
    agent._environment_probe = bool(_agent_section.get("environment_probe", True))

    # App-level API retry count (wraps each model API call).  Default 3,
    # overridable via agent.api_max_retries in config.yaml.  See #11616.
    try:
        _raw_api_retries = _agent_section.get("api_max_retries", 3)
        _api_retries = int(_raw_api_retries)
        _api_retries = max(_api_retries, 1)  # 1 = no retry (single attempt)
    except (TypeError, ValueError):
        _api_retries = 3
    agent._api_max_retries = _api_retries

    # Initialize context compressor for automatic context management
    # Compresses conversation when approaching model's context limit
    # Configuration via config.yaml (compression section)
    _compression_cfg = _agent_cfg.get("compression", {})
    if not isinstance(_compression_cfg, dict):
        _compression_cfg = {}
    compression_threshold = float(_compression_cfg.get("threshold", 0.50))
    try:
        from agent.auxiliary_client import _compression_threshold_for_model as _cthresh_fn
        _model_cthresh = _cthresh_fn(agent.model)
        if _model_cthresh is not None:
            compression_threshold = _model_cthresh
    except Exception:
        pass
    compression_enabled = str(_compression_cfg.get("enabled", True)).lower() in {"true", "1", "yes"}
    compression_target_ratio = float(_compression_cfg.get("target_ratio", 0.20))
    compression_protect_last = int(_compression_cfg.get("protect_last_n", 20))
    # protect_first_n is the number of non-system messages to protect at
    # the head, in addition to the system prompt (which is always
    # implicitly protected by the compressor).  Floor at 0 — a value of
    # 0 means "preserve only the system prompt + summary + tail", which
    # is a legitimate (and common) configuration for long-running
    # rolling-compaction sessions.
    compression_protect_first = max(
        0, int(_compression_cfg.get("protect_first_n", 3))
    )
    compression_abort_on_summary_failure = str(
        _compression_cfg.get("abort_on_summary_failure", False)
    ).lower() in {"true", "1", "yes"}

    # Read optional explicit context_length override for the auxiliary
    # compression model. Custom endpoints often cannot report this via
    # /models, so the startup feasibility check needs the config hint.
    try:
        _aux_cfg = cfg_get(_agent_cfg, "auxiliary", "compression", default={})
    except Exception:
        _aux_cfg = {}
    if isinstance(_aux_cfg, dict):
        _aux_context_config = _aux_cfg.get("context_length")
    else:
        _aux_context_config = None
    if _aux_context_config is not None:
        try:
            _aux_context_config = int(_aux_context_config)
        except (TypeError, ValueError):
            _aux_context_config = None
    agent._aux_compression_context_length_config = _aux_context_config

    # Read explicit model output-token override from config when the
    # caller did not pass one directly.
    _model_cfg = _agent_cfg.get("model", {})
    if agent.max_tokens is None and isinstance(_model_cfg, dict):
        _config_max_tokens = _model_cfg.get("max_tokens")
        if _config_max_tokens is not None:
            try:
                if isinstance(_config_max_tokens, bool):
                    raise ValueError
                _parsed_max_tokens = int(_config_max_tokens)
                if _parsed_max_tokens <= 0:
                    raise ValueError
                agent.max_tokens = _parsed_max_tokens
            except (TypeError, ValueError):
                _ra().logger.warning(
                    "Invalid model.max_tokens in config.yaml: %r — "
                    "must be a positive integer (e.g. 4096). "
                    "Falling back to provider default.",
                    _config_max_tokens,
                )
                print(
                    f"\n⚠ Invalid model.max_tokens in config.yaml: {_config_max_tokens!r}\n"
                    f"  Must be a positive integer (e.g. 4096).\n"
                    f"  Falling back to provider default.\n",
                    file=sys.stderr,
                )
    agent._session_init_model_config["max_tokens"] = agent.max_tokens

    # Read explicit context_length override from model config
    if isinstance(_model_cfg, dict):
        _config_context_length = _model_cfg.get("context_length")
    else:
        _config_context_length = None
    if _config_context_length is not None:
        try:
            _config_context_length = int(_config_context_length)
        except (TypeError, ValueError):
            _ra().logger.warning(
                "Invalid model.context_length in config.yaml: %r — "
                "must be a plain integer (e.g. 256000, not '256K'). "
                "Falling back to auto-detection.",
                _config_context_length,
            )
            print(
                f"\n⚠ Invalid model.context_length in config.yaml: {_config_context_length!r}\n"
                f"  Must be a plain integer (e.g. 256000, not '256K').\n"
                f"  Falling back to auto-detected context window.\n",
                file=sys.stderr,
            )
            _config_context_length = None

    # Resolve custom_providers list once for reuse below (startup
    # context-length override and plugin context-engine init).
    try:
        from atlaz_cli.config import get_compatible_custom_providers
        _custom_providers = get_compatible_custom_providers(_agent_cfg)
    except Exception:
        _custom_providers = _agent_cfg.get("custom_providers")
        if not isinstance(_custom_providers, list):
            _custom_providers = []

    # Store for reuse by _check_compression_model_feasibility (auxiliary
    # compression model context-length detection needs the same list).
    agent._custom_providers = _custom_providers
    _merge_custom_provider_extra_body(agent, _custom_providers)

    # Check custom_providers per-model context_length
    if _config_context_length is None and _custom_providers:
        try:
            from atlaz_cli.config import get_custom_provider_context_length
            _cp_ctx_resolved = get_custom_provider_context_length(
                model=agent.model,
                base_url=agent.base_url,
                custom_providers=_custom_providers,
            )
            if _cp_ctx_resolved:
                _config_context_length = int(_cp_ctx_resolved)
        except Exception:
            _cp_ctx_resolved = None

        # Surface a clear warning if the user set a context_length but it
        # wasn't a valid positive int — the helper silently skips those.
        if _config_context_length is None:
            _target = agent.base_url.rstrip("/") if agent.base_url else ""
            for _cp_entry in _custom_providers:
                if not isinstance(_cp_entry, dict):
                    continue
                _cp_url = (_cp_entry.get("base_url") or "").rstrip("/")
                if _target and _cp_url == _target:
                    _cp_models = _cp_entry.get("models", {})
                    if isinstance(_cp_models, dict):
                        _cp_model_cfg = _cp_models.get(agent.model, {})
                        if isinstance(_cp_model_cfg, dict):
                            _cp_ctx = _cp_model_cfg.get("context_length")
                            if _cp_ctx is not None:
                                try:
                                    _parsed = int(_cp_ctx)
                                    if _parsed <= 0:
                                        raise ValueError
                                except (TypeError, ValueError):
                                    _ra().logger.warning(
                                        "Invalid context_length for model %r in "
                                        "custom_providers: %r — must be a positive "
                                        "integer (e.g. 256000, not '256K'). "
                                        "Falling back to auto-detection.",
                                        agent.model, _cp_ctx,
                                    )
                                    print(
                                        f"\n⚠ Invalid context_length for model {agent.model!r} in custom_providers: {_cp_ctx!r}\n"
                                        f"  Must be a positive integer (e.g. 256000, not '256K').\n"
                                        f"  Falling back to auto-detected context window.\n",
                                        file=sys.stderr,
                                    )
                    break

    # Persist for reuse on switch_model / fallback activation. Must come
    # AFTER the custom_providers branch so per-model overrides aren't lost.
    agent._config_context_length = _config_context_length

    agent._ensure_lmstudio_runtime_loaded(_config_context_length)



    # Select context engine: config-driven (like memory providers).
    # 1. Check config.yaml context.engine setting
    # 2. Check plugins/context_engine/<name>/ directory (repo-shipped)
    # 3. Check general plugin system (user-installed plugins)
    # 4. Fall back to built-in ContextCompressor
    _selected_engine = None
    _engine_name = "compressor"  # default
    try:
        _ctx_cfg = _agent_cfg.get("context", {}) if isinstance(_agent_cfg, dict) else {}
        _engine_name = _ctx_cfg.get("engine", "compressor") or "compressor"
    except Exception:
        pass

    if _engine_name != "compressor":
        # Try loading from plugins/context_engine/<name>/
        try:
            from plugins.context_engine import load_context_engine
            _selected_engine = load_context_engine(_engine_name)
        except Exception as _ce_load_err:
            _ra().logger.debug("Context engine load from plugins/context_engine/: %s", _ce_load_err)

        # Try general plugin system as fallback
        if _selected_engine is None:
            try:
                from atlaz_cli.plugins import get_plugin_context_engine
                _candidate = get_plugin_context_engine()
                if _candidate and _candidate.name == _engine_name:
                    _selected_engine = _candidate
            except Exception:
                pass

        if _selected_engine is None:
            _ra().logger.warning(
                "Context engine '%s' not found — falling back to built-in compressor",
                _engine_name,
            )
    # else: config says "compressor" — use built-in, don't auto-activate plugins

    if _selected_engine is not None:
        agent.context_compressor = _selected_engine
        # Resolve context_length for plugin engines — mirrors switch_model() path
        from agent.model_metadata import get_model_context_length
        _plugin_ctx_len = get_model_context_length(
            agent.model,
            base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""),
            config_context_length=_config_context_length,
            provider=agent.provider,
            custom_providers=_custom_providers,
        )
        agent.context_compressor.update_model(
            model=agent.model,
            context_length=_plugin_ctx_len,
            base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""),
            provider=agent.provider,
            api_mode=agent.api_mode,
        )
        if not agent.quiet_mode:
            _ra().logger.info("Using context engine: %s", _selected_engine.name)
    else:
        agent.context_compressor = ContextCompressor(
            model=agent.model,
            threshold_percent=compression_threshold,
            protect_first_n=compression_protect_first,
            protect_last_n=compression_protect_last,
            summary_target_ratio=compression_target_ratio,
            summary_model_override=None,
            quiet_mode=agent.quiet_mode,
            base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""),
            config_context_length=_config_context_length,
            provider=agent.provider,
            api_mode=agent.api_mode,
            abort_on_summary_failure=compression_abort_on_summary_failure,
        )
    agent.compression_enabled = compression_enabled

    # Reject models whose context window is below the minimum required
    # for reliable tool-calling workflows (64K tokens).
    _ctx = getattr(agent.context_compressor, "context_length", 0)
    if _ctx and _ctx < MINIMUM_CONTEXT_LENGTH:
        raise ValueError(
            f"Model {agent.model} has a context window of {_ctx:,} tokens, "
            f"which is below the minimum {MINIMUM_CONTEXT_LENGTH:,} required "
            f"by Hermes Agent.  Choose a model with at least "
            f"{MINIMUM_CONTEXT_LENGTH // 1000}K context, or set "
            f"model.context_length in config.yaml to override."
        )

    # Inject context engine tool schemas (e.g. lcm_grep, lcm_describe, lcm_expand).
    # Skip names that are already present — the _ra().get_tool_definitions()
    # quiet_mode cache returned a shared list pre-#17335, so a stray
    # mutation here would poison subsequent agent inits in the same
    # Gateway process and trip provider-side 'duplicate tool name'
    # errors. Even with the cache fix, dedup is the right defense
    # against plugin paths that may register the same schemas via
    # ctx.register_tool(). Mirrors the memory tools dedup above.
    #
    # Respect the platform's enabled_toolsets configuration (#5544):
    # context engine tools follow the same gating pattern as memory
    # provider tools — without the gate, `platform_toolsets: telegram: []`
    # would still leak lcm_* tools into the tool surface and incur the
    # same local-model latency penalty.
    agent._context_engine_tool_names: set = set()
    if (
        hasattr(agent, "context_compressor")
        and agent.context_compressor
        and agent.tools is not None
        and (
            agent.enabled_toolsets is None
            or "context_engine" in agent.enabled_toolsets
        )
    ):
        _existing_tool_names = {
            t.get("function", {}).get("name")
            for t in agent.tools
            if isinstance(t, dict)
        }
        for _schema in agent.context_compressor.get_tool_schemas():
            _tname = _schema.get("name", "")
            if _tname and _tname in _existing_tool_names:
                continue  # already registered via plugin/cache path
            _wrapped = {"type": "function", "function": _schema}
            agent.tools.append(_wrapped)
            if _tname:
                agent.valid_tool_names.add(_tname)
                agent._context_engine_tool_names.add(_tname)
                _existing_tool_names.add(_tname)

    # Notify context engine of session start
    if hasattr(agent, "context_compressor") and agent.context_compressor:
        try:
            agent.context_compressor.on_session_start(
                agent.session_id,
                hermes_home=str(get_hermes_home()),
                platform=agent.platform or "cli",
                model=agent.model,
                context_length=getattr(agent.context_compressor, "context_length", 0),
                conversation_id=getattr(agent, "_gateway_session_key", None),
            )
        except Exception as _ce_err:
            _ra().logger.debug("Context engine on_session_start: %s", _ce_err)

    agent._subdirectory_hints = SubdirectoryHintTracker(
        working_dir=os.getenv("TERMINAL_CWD") or None,
    )
    agent._user_turn_count = 0

    # Cumulative token usage for the session
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_api_calls = 0
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "unknown"
    agent.session_cost_source = "none"
    
    # ── Ollama num_ctx injection ──
    # Ollama defaults to 2048 context regardless of the model's capabilities.
    # When running against an Ollama server, detect the model's max context
    # and pass num_ctx on every chat request so the full window is used.
    # User override: set model.ollama_num_ctx in config.yaml to cap VRAM use.
    # If model.context_length is set, it caps num_ctx so the user's VRAM
    # budget is respected even when GGUF metadata advertises a larger window.
    agent._ollama_num_ctx: int | None = None
    _ollama_num_ctx_override = None
    if isinstance(_model_cfg, dict):
        _ollama_num_ctx_override = _model_cfg.get("ollama_num_ctx")
    if _ollama_num_ctx_override is not None:
        try:
            agent._ollama_num_ctx = int(_ollama_num_ctx_override)
        except (TypeError, ValueError):
            _ra().logger.debug("Invalid ollama_num_ctx config value: %r", _ollama_num_ctx_override)
    if agent._ollama_num_ctx is None and agent.base_url and is_local_endpoint(agent.base_url):
        try:
            # ``agent.api_key`` may be a callable (Entra token provider).
            # Ollama detection makes a manual HTTP request and expects a
            # string — Azure Foundry isn't a local endpoint so this branch
            # never fires for Entra, but guard defensively.
            _key_for_ollama = agent.api_key if isinstance(agent.api_key, str) else ""
            _detected = query_ollama_num_ctx(agent.model, agent.base_url, api_key=_key_for_ollama or "")
            if _detected and _detected > 0:
                agent._ollama_num_ctx = _detected
        except Exception as exc:
            _ra().logger.debug("Ollama num_ctx detection failed: %s", exc)
    # Cap auto-detected ollama_num_ctx to the user's explicit context_length.
    # Without this, GGUF metadata can advertise 256K+ which Ollama honours
    # by allocating that much VRAM — blowing up small GPUs even though the
    # user explicitly set a smaller context_length in config.yaml.
    if (
        agent._ollama_num_ctx
        and _config_context_length
        and _ollama_num_ctx_override is None  # don't override explicit ollama_num_ctx
        and agent._ollama_num_ctx > _config_context_length
    ):
        _ra().logger.info(
            "Ollama num_ctx capped: %d -> %d (model.context_length override)",
            agent._ollama_num_ctx, _config_context_length,
        )
        agent._ollama_num_ctx = _config_context_length
    if agent._ollama_num_ctx and not agent.quiet_mode:
        _ra().logger.info(
            "Ollama num_ctx: will request %d tokens (model max from /api/show)",
            agent._ollama_num_ctx,
        )

    if not agent.quiet_mode:
        if compression_enabled:
            print(f"📊 Context limit: {agent.context_compressor.context_length:,} tokens (compress at {int(compression_threshold*100)}% = {agent.context_compressor.threshold_tokens:,})")
        else:
            print(f"📊 Context limit: {agent.context_compressor.context_length:,} tokens (auto-compression disabled)")

    # Check immediately so CLI users see the warning at startup.
    # Gateway status_callback is not yet wired, so any warning is stored
    # in _compression_warning and replayed in the first run_conversation().
    agent._compression_warning = None
    # Lazy feasibility check: deferred to the first turn that approaches the
    # compression threshold. Running it eagerly here costs ~400ms cold (network
    # probe of the auxiliary provider chain + /models lookup) on every agent
    # init, including short ``chat -q`` runs that never reach the threshold.
    # ``ensure_compression_feasibility_checked`` (called from
    # ``run_conversation``'s preflight) runs it at most once per agent.
    agent._compression_feasibility_checked = False

    # Snapshot primary runtime for per-turn restoration.  When fallback
    # activates during a turn, the next turn restores these values so the
    # preferred model gets a fresh attempt each time.  Uses a single dict
    # so new state fields are easy to add without N individual attributes.
    _cc = agent.context_compressor
    agent._primary_runtime = {
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": getattr(agent, "api_key", ""),
        "client_kwargs": dict(agent._client_kwargs),
        "use_prompt_caching": agent._use_prompt_caching,
        "use_native_cache_layout": agent._use_native_cache_layout,
        # Context engine state that _try_activate_fallback() overwrites.
        # Use getattr for model/base_url/api_key/provider since plugin
        # engines may not have these (they're ContextCompressor-specific).
        "compressor_model": getattr(_cc, "model", agent.model),
        "compressor_base_url": getattr(_cc, "base_url", agent.base_url),
        "compressor_api_key": getattr(_cc, "api_key", ""),
        "compressor_provider": getattr(_cc, "provider", agent.provider),
        "compressor_context_length": _cc.context_length,
        "compressor_threshold_tokens": _cc.threshold_tokens,
    }
    if agent.api_mode == "anthropic_messages":
        agent._primary_runtime.update({
            "anthropic_api_key": agent._anthropic_api_key,
            "anthropic_base_url": agent._anthropic_base_url,
            "is_anthropic_oauth": agent._is_anthropic_oauth,
        })



__all__ = ["init_agent"]