from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading

from fastapi.testclient import TestClient

from gh0st.db import StateLedger
from gh0st.models import MemoryClaim, QuotaRecord
from gh0st.relay import create_app
from gh0st.router import System1Router
from gh0st.worker import EphemeralWorker


class StubGateway:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete_turn(self, prompt, system_instruction, attached_tool, max_tokens):
        self.calls.append((prompt, attached_tool, max_tokens))
        return self.result


def ledger(tmp_path: Path) -> StateLedger:
    db = StateLedger(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    db.init_db()
    db.create_task("task_test", "Refactor code")
    db.create_quota_record(QuotaRecord(task_id="task_test", max_tokens_budget=1000, max_cost_usd_budget=2))
    return db


def test_route_worker_report_and_trusted_claims(tmp_path):
    db = ledger(tmp_path)
    db.save_memory_claim(MemoryClaim(scope="task_test", claim_text="trusted", is_trusted=True))
    db.save_memory_claim(MemoryClaim(scope="task_test", claim_text="untrusted", is_trusted=False))
    decision, ticket = System1Router(db).route("Refactor code", "task_test")
    assert decision.selected_capability == "code_refactor"
    assert ticket.attached_tool_schema.name == "file_patch_proposal"
    gateway = StubGateway({"content": "Review complete", "tool_calls": [], "total_tokens": 50, "cost_usd": 0.01})
    report = EphemeralWorker(db, gateway).execute_turn(ticket.ticket_id)
    assert report.status == "success"
    assert db.get_worker_report(ticket.ticket_id).output["content"] == "Review complete"
    assert db.get_quota_record("task_test").tokens_consumed == 50
    assert "trusted" in gateway.calls[0][0]
    assert "untrusted" not in gateway.calls[0][0]
    assert gateway.calls[0][2] > 0


def test_quota_blocks_dispatch(tmp_path):
    db = ledger(tmp_path)
    db.update_quota_consumption("task_test", 1000, 0)
    decision, ticket = System1Router(db).route("Refactor code", "task_test")
    assert ticket is None
    assert decision.selected_capability == "none"


def test_planner_child_reenters_router(tmp_path):
    db = ledger(tmp_path)
    _, ticket = System1Router(db).route("something unfamiliar", "task_test")
    assert ticket.capability == "planner"
    gateway = StubGateway({"content": "", "tool_calls": [{"function": {"name": "emit_child_tickets", "arguments": '{"child_proposals":[{"capability":"unit_testing","objective":"Run pytest"}]}'}}], "total_tokens": 60, "cost_usd": 0.01})
    EphemeralWorker(db, gateway).execute_turn(ticket.ticket_id)
    tickets = db.list_task_tickets("task_test")
    assert len(tickets) == 2
    assert tickets[1].parent_ticket_id == ticket.ticket_id
    assert tickets[1].depth == 1


def test_api_contract(tmp_path):
    db = ledger(tmp_path)
    client = TestClient(create_app(db))
    assert client.get("/v1/health").status_code == 200
    status = client.get("/v1/status", params={"task_id": "task_test"})
    assert status.status_code == 200
    assert status.json()["quota"]["remaining_tokens"] == 1000
    assert client.post("/v1/run", json={"prompt": ""}).status_code == 422


def test_cli_spawns_worker_against_gateway(tmp_path):
    class GatewayHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            assert self.path == "/v1/chat/completions"
            assert len(payload.get("tools", [])) == 1
            body = json.dumps({"choices": [{"message": {"content": "Reviewed", "tool_calls": []}}],
                               "usage": {"total_tokens": 25, "cost_usd": 0.001}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db_url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    env = os.environ.copy()
    env["GH0ST_DEFAULT_MODEL"] = "test-model"
    command = [sys.executable, "-m", "gh0st.cli", "--db-url", db_url,
               "run", "Refactor code", "--gateway-url", f"http://127.0.0.1:{server.server_port}/v1"]
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0, result.stderr
    db = StateLedger(db_url)
    from gh0st.db import TaskRow
    with db.get_session() as session:
        task = session.query(TaskRow).one()
        assert task.status == "completed"
        assert db.get_quota_record(task.task_id).tokens_consumed == 25
