"""Helper functions for the chat-completions code path.

Extracted from :class:`AIAgent` for cleanliness — bodies of the
non-streaming API call, request kwargs builder, assistant-message
materializer, provider-fallback activator, max-iterations handler,
and per-turn resource cleanup.

Each function takes the parent ``AIAgent`` as its first argument
(``agent``).  :class:`AIAgent` keeps thin forwarder methods so call
sites unchanged.  Symbols that tests patch on ``run_agent`` (e.g.
``cleanup_vm`` / ``cleanup_browser`` in
``test_zombie_process_cleanup.py``) are resolved through
:func:`_ra` so the patch contract is preserved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional

from atlaz_cli.timeouts import get_provider_request_timeout, get_provider_stale_timeout
from atlaz_constants import PARTIAL_STREAM_STUB_ID, FINISH_REASON_LENGTH
from atlaz_cli.timeouts import get_provider_request_timeout, get_provider_stale_timeout
from agent.error_classifier import FailoverReason
from agent.model_metadata import is_local_endpoint
from agent.message_sanitization import (
    _sanitize_surrogates,
    _repair_tool_call_arguments,
)
from tools.terminal_tool import is_persistent_env
from utils import base_url_host_matches, base_url_hostname

logger = logging.getLogger(__name__)


def _ra():
    """Lazy ``run_agent`` reference.

    Used to honor test patches like
    ``patch("run_agent.cleanup_vm")`` / ``patch("run_agent.cleanup_browser")``
    that target symbols imported into ``run_agent``'s namespace.
    """
    import run_agent
    return run_agent


def estimate_request_context_tokens(api_payload: Any) -> int:
    """Estimate context/load tokens from an API payload, dict or messages list.

    The stale-call detectors historically assumed a Chat Completions request:
    they pulled ``api_kwargs["messages"]`` and ran a cheap char/4 estimate.
    Codex / Responses API requests carry the conversational payload in
    ``input`` (with additional load in ``instructions`` and ``tools``), so the
    legacy estimator reported ~0 tokens for every Codex turn and the
    context-tier scaling never fired.

    This helper handles both shapes:
      - bare list -> treat as Chat Completions ``messages``
      - dict with ``messages`` -> Chat Completions (+ ``tools`` if present)
      - dict with ``input`` -> Responses API (+ ``instructions``/``tools``)
      - any other dict -> fall back to summing string values
    """

    def _chars(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value)
        return len(str(value))

    def _message_chars(messages: Any) -> int:
        if not isinstance(messages, list):
            return _chars(messages)
        return sum(_chars(item) for item in messages)

    if isinstance(api_payload, list):
        return _message_chars(api_payload) // 4

    if isinstance(api_payload, dict):
        messages = api_payload.get("messages")
        if isinstance(messages, list):
            total_chars = _message_chars(messages)
            if "tools" in api_payload:
                total_chars += _chars(api_payload.get("tools"))
            return total_chars // 4

        if "input" in api_payload:
            total_chars = (
                _chars(api_payload.get("input"))
                + _chars(api_payload.get("instructions"))
                + _chars(api_payload.get("tools"))
            )
            return total_chars // 4

        return sum(_chars(value) for value in api_payload.values()) // 4

    return _chars(api_payload) // 4


def _is_openai_codex_backend(agent) -> bool:
    base_url_lower = str(getattr(agent, "_base_url_lower", "") or "")
    base_url_hostname = str(getattr(agent, "_base_url_hostname", "") or "")
    return (
        getattr(agent, "provider", None) == "openai-codex"
        or (
            base_url_hostname == "chatgpt.com"
            and "/backend-api/codex" in base_url_lower
        )
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def interruptible_api_call(agent, api_kwargs: dict):
    """
    Run the API call in a background thread so the main conversation loop
    can detect interrupts without waiting for the full HTTP round-trip.

    Each worker thread gets its own OpenAI client instance. Interrupts only
    close that worker-local client, so retries and other requests never
    inherit a closed transport.

    Includes a stale-call detector: if no response arrives within the
    configured timeout, the connection is killed and an error raised so
    the main retry loop can try again with backoff / credential rotation /
    provider fallback.
    """
    result = {"response": None, "error": None}
    request_client_holder = {"client": None, "owner_tid": None}
    request_client_lock = threading.Lock()

    def _set_request_client(client):
        with request_client_lock:
            request_client_holder["client"] = client
            # #29507: stamp the owning thread so a stranger-thread interrupt
            # only shuts the connection down rather than racing the worker
            # for FD ownership during ``client.close()``.
            request_client_holder["owner_tid"] = threading.get_ident()
        return client

    def _close_request_client_once(reason: str) -> None:
        # #29507: dispatch on the calling thread.
        #
        # When ``_call`` (the worker) reaches its ``finally`` it owns the
        # close and we pop + fully close as before. When a *stranger* thread
        # (the interrupt-check loop, the stale-call detector) drives the
        # close, only shut the sockets down so the worker's blocked
        # ``recv``/``send`` unwinds with an ``EPIPE`` / EOF — and let the
        # worker close ``client`` from its own thread on its way out. That
        # avoids the FD-recycling race where the kernel reassigned a
        # just-closed TLS socket FD to ``kanban.db``, and the still-live SSL
        # BIO on the worker thread then wrote a 24-byte TLS application-data
        # record into the SQLite header (#29507).
        with request_client_lock:
            request_client = request_client_holder.get("client")
            owner_tid = request_client_holder.get("owner_tid")
            stranger_thread = (
                request_client is not None
                and owner_tid is not None
                and owner_tid != threading.get_ident()
            )
            if not stranger_thread:
                # Owning thread (or no recorded owner) → pop and fully close.
                request_client_holder["client"] = None
                request_client_holder["owner_tid"] = None
        if request_client is None:
            return
        if stranger_thread:
            agent._abort_request_openai_client(request_client, reason=reason)
        else:
            agent._close_request_openai_client(request_client, reason=reason)

    def _call():
        try:
            if agent.api_mode == "codex_responses":
                request_client = _set_request_client(
                    agent._create_request_openai_client(
                        reason="codex_stream_request",
                        api_kwargs=api_kwargs,
                    )
                )
                result["response"] = agent._run_codex_stream(
                    api_kwargs,
                    client=request_client,
                    on_first_delta=getattr(agent, "_codex_on_first_delta", None),
                )
            elif agent.api_mode == "anthropic_messages":
                result["response"] = agent._anthropic_messages_create(api_kwargs)
            elif agent.api_mode == "bedrock_converse":
                # Bedrock uses boto3 directly — no OpenAI client needed.
                # normalize_converse_response produces an OpenAI-compatible
                # SimpleNamespace so the rest of the agent loop can treat
                # bedrock responses like chat_completions responses.
                from agent.bedrock_adapter import (
                    _get_bedrock_runtime_client,
                    invalidate_runtime_client,
                    is_stale_connection_error,
                    normalize_converse_response,
                )
                region = api_kwargs.pop("__bedrock_region__", "us-east-1")
                api_kwargs.pop("__bedrock_converse__", None)
                client = _get_bedrock_runtime_client(region)
                try:
                    raw_response = client.converse(**api_kwargs)
                except Exception as _bedrock_exc:
                    # Evict the cached client on stale-connection failures
                    # so the outer retry loop builds a fresh client/pool.
                    if is_stale_connection_error(_bedrock_exc):
                        invalidate_runtime_client(region)
                    raise
                result["response"] = normalize_converse_response(raw_response)
            else:
                request_client = _set_request_client(
                    agent._create_request_openai_client(
                        reason="chat_completion_request",
                        api_kwargs=api_kwargs,
                    )
                )
                result["response"] = request_client.chat.completions.create(**api_kwargs)
        except Exception as e:
            result["error"] = e
        finally:
            _close_request_client_once("request_complete")

    # ── Stale-call timeout (mirrors streaming stale detector) ────────
    # Non-streaming calls return nothing until the full response is
    # ready.  Without this, a hung provider can block for the full
    # httpx timeout (default 1800s) with zero feedback.  The stale
    # detector kills the connection early so the main retry loop can
    # apply richer recovery (credential rotation, provider fallback).
    _stale_timeout = agent._compute_non_stream_stale_timeout(api_kwargs)

    # ── Codex Responses stream watchdogs ────────────────────────────────
    # The chatgpt.com/backend-api/codex endpoint has an intermittent failure
    # mode where it accepts the connection but never emits a single stream
    # event (observed directly: 0 events, no HTTP status, the socket just
    # hangs). A fresh reconnect succeeds in ~2s, but the wall-clock stale
    # timeout (often 180–900s) makes us wait minutes before retrying. While no
    # stream event has arrived yet we apply a much shorter TTFB cutoff so the
    # main retry loop can reconnect promptly. Large subscription-backed Codex
    # requests can legitimately spend tens of seconds in backend admission /
    # prompt prefill before the first SSE event, so the no-byte TTFB watchdog
    # is disabled for large chatgpt.com/backend-api/codex requests. A second
    # failure mode emits an opening SSE frame and then stalls forever in SSL
    # read; for that we watch the gap since the last Codex stream event. This
    # matches Codex CLI's stream_idle_timeout model: any valid SSE event is
    # activity. Operators can tune via HERMES_CODEX_TTFB_TIMEOUT_SECONDS and
    # HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS (0 disables each).
    _codex_watchdog_enabled = agent.api_mode == "codex_responses"
    _openai_codex_backend = _is_openai_codex_backend(agent)
    _est_tokens_for_codex_watchdog = estimate_request_context_tokens(api_kwargs)
    if _codex_watchdog_enabled and _openai_codex_backend:
        if _est_tokens_for_codex_watchdog > 100_000:
            _stale_timeout = max(_stale_timeout, 1200.0)
        elif _est_tokens_for_codex_watchdog > 50_000:
            _stale_timeout = max(_stale_timeout, 900.0)
        elif _est_tokens_for_codex_watchdog > 25_000:
            _stale_timeout = max(_stale_timeout, 600.0)

    if _est_tokens_for_codex_watchdog > 100_000:
        _codex_idle_timeout_default = 180.0
    elif _est_tokens_for_codex_watchdog > 50_000:
        _codex_idle_timeout_default = 120.0
    elif _est_tokens_for_codex_watchdog > 10_000:
        _codex_idle_timeout_default = 60.0
    else:
        _codex_idle_timeout_default = 12.0

    # No-byte TTFB cutoff. The OpenAI SDK's own streaming read timeout is far
    # longer (openai 2.x DEFAULT_TIMEOUT.read = 600s), so a tight 12s default
    # killed subscription-backed Codex requests mid-prefill before the backend
    # had a chance to emit its first SSE event. Default to 120s — long enough to
    # clear normal backend admission / prompt prefill, short enough to still
    # reconnect promptly when the socket is genuinely wedged. Set
    # HERMES_CODEX_TTFB_TIMEOUT_SECONDS=0 to disable this watchdog entirely.
    _ttfb_enabled = _codex_watchdog_enabled
    _ttfb_timeout = _env_float("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", 120.0)
    if _ttfb_timeout <= 0:
        _ttfb_enabled = False
    elif _openai_codex_backend:
        _ttfb_disable_above = _env_float("HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS", 25_000.0)
        _ttfb_strict = os.environ.get("HERMES_CODEX_TTFB_STRICT", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if (
            not _ttfb_strict
            and _ttfb_disable_above > 0
            and _est_tokens_for_codex_watchdog >= _ttfb_disable_above
        ):
            _ttfb_enabled = False
            logger.info(
                "Disabling openai-codex no-byte TTFB watchdog for large request "
                "(context=~%s tokens >= %.0f). Waiting for backend response instead. "
                "Set HERMES_CODEX_TTFB_STRICT=1 to force early reconnects.",
                f"{_est_tokens_for_codex_watchdog:,}",
                _ttfb_disable_above,
            )
        else:
            _ttfb_cap = _env_float("HERMES_CODEX_TTFB_MAX_SECONDS", 120.0)
            if _ttfb_cap > 0 and _ttfb_timeout > _ttfb_cap:
                logger.info(
                    "Capping openai-codex no-byte TTFB timeout from %.0fs to %.0fs "
                    "(context=~%s tokens). Set HERMES_CODEX_TTFB_MAX_SECONDS to tune.",
                    _ttfb_timeout,
                    _ttfb_cap,
                    f"{_est_tokens_for_codex_watchdog:,}",
                )
                _ttfb_timeout = _ttfb_cap

    _codex_idle_enabled = _codex_watchdog_enabled
    _codex_idle_timeout = _env_float(
        "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS",
        _codex_idle_timeout_default,
    )
    if _codex_idle_timeout <= 0:
        _codex_idle_enabled = False

    if _codex_watchdog_enabled:
        # Reset before the worker starts so a marker left over from a previous
        # call on this agent can't be misread as first-byte for this one.
        agent._codex_stream_last_event_ts = None
        agent._codex_stream_last_progress_ts = None

    _call_start = time.time()
    agent._touch_activity("waiting for non-streaming API response")

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    _poll_count = 0
    while t.is_alive():
        t.join(timeout=0.3)
        _poll_count += 1

        # Touch activity every ~30s so the gateway's inactivity
        # monitor knows we're alive while waiting for the response.
        if _poll_count % 100 == 0:  # 100 × 0.3s = 30s
            _elapsed = time.time() - _call_start
            agent._touch_activity(
                f"waiting for non-streaming response ({int(_elapsed)}s elapsed)"
            )

        _elapsed = time.time() - _call_start

        # TTFB detector: the Codex stream has produced no event at all and
        # we're past the first-byte cutoff → the backend opened the
        # connection but isn't responding. Kill it so the retry loop can
        # reconnect (a fresh connection typically succeeds in seconds),
        # instead of waiting out the much longer wall-clock stale timeout.
        if (
            _ttfb_enabled
            and _elapsed > _ttfb_timeout
            and getattr(agent, "_codex_stream_last_event_ts", None) is None
        ):
            _silent_hint: Optional[str] = None
            _hint_fn = getattr(agent, "_codex_silent_hang_hint", None)
            if callable(_hint_fn):
                try:
                    _silent_hint = _hint_fn(model=api_kwargs.get("model"))
                except Exception:
                    _silent_hint = None
            logger.warning(
                "Codex stream produced no bytes within TTFB cutoff "
                "(%.0fs > %.0fs, model=%s). Backend accepted the connection "
                "but sent no stream events. Killing connection so the retry "
                "loop can reconnect.",
                _elapsed, _ttfb_timeout, api_kwargs.get("model", "unknown"),
            )
            if _silent_hint:
                agent._buffer_status(
                    f"⚠️ No first byte from provider in {int(_elapsed)}s "
                    f"(codex stream, model: {api_kwargs.get('model', 'unknown')}). "
                    f"Reconnecting. {_silent_hint}"
                )
            else:
                agent._buffer_status(
                    f"⚠️ No first byte from provider in {int(_elapsed)}s "
                    f"(codex stream, model: {api_kwargs.get('model', 'unknown')}). "
                    f"Reconnecting."
                )
            try:
                _close_request_client_once("codex_ttfb_kill")
            except Exception:
                pass
            agent._touch_activity(
                f"codex stream killed after {int(_elapsed)}s with no first byte"
            )
            # Wait briefly for the worker to notice the closed connection.
            t.join(timeout=2.0)
            if result["error"] is None and result["response"] is None:
                if _silent_hint:
                    result["error"] = TimeoutError(
                        f"Codex stream produced no bytes within {int(_elapsed)}s "
                        f"(TTFB threshold: {int(_ttfb_timeout)}s). {_silent_hint}"
                    )
                else:
                    result["error"] = TimeoutError(
                        f"Codex stream produced no bytes within {int(_elapsed)}s "
                        f"(TTFB threshold: {int(_ttfb_timeout)}s)"
                    )
            break

        # Stream-idle detector: the Codex backend emitted at least one SSE
        # frame, then stopped emitting events. Valid keepalive / in_progress
        # frames refresh _codex_stream_last_event_ts and should not be killed.
        _last_codex_event_ts = getattr(agent, "_codex_stream_last_event_ts", None)
        if (
            _codex_idle_enabled
            and _last_codex_event_ts is not None
            and (time.time() - _last_codex_event_ts) > _codex_idle_timeout
        ):
            _event_stale_elapsed = time.time() - _last_codex_event_ts
            logger.warning(
                "Codex stream produced no SSE events for %.0fs after first byte "
                "(threshold %.0fs, model=%s, context=~%s tokens). Killing "
                "connection so the retry loop can reconnect.",
                _event_stale_elapsed,
                _codex_idle_timeout,
                api_kwargs.get("model", "unknown"),
                f"{_est_tokens_for_codex_watchdog:,}",
            )
            agent._buffer_status(
                f"⚠️ Codex stream sent no events for {int(_event_stale_elapsed)}s "
                f"after first byte (model: {api_kwargs.get('model', 'unknown')}). "
                f"Reconnecting."
            )
            try:
                _close_request_client_once("codex_stream_idle_kill")
            except Exception:
                pass
            agent._touch_activity(
                f"codex

... [OUTPUT TRUNCATED - 69259 chars omitted out of 119259 total] ...

  # Vercel AI patterns) is immune to this.
                            entry["function"]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["function"]["arguments"] += tc_delta.function.arguments
                    extra = getattr(tc_delta, "extra_content", None)
                    if extra is None and hasattr(tc_delta, "model_extra"):
                        extra = (tc_delta.model_extra or {}).get("extra_content")
                    if extra is not None:
                        if hasattr(extra, "model_dump"):
                            extra = extra.model_dump()
                        entry["extra_content"] = extra
                    # Fire once per tool when the full name is available
                    name = entry["function"]["name"]
                    if name and idx not in tool_gen_notified:
                        tool_gen_notified.add(idx)
                        _fire_first_delta()
                        agent._fire_tool_gen_started(name)
                        # Record the partial tool-call name so the outer
                        # stub-builder can surface a user-visible warning
                        # if streaming dies before this tool's arguments
                        # are fully delivered.  Without this, a stall
                        # during tool-call JSON generation lets the stub
                        # at line ~6107 return `tool_calls=None`, silently
                        # discarding the attempted action.
                        result["partial_tool_names"].append(name)

            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

            # Usage in the final chunk
            if hasattr(chunk, "usage") and chunk.usage:
                usage_obj = chunk.usage

        # Build mock response matching non-streaming shape
        full_content = "".join(content_parts) or None
        mock_tool_calls = None
        has_truncated_tool_args = False
        if tool_calls_acc:
            mock_tool_calls = []
            for idx in sorted(tool_calls_acc):
                tc = tool_calls_acc[idx]
                arguments = tc["function"]["arguments"]
                tool_name = tc["function"]["name"] or "?"
                if arguments and arguments.strip():
                    try:
                        json.loads(arguments)
                    except json.JSONDecodeError:
                        # Attempt repair before flagging as truncated.
                        # Models like GLM-5.1 via Ollama produce trailing
                        # commas, unclosed brackets, Python None, etc.
                        # Without repair, these hit the truncation handler
                        # and kill the session.  _repair_tool_call_arguments
                        # returns "{}" for unrepairable args, which is far
                        # better than a crashed session.
                        repaired = _repair_tool_call_arguments(arguments, tool_name)
                        if repaired != "{}":
                            # Successfully repaired — use the fixed args
                            arguments = repaired
                        else:
                            # Unrepairable — flag for truncation handling
                            has_truncated_tool_args = True
                mock_tool_calls.append(SimpleNamespace(
                    id=tc["id"],
                    type=tc["type"],
                    extra_content=tc.get("extra_content"),
                    function=SimpleNamespace(
                        name=tc["function"]["name"],
                        arguments=arguments,
                    ),
                ))

        effective_finish_reason = finish_reason or "stop"
        if has_truncated_tool_args:
            effective_finish_reason = "length"

        full_reasoning = "".join(reasoning_parts) or None
        mock_message = SimpleNamespace(
            role=role,
            content=full_content,
            tool_calls=mock_tool_calls,
            reasoning_content=full_reasoning,
        )
        mock_choice = SimpleNamespace(
            index=0,
            message=mock_message,
            finish_reason=effective_finish_reason,
        )
        return SimpleNamespace(
            id="stream-" + str(uuid.uuid4()),
            model=model_name,
            choices=[mock_choice],
            usage=usage_obj,
        )

    def _call_anthropic():
        """Stream an Anthropic Messages API response.

        Fires delta callbacks for real-time token delivery, but returns
        the native Anthropic Message object from get_final_message() so
        the rest of the agent loop (validation, tool extraction, etc.)
        works unchanged.
        """
        has_tool_use = False

        # Reset stale-stream timer for this attempt
        last_chunk_time["t"] = time.time()
        # Per-attempt diagnostic dict for the retry block to consume.
        _diag = agent._stream_diag_init()
        request_client_holder["diag"] = _diag
        # Use the Anthropic SDK's streaming context manager
        with agent._anthropic_client.messages.stream(**api_kwargs) as stream:
            # The Anthropic SDK exposes the raw httpx response on
            # ``stream.response``.  Snapshot diagnostic headers
            # immediately so they survive a stream that dies before the
            # first event.
            try:
                agent._stream_diag_capture_response(
                    _diag, getattr(stream, "response", None)
                )
            except Exception:
                pass
            for event in stream:
                # Update stale-stream timer on every event so the
                # outer poll loop knows data is flowing.  Without
                # this, the detector kills healthy long-running
                # Opus streams after 180 s even when events are
                # actively arriving (the chat_completions path
                # already does this at the top of its chunk loop).
                last_chunk_time["t"] = time.time()
                agent._touch_activity("receiving stream response")

                # Update per-attempt diagnostic counters (best-effort).
                try:
                    _diag["chunks"] = int(_diag.get("chunks", 0)) + 1
                    if _diag.get("first_chunk_at") is None:
                        _diag["first_chunk_at"] = last_chunk_time["t"]
                    try:
                        _diag["bytes"] = int(_diag.get("bytes", 0)) + len(repr(event))
                    except Exception:
                        pass
                except Exception:
                    pass

                if agent._interrupt_requested:
                    break

                event_type = getattr(event, "type", None)

                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", None) == "tool_use":
                        has_tool_use = True
                        tool_name = getattr(block, "name", None)
                        if tool_name:
                            _fire_first_delta()
                            agent._fire_tool_gen_started(tool_name)

                elif event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta:
                        delta_type = getattr(delta, "type", None)
                        if delta_type == "text_delta":
                            text = getattr(delta, "text", "")
                            if text and not has_tool_use:
                                _fire_first_delta()
                                agent._fire_stream_delta(text)
                                deltas_were_sent["yes"] = True
                        elif delta_type == "thinking_delta":
                            thinking_text = getattr(delta, "thinking", "")
                            if thinking_text:
                                _fire_first_delta()
                                agent._fire_reasoning_delta(thinking_text)

            # Return the native Anthropic Message for downstream processing
            return stream.get_final_message()

    def _call():
        import httpx as _httpx

        _max_stream_retries = int(os.getenv("HERMES_STREAM_RETRIES", 2))

        try:
            for _stream_attempt in range(_max_stream_retries + 1):
                # Check for interrupt before each retry attempt.  Without
                # this, /stop closes the HTTP connection (outer poll loop),
                # but the retry loop opens a FRESH connection — negating the
                # interrupt entirely.  On slow providers (ollama-cloud) each
                # retry can block for the full stream-read timeout (120s+),
                # causing multi-minute delays between /stop and response.
                if agent._interrupt_requested:
                    raise InterruptedError("Agent interrupted before stream retry")
                try:
                    if agent.api_mode == "anthropic_messages":
                        agent._try_refresh_anthropic_client_credentials()
                        result["response"] = _call_anthropic()
                    else:
                        result["response"] = _call_chat_completions()
                    return  # success
                except Exception as e:
                    _is_timeout = isinstance(
                        e, (_httpx.ReadTimeout, _httpx.ConnectTimeout, _httpx.PoolTimeout)
                    )
                    _is_conn_err = isinstance(
                        e, (_httpx.ConnectError, _httpx.RemoteProtocolError, ConnectionError)
                    )
                    _is_stream_parse_err = agent._is_provider_stream_parse_error(e)

                    # If the stream died AFTER some tokens were delivered:
                    # normally we don't retry (the user already saw text,
                    # retrying would duplicate it).  BUT: if a tool call
                    # was in-flight when the stream died, silently aborting
                    # discards the tool call entirely.  In that case we
                    # prefer to retry — the user sees a brief
                    # "reconnecting" marker + duplicated preamble text,
                    # which is strictly better than a failed action with
                    # a "retry manually" message.  Limit this to transient
                    # connection errors (Clawdbot-style narrow gate): no
                    # tool has executed yet within this API call, so
                    # silent retry is safe wrt side-effects.
                    if deltas_were_sent["yes"]:
                        _partial_tool_in_flight = bool(
                            result.get("partial_tool_names")
                        )
                        _is_sse_conn_err_preview = False
                        if not _is_timeout and not _is_conn_err:
                            from openai import APIError as _APIError
                            if isinstance(e, _APIError) and not getattr(e, "status_code", None):
                                _err_lower_preview = str(e).lower()
                                _SSE_PREVIEW_PHRASES = (
                                    "connection lost",
                                    "connection reset",
                                    "connection closed",
                                    "connection terminated",
                                    "network error",
                                    "network connection",
                                    "terminated",
                                    "peer closed",
                                    "broken pipe",
                                    "upstream connect error",
                                )
                                _is_sse_conn_err_preview = any(
                                    phrase in _err_lower_preview
                                    for phrase in _SSE_PREVIEW_PHRASES
                                )
                        _is_transient = (
                            _is_timeout
                            or _is_conn_err
                            or _is_sse_conn_err_preview
                            or _is_stream_parse_err
                        )
                        _can_silent_retry = (
                            _partial_tool_in_flight
                            and _is_transient
                            and _stream_attempt < _max_stream_retries
                        )
                        if not _can_silent_retry:
                            # Either no tool call was in-flight (so the
                            # turn was a pure text response — current
                            # stub-with-recovered-text behaviour is
                            # correct), or retries are exhausted, or the
                            # error isn't transient.  Fall through to the
                            # stub path.
                            logger.warning(
                                "Streaming failed after partial delivery, not retrying: %s", e
                            )
                            result["error"] = e
                            return
                        # Tool call was in-flight AND error is transient:
                        # retry silently.  Clear per-attempt state so the
                        # next stream starts clean.  Fire a "reconnecting"
                        # marker so the user sees why the preamble is
                        # about to be re-streamed.  Structured WARNING is
                        # emitted by ``_emit_stream_drop`` below; no
                        # additional INFO line needed.
                        try:
                            agent._fire_stream_delta(
                                "\n\n⚠ Connection dropped mid tool-call; "
                                "reconnecting…\n\n"
                            )
                        except Exception:
                            pass
                        # Reset the streamed-text buffer so the retry's
                        # fresh preamble doesn't get double-recorded in
                        # _current_streamed_assistant_text (which would
                        # pollute the interim-visible-text comparison).
                        try:
                            agent._reset_stream_delivery_tracking()
                        except Exception:
                            pass
                        # Reset in-memory accumulators so the next
                        # attempt's chunks don't concat onto the dead
                        # stream's partial JSON.
                        result["partial_tool_names"] = []
                        deltas_were_sent["yes"] = False
                        first_delta_fired["done"] = False
                        agent._emit_stream_drop(
                            error=e,
                            attempt=_stream_attempt + 2,
                            max_attempts=_max_stream_retries + 1,
                            mid_tool_call=True,
                            diag=request_client_holder.get("diag"),
                        )
                        _close_request_client_once("stream_mid_tool_retry_cleanup")
                        try:
                            agent._replace_primary_openai_client(
                                reason="stream_mid_tool_retry_pool_cleanup"
                            )
                        except Exception:
                            pass
                        continue

                    # SSE error events from proxies (e.g. OpenRouter sends
                    # {"error":{"message":"Network connection lost."}}) are
                    # raised as APIError by the OpenAI SDK.  These are
                    # semantically identical to httpx connection drops —
                    # the upstream stream died — and should be retried with
                    # a fresh connection.  Distinguish from HTTP errors:
                    # APIError from SSE has no status_code, while
                    # APIStatusError (4xx/5xx) always has one.
                    _is_sse_conn_err = False
                    if not _is_timeout and not _is_conn_err:
                        from openai import APIError as _APIError
                        if isinstance(e, _APIError) and not getattr(e, "status_code", None):
                            _err_lower_sse = str(e).lower()
                            _SSE_CONN_PHRASES = (
                                "connection lost",
                                "connection reset",
                                "connection closed",
                                "connection terminated",
                                "network error",
                                "network connection",
                                "terminated",
                                "peer closed",
                                "broken pipe",
                                "upstream connect error",
                            )
                            _is_sse_conn_err = any(
                                phrase in _err_lower_sse
                                for phrase in _SSE_CONN_PHRASES
                            )

                    if _is_timeout or _is_conn_err or _is_sse_conn_err or _is_stream_parse_err:
                        # Transient network / timeout error. Retry the
                        # streaming request with a fresh connection first.
                        if _stream_attempt < _max_stream_retries:
                            agent._emit_stream_drop(
                                error=e,
                                attempt=_stream_attempt + 2,
                                max_attempts=_max_stream_retries + 1,
                                mid_tool_call=False,
                                diag=request_client_holder.get("diag"),
                            )
                            # Close the stale request client before retry
                            _close_request_client_once("stream_retry_cleanup")
                            # Also rebuild the primary client to purge
                            # any dead connections from the pool.
                            try:
                                agent._replace_primary_openai_client(
                                    reason="stream_retry_pool_cleanup"
                                )
                            except Exception:
                                pass
                            continue
                        # Retries exhausted. Log the final failure with
                        # full diagnostic detail (chain, headers,
                        # bytes/elapsed) via the same helper used for
                        # mid-flight retries — subagent lines get the
                        # ``[subagent-N]`` log_prefix so the parent can
                        # attribute them.
                        agent._log_stream_retry(
                            kind="exhausted",
                            error=e,
                            attempt=_max_stream_retries + 1,
                            max_attempts=_max_stream_retries + 1,
                            mid_tool_call=False,
                            diag=request_client_holder.get("diag"),
                        )
                        agent._buffer_status(
                            "❌ Provider returned malformed streaming data after "
                            f"{_max_stream_retries + 1} attempts. "
                            "The provider may be experiencing issues — "
                            "try again in a moment."
                            if _is_stream_parse_err else
                            "❌ Connection to provider failed after "
                            f"{_max_stream_retries + 1} attempts. "
                            "The provider may be experiencing issues — "
                            "try again in a moment."
                        )
                    else:
                        _err_lower = str(e).lower()
                        _is_stream_unsupported = (
                            "stream" in _err_lower
                            and "not supported" in _err_lower
                        )
                        if _is_stream_unsupported:
                            agent._disable_streaming = True
                            agent._safe_print(
                                "\n⚠  Streaming is not supported for this "
                                "model/provider. Switching to non-streaming.\n"
                                "   To avoid this delay, set display.streaming: false "
                                "in config.yaml\n"
                            )
                        logger.info(
                            "Streaming failed before delivery: %s",
                            e,
                        )

                    # Propagate the error to the main retry loop instead of
                    # falling back to non-streaming inline.  The main loop has
                    # richer recovery: credential rotation, provider fallback,
                    # backoff, and — for "stream not supported" — will switch
                    # to non-streaming on the next attempt via _disable_streaming.
                    result["error"] = e
                    return
        except InterruptedError as e:
            # The interrupt may be noticed inside the worker thread before
            # the polling loop sees it. Surface it through the normal result
            # channel so callers never miss a fast pre-retry interrupt.
            result["error"] = e
            return
        finally:
            _close_request_client_once("stream_request_complete")

    # Provider-configured stale timeout takes priority over env default.
    _cfg_stale = get_provider_stale_timeout(agent.provider, agent.model)
    if _cfg_stale is not None:
        _stream_stale_timeout_base = _cfg_stale
    else:
        _stream_stale_timeout_base = float(os.getenv("HERMES_STREAM_STALE_TIMEOUT", 180.0))
    # Local providers (Ollama, oMLX, llama-cpp) can take 300+ seconds
    # for prefill on large contexts.  Disable the stale detector unless
    # the user explicitly set HERMES_STREAM_STALE_TIMEOUT.
    if _stream_stale_timeout_base == 180.0 and agent.base_url and is_local_endpoint(agent.base_url):
        _stream_stale_timeout = float("inf")
        logger.debug("Local provider detected (%s) — stale stream timeout disabled", agent.base_url)
    else:
        # Scale the stale timeout for large contexts: slow models (like Opus)
        # can legitimately think for minutes before producing the first token
        # when the context is large.  Without this, the stale detector kills
        # healthy connections during the model's thinking phase, producing
        # spurious RemoteProtocolError ("peer closed connection").
        _est_tokens = estimate_request_context_tokens(api_kwargs)
        if _est_tokens > 100_000:
            _stream_stale_timeout = max(_stream_stale_timeout_base, 300.0)
        elif _est_tokens > 50_000:
            _stream_stale_timeout = max(_stream_stale_timeout_base, 240.0)
        else:
            _stream_stale_timeout = _stream_stale_timeout_base

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    _last_heartbeat = time.time()
    _HEARTBEAT_INTERVAL = 30.0  # seconds between gateway activity touches
    while t.is_alive():
        t.join(timeout=0.3)

        # Periodic heartbeat: touch the agent's activity tracker so the
        # gateway's inactivity monitor knows we're alive while waiting
        # for stream chunks.  Without this, long thinking pauses (e.g.
        # reasoning models) or slow prefill on local providers (Ollama)
        # trigger false inactivity timeouts.  The _call thread touches
        # activity on each chunk, but the gap between API call start
        # and first chunk can exceed the gateway timeout — especially
        # when the stale-stream timeout is disabled (local providers).
        _hb_now = time.time()
        if _hb_now - _last_heartbeat >= _HEARTBEAT_INTERVAL:
            _last_heartbeat = _hb_now
            _waiting_secs = int(_hb_now - last_chunk_time["t"])
            agent._touch_activity(
                f"waiting for stream response ({_waiting_secs}s, no chunks yet)"
            )

        # Detect stale streams: connections kept alive by SSE pings
        # but delivering no real chunks.  Kill the client so the
        # inner retry loop can start a fresh connection.
        _stale_elapsed = time.time() - last_chunk_time["t"]
        if _stale_elapsed > _stream_stale_timeout:
            _est_ctx = estimate_request_context_tokens(api_kwargs)
            logger.warning(
                "Stream stale for %.0fs (threshold %.0fs) — no chunks received. "
                "model=%s context=~%s tokens. Killing connection.",
                _stale_elapsed, _stream_stale_timeout,
                api_kwargs.get("model", "unknown"), f"{_est_ctx:,}",
            )
            agent._buffer_status(
                f"⚠️ No response from provider for {int(_stale_elapsed)}s "
                f"(model: {api_kwargs.get('model', 'unknown')}, "
                f"context: ~{_est_ctx:,} tokens). "
                f"Reconnecting..."
            )
            try:
                _close_request_client_once("stale_stream_kill")
            except Exception:
                pass
            # Rebuild the primary client too — its connection pool
            # may hold dead sockets from the same provider outage.
            try:
                agent._replace_primary_openai_client(reason="stale_stream_pool_cleanup")
            except Exception:
                pass
            # Reset the timer so we don't kill repeatedly while
            # the inner thread processes the closure.
            last_chunk_time["t"] = time.time()
            agent._touch_activity(
                f"stale stream detected after {int(_stale_elapsed)}s, reconnecting"
            )

        if agent._interrupt_requested:
            try:
                if agent.api_mode == "anthropic_messages":
                    agent._anthropic_client.close()
                    agent._rebuild_anthropic_client()
                else:
                    _close_request_client_once("stream_interrupt_abort")
            except Exception:
                pass
            raise InterruptedError("Agent interrupted during streaming API call")
    if result["error"] is not None:
        if deltas_were_sent["yes"]:
            # Streaming failed AFTER some tokens were already delivered to
            # the platform.  Re-raising would let the outer retry loop make
            # Return a partial response stub with finish_reason="length"
            # so the conversation loop's continuation machinery fires.
            # tool_calls=None prevents auto-execution of incomplete calls.
            _partial_text = (
                getattr(agent, "_current_streamed_assistant_text", "") or ""
            ).strip() or None

            # Append a user-visible warning if tool calls were dropped so
            # the user and model both know what was attempted.
            _partial_names = list(result.get("partial_tool_names") or [])
            if _partial_names:
                _name_str = ", ".join(_partial_names[:3])
                if len(_partial_names) > 3:
                    _name_str += f", +{len(_partial_names) - 3} more"
                _warn = (
                    f"\n\n⚠ Stream stalled mid tool-call "
                    f"({_name_str}); the action was not executed. "
                    f"Ask me to retry if you want to continue."
                )
                _partial_text = (_partial_text or "") + _warn
                # Fire as streaming delta so the user sees it immediately.
                try:
                    agent._fire_stream_delta(_warn)
                except Exception:
                    pass
                logger.warning(
                    "Partial stream dropped tool call(s) %s after %s chars "
                    "of text; surfaced warning to user: %s",
                    _partial_names, len(_partial_text or ""), result["error"],
                )
                _stub_finish_reason = FINISH_REASON_LENGTH
            else:
                logger.warning(
                    "Partial stream delivered before error; returning "
                    "length-truncated stub with %s chars of recovered "
                    "content so the loop can continue from where the "
                    "stream died: %s",
                    len(_partial_text or ""),
                    result["error"],
                )
                _stub_finish_reason = FINISH_REASON_LENGTH
            _stub_msg = SimpleNamespace(
                role="assistant", content=_partial_text, tool_calls=None,
                reasoning_content=None,
            )
            return SimpleNamespace(
                id=PARTIAL_STREAM_STUB_ID,
                model=getattr(agent, "model", "unknown"),
                choices=[SimpleNamespace(
                    index=0, message=_stub_msg, finish_reason=_stub_finish_reason,
                )],
                usage=None,
                _dropped_tool_names=_partial_names or None,
            )
        raise result["error"]
    return result["response"]

# ── Provider fallback ──────────────────────────────────────────────────



__all__ = [
    "interruptible_api_call",
    "build_api_kwargs",
    "build_assistant_message",
    "try_activate_fallback",
    "handle_max_iterations",
    "cleanup_task_resources",
    "interruptible_streaming_api_call",
]