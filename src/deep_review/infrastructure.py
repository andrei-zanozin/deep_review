from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import httpx
import openai
from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookRegistry,
)
from strands.models.openai import OpenAIModel
from strands.tools.mcp import MCPClient

from deep_review.configuration import DeepReviewConfig, McpConfig, McpServerConfig
from deep_review.errors import WorkflowError
from deep_review.logging_config import api_log_context
from deep_review.models import (
    AgentRole,
    ConsolidationResult,
    CrossPrValidationResult,
    DiscoveryResult,
    FixVerifierDecision,
    ReviewResult,
)
from deep_review.repository import repository_tools

LOGGER = logging.getLogger(__name__)

NO_API_KEY = "deep-review-no-api-key"

AGENT_STEP_NAMES = {
    AgentRole.DISCOVERY: "Identify reviewer, requestor, and review type",
    AgentRole.FIX_VERIFIER: "Reconcile an existing reviewer comment",
    AgentRole.ARCHITECTURE_EXPERT: "Review architecture and design",
    AgentRole.IMPLEMENTATION_EXPERT: "Review implementation-level correctness",
    AgentRole.CODE_POLISH_EXPERT: "Review code quality and maintainability",
    AgentRole.CONSOLIDATOR: "Consolidate review findings",
    AgentRole.CROSS_PR_VALIDATOR: "Correlate findings across pull requests",
}

JIRA_READ_TOOLS = {"get_issue", "get_issue_comments"}
BITBUCKET_READ_TOOLS = {
    "search_review_pull_requests",
    "search_pull_requests",
    "get_pull_request",
    "get_pull_request_comments",
}


class Commands(Protocol):
    def jira(self, name: str, arguments: dict[str, Any]) -> Any: ...

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any: ...


class AgentRunner(Protocol):
    def discovery(self, context: dict[str, Any]) -> DiscoveryResult: ...

    def fix_verifier(
        self, context: dict[str, Any], repository_root: Path
    ) -> FixVerifierDecision: ...

    def architecture_expert(
        self, context: dict[str, Any], repository_root: Path
    ) -> ReviewResult: ...

    def implementation_expert(
        self, context: dict[str, Any], repository_root: Path
    ) -> ReviewResult: ...

    def code_polish_expert(
        self, context: dict[str, Any], repository_root: Path
    ) -> ReviewResult: ...

    def consolidator(self, context: dict[str, Any]) -> ConsolidationResult: ...

    def cross_pr_validator(self, context: dict[str, Any]) -> CrossPrValidationResult: ...


class McpFactory:
    def __init__(self, config: McpConfig, config_dir: Path) -> None:
        self.config = config
        self.config_dir = config_dir.resolve()

    def jira(self, allowed: set[str] | None = None) -> MCPClient:
        return self._client(self.config.jira, allowed)

    def bitbucket(self, allowed: set[str] | None = None) -> MCPClient:
        return self._client(self.config.bitbucket, allowed)

    def _client(
        self,
        config: McpServerConfig,
        allowed: set[str] | None,
    ) -> MCPClient:
        parameters = StdioServerParameters(
            command=config.command[0],
            args=config.command[1:],
            env=dict(config.environment),
            cwd=self.config_dir,
        )
        filters = {"allowed": sorted(allowed)} if allowed is not None else None
        return MCPClient(lambda: stdio_client(parameters), tool_filters=filters)


