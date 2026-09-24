"""One-ticket worker process and OpenAI-compatible gateway adapter."""

import argparse
import json
import os
import sys
import traceback
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field
import tiktoken

from .db import StateLedger
from .models import ChildTicketProposal, TicketStatus, ToolSchema, WorkerReport
from .router import System1Router


class GatewayConfig(BaseModel):
    gateway_url: str = Field(default_factory=lambda: os.getenv("GH0ST_GATEWAY_URL", "http://127.0.0.1:4000/v1"))
    api_key: str = Field(default_factory=lambda: os.getenv("GH0ST_GATEWAY_API_KEY", ""))
    default_model: str = Field(default_factory=lambda: os.getenv("GH0ST_DEFAULT_MODEL", ""))
    timeout_seconds: float = Field(default=60.0, gt=0)


class PluggableGatewayAdapter:
    def __init__(self, config: Optional[GatewayConfig] = None):
        self.config = config or GatewayConfig()

    def complete_turn(self, prompt: str, system_instruction: str, attached_tool: Optional[ToolSchema],
                      max_tokens: int) -> dict[str, Any]:
        if not self.config.default_model:
            raise ValueError("GH0ST_DEFAULT_MODEL must name a gateway model")
        if max_tokens < 1:
            raise ValueError("No output tokens remain")
        payload: dict[str, Any] = {
            "model": self.config.default_model,
            "messages": [{"role": "system", "content": system_instruction}, {"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if attached_tool:
            payload["tools"] = [{"type": "function", "function": attached_tool.model_dump()}]
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(f"{self.config.gateway_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return {
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost_usd": float(usage.get("cost_usd") or data.get("cost_usd") or 0.0),
        }


def _prompt_token_count(system_instruction: str, prompt: str, tool: Optional[ToolSchema]) -> int:
    encoding = tiktoken.get_encoding("cl100k_base")
    serialized = json.dumps({"system": system_instruction, "prompt": prompt,
                             "tool": tool.model_dump() if tool else None})
    # Reserve framing tokens for the gateway's chat message representation.
    return len(encoding.encode(serialized)) + 32


class EphemeralWorker:
    def __init__(self, db: StateLedger, gateway: Optional[PluggableGatewayAdapter] = None):
        self.db = db
        self.gateway = gateway or PluggableGatewayAdapter()

    def execute_turn(self, ticket_id: str, fan_out_cap: int = 25) -> WorkerReport:
        ticket = self.db.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Unknown ticket: {ticket_id}")
        if ticket.status != TicketStatus.PENDING:
            raise ValueError(f"Ticket {ticket_id} is {ticket.status.value}")
        quota = self.db.get_quota_record(ticket.task_id)
        if quota is None or not quota.is_active or quota.remaining_tokens <= 0 or quota.remaining_usd <= 0:
            raise ValueError("Task quota is inactive or exhausted")

        self.db.update_ticket_status(ticket_id, TicketStatus.RUNNING)
        status = "success"
        output: dict[str, Any] = {}
        proposals: list[ChildTicketProposal] = []
        tokens_used = 0
        cost_usd = 0.0
        error = ""
        try:
            system = f"You are a short-lived gh0st worker for {ticket.capability}. Return a bounded result. Do not claim tools ran unless they did."
            if ticket.capability == "planner":
                system += " Propose concrete child tickets through emit_child_tickets only."
            context = [c.claim_text for c in self.db.get_trusted_valid_claims(ticket.task_id)]
            prompt = json.dumps({"objective": ticket.objective, "context_slice": ticket.context_slice,
                                 "trusted_claims": context, "constraints": ticket.constraints})
            input_tokens = _prompt_token_count(system, prompt, ticket.attached_tool_schema)
            max_output = min(ticket.max_tokens_allocated, quota.remaining_tokens) - input_tokens
            if max_output < 1:
                raise ValueError("Input exceeds remaining ticket token budget")
            result = self.gateway.complete_turn(prompt, system, ticket.attached_tool_schema, max_output)
            output = {"content": result["content"], "tool_calls": result["tool_calls"]}
            tokens_used = result["total_tokens"]
            cost_usd = result["cost_usd"]
            if ticket.capability == "planner":
                for call in result["tool_calls"]:
                    function = call.get("function") or {}
                    if function.get("name") != "emit_child_tickets":
                        continue
                    arguments = function.get("arguments") or "{}"
                    decoded = json.loads(arguments) if isinstance(arguments, str) else arguments
                    raw = decoded.get("child_proposals") or []
                    proposals = [ChildTicketProposal.model_validate(item) for item in raw[:max(0, fan_out_cap)]]
                    break
            if tokens_used > ticket.max_tokens_allocated or cost_usd > ticket.max_cost_usd:
                status = "halted"
                error = "Gateway reported usage above ticket allocation"
        except Exception as exc:
            status = "failure"
            error = f"{type(exc).__name__}: {exc}"
            output = {"error": error}

        report = WorkerReport(ticket_id=ticket_id, task_id=ticket.task_id, status=status,
                              output=output, stderr=error, tokens_used=tokens_used,
                              cost_usd=cost_usd, child_ticket_proposals=proposals)
        self.db.save_worker_report(report)
        if status == "success" and proposals:
            router = System1Router(self.db)
            for proposal in proposals:
                router.route(proposal.objective, ticket.task_id, parent_ticket_id=ticket_id,
                             depth=ticket.depth + 1, capability=proposal.capability,
                             context_slice=proposal.context_slice)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one gh0st ticket and exit")
    parser.add_argument("ticket_id")
    parser.add_argument("--db-url", default=None)
    args = parser.parse_args()
    db = StateLedger(args.db_url)
    db.init_db()
    try:
        report = EphemeralWorker(db).execute_turn(args.ticket_id)
        print(report.model_dump_json())
        sys.exit(0 if report.status == "success" else 1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
