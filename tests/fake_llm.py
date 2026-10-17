"""A scripted stand-in for anthropic.AsyncAnthropic (no network, no API key).

Script items are consumed one per messages.create call:
  - a response built with text(...) / tool_use(...) / response(...)
  - an Exception instance (raised)
  - a callable(kwargs) -> response
When the script is empty it echoes the last user message ("You said: ..."), so tests written for
the echo bot keep exercising the full webhook -> worker -> reply path.
"""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2


def usage(input_tokens: int = 100, output_tokens: int = 20, cache_read: int = 0, cache_write: int = 0) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write)


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, tool_input: dict[str, Any], block_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id or f"toolu_{name}", name=name, input=tool_input)


def response(*blocks: Any, stop_reason: str = "end_turn", category: str | None = None, **usage_kw: int) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=usage(**usage_kw),
                           stop_details=SimpleNamespace(category=category) if stop_reason == "refusal" else None)


def text(reply: str, **usage_kw: int) -> SimpleNamespace:
    return response(text_block(reply), **usage_kw)


def tool_use(name: str, tool_input: dict[str, Any], block_id: str | None = None, **usage_kw: int) -> SimpleNamespace:
    return response(tool_block(name, tool_input, block_id), stop_reason="tool_use", **usage_kw)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def last_user_text(kwargs: dict[str, Any]) -> str:
    content = kwargs["messages"][-1]["content"]
    return content if isinstance(content, str) else "[tool results]"


class FakeLLM:
    def __init__(self) -> None:
        self.script: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if not self.script:
            return text(f"You said: {last_user_text(kwargs)}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(kwargs) if callable(item) else item
