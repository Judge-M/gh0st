"""Loopback control API and optional outbound relay connection."""

import asyncio
import argparse
import json
import os
from pathlib import Path
import threading
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect

from .cli import Gh0stCLI
from .db import StateLedger
from .models import QuotaRecord, TicketStatus
from .router import System1Router
from .workspace import WorkspaceManager


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1)
    task_id: Optional[str] = None
    max_budget_usd: float = Field(default=5.0, gt=0)
    max_budget_tokens: int = Field(default=100000, ge=1)
    source_repo: Optional[str] = None
    model: Optional[str] = None
    modality: Optional[str] = None


class GrantDecision(BaseModel):
    approved: bool
    operator_id: str = Field(min_length=1)


def create_app(db: Optional[StateLedger] = None) -> FastAPI:
    ledger = db or StateLedger()
    ledger.init_db()
    app = FastAPI(title="gh0st Control API", version="0.1.0")
    api_token = os.getenv("GH0ST_API_TOKEN", "")
    workspaces = WorkspaceManager()

    def authorize(authorization: Optional[str]) -> None:
        if api_token and authorization != f"Bearer {api_token}":
            raise HTTPException(status_code=401, detail="Invalid API token")

    @app.get("/ui", response_class=HTMLResponse)
    def dashboard():
        return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/v1/health")
    def health(authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        return {"status": "ok", "default_classifier": os.getenv("GH0ST_ROUTER_MODEL", "layla"),
                "gateway_target": os.getenv("GH0ST_GATEWAY_URL", "http://127.0.0.1:4000/v1")}

    @app.post("/v1/run", status_code=202)
    def run(request: RunRequest, authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        task_id = request.task_id or f"task_{uuid.uuid4().hex[:12]}"
        if ledger.get_task(task_id):
            raise HTTPException(status_code=409, detail="Task already exists")
        try:
            workspace_path, source_commit = workspaces.create(task_id, request.source_repo)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ledger.create_task(task_id, request.prompt)
        ledger.create_quota_record(QuotaRecord(task_id=task_id,
            max_tokens_budget=request.max_budget_tokens, max_cost_usd_budget=request.max_budget_usd))
        decision, ticket = System1Router(ledger).route(
            request.prompt, task_id, workspace_path=workspace_path,
            source_commit=source_commit, model_override=request.model,
            modality=request.modality,
        )
        if ticket is None:
            ledger.set_task_status(task_id, "halted")
            raise HTTPException(status_code=422, detail=decision.reasoning_summary)
        thread = threading.Thread(target=Gh0stCLI(ledger.db_url, str(workspaces.root)).process_queue,
                                  args=(task_id,), daemon=True)
        thread.start()
        return {"task_id": task_id, "status": "dispatched", "ticket_id": ticket.ticket_id,
                "decision": {"selected_capability": decision.selected_capability,
                             "selected_model": decision.selected_model,
                             "confidence_score": decision.confidence_score,
                             "execution_tier": decision.execution_tier.value,
                             "compute_placement": decision.compute_placement.value,
                             "attached_tool": ticket.attached_tool_schema.name if ticket.attached_tool_schema else None,
                             "requires_planner": decision.requires_planner}}

    @app.get("/v1/status")
    def status(task_id: str, authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        task = ledger.get_task(task_id)
        quota = ledger.get_quota_record(task_id)
        if task is None or quota is None:
            raise HTTPException(status_code=404, detail="Task not found")
        tickets = ledger.list_task_tickets(task_id)
        return {"task_id": task_id, "status": task.status,
                "quota": {"tokens_consumed": quota.tokens_consumed,
                          "tokens_reserved": quota.tokens_reserved,
                          "remaining_tokens": quota.remaining_tokens,
                          "cost_usd_consumed": quota.cost_usd_consumed,
                          "cost_usd_reserved": quota.cost_usd_reserved,
                          "remaining_usd": quota.remaining_usd},
                "pending_tickets_count": sum(item.status == TicketStatus.PENDING for item in tickets),
                "trusted_memory_count": len(ledger.get_trusted_valid_claims(task_id)),
                "grant_requests": [grant.model_dump(mode="json") for grant in ledger.list_grant_requests(task_id)],
                "tickets": [{"ticket_id": item.ticket_id, "status": item.status.value,
                             "capability": item.capability,
                             "report": (report.model_dump(mode="json") if (report := ledger.get_worker_report(item.ticket_id)) else None)}
                            for item in tickets]}

    @app.post("/v1/grants/{grant_id}/decision")
    def decide_grant(grant_id: str, decision: GrantDecision,
                     authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        try:
            grant = ledger.decide_grant_request(grant_id, decision.approved, decision.operator_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return grant.model_dump(mode="json")

    return app


class CloudRelayClient:
    """Authenticated outbound transport for a separately hosted relay broker."""

    def __init__(self, relay_url: str, device_id: str, relay_token: str):
        if not relay_url.startswith("wss://"):
            raise ValueError("Relay URL must use wss://")
        if not relay_token:
            raise ValueError("Relay token is required")
        self.relay_url = relay_url
        self.device_id = device_id
        self.relay_token = relay_token

    async def run(self) -> None:
        while True:
            try:
                async with connect(self.relay_url, additional_headers={"Authorization": f"Bearer {self.relay_token}"}) as socket:
                    await socket.send(json.dumps({"message_type": "device_register", "device_id": self.device_id}))
                    async for message in socket:
                        # A broker and pairing protocol must authenticate commands before execution.
                        # Until one is configured, the client only accepts heartbeat messages.
                        payload = json.loads(message)
                        if payload.get("message_type") == "ping":
                            await socket.send(json.dumps({"message_type": "pong", "device_id": self.device_id}))
            except (OSError, ValueError):
                await asyncio.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the gh0st local API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8383)
    parser.add_argument("--device-id", default="local")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("GH0ST_API_TOKEN"):
        parser.error("Set GH0ST_API_TOKEN before binding beyond loopback")
    relay_url = os.getenv("GH0ST_RELAY_URL", "")
    relay_token = os.getenv("GH0ST_RELAY_TOKEN", "")
    if relay_url:
        client = CloudRelayClient(relay_url, args.device_id, relay_token)
        threading.Thread(target=lambda: asyncio.run(client.run()), daemon=True).start()
    import uvicorn
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
