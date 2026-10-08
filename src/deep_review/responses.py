from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import openai
from strands.models._openai_errors import classify_openai_error
from strands.models.openai_responses import OpenAIResponsesModel
from strands.types.content import Messages
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from deep_review.errors import WorkflowError


class ResponsesModel(OpenAIResponsesModel):
    """Keep complete Responses history private to one agent invocation."""

    def __init__(self, client: openai.AsyncOpenAI, **config: Any) -> None:
        super().__init__(**config)
        self._client = client
        self._history: list[dict[str, Any]] = []
        self._message_count = 0

    def clear_history(self) -> None:
        self._history.clear()
        self._message_count = 0

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        new_messages = messages[self._message_count :]
        request = self._format_request(new_messages, tool_specs, system_prompt, tool_choice)
        new_items = self._format_request_messages(new_messages)
        # These fields belong to the invocation, including when extra_body is supplied.
        owned = {"input", "store", "stream", "previous_response_id", "conversation"}
        request = {key: value for key, value in request.items() if key not in owned}
        request["extra_body"] = {
            key: value
            for key, value in (request.get("extra_body") or {}).items()
            if key not in owned | {"include"}
        }
        request.update(input=self._history + new_items, store=False, stream=False)
        request["include"] = list(
            dict.fromkeys([*(request.get("include") or []), "reasoning.encrypted_content"])
        )
        try:
            response = await self._client.responses.create(**request)
        except openai.APIError as exc:
            match classify_openai_error(exc):
                case "throttling":
                    raise ModelThrottledException(str(exc)) from exc
                case "context_overflow":
                    raise ContextWindowOverflowException(str(exc)) from exc
            raise
        if response.status != "completed":
            raise WorkflowError(f"Responses request did not complete: {response.status}")

        self._history.extend(new_items)
        self._history.extend(
            item.model_dump(mode="json", exclude_none=True) for item in response.output
        )
        # Strands appends the assistant message emitted below. Its original API items
        # are already in _history, so skip that reconstructed message on the next call.
        self._message_count = len(messages) + 1
        yield self._format_chunk({"chunk_type": "message_start"})
        for item in response.output:
            for event in self._output_events(item):
                yield event
        has_tools = any(item.type == "function_call" for item in response.output)
        yield self._format_chunk(
            {
                "chunk_type": "message_stop",
                "data": "tool_calls" if has_tools else "stop",
            }
        )
        if response.usage is not None:
            yield self._format_chunk({"chunk_type": "metadata", "data": response.usage})

    def _output_events(self, item: Any) -> list[StreamEvent]:
        if item.type == "function_call":
            data_type = "tool"
            values = [
                SimpleNamespace(
                    id=item.call_id,
                    function=SimpleNamespace(name=item.name, arguments=item.arguments),
                )
            ]
        elif item.type == "message":
            data_type = "text"
            values = [part.text for part in item.content if part.type == "output_text"]
        else:
            return []  # Reasoning is replayed privately, never emitted as agent content.
        return [
            self._format_chunk({"chunk_type": chunk, "data_type": data_type, "data": value})
            for value in values
            for chunk in ("content_start", "content_delta", "content_stop")
        ]
