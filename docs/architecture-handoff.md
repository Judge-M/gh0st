# Handoff Specification: `gh0st` Architecture & Implementation Plan (v3)

**Repository:** `https://github.com/Judge-M/gh0st`
**Architectural Blueprint:** Orchestrator-Free, Seam-Based Ephemeral Runtime ("No-Agent" Stack)
**Status:** Approved Specification (v3 - Multi-Surface UI & Pluggable Router Edition)

**Implementation note (2026-09-25):** The task-scoped Linux worker, SQLite ledger, multi-policy model router, in-process aisuite provider adapter, provider secret resolvers, SDK, interactive REPL, and optional Mem0/Pinecone/Qdrant retrieval adapters are implemented. Local PNG, JPEG, and WebP vision inputs are accepted through CLI, SDK, and loopback API and copied outside the editable clone into bounded task-scoped storage. Optional image generation is explicitly opt-in with `modality="image"`, requires an operator-configured per-image price, uses a configured provider image API, and writes validated PNG artifacts into the task workspace. A self-hosted broker and encrypted persistent pairing support mobile status, bounded task requests, reconnect, and recording grant decisions; pairing persistence is covered by tests, with physical-device reconnect/revocation still to validate. The VS Code extension, Tauri desktop app, Linux bundles, and mobile pairing client are present. On Ubuntu 24.04, the current suite passed 57 tests; quota preflight measured 0.2152 ms p95 and Layla routing 0.0185 ms p95. The Linux release workflow repeats tests, both latency gates, and wheel builds on pushes and pull requests. Remaining release gates are hosted relay deployment, OS push delivery, physical iOS/Android validation, and signed package publishing.

---

## 1. Executive Summary & Core Philosophy

`gh0st` is a hyper-lightweight, orchestrator-free runtime built on the principles of the **Seam** architecture, designed specifically to eliminate the overhead, token inflation, and maintenance complexity of traditional agent harnesses and persistent manager models.

### Key Architectural Tenets
1. **Zero Orchestrator Model:** Eliminates persistent manager LLMs (e.g., Claude Code, Codex, or manager agents maintaining 50k+ token conversation histories). There is no "orchestrator" holding state in memory.
2. **Sub-100ms System-1 Gateway Router:** Replaces open-ended LLM planning with a fast, single-pass classifier using an **Ontological Decision Tree (OAG)**. Default router is **Layla** (local, $0 spend), with a pluggable interface supporting **Jev** or future specialized classification models.
3. **Purely Ephemeral Workers:** Execution workers are short-lived, task-scoped, serverless Python processes (`python3 worker.py`). A worker wakes up from a database/queue event, reads its ticket state, executes a bounded task, writes its result (`WorkerReport`) back to the database, and **immediately terminates (`sys.exit(0)`)**.
4. **Short-Lived Process Execution:** Eliminates persistent manager processes. Local workbench commands execute inside a Linux process and mount boundary with no network access, using a task-scoped clone as the only persistent writable mount.
5. **Pluggable Gateway Shell Architecture:** Offloads model provider translations, tool calling schema syntax, streaming protocols, vendor API churn, and provider failover to established external gateways (e.g., LiteLLM, OpenRouter, FastMCP / MCP Proxy). `gh0st` operates as an intelligent **System-1 Routing & Governance Shell** around these pluggable gateways.
6. **Multi-Surface Client Strategy with Relay Control:** Provides unified operator interfaces across CLI, IDE extensions, Desktop GUI, and Mobile App. Mobile/remote control operates via an **SSH/Tailscale-Free Cloud Relay Tunnel** (Outbound WebSocket / gRPC pairing) similar to Claude Code / Codex remote pairing, requiring zero open ports or VPN setups.

---

