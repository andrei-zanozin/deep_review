from __future__ import annotations

from pathlib import Path

import pytest

from deep_review.configuration import load_config
from deep_review.errors import ConfigurationError
from deep_review.models import AgentRole

BASE_CONFIG = """
mcp:
  jira:
    command: [jira, --stdio]
    environment:
      JIRA_PAT: "${JIRA_PAT}"
  bitbucket:
    command: [bitbucket]
llm:
  base_url: "https://llm.example.test/v1"
  api_key: "${LLM_KEY}"
  model_id: root-model
  parameters:
    temperature: 0
    max_tokens: 1000
"""


def write_config(tmp_path: Path, source: str = BASE_CONFIG) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(source, encoding="utf-8")
    return path


def test_loads_required_configuration_and_resolves_complete_environment_scalars(
    tmp_path: Path,
) -> None:
    config = load_config(
        write_config(tmp_path),
        {"JIRA_PAT": "jira-secret", "LLM_KEY": "llm-secret"},
    )

    assert config.mcp.jira.command == ["jira", "--stdio"]
    assert config.mcp.jira.environment == {"JIRA_PAT": "jira-secret"}
    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == "llm-secret"


def test_tracked_example_is_valid() -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = load_config(
        project_root / "config.example.yml",
        {
            "PROXY_USERNAME": "proxy-user",
            "PROXY_PASSWORD": "proxy-password",
            "JIRA_SERVER": "https://jira.example.test",
            "JIRA_PAT": "jira-token",
            "BITBUCKET_PAT": "bitbucket-token",
            "DEEP_REVIEW_OPENAI_BASE_URL": "https://llm.example.test/v1",
            "DEEP_REVIEW_OPENAI_API_KEY": "llm-token",
        },
    )

    assert config.resolve(AgentRole.CODE_POLISH_EXPERT).llm.api_key is None


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (BASE_CONFIG + "unknown: true\n", "unknown"),
        (
            BASE_CONFIG.replace("command: [jira, --stdio]", "command: []"),
            "at least 1 item",
        ),
        (BASE_CONFIG.replace("https://llm.example.test/v1", "not-a-url"), "URL"),
    ],
)
def test_rejects_invalid_configuration_without_echoing_values(
    tmp_path: Path, source: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message) as error:
        load_config(
            write_config(tmp_path, source),
            {"JIRA_PAT": "do-not-echo-jira", "LLM_KEY": "do-not-echo-llm"},
        )

    assert "do-not-echo" not in str(error.value)


def test_rejects_missing_file_and_malformed_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="file is missing"):
        load_config(tmp_path / "config.yml", {})
    with pytest.raises(ConfigurationError, match="YAML is malformed"):
        load_config(write_config(tmp_path, "mcp: [unterminated"), {})


@pytest.mark.parametrize("value", [None, "", "   "])
def test_rejects_missing_or_blank_environment_references(
    tmp_path: Path, value: str | None
) -> None:
    environment = {"LLM_KEY": "key"}
    if value is not None:
        environment["JIRA_PAT"] = value
    with pytest.raises(ConfigurationError, match="JIRA_PAT"):
        load_config(write_config(tmp_path), environment)


def test_rejects_embedded_environment_reference(tmp_path: Path) -> None:
    source = BASE_CONFIG.replace(
        'base_url: "https://llm.example.test/v1"',
        'base_url: "https://${HOST}/v1"',
    )
    with pytest.raises(ConfigurationError, match="complete YAML scalar"):
        load_config(
            write_config(tmp_path, source),
            {"JIRA_PAT": "jira", "LLM_KEY": "key", "HOST": "llm.example.test"},
        )


@pytest.mark.parametrize(
    "proxy",
    [
        "proxy:\n  server: https://proxy.example.test\n  username: user\n",
        "proxy:\n  server: https://proxy.example.test\n  password: password\n",
    ],
)
def test_rejects_partial_proxy_credentials(tmp_path: Path, proxy: str) -> None:
    with pytest.raises(ConfigurationError, match="configured together"):
        load_config(
            write_config(tmp_path, proxy + BASE_CONFIG),
            {"JIRA_PAT": "jira", "LLM_KEY": "key"},
        )


def test_rejects_proxy_use_without_root_proxy(tmp_path: Path) -> None:
    source = BASE_CONFIG + "agents:\n  discovery:\n    use_proxy: true\n"
    with pytest.raises(ConfigurationError, match="root proxy is required"):
        load_config(
            write_config(tmp_path, source),
            {"JIRA_PAT": "jira", "LLM_KEY": "key"},
        )


def test_rejects_unknown_agent_role(tmp_path: Path) -> None:
    source = BASE_CONFIG + "agents:\n  unknown_role:\n    use_proxy: false\n"
    with pytest.raises(ConfigurationError, match="unknown_role"):
        load_config(
            write_config(tmp_path, source),
            {"JIRA_PAT": "jira", "LLM_KEY": "key"},
        )


def test_resolves_inheritance_defaults_overrides_and_explicit_key_clearing(
    tmp_path: Path,
) -> None:
    source = (
        "proxy:\n"
        "  server: https://proxy.example.test\n"
        "  username: proxy-user\n"
        "  password: proxy-password\n"
        + BASE_CONFIG
        + """
agents:
  architecture_expert:
    use_proxy: true
    llm:
      model_id: architecture_expert-model
      parameters:
        max_tokens: 2000
        top_p: 0.5
  code_polish_expert:
    llm:
      base_url: http://127.0.0.1:8000/v1
      api_key: null
      model_id: local-model
"""
    )
    config = load_config(
        write_config(tmp_path, source),
        {"JIRA_PAT": "jira", "LLM_KEY": "root-key"},
    )

    architecture_expert = config.resolve(AgentRole.ARCHITECTURE_EXPERT)
    assert architecture_expert.use_proxy is True
    assert architecture_expert.llm.model_id == "architecture_expert-model"
    assert architecture_expert.llm.parameters == {
        "temperature": 0,
        "max_tokens": 2000,
        "top_p": 0.5,
    }
    assert architecture_expert.llm.api_key is not None
    assert architecture_expert.llm.api_key.get_secret_value() == "root-key"

    polish = config.resolve(AgentRole.CODE_POLISH_EXPERT)
    assert str(polish.llm.base_url) == "http://127.0.0.1:8000/v1"
    assert polish.llm.api_key is None
    assert polish.llm.parameters == {"temperature": 0, "max_tokens": 1000}

    for role in set(AgentRole) - {AgentRole.ARCHITECTURE_EXPERT, AgentRole.CODE_POLISH_EXPERT}:
        inherited = config.resolve(role)
        assert inherited.use_proxy is False
        assert inherited.llm.model_id == "root-model"
        assert inherited.llm.parameters == {"temperature": 0, "max_tokens": 1000}