class McpCommands:
    def __init__(self, servers: McpFactory) -> None:
        self._jira = servers.jira()
        self._bitbucket = servers.bitbucket()
        self._stack = ExitStack()

    def __enter__(self) -> McpCommands:
        try:
            self._stack.enter_context(self._jira)
            self._stack.enter_context(self._bitbucket)
            self._validate_tools(self._jira, JIRA_READ_TOOLS | {"add_comment", "assign_issue"})
            self._validate_tools(
                self._bitbucket,
                BITBUCKET_READ_TOOLS
                | {"add_pull_request_comment", "set_comment_resolved", "set_review_status"},
            )
            return self
        except Exception:
            self._stack.close()
            raise

    def __exit__(self, *exc_info: object) -> None:
        self._stack.__exit__(*exc_info)

    def jira(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._call(self._jira, name, arguments, source="jira")

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._call(self._bitbucket, name, arguments, source="bitbucket")

    @staticmethod
    def _validate_tools(client: MCPClient, required: set[str]) -> None:
        available = {candidate.tool_name for candidate in client.list_tools_sync()}
        if missing := required - available:
            raise WorkflowError(f"required MCP tools are unavailable: {', '.join(sorted(missing))}")

    @staticmethod
    def _call(
        client: MCPClient,
        name: str,
        arguments: dict[str, Any],
        *,
        source: str = "mcp",
    ) -> Any:
        started_at = time.monotonic()
        LOGGER.info(
            "Tool call started: source=%s, tool=%s",
            source,
            name,
            extra=api_log_context(),
        )
        try:
            result = client.call_tool_sync(str(uuid.uuid4()), name, arguments)
            if result.get("status") != "success" or result.get("isError") is True:
                raise WorkflowError(f"MCP tool {name} returned an error")
            value = _tool_value(result)
        except WorkflowError:
            _log_api_finished("Tool", f"source={source}, tool={name}", started_at, False)
            raise
        except Exception as exc:
            _log_api_finished("Tool", f"source={source}, tool={name}", started_at, False)
            raise WorkflowError(f"MCP tool {name} failed: {exc}") from exc
        _log_api_finished("Tool", f"source={source}, tool={name}", started_at, True)
        return value


class ApiCallLoggingHooks:
    def __init__(self, role: AgentRole, model_id: str) -> None:
        self._role = role
        self._model_id = model_id
        self._model_started_at: float | None = None
        self._tool_started_at: dict[str, float] = {}

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)
        registry.add_callback(AfterModelCallEvent, self._after_model_call)
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)

    def _before_model_call(self, _: BeforeModelCallEvent) -> None:
        self._model_started_at = time.monotonic()
        LOGGER.info(
            "LLM call started: agent=%s, model=%s",
            self._role.value,
            self._model_id,
            extra=api_log_context(),
        )

    def _after_model_call(self, event: AfterModelCallEvent) -> None:
        started_at = (
            self._model_started_at
            if self._model_started_at is not None
            else time.monotonic()
        )
        self._model_started_at = None
        _log_api_finished(
            "LLM",
            f"agent={self._role.value}, model={self._model_id}",
            started_at,
            event.exception is None,
        )

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_id = str(event.tool_use.get("toolUseId", ""))
        self._tool_started_at[tool_id] = time.monotonic()
        LOGGER.info(
            "Tool call started: agent=%s, tool=%s",
            self._role.value,
            event.tool_use.get("name", "unknown"),
            extra=api_log_context(),
        )

    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_id = str(event.tool_use.get("toolUseId", ""))
        started_at = self._tool_started_at.pop(tool_id, None)
        if started_at is None:
            started_at = time.monotonic()
        success = (
            event.exception is None
            and event.cancel_message is None
            and event.result.get("status") != "error"
        )
        _log_api_finished(
            "Tool",
            f"agent={self._role.value}, tool={event.tool_use.get('name', 'unknown')}",
            started_at,
            success,
            duration=event.duration,
        )


def _log_api_finished(
    call_type: str,
    details: str,
    started_at: float,
    success: bool,
    *,
    duration: float | None = None,
) -> None:
    elapsed = duration if duration is not None else time.monotonic() - started_at
    level = logging.INFO if success else logging.ERROR
    LOGGER.log(
        level,
        "%s call finished: %s, status=%s, duration=%.3fs",
        call_type,
        details,
        "success" if success else "error",
        max(0.0, elapsed),
        extra=api_log_context(),
    )