## 2. System Architecture & Component Diagram

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Multi-Surface Client Layer                                                            │
│  [ CLI ]   [ IDE Extension ]   [ Desktop GUI (Tauri) ]   [ Mobile App (iOS/Android) ]  │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │
                                           │ Encrypted WebSocket / gRPC Relay
                                           ▼ (SSH / Tailscale-Free Pairing)
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  System-1 Routing Shell (API Gateway + Pluggable Router)                               │
│  • Router Core: Default Layla (Local, $0 spend) | Pluggable: Jev / Custom Classifier   │
│  • Sub-100ms Ontological Decision Tree (OAG)                                           │
│  • Pre-flight Budget Gate & Quota Check (`quota_records`)                              │
│  • Prunes tool schemas down to 1-schema per turn                                       │
│  • Generates Approved Seam Ticket Payload                                              │
└───────────────────────────┬────────────────────────────────────────────────────────────┘
                            │
                            │ 2. Dispatches Ticket to State Ledger
                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Central State Ledger & Queue (SQLite / PostgreSQL)                                    │
│  • Tables: tasks, tickets, worker_reports, quota_records, memory_claims, paired_relays │
└───────────────────────────┬────────────────────────────────────────────────────────────┘
                            │
                            │ 3. Triggers Ephemeral Worker
                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Ephemeral Worker Runtime (`python3 worker.py`)                                         │
│  • Reads Ticket payload & context slice from State Ledger                               │
│  • Executes local workbench actions (File I/O, refactoring, lints)                     │
│  • Outbound LLM / Tool calls pass through Pluggable Gateway                            │
│  • Emits WorkerReport & Child Ticket proposals to State Ledger                         │
│  • Calls sys.exit(0) -> 100% Instant Kernel Memory Reclamation                         │
└───────────────────────────┬────────────────────────────────────────────────────────────┘
                            │
                            │ 4. OpenAI-Compatible / MCP API Requests
                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Pluggable External Gateway Layer                                                      │
│  (LiteLLM Proxy / OpenRouter / FastMCP / Local vLLM)                                   │
│  • Handles model provider translations (OpenAI, Anthropic, Gemini, etc.)                │
│  • Manages API keys, rate limits, retries, and provider failovers                      │
│  • Brokers external MCP tool execution & protocol translation                          │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Pluggable System-1 Router Architecture (Layla Default)

`gh0st` defines an abstract interface for System-1 routing, allowing operators to swap or upgrade classifier models without changing task execution, database ledger, or worker code.

```python
# System-1 Router Interface Abstraction (gh0st/router.py)
from abc import ABC, abstractmethod

class System1Classifier(ABC):
    """Abstract base class for System-1 classification models."""

    @abstractmethod
    def classify(self, prompt: str, templates: List[CapabilityTemplate]) -> System1Decision:
        """Classifies prompt intent into capability, complexity tier, and placement in <100ms."""
        pass

class LaylaClassifier(System1Classifier):
    """
    DEFAULT ROUTER: Local, zero-spend, sub-50ms classifier.
    Runs locally on CPU/GPU or lightweight local model (Layla engine).
    Requires $0 external token costs.
    """
    def classify(self, prompt: str, templates: List[CapabilityTemplate]) -> System1Decision:
        # Fast local keyword / embedding decision tree
        ...

class JevClassifier(System1Classifier):
    """
    PLUGGABLE ROUTER: High-precision cloud / specialized classification model (Jev engine).
    Used for enterprise or complex multi-domain disambiguation.
    """
    def classify(self, prompt: str, templates: List[CapabilityTemplate]) -> System1Decision:
        # Specialized Jev router completion call
        ...
```

### Router Model Selection Strategy
* **Default (`Layla`):** Selected out-of-the-box. Operates 100% locally on standard hardware, guaranteeing zero token costs for task routing and sub-50ms latency.
* **Pluggable (`Jev` / Custom):** Enabled via environment variable (`GH0ST_ROUTER_MODEL=jev` or `GH0ST_ROUTER_ENDPOINT=...`). Provides higher-precision classification for ambiguous, domain-heavy enterprise workflows.

---

## 4. Multi-Surface UI & SSH/Tailscale-Free Relay Control Architecture

`gh0st` supports a multi-surface operator workflow designed to manage both local developer runs and remote cloud/headless instances seamlessly.

