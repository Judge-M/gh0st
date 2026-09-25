# gh0st

An ephemeral, task-scoped runtime with local routing, a SQLite state ledger, and an OpenAI-compatible model gateway.

**Project:** [Judge-M/gh0st](https://github.com/Judge-M/gh0st)

**Author:** Matthew Judge (Judge-M) · matthew@cyberexperts.org

## Current implementation

- Each repository task runs against a separate local clone at a recorded source commit. The clone has independent Git objects and no configured remote. Scratch tasks receive an empty task repository.
- System-1 selects a capability and model from configured modality, context capacity, domain, quality, and optional `--model` override. Layla is the local keyword classifier; Jev is an optional classifier endpoint.
- A ticket carries workspace path, source commit, permitted local capabilities, gateway allowance, model, and token/USD allocations. Admission reserves quota atomically; the dispatcher claims a pending ticket atomically before launching its worker.
- A worker may run local shell commands only through Linux bubblewrap. The task clone is its only persistent writable mount; `/tmp` is temporary, system runtime directories are read-only, and the command has no network namespace access. The controller makes the authorized model gateway calls outside that boundary.
- The worker can iterate through edits and tests, then records commands, test results, a diff, unresolved failures, usage, and a final status. A gateway `402` or `429` halts the ticket.
- Running tickets hold a five-minute claim lease renewed by the worker. A dead child is halted immediately; after an unobserved controller crash, `gh0st resume TASK_ID` recovers an expired claim. Recovery records a halted report and never replays the worker automatically.
- External pushes, production writes, and non-gateway network actions are grant requests in the ledger. An operator can approve or reject a request locally; this release does not execute external actions after approval.
- A self-hosted WebSocket broker routes opaque encrypted frames. One-time pairing URIs carry a 256-bit secret that stays on the daemon and mobile client. Paired clients can read task status, submit bounded task requests, and record grant decisions; the broker does not see those payloads. Pairings expire after five minutes and are not reused after a disconnect.
- The CLI, loopback API, browser dashboard, VS Code extension, Tauri desktop app, and mobile pairing client provide control surfaces. Remaining release work includes OS push delivery, a hosted broker deployment, persistent multi-device pairing, physical mobile validation, and publishing signed desktop/mobile packages.

## Requirements

- Linux with Python 3.12+, Git, and `bubblewrap` (`bwrap`) available on `PATH` for local action execution
- A configured OpenAI-compatible `/chat/completions` gateway, such as LiteLLM Proxy or OpenRouter
- A gateway response with `usage.total_tokens` and `usage.cost_usd` (or top-level `cost_usd`) for every completion
- `cryptography` for encrypted relay pairing; the mobile client uses `@noble/ciphers` and `@noble/hashes`

```bash
python -m pip install -e .
```

Configure a model with known capacity and quality. The simple single-model setup is:

```bash
export GH0ST_GATEWAY_URL=http://127.0.0.1:4000/v1
export GH0ST_DEFAULT_MODEL=your-litellm-model-alias
export GH0ST_DEFAULT_MODEL_CONTEXT=128000
export GH0ST_DEFAULT_MODEL_PAYLOAD=100000
export GH0ST_DEFAULT_MODEL_QUALITY=frontier
export GH0ST_GATEWAY_API_KEY=your-gateway-key
```

For multiple targets, set `GH0ST_MODEL_CATALOG` to a JSON array or the path to one. Each target specifies `name`, `gateway_model`, `modalities`, `context_window`, `max_payload_tokens`, `quality` (`basic` or `frontier`), optional `domains`, `cost_rank`, and `placement`. High-complexity work requires a frontier target unless the operator explicitly uses `--model`. The override still has to satisfy modality and context capacity.

Catalog quality and capacity values are operator-maintained configuration; gh0st does not independently evaluate model quality. The selector can filter vision and image targets, but image attachments and image-generation transport are not implemented in the current worker.

The SQLite ledger defaults to `~/.gh0st/state.db`; task workspaces default to `~/.gh0st/workspaces`. Use `--db-url` and `--workspace-root` before a CLI subcommand, or set `GH0ST_WORKSPACE_ROOT` for the API. `GH0ST_SANDBOX_RUNTIME` may name a trusted Python environment to mount read-only at `/runtime` for tests and linters. The worker never mounts the source checkout or host home into its local command process.

## CLI

```bash
gh0st run "Fix the failing parser test" --source-repo /path/to/repo --budget 3.50 --tokens 50000
gh0st run "Fix the failing parser test" --source-repo /path/to/repo --model code-target
gh0st status task_id_from_previous_command
gh0st resume task_id_from_previous_command
gh0st grant grant_id --approve --operator "Matthew Judge"
gh0st api --host 127.0.0.1 --port 8383
gh0st pair --relay-url wss://relay.example.com --device-id my-linux --qr-output /tmp/gh0st-pair.svg
```

The `grant` command records an operator decision. It does not launch a remote push, production write, or other external action. Local `git commit` checkpoints within the task clone are permitted when the clone has a configured author identity.

## Self-hosted relay

Create a `devices.json` file containing a device ID and a long random token, for example `{"my-linux":"<random-token-of-at-least-32-characters>"}`. Keep that file private. Start `gh0st-broker --devices devices.json` on loopback behind a TLS reverse proxy, or provide `--cert` and `--key` when binding it to an external interface. The daemon always initiates the outbound connection:

```bash
export GH0ST_RELAY_URL=wss://relay.example.com
export GH0ST_RELAY_TOKEN=<same-token-as-devices.json>
export GH0ST_DEVICE_ID=my-linux
export GH0ST_RELAY_SOURCE_REPO=/path/to/approved/repo
gh0st-relay --device-id my-linux
gh0st pair --relay-url "$GH0ST_RELAY_URL" --device-id my-linux
```

The command prints a one-time `gh0st://pair?...` URI. With `--qr-output`, it also writes an SVG QR code; scan it with the mobile app camera or paste the URI into the pairing field. The URI and QR file contain the end-to-end secret; do not give them to the broker. A paired session supports encrypted status reads, bounded task dispatch, and grant approval or rejection. Remote dispatch uses only `GH0ST_RELAY_SOURCE_REPO` as its source repository; without it, remote requests create scratch tasks. `GH0ST_RELAY_MAX_BUDGET_USD` and `GH0ST_RELAY_MAX_TOKENS` cap remote requests (defaults: $5 and 100,000 tokens). Broker offers live in memory only, and a disconnected client needs a new pairing URI. Grant decisions are ledger records; they do not execute the external action.

The browser dashboard is at `http://127.0.0.1:8383/ui`. It uses the address and port from which it was opened. The API is intended for loopback use. Set `GH0ST_API_TOKEN` to require a bearer token; the dashboard accepts it in the token field and holds it in memory for the current tab only.

The VS Code extension in `interfaces/vscode` shows recent tasks, their tickets, and pending grant requests in the Explorer. Its Run command clones the active local workspace into a task-scoped workspace. Configure task budgets through `gh0st.maxBudgetUsd` and `gh0st.maxBudgetTokens`. If the local API has a token, use **gh0st: Set Local API Token** to save it in VS Code SecretStorage. Grant context actions record a ledger decision after operator confirmation.

The Tauri desktop app in `interfaces/tauri` uses a Rust bridge restricted to the local daemon on `127.0.0.1`. It lists tasks, shows quota and worker reports, dispatches repository tasks, and records grant decisions. The API token is entered for the current app session and is not saved. The Linux release build has produced Debian and AppImage bundles; rebuild on a Linux machine with the [Tauri system prerequisites](https://v2.tauri.app/start/prerequisites/) and Rust installed using `cd interfaces/tauri && pnpm install && pnpm build`.

```bash
curl -X POST http://127.0.0.1:8383/v1/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Fix the parser test","source_repo":"/path/to/repo","max_budget_usd":3.5,"max_budget_tokens":50000}'
```

`GET /v1/tasks` lists up to 100 recent tasks. `GET /v1/status?task_id=...` returns status, quota, tickets, reports, and grant requests. `POST /v1/grants/{grant_id}/decision` records an approval or rejection. See [the OpenAPI contract](docs/openapi.yaml).

## Budget behavior

gh0st reserves ticket allocations before dispatch and limits each gateway call by the ticket's remaining token allocation and selected model context window. Reports charge gateway-reported usage in the same transaction that updates ticket status and releases the reservation. Missing or invalid usage fails the ticket. A gateway `402` or `429` produces a halted report. Configure key-level hard USD limits in the gateway: gh0st can record and stop after reported usage, but it cannot prevent a provider from charging for an already completed call.

If a worker exits without a report, its gateway usage cannot be verified. Recovery moves the full allocation from `reserved` to `uncertain` budget and excludes it from future admission. The status API and CLI show that uncertainty separately from confirmed consumption. Reconcile provider billing outside gh0st before treating that allocation as available again.

## Development

```bash
python -m pip install -e . pytest
python -m pytest -q
python -m benchmarks.admission
python -m benchmarks.routing
cd interfaces/mobile && pnpm install && pnpm typecheck && pnpm test:crypto
cd interfaces/vscode && node test/extension.test.js
cd interfaces/tauri && pnpm test
cd interfaces/tauri && pnpm install && pnpm build
```

The Linux tests require `bwrap` and exercise an edit–test–report loop, out-of-workspace write rejection, non-gateway network rejection, and protection of the source checkout. Other tests cover model selection, quota reservation, duplicate claims, ledger reporting, grants, and API behavior.
The admission benchmark measures the warm SQLite quota read and budget comparison on Linux. It exits nonzero if p95 latency across 1,000 samples is at least 1 ms. Use `--directory` to place its temporary database on the release filesystem. The durable ticket reservation transaction is separate from that preflight measurement. The routing benchmark measures the local Layla classifier interface and exits nonzero if p95 latency is at least 100 ms; it does not include model catalog selection or ledger writes.
The relay tests start a broker on loopback and verify authenticated pairing, encrypted task/status/grant messages, secret removal after pairing, and a shared Python/mobile encryption vector. A physical iOS/Android device and production TLS deployment remain separate validation gates.

## License

Apache License 2.0. See [LICENSE](LICENSE).
