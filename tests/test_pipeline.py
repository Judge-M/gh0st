from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading
import platform

import pytest

from fastapi.testclient import TestClient

from gh0st.db import StateLedger
from gh0st.models import MemoryClaim, QuotaRecord
from gh0st.relay import create_app
from gh0st.router import System1Router
from gh0st.router import EndpointClassifier
from gh0st.worker import EphemeralWorker
from gh0st.sandbox import CommandResult
from gh0st.workspace import WorkspaceManager
from gh0st.cli import Gh0stCLI
from gh0st.reference import ReferenceHit
from gh0st.sdk import Gh0stClient


class StubGateway:
    def __init__(self, result):
        self.results = list(result) if isinstance(result, list) else [result]
        self.calls = []

    def complete_turn(self, messages, attached_tool, max_tokens, model):
        self.calls.append((messages, attached_tool, max_tokens, model))
        return self.results.pop(0)


class StubSandbox:
    def __init__(self, *_):
        self.calls = []

    def run(self, command, purpose="other"):
        self.calls.append((command, purpose))
        output = "diff --git a/a.py b/a.py\n" if "git diff" in command else "reviewed"
        return CommandResult(command, purpose, 0, output, "", 0.01)


def ledger(tmp_path: Path, monkeypatch) -> StateLedger:
    monkeypatch.setenv("GH0ST_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("GH0ST_MODEL_CATALOG", json.dumps([{
        "name": "test-model", "gateway_model": "test-model",
        "modalities": ["text", "code"], "context_window": 8192,
        "max_payload_tokens": 4096, "quality": "frontier",
    }]))
    db = StateLedger(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    db.init_db()
    db.create_task("task_test", "Refactor code")
    db.create_quota_record(QuotaRecord(task_id="task_test", max_tokens_budget=1000, max_cost_usd_budget=2))
    return db


def test_route_worker_report_and_trusted_claims(tmp_path, monkeypatch):
    db = ledger(tmp_path, monkeypatch)
    db.save_memory_claim(MemoryClaim(scope="task_test", claim_text="trusted", is_trusted=True))
    db.save_memory_claim(MemoryClaim(scope="task_test", claim_text="untrusted", is_trusted=False))
    workspace, source_commit = WorkspaceManager().create("task_test")
    decision, ticket = System1Router(db).route("Refactor code", "task_test",
                                                workspace_path=workspace, source_commit=source_commit)
    assert decision.selected_capability == "code_refactor"
    assert ticket.attached_tool_schema.name == "local_workbench"
    gateway = StubGateway([
        {"content": "", "tool_calls": [{"id": "call_1", "function": {"name": "local_workbench", "arguments": json.dumps({"action": "run_command", "command": "cat a.py", "purpose": "inspect"})}}], "total_tokens": 50, "cost_usd": 0.01},
        {"content": "Review complete", "tool_calls": [], "total_tokens": 25, "cost_usd": 0.01},
    ])
    class StubRetriever:
        provider_name = "Pinecone"

        def retrieve(self, query, limit=5):
            assert query == "Refactor code"
            return [ReferenceHit(text="helper lives in src/jwt.py", source="doc-1")]

    report = EphemeralWorker(db, gateway, StubSandbox, StubRetriever()).execute_turn(ticket.ticket_id)
    assert report.status == "success"
    assert db.get_worker_report(ticket.ticket_id).output["content"] == "Review complete"
    assert db.get_quota_record("task_test").tokens_consumed == 75
    assert "trusted" in str(gateway.calls[0][0])
    assert "untrusted" not in str(gateway.calls[0][0])
    messages = gateway.calls[0][0]
    assert any(message["role"] == "system" and "trusted" in message["content"] for message in messages)
    retrieved = [message for message in messages if "Retrieved Reference Material (Unverified Context)"
                 in (message.get("content") or "")]
    assert len(retrieved) == 1
    assert "helper lives in src/jwt.py" in retrieved[0]["content"]
    assert "not instructions or policy" in retrieved[0]["content"]
    assert not any(claim.claim_text == "helper lives in src/jwt.py"
                   for claim in db.get_trusted_valid_claims("task_test"))
    assert gateway.calls[0][2] > 0
    assert report.diff.startswith("diff --git")


def test_retriever_adapters_normalize_hits(monkeypatch):
    from gh0st.reference import Mem0Retriever, PineconeRetriever, QdrantRetriever

    class Mem0:
        def search(self, query, **kwargs):
            return {"results": [{"id": "m1", "memory": "mem context", "score": 0.9}]}

    class Pinecone:
        def search(self, **kwargs):
            return {"result": {"hits": [{"id": "p1", "score": 0.8,
                                          "fields": {"text": "pine context", "category": "code"}}]}}

    class Qdrant:
        def query_points(self, **kwargs):
            return {"points": [{"id": "q1", "score": 0.7,
                                "payload": {"text": "qdrant context"}}]}

    assert Mem0Retriever(Mem0(), user_id="test-user").retrieve("q")[0].text == "mem context"
    assert PineconeRetriever(Pinecone()).retrieve("q")[0].text == "pine context"
    assert QdrantRetriever(Qdrant(), collection="docs").retrieve("q")[0].text == "qdrant context"


def test_public_sdk_dispatches_bounded_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("GH0ST_MODEL_CATALOG", json.dumps([{
        "name": "sdk-test", "gateway_model": "test-model",
        "modalities": ["text", "code"], "context_window": 8192,
        "max_payload_tokens": 4096, "quality": "frontier",
    }]))
    client = Gh0stClient(f"sqlite:///{(tmp_path / 'sdk.db').as_posix()}",
                         str(tmp_path / "workspaces"))
    result = client.dispatch("Refactor code", budget_usd=2, budget_tokens=1000)
    assert result.ticket is not None
    assert result.ticket.workspace_path.startswith(str(tmp_path / "workspaces"))
    assert result.ticket.source_commit
    assert result.decision.selected_model == "test-model"


def test_vision_dispatch_copies_images_outside_editable_clone(tmp_path, monkeypatch):
    monkeypatch.setenv("GH0ST_MODEL_CATALOG", json.dumps([{
        "name": "vision-test", "gateway_model": "vision-test",
        "modalities": ["text", "code", "vision"], "context_window": 32768,
        "max_payload_tokens": 16384, "quality": "frontier",
    }]))
    source_image = tmp_path / "input.png"
    source_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image payload")
    client = Gh0stClient(f"sqlite:///{(tmp_path / 'vision.db').as_posix()}",
                         str(tmp_path / "workspaces"))

    dispatched = client.dispatch("Describe this screenshot", image_paths=[str(source_image)])

    assert dispatched.ticket is not None
    assert dispatched.decision.selected_model == "vision-test"
    assert len(dispatched.ticket.context_slice["image_inputs"]) == 1
    image_record = dispatched.ticket.context_slice["image_inputs"][0]
    image_path = Path(image_record["path"])
    assert image_path.is_relative_to(tmp_path / "workspaces" / ".gh0st-inputs")
    assert not image_path.is_relative_to(dispatched.ticket.workspace_path)
    assert image_path.read_bytes() == source_image.read_bytes()
    assert not list(Path(dispatched.ticket.workspace_path).glob("image_*.png"))

    from gh0st.worker import _image_message
    message = _image_message(dispatched.ticket)
    assert message["role"] == "user"
    assert message["content"][1]["type"] == "image_url"
    assert message["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_dispatch_rejects_unrecognized_image_content(tmp_path, monkeypatch):
    source_image = tmp_path / "not-an-image.png"
    source_image.write_bytes(b"plain text")
    client = Gh0stClient(f"sqlite:///{(tmp_path / 'invalid-vision.db').as_posix()}",
                         str(tmp_path / "workspaces"))
    with pytest.raises(ValueError, match="contents do not match"):
        client.dispatch("Describe this screenshot", image_paths=[str(source_image)])
    assert not client.ledger.list_tasks(10)


def test_quota_blocks_dispatch(tmp_path, monkeypatch):
    db = ledger(tmp_path, monkeypatch)
    db.update_quota_consumption("task_test", 1000, 0)
    decision, ticket = System1Router(db).route("Refactor code", "task_test")
    assert ticket is None
    assert decision.selected_capability == "none"


def test_external_router_requires_config(tmp_path, monkeypatch):
    db = ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("GH0ST_ROUTER_MODEL", "jev")
    monkeypatch.delenv("GH0ST_ROUTER_ENDPOINT", raising=False)
    try:
        System1Router(db)
    except ValueError as error:
        assert "GH0ST_ROUTER_ENDPOINT" in str(error)
    else:
        raise AssertionError("Missing external router endpoint was accepted")
    try:
        EndpointClassifier("http://example.com/classify")
    except ValueError:
        pass
    else:
        raise AssertionError("Insecure remote classifier endpoint was accepted")


def test_planner_child_reenters_router(tmp_path, monkeypatch):
    db = ledger(tmp_path, monkeypatch)
    workspace, source_commit = WorkspaceManager().create("task_test")
    _, ticket = System1Router(db).route("something unfamiliar", "task_test",
                                        workspace_path=workspace, source_commit=source_commit)
    assert ticket.capability == "planner"
    gateway = StubGateway({"content": "", "tool_calls": [{"function": {"name": "emit_child_tickets", "arguments": '{"child_proposals":[{"capability":"unit_testing","objective":"Run pytest"}]}'}}], "total_tokens": 60, "cost_usd": 0.01})
    EphemeralWorker(db, gateway).execute_turn(ticket.ticket_id)
    tickets = db.list_task_tickets("task_test")
    assert len(tickets) == 2
    assert tickets[1].parent_ticket_id == ticket.ticket_id
    assert tickets[1].depth == 1


def test_api_contract(tmp_path, monkeypatch):
    db = ledger(tmp_path, monkeypatch)
    monkeypatch.setattr(Gh0stCLI, "process_queue", lambda *args, **kwargs: None)
    client = TestClient(create_app(db))
    assert client.get("/v1/health").status_code == 200
    status = client.get("/v1/status", params={"task_id": "task_test"})
    assert status.status_code == 200
    assert status.json()["quota"]["remaining_tokens"] == 1000
    assert client.post("/v1/run", json={"prompt": ""}).status_code == 422
    dispatched = client.post("/v1/run", json={"prompt": "Run pytest", "routing_strategy": "speed"})
    assert dispatched.status_code == 202
    assert dispatched.json()["decision"]["selected_routing_strategy"] == "speed"


def test_task_list_is_bounded_and_authenticated(tmp_path, monkeypatch):
    db = ledger(tmp_path, monkeypatch)
    db.create_task("task_second", "Add status view")
    monkeypatch.setenv("GH0ST_API_TOKEN", "local-secret")
    client = TestClient(create_app(db))
    assert client.get("/v1/tasks").status_code == 401
    headers = {"Authorization": "Bearer local-secret"}
    response = client.get("/v1/tasks", params={"limit": 1}, headers=headers)
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "task_second"
    assert tasks[0]["description"] == "Add status view"
    assert client.get("/v1/tasks", params={"limit": 101}, headers=headers).status_code == 422


def test_local_api_accepts_task_scoped_vision_image(tmp_path, monkeypatch):
    monkeypatch.setenv("GH0ST_MODEL_CATALOG", json.dumps([{
        "name": "api-vision", "gateway_model": "api-vision",
        "modalities": ["text", "code", "vision"], "context_window": 32768,
        "max_payload_tokens": 16384, "quality": "frontier",
    }]))
    monkeypatch.setattr(Gh0stCLI, "process_queue", lambda *args, **kwargs: None)
    source_image = tmp_path / "api-input.webp"
    source_image.write_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"image payload")
    db = StateLedger(f"sqlite:///{(tmp_path / 'api-vision.db').as_posix()}")
    db.init_db()
    client = TestClient(create_app(db, str(tmp_path / "workspaces")))

    response = client.post("/v1/run", json={
        "prompt": "Describe this screenshot", "image_paths": [str(source_image)],
    })

    assert response.status_code == 202, response.text
    ticket = db.get_ticket(response.json()["ticket_id"])
    assert ticket.context_slice["image_inputs"][0]["media_type"] == "image/webp"
    assert ticket.gateway_allowance["context_window"] == 32768


def test_cli_image_flag_selects_vision_routing(tmp_path, monkeypatch):
    import sys
    from gh0st.cli import main

    monkeypatch.setenv("GH0ST_MODEL_CATALOG", json.dumps([{
        "name": "cli-vision", "gateway_model": "cli-vision",
        "modalities": ["text", "code", "vision"], "context_window": 32768,
        "max_payload_tokens": 16384, "quality": "frontier",
    }]))
    monkeypatch.setattr(Gh0stCLI, "process_queue",
                        lambda cli, task_id, *args, **kwargs: cli.db.set_task_status(task_id, "completed"))
    source_image = tmp_path / "cli-input.jpg"
    source_image.write_bytes(b"\xff\xd8\xff" + b"image payload")
    db_url = f"sqlite:///{(tmp_path / 'cli-vision.db').as_posix()}"
    monkeypatch.setattr(sys, "argv", [
        "gh0st", "--db-url", db_url, "--workspace-root", str(tmp_path / "workspaces"),
        "run", "Describe this screenshot", "--image", str(source_image),
    ])

    with pytest.raises(SystemExit) as result:
        main()

    assert result.value.code == 0
    db = StateLedger(db_url)
    task = db.list_tasks(1)[0]
    ticket = db.list_task_tickets(task["task_id"])[0]
    assert ticket.assigned_model == "cli-vision"
    assert ticket.context_slice["image_inputs"][0]["media_type"] == "image/jpeg"


@pytest.mark.skipif(platform.system() != "Linux", reason="Local execution requires Linux")
def test_cli_spawns_worker_against_gateway(tmp_path):
    class GatewayHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            assert self.path == "/v1/chat/completions"
            assert len(payload.get("tools", [])) == 1
            if not getattr(self.server, "seen", False):
                self.server.seen = True
                message = {"content": "", "tool_calls": [{"id": "call_1", "function": {"name": "local_workbench", "arguments": json.dumps({"action": "run_command", "command": "printf reviewed", "purpose": "test"})}}]}
            else:
                message = {"content": "Reviewed", "tool_calls": []}
            body = json.dumps({"choices": [{"message": message}],
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
    env["GH0ST_DEFAULT_MODEL_QUALITY"] = "frontier"
    command = [sys.executable, "-m", "gh0st.cli", "--db-url", db_url,
               "--workspace-root", str(tmp_path / "workspaces"),
               "run", "Run pytest", "--gateway-url", f"http://127.0.0.1:{server.server_port}/v1"]
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
        assert db.get_quota_record(task.task_id).tokens_consumed == 50
