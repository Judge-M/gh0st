import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest
import httpx
from fastapi.testclient import TestClient

from gh0st.catalog import ModelCatalog, ModelTarget
from gh0st.db import StateLedger, TicketRow
from gh0st.models import ChildTicketProposal, ComputePlacement, ExecutionTier, GrantRequest, QuotaRecord, TicketStatus, WorkerReport
from gh0st.cli import Gh0stCLI
from gh0st.relay import create_app
from gh0st.router import System1Router
from gh0st.sandbox import LinuxSandbox
from gh0st.worker import EphemeralWorker
from gh0st.workspace import WorkspaceManager


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def source_repository(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    git("init", "-q", cwd=source)
    git("config", "user.name", "Matthew Judge", cwd=source)
    git("config", "user.email", "matthew@cyberexperts.org", cwd=source)
    (source / "value.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (source / "test_value.py").write_text("from value import value\n\ndef test_value():\n    assert value() == 2\n", encoding="utf-8")
    git("add", ".", cwd=source)
    git("commit", "-qm", "Initial source", cwd=source)
    return source


def configured_ledger(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    monkeypatch.setenv("GH0ST_WORKSPACE_ROOT", str(root))
    catalog = ModelCatalog([ModelTarget(
        name="frontier", gateway_model="frontier",
        modalities={"text", "code"}, context_window=8192,
        max_payload_tokens=4096, quality="frontier", domains={"code_refactor"},
    )])
    db = StateLedger(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    db.init_db()
    db.create_task("task_one", "Fix code")
    db.create_quota_record(QuotaRecord(
        task_id="task_one", max_tokens_budget=2000, max_cost_usd_budget=2,
    ))
    return db, WorkspaceManager(str(root)), catalog


def test_task_clone_is_independent_and_original_is_unchanged(tmp_path):
    source = source_repository(tmp_path)
    git("remote", "add", "upstream", "https://example.invalid/upstream.git", cwd=source)
    source_head = git("rev-parse", "HEAD", cwd=source)
    manager = WorkspaceManager(str(tmp_path / "workspaces"))
    workspace, copied_head = manager.create("task_one", str(source))
    clone = Path(workspace)
    assert copied_head == source_head
    assert git("remote", cwd=clone) == ""
    assert (clone / "value.py").read_text(encoding="utf-8") == (source / "value.py").read_text(encoding="utf-8")
    (clone / "value.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    assert "return 1" in (source / "value.py").read_text(encoding="utf-8")
    assert git("rev-parse", "HEAD", cwd=source) == source_head


def test_state_ledger_close_releases_database_file(tmp_path):
    path = tmp_path / "closeable.db"
    ledger = StateLedger(f"sqlite:///{path.as_posix()}")
    ledger.init_db()
    ledger.create_task("task_close", "Close ledger")
    ledger.close()

    path.unlink()
    reopened = StateLedger(f"sqlite:///{path.as_posix()}")
    reopened.init_db()
    assert reopened.get_task("task_close") is None
    reopened.close()


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux resource limits")
def test_linux_sandbox_sets_a_process_count_limit(monkeypatch):
    import resource

    configured = {}
    monkeypatch.setattr(resource, "setrlimit", lambda limit, value: configured.update({limit: value}))
    LinuxSandbox._limits()
    soft, hard = configured[resource.RLIMIT_NPROC]
    assert 1 <= soft <= 128
    assert hard == soft


def test_atomic_claim_and_budget_reservation(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    assert ticket is not None
    reserved = db.get_quota_record("task_one")
    assert reserved.tokens_reserved == ticket.max_tokens_allocated
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: db.claim_ticket(ticket.ticket_id, "owner_one"), range(2)))
    assert sorted(claims) == [False, True]
    report = WorkerReport(ticket_id=ticket.ticket_id, task_id="task_one",
                          status="success", tokens_used=50, cost_usd=0.1)
    with pytest.raises(ValueError, match="claim"):
        db.save_worker_report(report, claim_owner="wrong_owner")
    db.save_worker_report(report, claim_owner="owner_one")
    final = db.get_quota_record("task_one")
    assert final.tokens_consumed == 50
    assert final.tokens_reserved == 0
    assert final.cost_usd_consumed == 0.1
    assert final.cost_usd_reserved == 0
    with pytest.raises(ValueError):
        db.save_worker_report(report, claim_owner="owner_one")
    assert db.get_quota_record("task_one").tokens_consumed == 50


def test_expired_claim_recovery_halts_once_and_preserves_unknown_spend(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    owner = "recovery_test"
    assert db.claim_ticket(ticket.ticket_id, owner)
    assert not db.recover_abandoned_ticket(ticket.ticket_id, "expired")
    assert db.renew_claim(ticket.ticket_id, owner)
    future = datetime.now(timezone.utc) + timedelta(seconds=db.CLAIM_LEASE_SECONDS + 1)
    assert db.recover_abandoned_ticket(ticket.ticket_id, "Worker claim expired", now=future)
    assert not db.recover_abandoned_ticket(ticket.ticket_id, "duplicate", now=future)
    assert not db.renew_claim(ticket.ticket_id, owner)
    assert db.get_ticket(ticket.ticket_id).status == TicketStatus.HALTED
    assert db.get_task("task_one").status == "halted"
    report = db.get_worker_report(ticket.ticket_id)
    assert report.status == "halted"
    assert report.output["usage"] == "unverified"
    quota = db.get_quota_record("task_one")
    assert quota.tokens_reserved == 0
    assert quota.cost_usd_reserved == 0
    assert quota.tokens_uncertain == ticket.max_tokens_allocated
    assert quota.cost_usd_uncertain == ticket.max_cost_usd
    assert quota.remaining_tokens == 0
    with pytest.raises(ValueError):
        db.save_worker_report(WorkerReport(
            ticket_id=ticket.ticket_id, task_id="task_one", status="success",
        ), claim_owner="recovery_test")


def test_worker_exit_without_report_is_recovered_immediately(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    def killed_worker(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 137, "", "killed")

    monkeypatch.setattr("gh0st.cli.subprocess.run", killed_worker)
    Gh0stCLI(db.db_url, str(manager.root)).process_queue("task_one")
    assert db.get_task("task_one").status == "halted"
    assert db.get_worker_report(ticket.ticket_id).output["usage"] == "unverified"


def test_worker_exit_before_execution_keeps_allocation_uncertain(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    monkeypatch.setattr("gh0st.cli.subprocess.run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, "", "import failed"))
    Gh0stCLI(db.db_url, str(manager.root)).process_queue("task_one")
    assert db.get_task("task_one").status == "halted"
    assert db.get_ticket(ticket.ticket_id).status == TicketStatus.HALTED
    assert db.get_worker_report(ticket.ticket_id).status == "halted"
    quota = db.get_quota_record("task_one")
    assert quota.tokens_reserved == 0
    assert quota.tokens_uncertain == ticket.max_tokens_allocated
    assert quota.remaining_tokens == 0


def test_duplicate_dispatch_does_not_start_second_worker(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    launches = []

    def first_worker(*args, **kwargs):
        launches.append(args[0])
        Gh0stCLI(db.db_url, str(manager.root)).process_queue("task_one")
        return subprocess.CompletedProcess(args[0], 137, "", "killed")

    monkeypatch.setattr("gh0st.cli.subprocess.run", first_worker)
    Gh0stCLI(db.db_url, str(manager.root)).process_queue("task_one")
    assert len(launches) == 1
    assert db.get_ticket(ticket.ticket_id).status == TicketStatus.HALTED
    assert db.get_quota_record("task_one").tokens_uncertain == ticket.max_tokens_allocated


def test_resume_recovers_claim_left_by_previous_process(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    assert db.claim_ticket(ticket.ticket_id, "planner_test_owner")
    with db.get_session() as session:
        session.get(TicketRow, ticket.ticket_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    Gh0stCLI(db.db_url, str(manager.root)).process_queue("task_one")
    assert db.get_ticket(ticket.ticket_id).status == TicketStatus.HALTED
    assert db.get_worker_report(ticket.ticket_id).output["usage"] == "unverified"
    assert db.get_quota_record("task_one").tokens_uncertain == ticket.max_tokens_allocated


def test_model_quality_capacity_and_explicit_override():
    catalog = ModelCatalog([
        ModelTarget(name="economy", gateway_model="cheap", modalities={"code"},
                    context_window=4096, max_payload_tokens=2048, quality="basic", cost_rank=1),
        ModelTarget(name="frontier", gateway_model="strong", modalities={"code", "text"},
                    context_window=16000, max_payload_tokens=12000, quality="frontier", cost_rank=5),
    ])
    assert catalog.select("code", 1000, 500, "code_refactor", ExecutionTier.HIGH_COMPLEXITY).name == "frontier"
    assert catalog.select("code", 1000, 500, "code_refactor", ExecutionTier.HIGH_COMPLEXITY, "economy").name == "economy"
    with pytest.raises(ValueError):
        catalog.select("vision", 1000, 500, "code_refactor", ExecutionTier.HIGH_COMPLEXITY)
    with pytest.raises(ValueError):
        catalog.select("code", 20000, 500, "code_refactor", ExecutionTier.HIGH_COMPLEXITY)


def test_model_routing_strategies_use_more_than_cost():
    catalog = ModelCatalog([
        ModelTarget(name="cheap", gateway_model="openai:cheap", modalities={"text"},
                    context_window=8192, max_payload_tokens=4096, quality="basic",
                    domains={"general"}, cost_rank=1, latency_rank=40,
                    placement=ComputePlacement.CLOUD_API),
        ModelTarget(name="fast-local", gateway_model="ollama:fast", modalities={"text"},
                    context_window=8192, max_payload_tokens=4096, quality="frontier",
                    domains={"general"}, cost_rank=5, latency_rank=1,
                    placement=ComputePlacement.LOCAL_GPU),
        ModelTarget(name="specialist", gateway_model="anthropic:strong", modalities={"text"},
                    context_window=8192, max_payload_tokens=4096, quality="frontier",
                    domains={"code_refactor"}, cost_rank=9, latency_rank=10,
                    placement=ComputePlacement.CLOUD_API),
    ])
    tier = ExecutionTier.LOW_COMPLEXITY
    select = lambda strategy: catalog.select("text", 100, 100, "code_refactor", tier,
                                             strategy=strategy).name
    assert select("smart") == "specialist"
    assert select("cost") == "cheap"
    assert select("quality") == "specialist"
    assert select("speed") == "fast-local"
    assert select("local") == "fast-local"
    assert catalog.select("text", 100, 100, "code_refactor", tier,
                          override="openai:cheap", strategy="speed").name == "cheap"
    with pytest.raises(ValueError, match="Unknown routing strategy"):
        catalog.select("text", 100, 100, "code_refactor", tier, strategy="random")


def test_interactive_session_persists_context_and_raises_budget(tmp_path):
    db = StateLedger(f"sqlite:///{(tmp_path / 'sessions.db').as_posix()}")
    db.init_db()
    db.create_task("task_session", "interactive")
    db.create_quota_record(QuotaRecord(task_id="task_session", max_tokens_budget=5000,
                                       max_cost_usd_budget=5))
    db.create_interactive_session("sess_test", "task_session", str(tmp_path / "workspace"), None)
    db.update_interactive_session("sess_test", selected_model="openai:gpt-test",
                                  routing_strategy="quality",
                                  history=[{"role": "user", "content": "first turn"}])
    session = db.get_interactive_session("sess_test")
    assert session["selected_model"] == "openai:gpt-test"
    assert session["routing_strategy"] == "quality"
    assert session["history"][0]["content"] == "first turn"
    db.set_session_cost_budget("sess_test", 10)
    assert db.get_quota_record("task_session").max_cost_usd_budget == 10
    db.clear_session_history("sess_test")
    assert db.get_interactive_session("sess_test")["history"] == []


class SequenceGateway:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete_turn(self, messages, attached_tool, max_tokens, model):
        return self.responses.pop(0)


def response(command=None, purpose="other", action="run_command", content=""):
    arguments = {"action": action, "command": command, "purpose": purpose}
    if action == "request_grant":
        arguments = {"action": action, "action_type": "git_push", "details": {"ref": "main"}}
    calls = [{"id": "call_1", "function": {"name": "local_workbench", "arguments": json.dumps(arguments)}}] if action else []
    return {"content": content, "tool_calls": calls, "total_tokens": 25, "cost_usd": 0.01}


def test_dispatch_claim_is_bound_to_its_worker(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    assert db.claim_ticket(ticket.ticket_id, "owner_one")
    assert not db.renew_claim(ticket.ticket_id, "owner_two")
    assert not db.recover_abandoned_ticket(
        ticket.ticket_id, "wrong worker", worker_exited=True,
        expected_owner="owner_two",
    )
    with pytest.raises(ValueError):
        EphemeralWorker(db, SequenceGateway([])).execute_turn(ticket.ticket_id,
                                                                claim_owner="owner_two")
    report = EphemeralWorker(db, SequenceGateway([response(action="request_grant")])).execute_turn(
        ticket.ticket_id, claim_owner="owner_one",
    )
    assert report.status == "halted"
    assert db.get_quota_record("task_one").tokens_reserved == 0


def test_external_action_is_logged_without_execution(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    report = EphemeralWorker(db, SequenceGateway([response(action="request_grant")])).execute_turn(ticket.ticket_id)
    assert report.status == "halted"
    assert report.commands_executed == []
    grants = db.list_grant_requests("task_one")
    assert len(grants) == 1
    assert grants[0].action_type == "git_push"
    assert grants[0].status == "pending"
    decided = db.decide_grant_request(grants[0].grant_id, True, "Matthew Judge")
    assert decided.status == "approved"
    assert decided.decided_by == "Matthew Judge"
    assert decided.decided_at is not None and decided.decided_at.tzinfo is None
    with pytest.raises(ValueError):
        db.decide_grant_request(grants[0].grant_id, False, "Matthew Judge")


def test_gateway_budget_429_halts_and_releases_reservation(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )

    class BudgetGateway:
        def complete_turn(self, *_):
            request = httpx.Request("POST", "http://127.0.0.1:4000/v1/chat/completions")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("Budget Exceeded", request=request, response=response)

    report = EphemeralWorker(db, BudgetGateway()).execute_turn(ticket.ticket_id)
    assert report.status == "halted"
    assert "Gateway HTTP 429" in report.stderr
    assert db.get_quota_record("task_one").tokens_reserved == 0


def test_unroutable_planner_child_does_not_complete_task(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "something unfamiliar", "task_one", workspace_path=workspace,
        source_commit=source_commit,
    )
    assert db.claim_ticket(ticket.ticket_id, "planner_test_owner")
    db.save_worker_report(WorkerReport(
        ticket_id=ticket.ticket_id, task_id="task_one", status="success",
        child_ticket_proposals=[ChildTicketProposal(
            capability="unsupported", objective="Impossible child",
        )],
    ), claim_owner="planner_test_owner")
    Gh0stCLI(db.db_url, str(manager.root)).process_queue("task_one")
    assert db.get_task("task_one").status == "halted"


def test_existing_sqlite_ledger_receives_phase1_columns(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE tickets (ticket_id VARCHAR(64) PRIMARY KEY);
            CREATE TABLE worker_reports (report_id VARCHAR(64) PRIMARY KEY);
            CREATE TABLE quota_records (quota_id VARCHAR(64) PRIMARY KEY);
        """)
    db = StateLedger(f"sqlite:///{path.as_posix()}")
    db.init_db()
    with sqlite3.connect(path) as connection:
        ticket_columns = {row[1] for row in connection.execute("PRAGMA table_info(tickets)")}
        quota_columns = {row[1] for row in connection.execute("PRAGMA table_info(quota_records)")}
        report_columns = {row[1] for row in connection.execute("PRAGMA table_info(worker_reports)")}
    assert {"workspace_path", "source_commit", "assigned_model", "routing_strategy", "claimed_at", "claim_owner", "lease_expires_at"} <= ticket_columns
    assert {"tokens_reserved", "cost_usd_reserved", "tokens_uncertain", "cost_usd_uncertain"} <= quota_columns
    assert {"diff", "commands_executed_json", "test_results_json"} <= report_columns


def test_grant_decision_api_requires_configured_token(tmp_path, monkeypatch):
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one")
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    grant = db.save_grant_request(GrantRequest(
        task_id="task_one", ticket_id=ticket.ticket_id,
        action_type="git_push", details={"ref": "main"},
    ))
    monkeypatch.setenv("GH0ST_API_TOKEN", "test-token")
    client = TestClient(create_app(db))
    endpoint = f"/v1/grants/{grant.grant_id}/decision"
    assert client.post(endpoint, json={"approved": True, "operator_id": "Matthew Judge"}).status_code == 401
    decided = client.post(endpoint, headers={"Authorization": "Bearer test-token"},
                          json={"approved": True, "operator_id": "Matthew Judge"})
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"


@pytest.mark.skipif(platform.system() != "Linux" or not shutil.which("bwrap"),
                    reason="Linux bubblewrap isolation gate")
def test_linux_workspace_and_network_boundaries(tmp_path):
    source = source_repository(tmp_path)
    manager = WorkspaceManager(str(tmp_path / "workspaces"))
    workspace, source_commit = manager.create("task_one", str(source))
    sandbox = LinuxSandbox(workspace, str(manager.root))
    assert sandbox.run("printf local > local.txt", "edit").exit_code == 0
    assert (Path(workspace) / "local.txt").read_text() == "local"
    assert sandbox.run(f"printf escape > '{source / 'escape.txt'}'", "edit").exit_code != 0
    assert not (source / "escape.txt").exists()
    assert sandbox.run("printf escape > /etc/gh0st_escape", "edit").exit_code != 0
    network = sandbox.run("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=1)\"", "other")
    assert network.exit_code != 0
    assert sandbox.run("git add local.txt && git commit -qm checkpoint", "git").exit_code == 0
    assert git("rev-parse", "HEAD", cwd=source) == source_commit


@pytest.mark.skipif(platform.system() != "Linux" or not shutil.which("bwrap"),
                    reason="Linux bubblewrap isolation gate")
def test_edit_test_report_loop_in_isolated_clone(tmp_path, monkeypatch):
    source = source_repository(tmp_path)
    db, manager, catalog = configured_ledger(tmp_path, monkeypatch)
    workspace, source_commit = manager.create("task_one", str(source))
    _, ticket = System1Router(db, catalog=catalog).route(
        "Fix code", "task_one", workspace_path=workspace, source_commit=source_commit,
    )
    gateway = SequenceGateway([
        response("printf 'def value():\\n    return 2\\n' > value.py", "edit"),
        response("python3 -m pytest -q", "test"),
        response(action=None, content="Patched and tests passed"),
    ])
    report = EphemeralWorker(db, gateway).execute_turn(ticket.ticket_id)
    assert report.status == "success", report.stderr
    assert len(report.commands_executed) == 2
    assert report.test_results[0]["exit_code"] == 0
    assert "return 2" in report.diff
    assert report.unresolved_failures == []
    assert "return 1" in (source / "value.py").read_text(encoding="utf-8")
