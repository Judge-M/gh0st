# gh0st

An ephemeral, task-scoped runtime with local routing, a SQLite state ledger, and an OpenAI-compatible model gateway.

**Project:** [Judge-M/gh0st](https://github.com/Judge-M/gh0st)

**Author:** Matthew Judge (Judge-M) · matthew@cyberexperts.org

## Current implementation

- `gh0st.models` defines tickets, worker reports, quotas, and memory claims.
- `gh0st.db` persists tasks and reports with SQLAlchemy. Trusted, valid, promoted claims are the only claims included in worker context.
- `gh0st.router` uses a local deterministic classifier by default, attaches one tool schema, and checks the task quota before ticket creation. Ambiguous prompts enter a planner ticket.
- `gh0st.worker` executes one ticket per process, calls a configured OpenAI-compatible gateway, records its report, and exits. Planner proposals re-enter the router with depth and fan-out limits.
- `gh0st.cli` runs queued tickets in separate worker processes and reports task status.
- `gh0st.relay` exposes a loopback control API and an optional outbound WebSocket transport class. The API serves a basic dashboard at `/ui`.
- `interfaces/vscode` contains a local API extension. `interfaces/mobile` contains an Expo relay client prototype.

The model may return tool-call proposals, but this release does **not** execute file patches, Git writes, or tests on the model's behalf. The outbound relay transport requires a separately hosted broker. Pairing, authenticated remote commands, push delivery, and end-to-end encryption are not implemented in this repository. The mobile client cannot control a machine until those services are built and configured. `interfaces/tauri/index.html` is a dashboard frontend; a packaged Tauri desktop application is not included.

## Requirements

- Python 3.12+
- An OpenAI-compatible `/chat/completions` gateway

```bash
python -m pip install -e .
```

Set the gateway model and URL before running a task:

```bash
export GH0ST_GATEWAY_URL=http://127.0.0.1:4000/v1
export GH0ST_DEFAULT_MODEL=your-gateway-model
export GH0ST_GATEWAY_API_KEY=your-gateway-key
```

The SQLite database defaults to `~/.gh0st/state.db`. Pass `--db-url sqlite:///path/to/state.db` before a CLI subcommand to use another location.

## CLI

```bash
gh0st run "Refactor code" --budget 3.50 --tokens 50000
gh0st status task_id_from_previous_command
gh0st api --host 127.0.0.1 --port 8383
```

The API dashboard is available at `http://127.0.0.1:8383/ui`. The control API is designed for loopback use. Set `GH0ST_API_TOKEN` to require a bearer token; when set, clients must send `Authorization: Bearer <token>`.

API requests use `max_budget_usd` and `max_budget_tokens`:

```bash
curl -X POST http://127.0.0.1:8383/v1/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Refactor code","max_budget_usd":3.5,"max_budget_tokens":50000}'
```

`GET /v1/status?task_id=...` returns the task state, remaining quota, and tickets. See [the OpenAPI contract](docs/openapi.yaml).

## Budget behavior

Admission rejects inactive or exhausted quotas. Before a gateway call, the worker tokenizes its prompt locally and limits requested output to the remaining ticket token allocation. Gateway-reported token and cost usage is stored in the quota ledger. Because gateways use different pricing and may omit cost data, a USD cap cannot be guaranteed for a live call without a gateway that enforces it; configure a spend limit there as well.

## Development

```bash
python -m pip install -e . pytest
python -m pytest -q
```

The integration suite covers routing, trusted memory, quota rejection, planner re-entry, the local API, and a real worker subprocess against a local gateway stub.

## License

Apache License 2.0. See [LICENSE](LICENSE).
