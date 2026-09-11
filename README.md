# deep_review

`deep_review` is a Python workflow for evidence-based Bitbucket pull-request review. It retrieves
the Jira issue, finds matching open pull requests, reviews their local diffs with specialist agents,
consolidates verified findings, and publishes the result to Bitbucket and Jira.

## Before you start

You need:

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
- A local clone of every repository that may be reviewed. Start the command from one of those
  clones. The workflow also considers Git repositories directly beside it.
- A clean working tree in each candidate clone. During review, the workflow fetches the PR source
  branch and may switch the checkout to that branch; it never proceeds with uncommitted changes.
- Working Jira and Bitbucket MCP servers, and an OpenAI-compatible endpoint for the configured
  models.

The local clone's `origin` must be an unambiguous Bitbucket URL whose project and repository match
the pull request. If an issue has a matching PR without one unambiguous local clone, that PR is
reported as skipped rather than reviewed.

## Configure the workflow

From this project directory, create the local configuration:

```bash
cp config.example.yml config.yml
```

Edit `config.yml` before the first run. It is ignored by Git and is the only application
configuration file. The tracked example shows the required sections:

- `mcp.jira` and `mcp.bitbucket`: command arrays for the MCP servers and the exact environment
  variables each process receives. Commands are executed without a shell and relative paths are
  resolved from this project directory.
- `llm`: the OpenAI-compatible `base_url`, `api_key`, and `model_id` used by every agent unless a
  role overrides them under `agents`.
- `proxy`: required only when an agent sets `use_proxy: true`. Configure the username and password
  together. Model clients otherwise ignore ambient proxy variables.

Set every environment variable referenced as `${NAME}` in `config.yml` before running. References
must occupy the complete YAML value and resolve to a non-empty value. For the bundled example,
that includes the Jira, Bitbucket, proxy, and model credentials it references. Replace the example
MCP command paths and model IDs when your local layout or providers differ.

Agent LLM entries inherit unspecified fields from `llm`; their `parameters` values are merged with
the root parameters. Set `api_key: null` only for an endpoint that does not require authentication.
Optional token pricing is expressed in USD per million tokens in the relevant `llm` block. A cost
is shown only when pricing is available for all reported usage.

## Run a review

Change to a clean checkout that can be reviewed, then run this project's command explicitly:

```bash
cd /path/to/repository-to-review
uv run --project /path/to/deep_review deep-review ABC-123
```

Use the Jira issue key as the only argument. The workflow reads the issue and its comments, then
searches for matching open pull requests. It reviews matching local checkouts, including eligible
sibling checkouts, and verifies the pull-request head and base again before publishing.

This command has external effects. For reviewed pull requests it can add finding comments, resolve
or reply to earlier reviewer comments during a fix-verification review, and set the review status
to `APPROVED` or `NEEDS_WORK`. On successful completion it also comments on the Jira issue and
assigns it to the requestor. Run it only with credentials authorized to make those changes.

If a target cannot be safely prepared or changes during review, the workflow skips or fails that
target and reports the incomplete review to Jira. The process exits with a non-zero status unless
the whole ticket review completes.

## Development

```bash
UV_CACHE_DIR=/tmp/deep-review-uv-cache uv run pytest
UV_CACHE_DIR=/tmp/deep-review-uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/deep-review-uv-cache uv build
git diff --check
```

Tests use fakes and disposable repositories. They do not launch or contact production MCP servers,
LLMs, Jira, Bitbucket, or a proxy.
