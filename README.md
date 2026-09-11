# deep_review

A deterministic Python prototype for the `deep_review_workflow`. Python owns sequencing, safety
gates, and external mutations; Strands agents provide bounded engineering judgment.

## Configuration

Copy the tracked example to the project root and edit the local copy:

```bash
cp config.example.yml config.yml
```

`config.yml` is ignored by Git and is the application's only configuration source. Environment
variables are supported through complete-scalar references such as `${JIRA_PAT}`. Every reference
must resolve to a non-blank value before any MCP server, model, or external action starts.

Both `mcp.jira` and `mcp.bitbucket` require a non-empty command array. Commands run directly, without
a shell, from the directory containing `config.yml`. Each MCP process receives exactly its configured
`environment` map, so proxy values must be listed there when wanted. The MCP server's own local
configuration continues to determine how those forwarded values are interpreted.

The root `llm` supplies the OpenAI-compatible Chat Completions endpoint and model for every role.
Agent entries are optional. An unlisted role inherits the root LLM and defaults to `use_proxy: false`.
An agent LLM overrides only fields written in that entry, and its `parameters` map is shallow-merged
over the root parameters. Setting `api_key: null` explicitly removes the inherited key.

For example, an OpenAI-compatible local endpoint that requires no authentication can be configured
for one role as follows:

```yaml
agents:
  code_polish_expert:
    use_proxy: false
    llm:
      base_url: "http://127.0.0.1:8000/v1"
      api_key: null
      model_id: "local-model"
```

Only agents with `use_proxy: true` use the root proxy. Such entries require a root `proxy` section.
Proxy username and password must either both be present or both be absent. Direct and proxied model
clients ignore ambient proxy variables. TLS certificate verification is disabled for all model
connections; use only trusted endpoints and networks because this permits interception of model
traffic.

## Run

Run from the Bitbucket repository to review:

```bash
uv run deep-review ABC-123
```

OPEN PRs are discovered across repositories where the configured Bitbucket user is a reviewer. The
current Git repository and one-level sibling checkouts are matched to those PRs by their validated
`origin`; PRs without an unambiguous local checkout are reported but not reviewed.

## Development

```bash
UV_CACHE_DIR=/tmp/deep-review-uv-cache uv run pytest
UV_CACHE_DIR=/tmp/deep-review-uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/deep-review-uv-cache uv build
git diff --check
```

Tests use fakes and disposable repositories. They do not launch or contact production MCP servers,
LLMs, Jira, Bitbucket, or a proxy.