class StrandsAgentRunner:
    def __init__(
        self,
        config: DeepReviewConfig,
        servers: McpFactory,
        prompts: Path,
    ) -> None:
        self.config = config
        self.servers = servers
        self.prompts = prompts

    def discovery(self, context: dict[str, Any]) -> DiscoveryResult:
        async def invoke() -> DiscoveryResult:
            role = AgentRole.DISCOVERY
            async with self._model(role) as model:
                agent = Agent(
                    model=model,
                    system_prompt=self._prompt(role),
                    tools=[],
                    callback_handler=None,
                    hooks=self._logging_hooks(role),
                )
                try:
                    LOGGER.info(
                        "Starting workflow step: %s (agent: %s)",
                        AGENT_STEP_NAMES[role],
                        role.value,
                    )
                    result = await agent.invoke_async(
                        json.dumps(context, default=str, ensure_ascii=False),
                        structured_output_model=DiscoveryResult,
                    )
                except Exception as exc:
                    raise WorkflowError(f"{role.value} agent failed: {exc}") from exc
                if result.structured_output is None:
                    raise WorkflowError(f"{role.value} agent returned no structured output")
                _log_finished_step(role)
                return result.structured_output

        return asyncio.run(invoke())

    def fix_verifier(self, context: dict[str, Any], repository_root: Path) -> FixVerifierDecision:
        async def invoke() -> FixVerifierDecision:
            role = AgentRole.FIX_VERIFIER
            async with self._model(role) as model:
                agent = Agent(
                    model=model,
                    system_prompt=self._prompt(role),
                    tools=[
                        self.servers.jira(JIRA_READ_TOOLS),
                        *repository_tools(repository_root),
                    ],
                    callback_handler=None,
                    hooks=self._logging_hooks(role),
                )
                try:
                    LOGGER.info(
                        "Starting workflow step: %s (agent: %s)",
                        AGENT_STEP_NAMES[role],
                        role.value,
                    )
                    result = await agent.invoke_async(
                        json.dumps(context, default=str, ensure_ascii=False),
                        structured_output_model=FixVerifierDecision,
                    )
                except Exception as exc:
                    raise WorkflowError(f"{role.value} agent failed: {exc}") from exc
                if result.structured_output is None:
                    raise WorkflowError(f"{role.value} agent returned no structured output")
                _log_finished_step(role)
                return result.structured_output

        return asyncio.run(invoke())

    def architecture_expert(self, context: dict[str, Any], repository_root: Path) -> ReviewResult:
        async def invoke() -> ReviewResult:
            role = AgentRole.ARCHITECTURE_EXPERT
            async with self._model(role) as model:
                agent = Agent(
                    model=model,
                    system_prompt=self._prompt(role),
                    tools=repository_tools(repository_root),
                    callback_handler=None,
                    hooks=self._logging_hooks(role),
                )
                try:
                    LOGGER.info(
                        "Starting workflow step: %s (agent: %s)",
                        AGENT_STEP_NAMES[role],
                        role.value,
                    )
                    result = await agent.invoke_async(
                        json.dumps(context, default=str, ensure_ascii=False),
                        structured_output_model=ReviewResult,
                    )
                except Exception as exc:
                    raise WorkflowError(f"{role.value} agent failed: {exc}") from exc
                if result.structured_output is None:
                    raise WorkflowError(f"{role.value} agent returned no structured output")
                _log_finished_step(role, len(result.structured_output.findings))
                return result.structured_output

        return asyncio.run(invoke())

    def implementation_expert(self, context: dict[str, Any], repository_root: Path) -> ReviewResult:
        async def invoke() -> ReviewResult:
            role = AgentRole.IMPLEMENTATION_EXPERT
            async with self._model(role) as model:
                agent = Agent(
                    model=model,
                    system_prompt=self._prompt(role),
                    tools=repository_tools(repository_root),
                    callback_handler=None,
                    hooks=self._logging_hooks(role),
                )
                try:
                    LOGGER.info(
                        "Starting workflow step: %s (agent: %s)",
                        AGENT_STEP_NAMES[role],
                        role.value,
                    )
                    result = await agent.invoke_async(
                        json.dumps(context, default=str, ensure_ascii=False),
                        structured_output_model=ReviewResult,
                    )
                except Exception as exc:
                    raise WorkflowError(f"{role.value} agent failed: {exc}") from exc
                if result.structured_output is None:
                    raise WorkflowError(f"{role.value} agent returned no structured output")
                _log_finished_step(role, len(result.structured_output.findings))
                return result.structured_output

        return asyncio.run(invoke())

    def code_polish_expert(self, context: dict[str, Any], repository_root: Path) -> ReviewResult:
        async def invoke() -> ReviewResult:
            role = AgentRole.CODE_POLISH_EXPERT
            async with self._model(role) as model:
                agent = Agent(
                    model=model,
                    system_prompt=self._prompt(role),
                    tools=repository_tools(repository_root),
                    callback_handler=None,
                    hooks=self._logging_hooks(role),
                )
                try:
                    LOGGER.info(
                        "Starting workflow step: %s (agent: %s)",
                        AGENT_STEP_NAMES[role],
                        role.value,
                    )
                    result = await agent.invoke_async(
                        json.dumps(context, default=str, ensure_ascii=False),
                        structured_output_model=ReviewResult,
                    )
                except Exception as exc:
                    raise WorkflowError(f"{role.value} agent failed: {exc}") from exc
                if result.structured_output is None:
                    raise WorkflowError(f"{role.value} agent returned no structured output")
                _log_finished_step(role, len(result.structured_output.findings))
                return result.structured_output

        return asyncio.run(invoke())

    def consolidator(self, context: dict[str, Any]) -> ConsolidationResult:
        async def invoke() -> ConsolidationResult:
            role = AgentRole.CONSOLIDATOR
            async with self._model(role) as model:
                agent = Agent(
                    model=model,
                    system_prompt=self._prompt(role),
                    tools=[],
                    callback_handler=None,
                    hooks=self._logging_hooks(role),
                )
                try:
                    LOGGER.info(
                        "Starting workflow step: %s (agent: %s)",
                        AGENT_STEP_NAMES[role],
                        role.value,
                    )
                    result = await agent.invoke_async(
                        json.dumps(context, default=str, ensure_ascii=False),
                        structured_output_model=ConsolidationResult,
                    )
                except Exception as exc:
                    raise WorkflowError(f"{role.value} agent failed: {exc}") from exc
                if result.structured_output is None:
                    raise WorkflowError(f"{role.value} agent returned no structured output")
                _log_finished_step(role)
                return result.structured_output

        return asyncio.run(invoke())

    def cross_pr_validator(self, context: dict[str, Any]) -> CrossPrValidationResult:
        async def invoke() -> CrossPrValidationResult:
            role = AgentRole.CROSS_PR_VALIDATOR
            async with self._model(role) as model:
                agent = Agent(
                    model=model,
                    system_prompt=self._prompt(role),
                    tools=[
                        self.servers.jira(JIRA_READ_TOOLS),
                        self.servers.bitbucket(BITBUCKET_READ_TOOLS),
                    ],
                    callback_handler=None,
                    hooks=self._logging_hooks(role),
                )
                try:
                    LOGGER.info(
                        "Starting workflow step: %s (agent: %s)",
                        AGENT_STEP_NAMES[role],
                        role.value,
                    )
                    result = await agent.invoke_async(
                        json.dumps(context, default=str, ensure_ascii=False),
                        structured_output_model=CrossPrValidationResult,
                    )
                except Exception as exc:
                    raise WorkflowError(f"{role.value} agent failed: {exc}") from exc
                if result.structured_output is None:
                    raise WorkflowError(f"{role.value} agent returned no structured output")
                _log_finished_step(role, len(result.structured_output.findings))
                return result.structured_output

        return asyncio.run(invoke())

    @asynccontextmanager
    async def _model(self, role: AgentRole) -> AsyncIterator[OpenAIModel]:
        spec = self.config.resolve(role)
        proxy = None
        if spec.use_proxy:
            proxy_config = self.config.proxy
            if proxy_config is None:  # Defensive; configuration validation rejects this.
                raise WorkflowError(f"{role.value} agent requires an unavailable proxy")
            auth = None
            if proxy_config.username is not None and proxy_config.password is not None:
                auth = (
                    proxy_config.username.get_secret_value(),
                    proxy_config.password.get_secret_value(),
                )
            proxy = httpx.Proxy(str(proxy_config.server), auth=auth)

        http_client = openai.DefaultAsyncHttpxClient(
            trust_env=False,
            proxy=proxy,
            verify=False,
        )
        llm = spec.llm
        api_key = llm.api_key.get_secret_value() if llm.api_key is not None else NO_API_KEY
        try:
            openai_client = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=str(llm.base_url),
                http_client=http_client,
            )
        except Exception:
            await http_client.aclose()
            raise
        try:
            model = OpenAIModel(
                client=openai_client,
                model_id=llm.model_id,
                params=dict(llm.parameters),
            )
            yield model
        finally:
            try:
                await openai_client.close()
            finally:
                await http_client.aclose()

    def _logging_hooks(self, role: AgentRole) -> list[ApiCallLoggingHooks]:
        return [ApiCallLoggingHooks(role, self.config.resolve(role).llm.model_id)]

    def _prompt(self, role: AgentRole) -> str:
        path = self.prompts / f"{role.value}.md"
        if not path.is_file():
            raise WorkflowError(f"prompt is missing for {role.value}: {path}")
        return path.read_text(encoding="utf-8")


def _log_finished_step(role: AgentRole, issues_found: int | None = None) -> None:
    LOGGER.info(
        "Finished workflow step: %s (agent: %s)",
        AGENT_STEP_NAMES[role],
        role.value,
    )
    if issues_found is not None:
        LOGGER.info("%s found %d issues", role.value, issues_found)


def runtime_agent_runner(
    config: DeepReviewConfig, servers: McpFactory
) -> StrandsAgentRunner:
    return StrandsAgentRunner(
        config=config,
        servers=servers,
        prompts=Path(__file__).with_name("prompts"),
    )


def _tool_value(result: Any) -> Any:
    value = result.model_dump(by_alias=True) if hasattr(result, "model_dump") else result
    if isinstance(value, dict):
        structured = value.get("structuredContent") or value.get("structured_content")
        if structured is not None:
            if isinstance(structured, dict):
                return structured.get("result", structured)
            return structured
        content = value.get("content")
        if isinstance(content, list) and content:
            block = content[0]
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    raise WorkflowError("MCP tool returned no usable content")
