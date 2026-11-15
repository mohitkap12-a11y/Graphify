# Deployment and operations

See the [README](../README.md) for local installation and first-run setup. This page covers operating the shared service on an internal server.

## Internal network deployment

Run the process as a dedicated least-privilege account. Keep `GRAPHIFY_HOME` outside the checkout with owner-only permissions. Use the default loopback bind when a local TLS proxy is present; if the proxy runs on another host, explicitly set `GRAPHIFY_BIND` to the internal interface. Limit ingress to the company VPN/LAN and terminate TLS with an internal certificate. OpenCode clients should connect to the HTTPS endpoint and use individual agent tokens.

Do not place Git passwords or tokens in remote URLs. Prefer a service-account SSH key with a restrictive Git-server deploy key policy or an OS credential helper. Repository roots must be local Git checkout roots. The Graphify worker invokes fixed executables with argument arrays and uses detached worktrees at exact commits.

## First admin

Generate a one-use bootstrap secret using `python -m graphify_platform.cli bootstrap-secret`, provide it to the service as `GRAPHIFY_BOOTSTRAP_SECRET`, start the service, and finish the setup from loopback. Remove the environment variable after the first admin is created. The setup API rejects remote clients and refuses a second bootstrap.

## Backups and restore

Stop the service to get a consistent filesystem backup. Copy the SQLite database and `graphs/` directory from the same `GRAPHIFY_HOME`. Restore both together, set ownership to the service account, and restart. Check `/health`, `/ready`, and the graph revision and commit in the dashboard before allowing clients back in. Keep a separate encrypted backup according to the organization's retention policy.

## Credential response

In **Team & access**, revoke a leaked token; revocation is checked on every MCP request. Review audit entries and MCP calls for its prefix and request time. Create a replacement scoped to the affected user's currently approved repositories, deliver it through the organization's secret manager, and remove the old credential from client environments. Rotate Git deploy keys at the Git host if the incident involved repository access.

## Git refresh settings

Polling is enabled by default. `GRAPHIFY_POLL_SECONDS` controls its interval. Signed push events use `POST /hooks/git/{registered-repository-id}` and an HMAC-SHA256 signature over the exact raw request body in `X-Graphify-Signature`. Store the webhook secret in the service environment. Only configured repositories/branches are mapped; payloads cannot specify commands or local paths. Polling remains a fallback when no webhook is configured.

## OpenCode

The reference CLI version is OpenCode v2.0.23. Configure a remote MCP server with `opencode mcp add graphify --url https://graphify.internal.example/mcp --header 'Authorization=Bearer {env:GRAPHIFY_MCP_TOKEN}'`. Set the token in the OpenCode process environment or an OS credential manager. OpenCode v2 config uses `mcp.servers`, `type: "remote"`, and `oauth: false` for this bearer-token endpoint.
