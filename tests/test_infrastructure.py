from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deep_review import infrastructure
from deep_review.configuration import DeepReviewConfig
from deep_review.infrastructure import (
    AGENT_STEP_NAMES,
    NO_API_KEY,
    ApiCallLoggingHooks,
    McpCommands,
    McpFactory,
    StrandsAgentRunner,
    _tool_value,
)
from deep_review.logging_config import API_LOG_CATEGORY, LOG_CATEGORY_ATTRIBUTE
from deep_review.models import (
    AgentRole,
    ConsolidationResult,
    CrossPrValidationResult,
    DiscoveryResult,
    FixVerifierDecision,
    ReviewResult,
)
from deep_review.usage import UsageTrackingHooks


class FakeHttpClient:
    instances: list[FakeHttpClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


class FakeOpenAIClient:
    instances: list[FakeOpenAIClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.instances.append(self)

    async def close(self) -> None:
        self.closed = True


class FakeModel:
    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.instances.append(kwargs)


class FakeResult:
    def __init__(self, structured_output: Any) -> None:
        self.structured_output = structured_output


class FakeAgent:
    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.instances.append(kwargs)

    async def invoke_async(
        self, *_: Any, structured_output_model: type[Any], **__: Any
    ) -> FakeResult:
        await asyncio.sleep(0)
        outputs = {
            DiscoveryResult: DiscoveryResult(
                reviewer={"username": "reviewer"},
                requestor={"username": "requestor"},
                review_type="primary",
            ),
            FixVerifierDecision: FixVerifierDecision(
                comment_id=1,
                action="resolve",
                evidence="The defect is fixed.",
            ),
            ReviewResult: ReviewResult(status="no_issues", coverage=["reviewed"]),
            ConsolidationResult: ConsolidationResult(selections=[]),
            CrossPrValidationResult: CrossPrValidationResult(),
        }
        return FakeResult(outputs[structured_output_model])


class FakeServers:
    def jira(self, _: Any) -> object:
        return object()

    def bitbucket(self, _: Any) -> object:
        return object()


class CapturingServers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, set[str]]] = []

    def jira(self, allowed: set[str]) -> str:
        self.calls.append(("jira", allowed))
        return "jira-tools"

    def bitbucket(self, allowed: set[str]) -> str:
        self.calls.append(("bitbucket", allowed))
        return "bitbucket-tools"


def runtime_config() -> DeepReviewConfig:
    return DeepReviewConfig.model_validate(
        {
            "proxy": {
                "server": "http://proxy.example.test:8080",
                "username": "proxy-user",
                "password": "proxy-password",
            },
            "mcp": {
                "jira": {
                    "command": ["jira-command", "--stdio"],
                    "environment": {"ONLY_JIRA": "jira-value"},
                },
                "bitbucket": {
                    "command": ["bitbucket-command"],
                    "environment": {"ONLY_BITBUCKET": "bitbucket-value"},
                },
            },
            "llm": {
                "base_url": "https://llm.example.test/v1",
                "api_key": "root-key",
                "model_id": "root-model",
                "parameters": {"temperature": 0, "max_tokens": 1000},
            },
            "agents": {
                "architecture_expert": {
                    "use_proxy": True,
                    "llm": {
                        "model_id": "architecture_expert-model",
                        "parameters": {"max_tokens": 2000},
                    },
                },
                "implementation_expert": {"llm": {"api_key": None}},
            },
        }
    )


def test_server_factory_splits_commands_and_forwards_only_configured_environment(
    monkeypatch: Any, tmp_path: Path
) -> None:
    captured: list[tuple[Any, Any]] = []

    class FakeMCPClient:
        def __init__(self, transport: Any, tool_filters: Any = None) -> None:
            captured.append((transport(), tool_filters))

    monkeypatch.setattr(infrastructure, "MCPClient", FakeMCPClient)
    monkeypatch.setattr(infrastructure, "stdio_client", lambda parameters: parameters)
    monkeypatch.setenv("AMBIENT_SECRET", "must-not-be-forwarded")

    servers = McpFactory(runtime_config().mcp, tmp_path)
    servers.jira({"get_issue"})
    servers.bitbucket()

    jira, jira_filters = captured[0]
    bitbucket, bitbucket_filters = captured[1]
    assert (jira.command, jira.args) == ("jira-command", ["--stdio"])
    assert jira.env == {"ONLY_JIRA": "jira-value"}
    assert jira.cwd == tmp_path.resolve()
    assert jira_filters == {"allowed": ["get_issue"]}
    assert (bitbucket.command, bitbucket.args) == ("bitbucket-command", [])
    assert bitbucket.env == {"ONLY_BITBUCKET": "bitbucket-value"}
    assert bitbucket.cwd == tmp_path.resolve()
    assert bitbucket_filters is None