### Supported UI Surfaces
1. **CLI (`gh0st run`, `gh0st status`):** Primary developer surface for terminal-native execution and CI/CD pipelines.
2. **IDE Extensions (VS Code / JetBrains):** In-editor workspace panel showing live task progress, ticket status, and inline diff previews.
3. **Desktop GUI (Tauri / Electron):** Standalone visual dashboard for managing workspace tasks, inspecting `quota_records`, and managing human-gated `MemoryClaim` records.
4. **Mobile App (iOS / Android):** Companion app for remote monitoring, real-time push notifications, human-in-the-loop budget gate approvals, and triggering remote task runs.

### SSH/Tailscale-Free Cloud Relay Architecture

To control local or cloud `gh0st` instances from the mobile app or web interface without requiring complex networking, VPNs, or open inbound firewall ports, `gh0st` implements an **Outbound Relay Pairing Mechanism** (similar to Claude Code / OpenAI Codex remote pairing):

```text
[ Mobile App / Remote Web UI ]
          │
          │ 1. HTTPS / WSS Webhook (Paired via QR Code / 6-digit Code)
          ▼
┌────────────────────────────────────────────────────────┐
│  `gh0st` Cloud Relay Broker (`relay.gh0st.ai` / Self-Host)│
│  • Lightweight message router & pairing registry        │
│  • Holds zero state or sensitive code                  │
└──────────────────────────▲─────────────────────────────┘
                           │
                           │ 2. Outbound Encrypted WebSocket (WSS)
                           │    (Persistent reverse tunnel from local daemon)
                           │
┌──────────────────────────┴─────────────────────────────┐
│  Local / Remote `gh0st` Runtime Daemon                  │
│  • Initiates outbound connection on startup            │
│  • Receives remote prompt triggers & approval requests │
│  • Streams stdout/stderr & WorkerReports back to Relay │
└────────────────────────────────────────────────────────┘
```

#### Key Relay Features
* **Zero Open Ports:** The `gh0st` daemon initiates an **outbound** persistent WebSocket/gRPC connection to the relay broker. NATs, home routers, and corporate firewalls do not block inbound traffic because no inbound ports are opened.
* **No SSH / Tailscale Required:** Eliminates configuring SSH keys, managing Tailscale networks, or opening firewall ports.
* **Zero-Trust Encrypted Pairing:** Pairing between a mobile app and a `gh0st` instance occurs via a single-use 6-digit PIN or QR code, establishing end-to-end encrypted messaging (`E2EE`) over the relay.

---

## 5. Sub-100ms System-1 Gateway & Ontological Decision Tree (OAG)

System-1 operates as an upfront **Governance & Routing Shell** that traverses a deterministic 3-hop decision tree focused strictly on **Capability Matching** and **Budget Constraints**.

### The 3-Hop Decision Flow
1. **Hop 1 — Capability Matching (What needs to be done?):**
   * Classifies user prompt intent against a static **Template Registry**.
   * Selects the corresponding Seam `Ticket` template payload (`objective`, `constraints`, `capabilities`).
   * **1-Schema Rule:** Prunes the global tool registry down to **exactly 1 tool schema per worker turn**, protecting worker context windows.
2. **Hop 2 — Performance & Complexity Tiering (How hard is it?):**
   * Classifies reasoning depth:
     * *Low Complexity:* Formatting, syntax checks, basic file lookups $\rightarrow$ assigned to low-cost or local model endpoints.
     * *High Complexity:* Multi-file refactoring, bug diagnosis $\rightarrow$ assigned to high-capability cloud/GPU model endpoints.
3. **Hop 3 — Budget Gate & Gateway Routing (Where is it cheapest?):**
   * Inspects active task budget in `quota_records`.
   * Maps required performance tier to configured pluggable gateway target (e.g., Local vLLM endpoint $\rightarrow$ Rented GPU $\rightarrow$ Cloud API via LiteLLM/OpenRouter).
   * **Fail-Closed:** Over-budget requests halt in <1ms without launching worker processes.

