"""Loopback control API and optional outbound relay connection."""

import asyncio
import argparse
import json
import os
from pathlib import Path
import threading
import uuid
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .cli import Gh0stCLI
from .db import StateLedger
from .models import TicketStatus
from .sdk import Gh0stClient
from .workspace import WorkspaceManager
from .relay_crypto import PairCipher


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1)
    task_id: Optional[str] = None
    max_budget_usd: float = Field(default=5.0, gt=0)
    max_budget_tokens: int = Field(default=100000, ge=1)
    source_repo: Optional[str] = None
    model: Optional[str] = None
    modality: Optional[str] = None
    routing_strategy: Literal["smart", "cost", "quality", "speed", "local"] = "smart"


class GrantDecision(BaseModel):
    approved: bool
    operator_id: str = Field(min_length=1)


def create_app(db: Optional[StateLedger] = None,
               workspace_root: Optional[str] = None) -> FastAPI:
    ledger = db or StateLedger()
    ledger.init_db()
    app = FastAPI(title="gh0st Control API", version="0.1.0")
    api_token = os.getenv("GH0ST_API_TOKEN", "")
    workspaces = WorkspaceManager(workspace_root)
    sdk = Gh0stClient(ledger.db_url, str(workspaces.root))

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

    @app.get("/v1/tasks")
    def tasks(limit: int = Query(default=50, ge=1, le=100),
              authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        return {"tasks": ledger.list_tasks(limit)}

    @app.post("/v1/run", status_code=202)
    def run(request: RunRequest, authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        task_id = request.task_id or f"task_{uuid.uuid4().hex[:12]}"
        if ledger.get_task(task_id):
            raise HTTPException(status_code=409, detail="Task already exists")
        try:
            dispatch = sdk.dispatch(request.prompt, task_id=task_id,
                                   budget_usd=request.max_budget_usd,
                                   budget_tokens=request.max_budget_tokens,
                                   source_repo=request.source_repo, model=request.model,
                                   modality=request.modality,
                                   routing_strategy=request.routing_strategy)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        decision, ticket = dispatch.decision, dispatch.ticket
        if ticket is None:
            raise HTTPException(status_code=422, detail=decision.reasoning_summary)
        thread = threading.Thread(target=Gh0stCLI(ledger.db_url, str(workspaces.root)).process_queue,
                                  args=(task_id,), daemon=True)
        thread.start()
        return {"task_id": task_id, "status": "dispatched", "ticket_id": ticket.ticket_id,
                "decision": {"selected_capability": decision.selected_capability,
                             "selected_model": decision.selected_model,
                             "selected_routing_strategy": decision.selected_routing_strategy,
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
                          "tokens_uncertain": quota.tokens_uncertain,
                          "remaining_tokens": quota.remaining_tokens,
                          "cost_usd_consumed": quota.cost_usd_consumed,
                          "cost_usd_reserved": quota.cost_usd_reserved,
                          "cost_usd_uncertain": quota.cost_usd_uncertain,
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
    """Outbound daemon connection. Only encrypted paired commands are accepted."""

    def __init__(self, relay_url: str, device_id: str, relay_token: str,
                 db: Optional[StateLedger] = None, workspace_root: Optional[str] = None):
        if not relay_url.startswith("wss://") and not relay_url.startswith("ws://127.0.0.1:"):
            raise ValueError("Relay URL must use wss:// outside loopback")
        if not relay_token:
            raise ValueError("Relay token is required")
        self.relay_url = relay_url
        self.device_id = device_id
        self.relay_token = relay_token
        self.db = db or StateLedger()
        self.db.init_db()
        self.cli = Gh0stCLI(self.db.db_url, workspace_root)
        self.background_tasks: set[asyncio.Task] = set()

    async def _publish_offers(self, socket, sent: set[str], registered: set[str],
                              revoked_sent: set[str], ciphers: dict[str, PairCipher],
                              seen_actions: dict[str, set[str]]) -> None:
        while True:
            self.db.expire_relay_pairs()
            for pair in self.db.list_live_relay_pairs(self.device_id):
                if pair["relay_url"] == self.relay_url and pair["pair_id"] not in sent:
                    await socket.send(json.dumps({"message_type": "pair_offer",
                                                  "pair_id": pair["pair_id"]}))
                    sent.add(pair["pair_id"])
            active = {pair["pair_id"] for pair in self.db.list_paired_devices(self.device_id)
                      if pair["relay_url"] == self.relay_url}
            for pair_id in active - registered:
                await socket.send(json.dumps({"message_type": "pair_register",
                                              "pair_id": pair_id}))
                registered.add(pair_id)
            for pair_id in set(self.db.list_revoked_pair_ids(self.device_id, self.relay_url)) - revoked_sent:
                await socket.send(json.dumps({"message_type": "pair_revoke",
                                              "pair_id": pair_id}))
                ciphers.pop(pair_id, None)
                seen_actions.pop(pair_id, None)
                registered.discard(pair_id)
                revoked_sent.add(pair_id)
            await asyncio.sleep(2)

    async def _send_encrypted(self, socket, cipher: PairCipher, payload: dict) -> None:
        await socket.send(json.dumps(cipher.encrypt(payload)))

    def _task_snapshot(self, task_id: str) -> dict:
        task = self.db.get_task(task_id)
        quota = self.db.get_quota_record(task_id)
        if not task or not quota:
            raise ValueError("Unknown task")
        return {"task_id": task_id, "status": task.status,
                "remaining_tokens": quota.remaining_tokens,
                "remaining_usd": quota.remaining_usd,
                "tokens_consumed": quota.tokens_consumed,
                "cost_usd_consumed": quota.cost_usd_consumed,
                "grant_requests": [grant.model_dump(mode="json")
                                   for grant in self.db.list_grant_requests(task_id)]}

    async def _handle_action(self, socket, cipher: PairCipher,
                             payload: dict, seen_actions: set[str]) -> None:
        action_id = payload.get("action_id")
        action = payload.get("action_type")
        if not isinstance(action_id, str) or not action_id.startswith("act_") or action_id in seen_actions:
            return
        seen_actions.add(action_id)
        try:
            if action == "status":
                result = self._task_snapshot(str(payload.get("task_id") or ""))
            elif action in {"approve_grant", "reject_grant"}:
                grant_id = str(payload.get("grant_id") or "")
                task_id = str(payload.get("task_id") or "")
                if not any(grant.grant_id == grant_id for grant in self.db.list_grant_requests(task_id)):
                    raise ValueError("Grant does not belong to the requested task")
                grant = self.db.decide_grant_request(
                    grant_id, action == "approve_grant",
                    f"paired:{self.device_id}:{cipher.pair_id}",
                )
                result = {"grant_id": grant.grant_id, "status": grant.status}
            elif action == "dispatch_task":
                prompt = str(payload.get("prompt") or "").strip()
                budget_usd = float(payload.get("budget_usd", 5))
                budget_tokens = int(payload.get("budget_tokens", 100000))
                if not prompt or len(prompt) > 8000:
                    raise ValueError("Prompt must be 1 to 8000 characters")
                if not (0 < budget_usd <= float(os.getenv("GH0ST_RELAY_MAX_BUDGET_USD", "5"))):
                    raise ValueError("Remote task exceeds the configured USD cap")
                if not (0 < budget_tokens <= int(os.getenv("GH0ST_RELAY_MAX_TOKENS", "100000"))):
                    raise ValueError("Remote task exceeds the configured token cap")
                task_id = f"task_{uuid.uuid4().hex[:12]}"
                source_repo = os.getenv("GH0ST_RELAY_SOURCE_REPO")

                async def run_task() -> None:
                    try:
                        await asyncio.to_thread(self.cli.run_prompt, prompt,
                                                task_id=task_id, budget_usd=budget_usd,
                                                budget_tokens=budget_tokens,
                                                source_repo=source_repo)
                        await self._send_encrypted(socket, cipher,
                                                   {"message_type": "task_update", **self._task_snapshot(task_id)})
                    except ConnectionClosed:
                        # The task remains inspectable in the local ledger after a disconnect.
                        pass
                    except Exception as exc:
                        try:
                            await self._send_encrypted(socket, cipher,
                                                       {"message_type": "task_update",
                                                        "task_id": task_id, "status": "failed",
                                                        "error": str(exc)})
                        except ConnectionClosed:
                            pass

                task = asyncio.create_task(run_task())
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
                result = {"task_id": task_id, "status": "accepted"}
            else:
                raise ValueError("Unsupported remote action")
            await self._send_encrypted(socket, cipher, {"message_type": "action_result",
                                                        "action_id": action_id, "ok": True,
                                                        "result": result})
        except (ValueError, TypeError) as exc:
            await self._send_encrypted(socket, cipher, {"message_type": "action_result",
                                                        "action_id": action_id, "ok": False,
                                                        "error": str(exc)})

    async def run(self) -> None:
        while True:
            try:
                async with connect(self.relay_url, additional_headers={"Authorization": f"Bearer {self.relay_token}"}) as socket:
                    await socket.send(json.dumps({"message_type": "device_register", "device_id": self.device_id}))
                    registered = json.loads(await socket.recv())
                    if registered.get("message_type") != "device_registered":
                        raise ValueError("Broker did not authenticate the device")
                    sent_offers: set[str] = set()
                    registered_pairs: set[str] = set()
                    revoked_sent: set[str] = set()
                    ciphers: dict[str, PairCipher] = {}
                    seen_actions: dict[str, set[str]] = {}
                    offer_task = asyncio.create_task(self._publish_offers(
                        socket, sent_offers, registered_pairs, revoked_sent,
                        ciphers, seen_actions))
                    try:
                        async for message in socket:
                            payload = json.loads(message)
                            pair_id = payload.get("pair_id")
                            kind = payload.get("message_type")
                            if kind == "offer_ready" and pair_id in sent_offers:
                                self.db.set_relay_pair_status(pair_id, ("pending", "offered"), "offered")
                            elif kind == "client_connected" and (pair_id in sent_offers or pair_id in registered_pairs):
                                secret = self.db.consume_relay_pair(pair_id, self.device_id)
                                if secret is None:
                                    paired_device = self.db.get_paired_device(pair_id, self.device_id)
                                    secret = paired_device["secret"] if paired_device else None
                                if secret:
                                    self.db.touch_paired_device(pair_id, self.device_id)
                                    cipher = PairCipher(secret, pair_id, "device")
                                    ciphers[pair_id] = cipher
                                    seen_actions[pair_id] = set()
                                    await self._send_encrypted(socket, cipher,
                                                               {"message_type": "paired", "device_id": self.device_id})
                            elif kind == "client_disconnected":
                                ciphers.pop(pair_id, None)
                                seen_actions.pop(pair_id, None)
                            elif kind == "ciphertext" and pair_id in ciphers:
                                try:
                                    command = ciphers[pair_id].decrypt(payload)
                                except ValueError:
                                    continue
                                await self._handle_action(socket, ciphers[pair_id], command,
                                                          seen_actions[pair_id])
                    finally:
                        offer_task.cancel()
                        await asyncio.gather(offer_task, return_exceptions=True)
            except (OSError, ValueError, ConnectionClosed, json.JSONDecodeError):
                await asyncio.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the gh0st local API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8383)
    parser.add_argument("--device-id", default=os.getenv("GH0ST_DEVICE_ID", "local"))
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--workspace-root", default=None)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("GH0ST_API_TOKEN"):
        parser.error("Set GH0ST_API_TOKEN before binding beyond loopback")
    relay_url = os.getenv("GH0ST_RELAY_URL", "")
    relay_token = os.getenv("GH0ST_RELAY_TOKEN", "")
    db = StateLedger(args.db_url)
    db.init_db()
    if relay_url:
        client = CloudRelayClient(relay_url, args.device_id, relay_token,
                                  db, args.workspace_root)
        threading.Thread(target=lambda: asyncio.run(client.run()), daemon=True).start()
    import uvicorn
    uvicorn.run(create_app(db, args.workspace_root), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
