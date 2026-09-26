<p align="center">
  <img src="docs/gh0st-hero.svg" alt="gh0st — local-first AI task execution" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Judge-M/gh0st/actions/workflows/linux-release-gates.yml"><img src="https://github.com/Judge-M/gh0st/actions/workflows/linux-release-gates.yml/badge.svg" alt="Linux release gates"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="Apache 2.0"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-3776AB.svg" alt="Python 3.12+"></a>
  <a href="https://github.com/Judge-M/gh0st"><img src="https://img.shields.io/badge/status-active%20development-orange.svg" alt="Active development"></a>
</p>

<p align="center">
  <strong>Give AI a safe workbench.</strong><br>
  gh0st runs each task in its own workspace, checks the work, and keeps external actions under your control.
</p>

<p align="center">
  <a href="#-try-it">Try it</a> ·
  <a href="#-how-it-works">How it works</a> ·
  <a href="#-choose-your-interface">Interfaces</a> ·
  <a href="#-for-developers">Developers</a> ·
  <a href="#-security-model">Security</a>
</p>

---

## 👻 Meet gh0st

AI coding tools are most useful when they can actually do the work: edit files, run tests, learn from failures, and show you what changed. gh0st gives them a **separate, task-scoped workbench** for that job.

<table>
  <tr>
    <td width="50%"><strong>🧰 A workspace for every task</strong><br>Work happens in a separate repository clone. Your original checkout stays outside the worker's write boundary.</td>
    <td width="50%"><strong>🧪 A real feedback loop</strong><br>Workers can edit, run tests, inspect failures, and report the diff and results.</td>
  </tr>
  <tr>
    <td><strong>🧭 Routing with choice</strong><br>Choose smart, cost, quality, speed, or local routing—or pin a model yourself.</td>
    <td><strong>🔐 You keep the keys</strong><br>External actions become grant requests for an operator to review.</td>
  </tr>
</table>

### The flow

```mermaid
flowchart LR
    A[Your task] --> B[Route and budget]
    B --> C[Isolated task clone]
    C --> D[Edit · run · verify]
    D --> E[Report and diff]
    C -. external action .-> F{Operator grant}
```

## 🚀 Try it

**Needs:** Linux, Python 3.12+, Git, and `bubblewrap` (`bwrap`). Add a supported model provider and credentials.

```bash
python -m pip install "gh0st-core[openai]"

export OPENAI_API_KEY=your-provider-key
export GH0ST_DEFAULT_MODEL=openai:gpt-4o-mini
export GH0ST_DEFAULT_MODEL_CONTEXT=128000
export GH0ST_DEFAULT_MODEL_PAYLOAD=100000
export GH0ST_DEFAULT_MODEL_QUALITY=frontier

gh0st run "Fix the failing parser test" \
  --source-repo /path/to/repo \
  --budget 3.50 \
  --tokens 50000
```

The worker operates in a task clone and returns a report with its status, commands, test results, diff, and any unresolved failures.

## 🖥️ Choose your interface

| Interface | What it’s for |
| :--- | :--- |
| **Terminal** | Run tasks, inspect reports, resume work, and review grants. |
| **Browser dashboard** | Submit tasks and explore status, diffs, tests, and grants in a local web UI. |
| **Python SDK** | Embed one-turn task execution in your own Python tools and services. |
| **VS Code** | Launch work from your workspace and inspect tasks and grants in the Explorer. |
| **Desktop app** | Use the Tauri client with the local gh0st daemon. |
| **Mobile pairing** | Reach a self-hosted relay from a paired phone. |

Start the browser UI on loopback:

```bash
gh0st api --host 127.0.0.1 --port 8383
```

