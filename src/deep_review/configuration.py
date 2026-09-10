from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from deep_review.errors import ConfigurationError
from deep_review.models import AgentRole

CONFIG_FILENAME = "config.yml"
ENVIRONMENT_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
NonEmptyString = Annotated[str, Field(min_length=1)]


class StrictConfigurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProxyConfig(StrictConfigurationModel):
    server: AnyHttpUrl
    username: SecretStr | None = None
    password: SecretStr | None = None

    def model_post_init(self, __context: Any) -> None:
        if (self.username is None) != (self.password is None):
            raise ValueError("proxy username and password must be configured together")
        for name, value in (("username", self.username), ("password", self.password)):
            if value is not None and not value.get_secret_value().strip():
                raise ValueError(f"proxy {name} must not be blank")


class McpServerConfig(StrictConfigurationModel):
    command: list[NonEmptyString] = Field(min_length=1)
    environment: dict[NonEmptyString, str] = Field(default_factory=dict)


class McpConfig(StrictConfigurationModel):
    jira: McpServerConfig
    bitbucket: McpServerConfig


class LlmConfig(StrictConfigurationModel):
    base_url: AnyHttpUrl
    api_key: SecretStr | None = None
    model_id: NonEmptyString
    parameters: dict[str, Any] = Field(default_factory=lambda: {"temperature": 0})

    def model_post_init(self, __context: Any) -> None:
        if self.api_key is not None and not self.api_key.get_secret_value().strip():
            raise ValueError("LLM API key must not be blank")


class AgentLlmConfig(StrictConfigurationModel):
    base_url: AnyHttpUrl | None = None
    api_key: SecretStr | None = None
    model_id: NonEmptyString | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        for field in ("base_url", "model_id"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"agent LLM {field} must not be null")
        if "api_key" in self.model_fields_set and self.api_key is not None:
            if not self.api_key.get_secret_value().strip():
                raise ValueError("agent LLM API key must not be blank")


class AgentConfig(StrictConfigurationModel):
    use_proxy: bool = False
    llm: AgentLlmConfig | None = None


class EffectiveAgentConfig(StrictConfigurationModel):
    use_proxy: bool
    llm: LlmConfig


class DeepReviewConfig(StrictConfigurationModel):
    proxy: ProxyConfig | None = None
    mcp: McpConfig
    llm: LlmConfig
    agents: dict[AgentRole, AgentConfig] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if self.proxy is None and any(agent.use_proxy for agent in self.agents.values()):
            raise ValueError("root proxy is required when an agent has use_proxy=true")

    def resolve(self, role: AgentRole) -> EffectiveAgentConfig:
        agent = self.agents.get(role, AgentConfig())
        values = {
            "base_url": self.llm.base_url,
            "api_key": self.llm.api_key,
            "model_id": self.llm.model_id,
            "parameters": dict(self.llm.parameters),
        }
        override = agent.llm
        if override is not None:
            for field in ("base_url", "api_key", "model_id"):
                if field in override.model_fields_set:
                    values[field] = getattr(override, field)
            values["parameters"].update(override.parameters)
        return EffectiveAgentConfig(use_proxy=agent.use_proxy, llm=LlmConfig(**values))


def project_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / CONFIG_FILENAME


def load_config(
    path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> DeepReviewConfig:
    config_path = (path or project_config_path()).resolve()
    try:
        source = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file is missing: {config_path}") from exc
    except OSError as exc:
        raise ConfigurationError(f"configuration file cannot be read: {config_path}") from exc

    try:
        raw = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"configuration YAML is malformed: {config_path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be a mapping")

    try:
        resolved = _resolve_environment(raw, os.environ if environment is None else environment)
        return DeepReviewConfig.model_validate(resolved)
    except ConfigurationError:
        raise
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
        raise ConfigurationError(f"configuration is invalid: {details}") from exc


def _resolve_environment(value: Any, environment: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_environment(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment(item, environment) for item in value]
    if not isinstance(value, str) or "${" not in value:
        return value
    match = ENVIRONMENT_REFERENCE.fullmatch(value)
    if match is None:
        raise ConfigurationError(
            "environment reference must occupy the complete YAML scalar"
        )
    name = match.group(1)
    resolved = environment.get(name)
    if resolved is None or not resolved.strip():
        raise ConfigurationError(f"referenced environment variable is missing or blank: {name}")
    return resolved