def test_agent_invocations_own_distinct_direct_and_proxied_clients(
    monkeypatch: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    FakeHttpClient.instances.clear()
    FakeOpenAIClient.instances.clear()
    FakeModel.instances.clear()
    FakeAgent.instances.clear()

    monkeypatch.setattr(infrastructure.openai, "DefaultAsyncHttpxClient", FakeHttpClient)
    monkeypatch.setattr(infrastructure.openai, "AsyncOpenAI", FakeOpenAIClient)
    monkeypatch.setattr(infrastructure, "OpenAIModel", FakeModel)
    monkeypatch.setattr(infrastructure, "Agent", FakeAgent)
    caplog.set_level(logging.INFO, logger=infrastructure.__name__)
    for role in (AgentRole.ARCHITECTURE_EXPERT, AgentRole.IMPLEMENTATION_EXPERT):
        (tmp_path / f"{role.value}.md").write_text("prompt", encoding="utf-8")
    runner = StrandsAgentRunner(runtime_config(), FakeServers(), tmp_path)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda method: method({}, tmp_path),
                (runner.architecture_expert, runner.implementation_expert),
            )
        )

    assert all(result.status == "no_issues" for result in results)
    assert len({id(client) for client in FakeHttpClient.instances}) == 2
    assert all(client.kwargs["trust_env"] is False for client in FakeHttpClient.instances)
    assert all(client.kwargs["verify"] is False for client in FakeHttpClient.instances)
    proxied = next(
        client for client in FakeHttpClient.instances if client.kwargs["proxy"] is not None
    )
    direct = next(
        client for client in FakeHttpClient.instances if client.kwargs["proxy"] is None
    )
    assert str(proxied.kwargs["proxy"].url) == "http://proxy.example.test:8080/"
    assert proxied.kwargs["proxy"].auth == ("proxy-user", "proxy-password")
    assert direct.kwargs["proxy"] is None
    assert {client.kwargs["api_key"] for client in FakeOpenAIClient.instances} == {
        "root-key",
        NO_API_KEY,
    }
    assert all(client.closed for client in FakeOpenAIClient.instances)
    assert all(client.closed for client in FakeHttpClient.instances)
    by_model = {model["model_id"]: model for model in FakeModel.instances}
    assert by_model["architecture_expert-model"]["params"] == {
        "temperature": 0,
        "max_tokens": 2000,
    }
    assert by_model["root-model"]["params"] == {
        "temperature": 0,
        "max_tokens": 1000,
    }
    assert {id(model["client"]) for model in FakeModel.instances} == {
        id(client) for client in FakeOpenAIClient.instances
    }
    messages = {record.getMessage() for record in caplog.records}
    assert messages >= {
        "Starting workflow step: Review architecture and design (agent: architecture_expert)",
        "Starting workflow step: Review implementation-level correctness "
        "(agent: implementation_expert)",
        "Finished workflow step: Review architecture and design (agent: architecture_expert)",
        "Finished workflow step: Review implementation-level correctness "
        "(agent: implementation_expert)",
        "architecture_expert found 0 issues",
        "implementation_expert found 0 issues",
    }


def test_agent_runners_expose_only_their_required_tools(
    monkeypatch: Any, tmp_path: Path
) -> None:
    FakeAgent.instances.clear()
    FakeHttpClient.instances.clear()
    FakeOpenAIClient.instances.clear()
    FakeModel.instances.clear()
    servers = CapturingServers()
    monkeypatch.setattr(infrastructure.openai, "DefaultAsyncHttpxClient", FakeHttpClient)
    monkeypatch.setattr(infrastructure.openai, "AsyncOpenAI", FakeOpenAIClient)
    monkeypatch.setattr(infrastructure, "OpenAIModel", FakeModel)
    monkeypatch.setattr(infrastructure, "Agent", FakeAgent)
    monkeypatch.setattr(infrastructure, "repository_tools", lambda _: ["repository-tools"])
    for role in AgentRole:
        (tmp_path / f"{role.value}.md").write_text(role.value, encoding="utf-8")
    runner = StrandsAgentRunner(runtime_config(), servers, tmp_path)  # type: ignore[arg-type]

    runner.discovery({})
    runner.fix_verifier({}, tmp_path)
    runner.architecture_expert({}, tmp_path)
    runner.implementation_expert({}, tmp_path)
    runner.code_polish_expert({}, tmp_path)
    runner.consolidator({})
    runner.cross_pr_validator({})

    assert [instance["system_prompt"] for instance in FakeAgent.instances] == [
        role.value for role in AgentRole
    ]
    assert [instance["tools"] for instance in FakeAgent.instances] == [
        [],
        ["jira-tools", "repository-tools"],
        ["repository-tools"],
        ["repository-tools"],
        ["repository-tools"],
        [],
        ["jira-tools", "bitbucket-tools"],
    ]
    assert all(
        len(instance["hooks"]) == 2
        and isinstance(instance["hooks"][0], ApiCallLoggingHooks)
        and isinstance(instance["hooks"][1], UsageTrackingHooks)
        for instance in FakeAgent.instances
    )
    assert servers.calls == [
        ("jira", infrastructure.JIRA_READ_TOOLS),
        ("jira", infrastructure.JIRA_READ_TOOLS),
        ("bitbucket", infrastructure.BITBUCKET_READ_TOOLS),
    ]