Then open [127.0.0.1:8383/ui](http://127.0.0.1:8383/ui). The dashboard is built with the checked-in shadcn preset and served locally by the Python package. Add `GH0ST_API_TOKEN` to require a bearer token.

<details>
  <summary><strong>Work on the dashboard</strong></summary>
  <br>

  ```bash
  cd interfaces/web/gh0st-web
  npm ci
  npm run dev
  ```

  Run `npm run build:local` to rebuild the static UI bundled in the Python package.
</details>

## 🐍 For developers

```python
from gh0st import Gh0stClient

client = Gh0stClient(
    db_url="sqlite:///workspace.db",
    workspace_root="./gh0st-workspaces",
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
    print(report.status, report.diff)
```

Install another provider with an optional extra, for example `gh0st-core[anthropic]`, `gh0st-core[google]`, or `gh0st-core[ollama]`. Direct provider calls are the default; an OpenAI-compatible gateway is optional.

<details>
  <summary><strong>Routing, models, and images</strong></summary>
  <br>

  Choose `smart`, `cost`, `quality`, `speed`, or `local`. Routing considers capability, modality, domain, quality floor, context and payload capacity, latency, and configured price. A `--model` override pins a catalog model and still checks modality and context fit.

  Configure multiple targets through `GH0ST_MODEL_CATALOG` (JSON or a path to JSON). Catalog quality and capacity are operator-provided settings; gh0st does not independently benchmark model quality. Unknown prices are blocked unless explicit rates are configured.

  Attach PNG, JPEG, or WebP inputs with repeatable `--image` flags, the SDK's `image_paths`, or the API's `image_paths`. Inputs are copied outside the editable clone. Image generation is disabled by default and needs an explicitly configured price.
</details>

<details>
  <summary><strong>Secrets and reference memory</strong></summary>
  <br>

  Provider secrets can come from environment variables, files, HashiCorp Vault, or 1Password Connect. Set `GH0ST_SECRETS_PROVIDER` to `env`, `file`, `vault`, or `onepassword` and install `gh0st-core[onepassword]` for the 1Password Connect SDK.

  Human-approved governance claims in SQLite are injected as system constraints. Retrieved Mem0, Pinecone, or Qdrant material is kept separate, labeled unverified, and passed only to the current ticket. Install the matching extra (`gh0st-core[mem0]`, `[pinecone]`, or `[qdrant]`) and configure `GH0ST_REFERENCE_PROVIDER` plus that service's connection settings.
</details>

## 🛡️ Security model

| Inside the task workspace | Outside the workspace |
| :--- | :--- |
| Read and edit task files, run local commands and tests, create local commits. | Remote pushes, production writes, and non-gateway network actions are gated as grant requests. |

On Linux, local commands run through bubblewrap. The task clone is the only persistent writable mount; `/tmp` is temporary, runtime directories are read-only, and the command has no network namespace access. Model requests use the configured provider or optional gateway in-process.

**Workers never write to the source checkout.** A grant records an operator's decision; it does not itself perform the external action.

Budgets are reserved before dispatch and clamp model calls to the ticket's remaining tokens and selected model context. If a worker exits without verified usage, the allocation is marked uncertain and is not made available again automatically.

## 📱 Mobile and relay

Run the relay locally or self-host the broker on a VPS with Docker Compose and TLS. Pairing uses a one-time `gh0st://pair?...` URI or QR code; the broker routes encrypted frames. **Managed third-party hosting is a possible paid feature for later and is not available now.**

See the [relay deployment guide](docs/relay-deployment.md) for local and VPS setup, and the [mobile release checklist](docs/mobile-release.md) for physical-device and store-release gates.

## 🧪 Develop and test

```bash
python -m pip install -e . pytest
python -m pytest -q
python -m benchmarks.admission
python -m benchmarks.routing
```

The Linux integration suite exercises isolated edit–test–report execution, workspace boundaries, network restrictions, and source checkout protection. Other tests cover routing, budgets, duplicate claims, reports, grants, and the API.

## 🗺️ What’s next

- Validate mobile reconnect and revocation on physical iOS and Android devices.
- Complete signed desktop and mobile package distribution.
- Keep local and self-hosted relay available; managed hosting is future work.

## 📄 License

[Apache License 2.0](LICENSE)

<p align="center"><sub>Local work. Verifiable results. Operator-controlled boundaries.</sub></p>
