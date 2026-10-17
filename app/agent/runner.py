"""The Claude tool-use loop (manual loop over client.beta.messages.create)."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app.agent import context, tools
from app.config import AgentSettings
from app.models import Conversation, Message

logger = logging.getLogger(__name__)

# Server-side refusal fallback: if the model declines, Anthropic re-runs the request on its recommended
# fallback model inside the same call (beta, Claude API only).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
RETRYABLE = (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.InternalServerError,
             anthropic.OverloadedError)

_client: anthropic.AsyncAnthropic | None = None


def get_client(settings: AgentSettings) -> anthropic.AsyncAnthropic:
    """Shared async client. SDK retries are off: we retry exactly once ourselves, then fall back."""
    global _client
    if _client is None:
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        _client = anthropic.AsyncAnthropic(api_key=key, timeout=settings.llm_timeout_seconds, max_retries=0)
    return _client


@dataclass
class AgentResult:
    """Outcome of one agent run. When fallback is True the caller sends the fallback text and hands over."""

    replies: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str | None = None
    error: str | None = None
    fallback: bool = False


def _summary(result: dict[str, Any], limit: int = 300) -> str:
    """Short, log-safe summary of a tool result for llm_turns."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _add_usage(result: AgentResult, usage: Any) -> None:
    if usage is None:
        return
    result.input_tokens += getattr(usage, "input_tokens", 0) or 0
    result.output_tokens += getattr(usage, "output_tokens", 0) or 0
    result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
    result.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


async def _call(client: Any, result: AgentResult, **kwargs: Any) -> Any:
    """One model call with a single retry on timeout / connection error / 5xx / overloaded."""
    for attempt in (1, 2):
        try:
            return await client.beta.messages.create(**kwargs)
        except RETRYABLE as exc:
            if attempt == 2:
                raise
            logger.warning("LLM call failed, retrying once", extra={"fields": {"error": type(exc).__name__}})


async def run_agent(conversation: Conversation, history: list[Message], ctx: tools.ToolContext,
                    settings: AgentSettings, *, client: Any = None) -> AgentResult:
    """Run Claude with our tools until it answers (or a limit/error is hit). Never raises."""
    started = time.monotonic()
    result = AgentResult(model=settings.llm_model)
    messages = context.build_messages(history)
    if not messages:
        result.error, result.fallback = "empty_history", True
        return result

    request = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_tokens,
        "system": context.build_system(conversation, settings.brand_name, await tools.progress(ctx)),
        "output_config": {"effort": settings.llm_effort},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }
    client = client or get_client(settings)
    tool_rounds = 0
    raised_max_tokens = False
    try:
        while True:
            # Tools follow the CURRENT stage (a tool like start_order can move it during this run).
            response = await _call(client, result, messages=messages, tools=tools.tool_definitions(ctx.stage), **request)
            _add_usage(result, getattr(response, "usage", None))
            result.stop_reason = response.stop_reason

            if response.stop_reason == "refusal":
                category = getattr(getattr(response, "stop_details", None), "category", None)
                result.error, result.fallback = f"refusal:{category}", True
                break

            if response.stop_reason == "max_tokens" and not raised_max_tokens:
                # Thinking counts toward max_tokens; retry this step once with more room.
                raised_max_tokens = True
                request["max_tokens"] = settings.llm_max_tokens * 2
                logger.warning("LLM hit max_tokens, retrying with more room",
                               extra={"fields": {"max_tokens": request["max_tokens"]}})
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                text = "\n\n".join(b.text.strip() for b in response.content if b.type == "text" and b.text.strip())
                if response.stop_reason == "max_tokens":
                    result.error, result.fallback = "max_tokens", True
                elif text:
                    result.replies = [text]
                elif any(a["action"] == "human_handoff" for a in ctx.actions):
                    result.replies = [tools.HANDOFF_MESSAGE]
                else:
                    result.error, result.fallback = "empty_reply", True
                break

            if tool_rounds >= settings.llm_max_tool_iterations:
                result.error, result.fallback = "tool_iteration_limit", True
                logger.error("LLM tool iteration limit reached",
                             extra={"fields": {"conversation_id": str(conversation.id), "rounds": tool_rounds}})
                break
            tool_rounds += 1

            # Echo the assistant turn back unchanged (thinking blocks included), then all results in ONE message.
            messages = [*messages, {"role": "assistant", "content": response.content}]
            tool_results = []
            for block in tool_uses:
                output, is_error, duration_ms = await tools.run_tool(ctx, block.name, block.input)
                result.tool_calls.append({"name": block.name, "input": block.input,
                                          "result": _summary(output), "duration_ms": duration_ms})
                logger.info("Tool call", extra={"fields": {"tool": block.name, "duration_ms": duration_ms,
                                                           "error": output.get("error")}})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": json.dumps(output, ensure_ascii=False, default=str),
                                     **({"is_error": True} if is_error else {})})
            messages = [*messages, {"role": "user", "content": tool_results}]
    except anthropic.APIError as exc:
        status = getattr(exc, "status_code", None)
        result.error, result.fallback = f"{type(exc).__name__}{f' {status}' if status else ''}", True
    except Exception as exc:  # never let the agent crash the worker
        logger.exception("Agent run failed")
        result.error, result.fallback = f"unexpected:{type(exc).__name__}", True

    result.actions = list(ctx.actions)
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result