### Confidence Thresholds
* **High Confidence ($\ge 0.90$):** Gateway packages the Seam `Ticket` and pushes it directly onto the central queue.
* **Low Confidence ($< 0.90$):** Gateway routes the prompt to an ephemeral **Planner Worker** to generate a structured sub-task list, maintaining execution quality without needing an orchestrator.

---

## 6. Pluggable External Gateway Layer Integration

By positioning `gh0st` as a System-1 Shell around existing gateways, `gh0st` offloads all low-level API maintenance while maintaining strict operational control.

### Responsibilities Split

| Function | Handled by `gh0st` System-1 Shell | Handled by Pluggable Gateway (LiteLLM / OpenRouter / MCP) |
| :--- | :--- | :--- |
| **Model Provider Adapters** | ❌ None (Vendor-Agnostic) | ✅ Anthropic, OpenAI, Gemini, DeepSeek, vLLM, Bedrock |
| **Tool Translation & Schemas**| ✅ Prunes to 1-schema per turn | ✅ Formats function calling JSON across different vendor schemas |
| **Pre-Flight Task Budgets** | ✅ Enforces `quota_records` & ticket caps | ❌ Unaware of task lineage or ticket context |
| **API Keys & Rate Limits** | ❌ No vendor key storage in worker | ✅ Secure key vault, provider retries, and rate-limit backoffs |
| **Child Ticket Re-Entry** | ✅ Validates depth & fan-out caps | ❌ Unaware of DAG / sub-task relationships |
| **Provider Failover & Load Balancing** | ❌ Offloaded | ✅ Fallbacks (e.g., Claude 3.5 Sonnet $\rightarrow$ GPT-4o if down) |

---

## 7. Ephemeral Worker Runtime & Lifecycle

Workers in `gh0st` are strictly **ephemeral, stateless, and memory-blind**. They communicate with external models and tools by directing standardized OpenAI-compatible HTTP or MCP requests to the local pluggable gateway URL.

```python
# Conceptual Ephemeral Worker Loop (gh0st/worker.py)
import os
import sys
import httpx
from gh0st.db import StateLedger

def run_worker_turn(ticket_id: str):
    db = StateLedger()
    ticket = db.get_ticket(ticket_id)
    gateway_url = os.getenv("GH0ST_GATEWAY_URL", "http://localhost:4000/v1")

    try:
        # 1. Execute local work (File I/O, script execution, refactoring)
        local_context = execute_local_workbench(ticket)

        # 2. Call pluggable gateway for model completion if required
        if ticket.requires_model_call:
            client = httpx.Client(base_url=gateway_url)
            response = client.post("/chat/completions", json={
                "model": ticket.assigned_model,
                "messages": [{"role": "user", "content": ticket.build_prompt(local_context)}],
                "tools": ticket.pruned_tool_schemas,  # Exactly 1 tool schema
                "max_tokens": ticket.remaining_budget_tokens
            })
            model_output = response.json()

        # 3. Write WorkerReport & Child Ticket proposals to DB
        db.save_report(
            ticket_id=ticket_id,
            status="success",
            output=model_output,
            child_tickets=extract_child_tickets(model_output)
        )
    except Exception as e:
        db.save_report(ticket_id=ticket_id, status="failure", error=str(e))
    finally:
        # 4. Terminate immediately - Kernel reclaims 100% of memory
        sys.exit(0)
```

### Worker Design Rules
* **No Long-Running Daemons:** Workers do not run loop processes waiting for user input.
* **Informational Least Privilege:** Workers receive *only* the specific context slice and 1 tool schema required for their ticket.
* **Deterministic Teardown:** Once the report is written, `sys.exit(0)` is invoked. Memory leaks and state pollution across turns are physically impossible.

---

## 8. Worker-Generated Tickets & Scaling Mechanisms

When a Planner worker decomposes a project or an execution worker discovers required sub-tasks, child tickets **must re-enter System-1** rather than executing directly.

