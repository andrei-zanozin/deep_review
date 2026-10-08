from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from strands import Agent, tool

from deep_review import infrastructure
from deep_review.configuration import DeepReviewConfig
from deep_review.errors import WorkflowError
from deep_review.infrastructure import StrandsAgentRunner
from deep_review.models import AgentRole, DiscoveryResult
from deep_review.responses import ResponsesModel

DISCOVERY = {
    "reviewer": {"username": "reviewer"},
    "requestor": {"username": "requestor"},
    "review_type": "primary",
}


@tool
def probe() -> str:
    """Return a harmless local observation."""
    return "observed"


class Gateway:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.clients: list[httpx.AsyncClient] = []
        self.models: list[ResponsesModel] = []
        self.fail_followup = False
        self._lock = threading.Lock()

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self._lock:
            number = len(self.requests) + 1
            self.requests.append(body)
        assert request.url.path == "/v1/responses"
        assert body["store"] is False
        assert body["stream"] is False
        assert "previous_response_id" not in body
        assert "conversation" not in body
        assert "reasoning.encrypted_content" in body["include"]
        followup = any(
            item["type"] == "function_call_output" for item in body["input"] if "type" in item
        )
        if followup and self.fail_followup:
            return httpx.Response(400, json={"error": {"message": "test failure"}})
        output: list[dict[str, Any]] = [
            {
                "type": "reasoning",
                "id": f"rs_{number}",
                "summary": [],
                "encrypted_content": f"private-{body['model']}-{number}",
            },
            {
                "type": "function_call",
                "id": f"fc_{number}",
                "call_id": f"call_{number}",
                "name": "DiscoveryResult" if followup else "probe",
                "arguments": json.dumps(DISCOVERY) if followup else "{}",
                "status": "completed",
            },
        ]
        return httpx.Response(
            200,
            json={
                "id": f"resp_{number}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": body["model"],
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def http_client(**kwargs: Any) -> httpx.AsyncClient:
            # Keep production transport options, substituting only the network.
            client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle), **kwargs)
            self.clients.append(client)
            return client

        class CapturingModel(ResponsesModel):
            def __init__(model_self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.models.append(model_self)

        def agent(**kwargs: Any) -> Agent:
            kwargs["tools"] = [*kwargs["tools"], probe]
            return Agent(**kwargs)

        monkeypatch.setattr(infrastructure.openai, "DefaultAsyncHttpxClient", http_client)
        monkeypatch.setattr(infrastructure, "ResponsesModel", CapturingModel)
        monkeypatch.setattr(infrastructure, "Agent", agent)


def runner(tmp_path: Path) -> StrandsAgentRunner:
    (tmp_path / "discovery.md").write_text("Use probe, then return DiscoveryResult.")
    config = DeepReviewConfig.model_validate(
        {
            "mcp": {"jira": {"command": ["unused"]}, "bitbucket": {"command": ["unused"]}},
            "llm": {
                "base_url": "https://gateway.example.test/v1",
                "api_key": "test-key",
                "model_id": "luna",
                "api": "responses",
                "parameters": {"reasoning": {"effort": "medium"}},
            },
            "agents": {
                "architecture_expert": {
                    "llm": {
                        "model_id": "sol",
                        "parameters": {"reasoning": {"effort": "high"}},
                    }
                }
            },
        }
    )
    return StrandsAgentRunner(config, None, tmp_path)  # type: ignore[arg-type]


def assert_private_continuation(first: dict[str, Any], followup: dict[str, Any]) -> None:
    assert all(item.get("type") != "reasoning" for item in first["input"])
    reasoning = next(item for item in followup["input"] if item.get("type") == "reasoning")
    assert reasoning["encrypted_content"].startswith(f"private-{first['model']}-")
    calls = [item for item in followup["input"] if item.get("type") == "function_call"]
    outputs = [item for item in followup["input"] if item.get("type") == "function_call_output"]
    assert len(calls) == len(outputs) == 1
    assert calls[0]["call_id"] == outputs[0]["call_id"]


def test_reasoning_is_replayed_but_not_shared_or_retained_on_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.install(monkeypatch)
    agents = runner(tmp_path)
    first = agents.discovery({"issue": "first"})
    second = agents.discovery({"previous_result": first.model_dump(mode="json")})

    assert first == second == DiscoveryResult.model_validate(DISCOVERY)
    assert len(gateway.requests) == 4
    for initial, followup in zip(gateway.requests[::2], gateway.requests[1::2], strict=True):
        assert_private_continuation(initial, followup)
        assert initial["reasoning"] == {"effort": "medium"}
    assert "private-luna-" not in json.dumps(gateway.requests[2])
    assert len({id(model) for model in gateway.models}) == 2
    assert all(model._history == [] for model in gateway.models)
    assert all(client.is_closed for client in gateway.clients)
    assert sum(record.output_tokens for record in agents.usage._records) == 20


def test_failed_run_does_not_leak_reasoning_to_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.install(monkeypatch)
    agents = runner(tmp_path)
    gateway.fail_followup = True
    with pytest.raises(WorkflowError, match="test failure"):
        agents.discovery({"issue": "failed"})
    gateway.fail_followup = False
    assert agents.discovery({"issue": "retry"}).reviewer.username == "reviewer"
    assert_private_continuation(gateway.requests[2], gateway.requests[3])
    assert "private-luna-1" not in json.dumps(gateway.requests[2:])
    assert all(model._history == [] for model in gateway.models)
    assert all(client.is_closed for client in gateway.clients)


def test_concurrent_agents_have_independent_history_and_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = Gateway()
    gateway.install(monkeypatch)
    agents = runner(tmp_path)

    async def invoke(role: AgentRole) -> None:
        async with agents._model(role) as model:
            from strands.agent.conversation_manager import NullConversationManager

            agent = Agent(
                model=model,
                tools=[probe],
                callback_handler=None,
                conversation_manager=NullConversationManager(),
            )
            result = await agent.invoke_async("Inspect", structured_output_model=DiscoveryResult)
            assert result.structured_output == DiscoveryResult.model_validate(DISCOVERY)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda role: asyncio.run(invoke(role)),
                [AgentRole.DISCOVERY, AgentRole.ARCHITECTURE_EXPERT],
            )
        )
    for model, effort in [("luna", "medium"), ("sol", "high")]:
        requests = [request for request in gateway.requests if request["model"] == model]
        assert len(requests) == 2
        assert_private_continuation(*requests)
        assert all(request["reasoning"] == {"effort": effort} for request in requests)
        other = "sol" if model == "luna" else "luna"
        assert f"private-{other}-" not in json.dumps(requests)
    assert all(model._history == [] for model in gateway.models)
    assert all(client.is_closed for client in gateway.clients)


def test_history_controls_cannot_be_overridden() -> None:
    captured: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "test",
                "output": [
                    {
                        "type": "message",
                        "id": "msg",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}],
                    }
                ],
            },
        )

    async def invoke() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with openai.AsyncOpenAI(api_key="test", http_client=http) as client:
                model = ResponsesModel(
                    client=client,
                    model_id="test",
                    params={
                        "previous_response_id": "old",
                        "store": True,
                        "extra_body": {
                            "store": True,
                            "input": [],
                            "conversation": "old",
                            "include": [],
                        },
                    },
                )
                events = [
                    event
                    async for event in model.stream(
                        [{"role": "user", "content": [{"text": "fresh"}]}]
                    )
                ]
                assert any(
                    event.get("contentBlockDelta", {}).get("delta") == {"text": "done"}
                    for event in events
                )

    asyncio.run(invoke())
    assert captured[0]["store"] is False
    assert "previous_response_id" not in captured[0]
    assert "conversation" not in captured[0]
    assert captured[0]["include"] == ["reasoning.encrypted_content"]
    assert captured[0]["input"][0]["content"][0]["text"] == "fresh"
