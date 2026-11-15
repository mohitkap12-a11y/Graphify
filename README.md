# Graphify Local Platform

A self-hosted repository knowledge graph service with a browser admin console and a scoped, read-only MCP endpoint. This first release follows the local-first architecture in [the platform spec](docs/GRAPHIFY-LOCAL-PLATFORM-SPEC.md) and the shared-server login, account approval, MCP guide, analytics dashboard, and theme patterns in SpecForge.

## Requirements

- Python 3.11 or newer
- Graphify CLI installed and available as `graphify` on the service user's `PATH`
- Git
- Optional local Ollama or OpenAI-compatible local model for document and image extraction

The implementation was developed with Graphify 0.9.111 and OpenCode 2.0.23. It uses Graphify's `extract` command, verified from the installed CLI help. It does not send source, prompts, or usage analytics to hosted services. Code-only indexing is the default; semantic extraction requires an explicitly configured local backend and model.

## Install and run

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e backend
graphify-platform serve
```

The service binds to `127.0.0.1:8765` by default and writes its database, graphs, and staging worktrees to `~/.graphify-platform/`. To choose another data directory, set `GRAPHIFY_HOME`.

On first startup, make a one-use bootstrap secret:

```sh
python -m graphify_platform.cli bootstrap-secret
```

Set the printed value as `GRAPHIFY_BOOTSTRAP_SECRET` within one hour, start or restart the service, then open `http://127.0.0.1:8765`. The first admin setup is accepted from loopback only and is disabled after the active admin account is created. Use a password of at least 12 characters.

## Add a repository and build its graph

In **Repositories → Add repository**, provide a stable ID, display name, local Git checkout, and default branch. Optionally set an internal remote. Registration validates the checkout and branch and does not trigger a build. Use **Update** for incremental extraction or **Full rebuild** to explicitly rebuild. Builds run against a detached Git worktree at the selected commit, stage outputs outside the checkout, and switch the served graph only after `graph.json` validates. The current and previous graph revisions are retained after a successful build.

The default is Graphify code-only extraction with clustering disabled, so graph builds do not require an LLM. To include documents/images, choose the local `ollama` backend or a self-hosted OpenAI-compatible endpoint and provide a model. Configure the endpoint with Graphify's local provider environment variables on the service; no hosted fallback is enabled. `GRAPHIFY_QUERY_PRIVACY=hash` (default) stores query hashes rather than query text. Set it to `preview` only if your internal privacy policy allows short query previews in admin analytics.

## Git refresh

Polling is enabled by default and checks configured remotes every 120 seconds (`GRAPHIFY_POLL_SECONDS`). The service fetches a changed configured branch and queues the exact remote commit. For a push webhook, set a per-repository secret such as `GRAPHIFY_WEBHOOK_SECRET_PAYMENTS_API`, then configure the internal Git server to POST the JSON body to `/hooks/git/payments-api` with `X-Graphify-Signature: sha256=<HMAC-SHA256-of-raw-body>`. The body must contain `branch` and `after`. Only pre-registered repositories and branches are accepted. Webhook values never become filesystem paths or shell commands.

## OpenCode / MCP

Create an agent token in **Team & access**. The plaintext is displayed once. In the agent's shell, set it through an OS secret manager or shell environment:

```sh
export GRAPHIFY_MCP_TOKEN='gf_…'
opencode mcp add graphify --url http://127.0.0.1:8765/mcp --header 'Authorization=Bearer {env:GRAPHIFY_MCP_TOKEN}'
opencode mcp list
```

The command above matches the installed OpenCode v2 remote MCP configuration and keeps the credential out of the config file. A hand-written OpenCode v2 `opencode.json` entry uses `mcp.servers.graphify`, `type: "remote"`, `oauth: false`, and the same `{env:GRAPHIFY_MCP_TOKEN}` header substitution. For a shared service, replace loopback with the internal HTTPS hostname.

Available read-only tools: `list_repositories`, `graph_status`, `search_graph`, `explain_node`, `trace_path`, and `query_graph`. Tool results include request ID, repository, branch, graph version, source commit, and source provenance where available. Results are capped and report truncation. Each credential is restricted to its owner's currently assigned repositories and branches. Revoking a token immediately stops its use.

Health check: `curl http://127.0.0.1:8765/health`. For an MCP check, ask OpenCode to run `graph_status` for one repository visible to the token.

## Team access and operations

New accounts remain pending until an admin approves them. Admins grant repository access from **Team & access**. Members can query authorized graphs and create/revoke their own agent tokens. Admin-only API routes are protected by the same session role check as the UI.

Theme preference (system/light/dark) is stored in the browser before first paint. UI assets use local system fonts and no analytics, CDN, remote images, or external scripts.

For team access, set `GRAPHIFY_BIND` to an internal interface and place the service behind an internal TLS reverse proxy or trusted local certificate. Restrict firewall access to the LAN/VPN. Do not expose the service directly to a public network. Session cookies are HttpOnly and SameSite strict; cross-origin writes are rejected.

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GRAPHIFY_HOME` | `~/.graphify-platform` | Database, graph revisions, staging and logs |
| `GRAPHIFY_BIND` | `127.0.0.1` | Listen address |
| `GRAPHIFY_PORT` | `8765` | HTTP port |
| `GRAPHIFY_BOOTSTRAP_SECRET` | unset | One-use loopback admin bootstrap |
| `GRAPHIFY_POLLING` | `1` | Enable remote polling fallback |
| `GRAPHIFY_POLL_SECONDS` | `120` | Poll interval, minimum 30 seconds |
| `GRAPHIFY_QUERY_RETENTION_DAYS` | `30` | MCP call retention |
| `GRAPHIFY_QUERY_PRIVACY` | `hash` | `hash` (default) or `preview` |
| `GRAPHIFY_WEBHOOK_SECRET_<REPO_ID>` | unset | Per-repository raw-body HMAC secret |

## Backup and restore

Stop the service before copying `platform.sqlite3` and `graphs/` together from `GRAPHIFY_HOME`. Keep the database and graphs paired because the DB stores paths, graph revisions, and source commits. Restore both to the same data directory, verify file ownership is limited to the service account, then start the service and check `/ready` plus each repository's graph status. Do not include active staging directories in a backup.

## Development

```sh
PYTHONPATH=backend python -m graphify_platform.cli serve
```

The admin UI is served from `backend/web/`; the API and MCP server are in `backend/graphify_platform/`. Database tables are initialized in `db.py`. Jobs run in isolated Git worktrees and promote validated graph directories atomically by activating a new DB revision. The old active graph is left in place on worker failure.
