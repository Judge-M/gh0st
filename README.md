# gh0st

An ephemeral, task-scoped execution SDK and CLI with local routing, an SQLite state ledger, and in-process model-provider adapters.

**Project:** [Judge-M/gh0st](https://github.com/Judge-M/gh0st)

**Author:** Matthew Judge (Judge-M) · matthew@cyberexperts.org

## Current implementation

- Each repository task runs against a separate local clone at a recorded source commit. The clone has independent Git objects and no configured remote. Scratch tasks receive an empty task repository.
- The `gh0st-core` Python distribution provides the importable SDK and the `gh0st`, `gh0st-broker`, and `gh0st-relay` entry points. Layla is the local keyword classifier; Jev is an optional classifier endpoint.
- System-1 supports `smart`, `cost`, `quality`, `speed`, and `local` routing policies. It considers capability, modality, domain, quality floor, context and payload capacity, latency, and configured price. `--model` selects a fixed model ahead of the policy.
- A ticket carries workspace path, source commit, permitted local capabilities, gateway allowance, model, and token/USD allocations. Admission reserves quota atomically; the dispatcher claims a pending ticket atomically before launching its worker.
- A worker may run local shell commands only through Linux bubblewrap. The task clone is its only persistent writable mount; `/tmp` is temporary, system runtime directories are read-only, and the command has no network namespace access. Model requests run in-process through aisuite outside that shell boundary; an OpenAI-compatible gateway remains an explicit option.
- The worker can iterate through edits and tests, then records commands, test results, a diff, unresolved failures, usage, and a final status. A provider or gateway `402` or `429` halts the ticket.
- Running tickets hold a five-minute claim lease renewed by the worker. A dead child is halted immediately; after an unobserved controller crash, `gh0st resume TASK_ID` recovers an expired claim. Recovery records a halted report and never replays the worker automatically.
- External pushes, production writes, and non-gateway network actions are grant requests in the ledger. An operator can approve or reject a request locally; external actions remain gated.
- A self-hosted WebSocket broker routes opaque encrypted frames. One-time pairing URIs carry a 256-bit secret that stays on the daemon and mobile client. Paired clients can read task status, submit bounded task requests, and record grant decisions; the broker does not see those payloads. Pair offers expire after five minutes. Paired phones reconnect using a credential in OS secure storage; operators can list and revoke pairings locally.
- The CLI, loopback API, browser dashboard, VS Code extension, Tauri desktop app, and mobile pairing client provide control surfaces. Remaining release work includes OS push delivery, a hosted broker deployment, physical mobile reconnect/revocation validation, and publishing signed desktop/mobile packages.

## Requirements

- Linux with Python 3.12+, Git, and `bubblewrap` (`bwrap`) available on `PATH` for local action execution
- A supported aisuite provider and a configured model with known pricing; unknown prices are blocked unless explicit per-model rates are configured
- `cryptography` for encrypted relay pairing; the mobile client uses `@noble/ciphers` and `@noble/hashes`

```bash
python -m pip install ".[openai]"
# Or select another provider: ".[anthropic]", ".[google]", or ".[ollama]"
```

Configure a model with known capacity and quality. The simple single-model setup is:

```bash
export OPENAI_API_KEY=your-provider-key
export GH0ST_DEFAULT_MODEL=openai:gpt-4o-mini
export GH0ST_DEFAULT_MODEL_CONTEXT=128000
export GH0ST_DEFAULT_MODEL_PAYLOAD=100000
export GH0ST_DEFAULT_MODEL_QUALITY=frontier
```

Model identifiers use `provider:model` (for example `anthropic:claude-3-5-sonnet` or `openrouter:deepseek/deepseek-r1`). Provider secrets can come from environment variables, files, HashiCorp Vault, or 1Password Connect. Set `GH0ST_SECRETS_PROVIDER` to `env`, `file`, `vault`, or `onepassword` and configure that resolver's environment. Install `gh0st-core[onepassword]` for the 1Password Connect SDK. `--gateway-url` opts into an OpenAI-compatible gateway; aisuite direct calls are the default.

For multiple targets, set `GH0ST_MODEL_CATALOG` to a JSON array or the path to one. Each target specifies `name`, `gateway_model`, `modalities`, `context_window`, `max_payload_tokens`, `quality` (`basic` or `frontier`), optional `domains`, `cost_rank`, and `placement`. Image targets also require `image_generation_cost_usd`. High-complexity work requires a frontier target unless the operator explicitly uses `--model`. The override still has to satisfy modality and context capacity.

Catalog quality and capacity values are operator-maintained configuration; gh0st does not independently evaluate model quality. Attach local PNG, JPEG, or WebP files with repeatable `--image` flags, the SDK's `image_paths` argument, or the loopback API's `image_paths` field. Files are copied outside the editable clone into a task-scoped input directory and sent as data URLs to the selected vision model or gateway. Image generation is disabled by default; explicitly set `--modality image` (or `modality="image"` in the SDK/API) to opt into a configured image model. Configure an explicit `image_generation_cost_usd` for each image target; without it, gh0st rejects the request before dispatch. Generated PNGs are saved inside the task workspace and included in the report diff.

The SQLite ledger defaults to `~/.gh0st/state.db`; task workspaces default to `~/.gh0st/workspaces`. Use `--db-url` and `--workspace-root` before a CLI subcommand, or set `GH0ST_WORKSPACE_ROOT` for the API. `GH0ST_SANDBOX_RUNTIME` may name a trusted Python environment to mount read-only at `/runtime` for tests and linters. The worker never mounts the source checkout or host home into its local command process.

## Python SDK

```python
from gh0st import Gh0stClient, VaultSecretResolver

client = Gh0stClient(
    db_url="sqlite:///workspace.db",
    workspace_root="./gh0st-workspaces",
    secret_resolver=VaultSecretResolver(),
)
job = client.dispatch(
    "Refactor the auth middleware and run its tests",
    source_repo="/path/to/auth-service",
    budget_usd=5.00,
    budget_tokens=100_000,
    routing_strategy="smart",
)
if job.ticket is not None:
    report = client.execute_turn(job.ticket.ticket_id)
    print(report.status, report.diff, report.test_results)
```

`dispatch` creates a task-scoped clone and ticket; `execute_turn` runs the bounded worker and returns its report. Advanced integrations can import `EphemeralWorker`, `StateLedger`, `System1Router`, and the secret resolver interfaces directly from `gh0st`.
For a vision request, pass `image_paths=["/path/to/screenshot.png"]` to `dispatch`; this selects vision routing and copies the input alongside (outside) the editable task clone.

## Memory and retrieved reference material

Human-approved governance claims in the SQLite ledger are injected as **System Instructions / Constraints**. External retrieval results are a separate, unverified context channel and are not copied into SQLite or treated as instructions. Optional adapters are available with `pip install ".[mem0]"`, `pip install ".[pinecone]"`, or `pip install ".[qdrant]"`. Choose one with `GH0ST_REFERENCE_PROVIDER=mem0|pinecone|qdrant`:

- Mem0: `MEM0_API_KEY` and a tenant/project-specific `GH0ST_MEM0_USER_ID` are required.
- Pinecone: set `PINECONE_API_KEY` and `GH0ST_PINECONE_INDEX`; optionally set `GH0ST_PINECONE_NAMESPACE` and `GH0ST_PINECONE_TEXT_FIELD`.
- Qdrant: set `GH0ST_QDRANT_COLLECTION`; optionally set `GH0ST_QDRANT_URL` (default `http://localhost:6333`), `QDRANT_API_KEY`, `GH0ST_QDRANT_TEXT_FIELD`, and `GH0ST_QDRANT_VECTOR_NAME`.

Pinecone and Qdrant indexes must support text query embedding; Qdrant also requires a collection configured for text queries. `GH0ST_REFERENCE_LIMIT` caps results at 20 and `GH0ST_REFERENCE_MAX_CHARS` bounds the transient text injected into one ticket. Retrieved text is labeled with its provider and passed only to the current ticket.

## CLI

```bash
gh0st run "Fix the failing parser test" --source-repo /path/to/repo --budget 3.50 --tokens 50000
gh0st run "Describe this screenshot" --image ./screen.png
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

For a TLS-enabled Docker Compose deployment, see [the relay deployment guide](docs/relay-deployment.md), which covers loopback-only local hosting and a publicly reachable VPS setup.

```bash
export GH0ST_RELAY_URL=wss://relay.example.com
export GH0ST_RELAY_TOKEN=<same-token-as-devices.json>
export GH0ST_DEVICE_ID=my-linux
export GH0ST_RELAY_SOURCE_REPO=/path/to/approved/repo
gh0st-relay --device-id my-linux
gh0st pair --relay-url "$GH0ST_RELAY_URL" --device-id my-linux
```

The command prints a one-time `gh0st://pair?...` URI. With `--qr-output`, it also writes an SVG QR code; scan it with the mobile app camera or paste the URI into the pairing field. The URI and QR file contain the end-to-end secret; do not give them to the broker. After the first encrypted handshake, the phone stores its pairing in iOS Keychain or Android Keystore-backed storage and reconnects automatically. The daemon stores the pairing key encrypted with `~/.gh0st/relay.key` (override with `GH0ST_RELAY_KEY_FILE`); protect and back up that key with the state database. Losing the key requires pairing phones again. Run `gh0st pairings` to list paired phones and `gh0st unpair PAIR_ID` to revoke one. The broker keeps live routing state in memory; the daemon republishes active pair IDs when it reconnects. Remote dispatch uses only `GH0ST_RELAY_SOURCE_REPO` as its source repository; without it, remote requests create scratch tasks. `GH0ST_RELAY_MAX_BUDGET_USD` and `GH0ST_RELAY_MAX_TOKENS` cap remote requests (defaults: $5 and 100,000 tokens). Grant decisions are ledger records; they do not execute the external action.

The browser dashboard is at `http://127.0.0.1:8383/ui` (opening the server root redirects there). It uses the address and port from which it was opened. The API is intended for loopback use. Set `GH0ST_API_TOKEN` to require a bearer token; the dashboard accepts it in the token field and holds it in memory for the current tab only. The dashboard is built from `interfaces/web/gh0st-web` with the checked-in shadcn preset and shipped as static files in the Python package. For frontend development, run `npm ci && npm run dev` in that directory; `npm run build:local` exports and copies the static UI into the package.

The VS Code extension in `interfaces/vscode` shows recent tasks, their tickets, and pending grant requests in the Explorer. Its Run command clones the active local workspace into a task-scoped workspace. Configure task budgets through `gh0st.maxBudgetUsd` and `gh0st.maxBudgetTokens`. If the local API has a token, use **gh0st: Set Local API Token** to save it in VS Code SecretStorage. Grant context actions record a ledger decision after operator confirmation.

The Tauri desktop app in `interfaces/tauri` uses a Rust bridge restricted to the local daemon on `127.0.0.1`. It lists tasks, shows quota and worker reports, dispatches repository tasks, and records grant decisions. The API token is entered for the current app session and is not saved. The Linux release build has produced Debian and AppImage bundles; rebuild on a Linux machine with the [Tauri system prerequisites](https://v2.tauri.app/start/prerequisites/) and Rust installed using `cd interfaces/tauri && pnpm install && pnpm build`.

Mobile preview and production build profiles are configured in `interfaces/mobile/eas.json`. See the [mobile release checklist](docs/mobile-release.md) for EAS commands and the physical-device checks still required before store publication.

The broker container and TLS-enabled Compose deployment are defined in `Dockerfile.broker` and `compose.broker.yaml`; see the [relay deployment guide](docs/relay-deployment.md). CI builds the container and checks its command entry point without publishing an image.

Local and self-hosted Docker Compose relay setups are the current options. A gh0st-managed relay is a planned paid-tier service and is not available yet; it will require account entitlements and automatic device credential provisioning while preserving the end-to-end encrypted relay protocol.

```bash
curl -X POST http://127.0.0.1:8383/v1/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Fix the parser test","source_repo":"/path/to/repo","max_budget_usd":3.5,"max_budget_tokens":50000}'
```

`GET /v1/tasks` lists up to 100 recent tasks. `GET /v1/status?task_id=...` returns status, quota, tickets, reports, and grant requests. `POST /v1/grants/{grant_id}/decision` records an approval or rejection. See [the OpenAPI contract](docs/openapi.yaml).

## Budget behavior

gh0st reserves ticket allocations before dispatch and limits each model call by the ticket's remaining token allocation and selected model context window. Reports charge provider- or gateway-reported usage in the same transaction that updates ticket status and releases the reservation. Missing or invalid usage fails the ticket. A provider or gateway `402` or `429` produces a halted report. Configure key-level hard USD limits with the provider or gateway: gh0st records usage after a completed call and cannot prevent charges already incurred.

If a worker exits without a report, its model usage cannot be verified. Recovery moves the full allocation from `reserved` to `uncertain` budget and excludes it from future admission. The status API and CLI show that uncertainty separately from confirmed consumption. Reconcile provider billing outside gh0st before treating that allocation as available again.

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
The Linux release workflow in `.github/workflows/linux-release-gates.yml` runs the integration tests, the 1 ms quota and 100 ms router latency benchmarks, and an SDK wheel build on pushes and pull requests targeting `main`.
The admission benchmark measures the warm SQLite quota read and budget comparison on Linux. It exits nonzero if p95 latency across 1,000 samples is at least 1 ms. Use `--directory` to place its temporary database on the release filesystem. The durable ticket reservation transaction is separate from that preflight measurement. The routing benchmark measures the local Layla classifier interface and exits nonzero if p95 latency is at least 100 ms; it does not include model catalog selection or ledger writes.
The relay tests start a broker on loopback and verify authenticated pairing, encrypted task/status/grant messages, secret removal after pairing, and a shared Python/mobile encryption vector. A physical iOS/Android device and production TLS deployment remain separate validation gates.

## License

Apache License 2.0. See [LICENSE](LICENSE).