### Rules for Worker-Generated Tickets
1. **Proposal Emission:** Workers emit child `Ticket` proposals as JSON inside their `WorkerReport`. They do not spawn processes directly.
2. **System-1 Re-Evaluation:** Child tickets pass through System-1 to check remaining parent `quota_records`, prune tool schemas (1-schema rule), and assign compute placement.
3. **Configurable Fan-Out Limits:**
   * `default_fan_out_cap`: 25 sub-tasks per turn.
   * `batch_fan_out_cap`: Up to 100 sub-tasks for bulk repository tasks / migrations.
4. **Hierarchical Decomposition:** Large projects break down into Milestone trees (e.g., Top-Level Planner $\rightarrow$ Domain Sub-Planners $\rightarrow$ Leaf Task Workers).
5. **Batching / Chunking:** Uniform operations across many files are batched (e.g., 8 workers handling 10 files each, rather than 80 containers).

---

## 9. Memory Ledger & Token Economy

`gh0st` keeps authoritative governance separate from retrieved material.

### Memory & Token Rules
* **System Rules & Governance:** Human-approved SQLite claims are included in a distinct system instruction message. Only valid claims with `is_trusted = TRUE` are eligible; untrusted or expired claims are excluded.
* **Retrieved Reference Context:** Optional Mem0, Pinecone, and Qdrant adapters query their configured service directly. Results are labeled as unverified reference data, are not written to SQLite, and do not acquire policy authority through retrieval.
* **Pre-Dispatch Token Clamping:** Prompts are tokenized locally (`tiktoken` / `cl100k_base` / `o200k_base`) before dispatch. `max_tokens` is clamped to the remaining task budget in `quota_records`.

---

## 10. Technology Stack & Implementation Roadmap

### Tech Stack
* **Language:** Python 3.12
* **Data & Schema Validation:** `pydantic` v2, `SQLAlchemy` / `Alembic`
* **Persistence:** SQLite (Zero-config local) / PostgreSQL (Production)
* **Networking & APIs:** `httpx`, `fastapi` / `uvicorn`, `websockets`
* **Tokenization:** `tiktoken`
* **Model Interface:** In-process aisuite adapters by default; OpenAI-compatible gateways remain an opt-in transport.
* **Remote Control Relay:** Reverse WebSocket / gRPC tunnel daemon (`gh0st-relay`)

### Implementation Milestones for `https://github.com/Judge-M/gh0st`

* [x] **Milestone 1: Database Schema & Protocol Contracts (`gh0st-milestone1-db.py`)**
  * Implemented `Ticket`, `WorkerReport`, `QuotaRecord`, and `MemoryClaim` Pydantic models & SQLAlchemy persistence.
* [x] **Milestone 2: Sub-100ms System-1 Gateway Shell & Pluggable Router (`gh0st-milestone2-router.py`)**
  * Implemented 3-hop OAG decision tree, `LaylaClassifier` default, 1-schema tool pruner, and budget checks.
* [x] **Milestone 3: Ephemeral Worker Runtime & Pluggable Gateway Adapter (`gh0st-milestone3-worker.py`)**
  * Implemented `EphemeralWorker` execution loop, `PluggableGatewayAdapter`, and child ticket re-entry handlers.
* [x] **Milestone 4: Multi-Surface CLI & Self-Hosted Relay Tunnel (`gh0st/cli.py`, `gh0st/relay.py`, `gh0st/broker.py`)**
  * Implemented the `gh0st` CLI (`run`, `status`, `resume`, `grant`, `api`, and `pair`).
  * Implemented an outbound WebSocket relay, authenticated self-hosted broker, encrypted pairing, bounded remote task control, and QR pairing.
* [x] **Milestone 5: IDE Extension & Desktop / Mobile App Integration**
  * Implemented the VS Code extension, Tauri desktop app and Linux bundle build, and mobile QR pairing/control client.
  * Remaining release gates: hosted broker deployment, OS push delivery, persistent multi-device pairing, physical mobile validation, and signed package publishing.