def test_every_agent_role_has_a_user_friendly_step_name() -> None:
    assert AGENT_STEP_NAMES.keys() == set(AgentRole)


def test_mcp_result_parsing() -> None:
    assert _tool_value(
        {"status": "success", "structuredContent": {"answer": 42}}
    ) == {"answer": 42}

    class FailedClient:
        def call_tool_sync(self, *_: object) -> dict[str, object]:
            return {"status": "error", "content": []}

    with pytest.raises(infrastructure.WorkflowError, match="returned an error"):
        infrastructure.McpCommands._call(FailedClient(), "test", {})  # type: ignore[arg-type]


def test_direct_mcp_calls_log_boundaries_without_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class SuccessfulClient:
        def call_tool_sync(self, *_: object) -> dict[str, object]:
            return {
                "status": "success",
                "structuredContent": {"result": {"value": 42}},
            }

    with caplog.at_level(logging.INFO, logger=infrastructure.__name__):
        value = McpCommands._call(  # type: ignore[arg-type]
            SuccessfulClient(),
            "get_issue",
            {"token": "do-not-log-this"},
            source="jira",
        )

    assert value == {"value": 42}
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == "Tool call started: source=jira, tool=get_issue"
    assert messages[1].startswith(
        "Tool call finished: source=jira, tool=get_issue, status=success, duration="
    )
    assert "do-not-log-this" not in "\n".join(messages)
    assert all(
        getattr(record, LOG_CATEGORY_ATTRIBUTE) == API_LOG_CATEGORY
        for record in caplog.records
    )


def test_failed_mcp_call_logs_an_error_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailedClient:
        def call_tool_sync(self, *_: object) -> dict[str, object]:
            return {"status": "error", "content": []}

    with (
        caplog.at_level(logging.INFO, logger=infrastructure.__name__),
        pytest.raises(infrastructure.WorkflowError, match="returned an error"),
    ):
        McpCommands._call(FailedClient(), "get_issue", {}, source="jira")  # type: ignore[arg-type]

    completion = caplog.records[-1]
    assert completion.levelno == logging.ERROR
    assert "status=error" in completion.getMessage()


def test_agent_hooks_log_llm_and_tool_boundaries(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timestamps = iter((10.0, 11.25, 20.0))
    monkeypatch.setattr(infrastructure.time, "monotonic", lambda: next(timestamps))
    hooks = ApiCallLoggingHooks(AgentRole.ARCHITECTURE_EXPERT, "review-model")

    with caplog.at_level(logging.INFO, logger=infrastructure.__name__):
        hooks._before_model_call(None)  # type: ignore[arg-type]
        hooks._after_model_call(SimpleNamespace(exception=None))  # type: ignore[arg-type]
        hooks._before_tool_call(  # type: ignore[arg-type]
            SimpleNamespace(tool_use={"toolUseId": "tool-1", "name": "read_file"})
        )
        hooks._after_tool_call(  # type: ignore[arg-type]
            SimpleNamespace(
                tool_use={"toolUseId": "tool-1", "name": "read_file"},
                exception=None,
                cancel_message=None,
                result={"status": "success"},
                duration=0.25,
            )
        )

    assert [record.getMessage() for record in caplog.records] == [
        "LLM call started: agent=architecture_expert, model=review-model",
        "LLM call finished: agent=architecture_expert, model=review-model, "
        "status=success, duration=1.250s",
        "Tool call started: agent=architecture_expert, tool=read_file",
        "Tool call finished: agent=architecture_expert, tool=read_file, "
        "status=success, duration=0.250s",
    ]
