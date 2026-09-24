"""One-ticket controller. All model-requested local commands run in LinuxSandbox."""

import argparse
import json
import os
import re
import sys
import traceback
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, Field
import tiktoken

from .db import StateLedger
from .models import ChildTicketProposal, GrantRequest, TicketStatus, ToolSchema, WorkerReport
from .router import System1Router
from .sandbox import LinuxSandbox
from .workspace import WorkspaceManager


class GatewayConfig(BaseModel):
    gateway_url: str = Field(default_factory=lambda: os.getenv("GH0ST_GATEWAY_URL", "http://127.0.0.1:4000/v1"))
    api_key: str = Field(default_factory=lambda: os.getenv("GH0ST_GATEWAY_API_KEY", ""))
    timeout_seconds: float = Field(default=60.0, gt=0)


class PluggableGatewayAdapter:
    def __init__(self, config: Optional[GatewayConfig] = None):
        self.config = config or GatewayConfig()

    def complete_turn(self, messages: list[dict[str, Any]], attached_tool: Optional[ToolSchema],
                      max_tokens: int, model: str) -> dict[str, Any]:
        if not model or max_tokens < 1:
            raise ValueError("A configured gateway model and output allocation are required")
        payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if attached_tool:
            payload["tools"] = [{"type": "function", "function": attached_tool.model_dump()}]
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(
                f"{self.config.gateway_url.rstrip('/')}/chat/completions",
                headers=headers, json=payload,
            )
            response.raise_for_status()
            data = response.json()
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        if "total_tokens" not in usage:
            raise ValueError("Gateway did not return total token usage")
        if "cost_usd" not in usage and "cost_usd" not in data:
            raise ValueError("Gateway did not return USD usage")
        total_tokens = int(usage["total_tokens"])
        cost_usd = float(usage.get("cost_usd", data.get("cost_usd")))
        if total_tokens < 0 or cost_usd < 0:
            raise ValueError("Gateway returned negative usage")
        return {
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
        }


def _prompt_token_count(messages: list[dict[str, Any]], tool: Optional[ToolSchema]) -> int:
    encoding = tiktoken.get_encoding("cl100k_base")
    serialized = json.dumps({"messages": messages, "tool": tool.model_dump() if tool else None})
    return len(encoding.encode(serialized)) + 32


class EphemeralWorker:
    def __init__(self, db: StateLedger, gateway: Optional[PluggableGatewayAdapter] = None,
                 sandbox_factory: Callable[..., LinuxSandbox] = LinuxSandbox):
        self.db = db
        self.gateway = gateway or PluggableGatewayAdapter()
        self.sandbox_factory = sandbox_factory

    def execute_turn(self, ticket_id: str, fan_out_cap: int = 25) -> WorkerReport:
        ticket = self.db.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Unknown ticket: {ticket_id}")
        if not self.db.claim_ticket(ticket_id):
            raise ValueError(f"Ticket {ticket_id} has already been claimed")
        quota = self.db.get_quota_record(ticket.task_id)
        status = "success"
        output: dict[str, Any] = {}
        proposals: list[ChildTicketProposal] = []
        commands: list[dict[str, Any]] = []
        tests: list[dict[str, Any]] = []
        failures: dict[str, str] = {}
        tokens_used = 0
        cost_usd = 0.0
        error = ""
        diff = ""
        sandbox = None
        try:
            if quota is None or not quota.is_active:
                raise ValueError("Task quota is inactive")
            if not ticket.gateway_allowance.get("enabled"):
                raise ValueError("This ticket has no gateway allowance")
            max_calls = min(25, int(ticket.gateway_allowance.get("max_calls", 12)))
            if max_calls < 1:
                raise ValueError("Gateway call allowance is empty")
            system = (
                f"You are a short-lived gh0st worker for {ticket.capability}. "
                "The local_workbench tool runs only inside this task's isolated clone. "
                "Use run_command to inspect, edit, test, lint, and make local Git checkpoints. "
                "Use request_grant for any remote push, production write, or non-gateway network action. "
                "Do not claim an action happened unless its tool result confirms it."
            )
            if ticket.capability == "planner":
                system += " Propose concrete child tickets with emit_child_tickets."
            context = [claim.claim_text for claim in self.db.get_trusted_valid_claims(ticket.task_id)]
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({
                    "objective": ticket.objective, "context_slice": ticket.context_slice,
                    "trusted_claims": context, "constraints": ticket.constraints,
                    "workspace_source_commit": ticket.source_commit,
                })},
            ]
            for _ in range(max_calls):
                input_tokens = _prompt_token_count(messages, ticket.attached_tool_schema)
                remaining = ticket.max_tokens_allocated - tokens_used - input_tokens
                context_window = int(ticket.gateway_allowance.get("context_window", 8192))
                max_output = min(remaining, context_window - input_tokens)
                if max_output < 1:
                    status = "halted"
                    error = "Ticket token or model context allocation exhausted"
                    break
                result = self.gateway.complete_turn(
                    messages, ticket.attached_tool_schema, max_output, ticket.assigned_model,
                )
                tokens_used += result["total_tokens"]
                cost_usd += result["cost_usd"]
                if tokens_used > ticket.max_tokens_allocated or cost_usd > ticket.max_cost_usd:
                    status = "halted"
                    error = "Gateway usage exceeded the ticket allocation"
                    break
                output = {"content": result["content"], "tool_calls": result["tool_calls"]}
                calls = result["tool_calls"]
                if not calls:
                    break
                messages.append({"role": "assistant", "content": result["content"] or None, "tool_calls": calls})
                pending_grant = False
                for index, call in enumerate(calls):
                    function = call.get("function") or {}
                    if function.get("name") != (ticket.attached_tool_schema.name if ticket.attached_tool_schema else None):
                        raise ValueError("Gateway called a tool outside this ticket's one-schema allowance")
                    raw_arguments = function.get("arguments") or "{}"
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                    if ticket.capability == "planner":
                        raw = arguments.get("child_proposals")
                        if not isinstance(raw, list) or not raw:
                            raise ValueError("Planner produced no child proposals")
                        if len(raw) > fan_out_cap:
                            raise ValueError("Planner exceeded the ticket fan-out cap")
                        proposals = [ChildTicketProposal.model_validate(item) for item in raw]
                        tool_result = {"accepted_proposals": len(proposals)}
                    else:
                        action = arguments.get("action")
                        if action == "request_grant":
                            action_type = str(arguments.get("action_type") or "")
                            if action_type not in {"git_push", "production_write", "external_network"}:
                                raise ValueError("Unsupported external grant type")
                            grant = self.db.save_grant_request(GrantRequest(
                                task_id=ticket.task_id, ticket_id=ticket_id,
                                action_type=action_type, details=arguments.get("details") or {},
                            ))
                            tool_result = {"grant_id": grant.grant_id, "status": "pending"}
                            pending_grant = True
                        elif action == "run_command":
                            if "local_shell" not in ticket.permitted_local_capabilities:
                                raise ValueError("Local shell is not permitted by this ticket")
                            if sandbox is None:
                                sandbox = self.sandbox_factory(ticket.workspace_path, str(WorkspaceManager().root))
                            purpose = str(arguments.get("purpose") or "other")
                            if purpose not in {"inspect", "edit", "test", "lint", "git", "other"}:
                                raise ValueError("Unsupported local command purpose")
                            command = str(arguments.get("command") or "")
                            command_result = sandbox.run(command, purpose)
                            tool_result = command_result.as_dict()
                            commands.append(tool_result)
                            if purpose in {"test", "lint"}:
                                tests.append(tool_result)
                            if command_result.exit_code:
                                failures[purpose] = f"{purpose} command failed: {command}"
                            else:
                                failures.pop(purpose, None)
                        else:
                            raise ValueError("Unsupported workbench action")
                    messages.append({
                        "role": "tool", "tool_call_id": call.get("id") or f"call_{index}",
                        "content": json.dumps(tool_result),
                    })
                if pending_grant:
                    status = "halted"
                    error = "Awaiting an operator grant for an external action"
                    break
                if proposals:
                    break
            else:
                status = "halted"
                error = "Gateway call allowance exhausted"

            if ticket.capability != "planner":
                if sandbox is None and status == "success":
                    status = "halted"
                    error = "No local action was executed"
                elif sandbox is not None and ticket.source_commit:
                    if not re.fullmatch(r"[0-9a-f]{40,64}", ticket.source_commit):
                        raise ValueError("Invalid source commit")
                    result = sandbox.run(
                        f"git add -N --all && git diff --binary --no-ext-diff {ticket.source_commit} --",
                        "inspect",
                    )
                    if result.exit_code:
                        failures["diff"] = "Could not collect workspace diff"
                    elif result.truncated:
                        failures["diff"] = "Workspace diff exceeded the report size limit"
                    else:
                        diff = result.stdout
            if ticket.capability == "planner" and not proposals and status == "success":
                status = "halted"
                error = "Planner returned no child tickets"
            if ticket.capability == "code_refactor" and re.search(r"\b(fix|refactor|rewrite|implement|change|edit)\b", ticket.objective.casefold()) and not diff and status == "success":
                status = "halted"
                error = "Code-change task produced no workspace diff"
            if ticket.capability == "unit_testing" and not tests and status == "success":
                status = "halted"
                error = "Test task ran no test or lint command"
            if failures and status == "success":
                status = "halted"
                error = "Local checks or actions remain unresolved"
        except httpx.HTTPStatusError as exc:
            status = "halted" if exc.response.status_code in {402, 429} else "failure"
            error = f"Gateway HTTP {exc.response.status_code}"
            output = {"error": error}
        except Exception as exc:
            status = "failure"
            error = f"{type(exc).__name__}: {exc}"
            output = {"error": error}

        report = WorkerReport(
            ticket_id=ticket_id, task_id=ticket.task_id, status=status,
            output=output, stderr=error, diff=diff,
            commands_executed=commands, test_results=tests,
            unresolved_failures=list(failures.values()) + ([error] if error else []),
            tokens_used=tokens_used, cost_usd=cost_usd,
            child_ticket_proposals=proposals,
        )
        self.db.save_worker_report(report)
        if status == "success" and proposals:
            router = System1Router(self.db)
            for proposal in proposals:
                router.route(
                    proposal.objective, ticket.task_id,
                    parent_ticket_id=ticket_id, depth=ticket.depth + 1,
                    capability=proposal.capability, context_slice=proposal.context_slice,
                    model_override=ticket.assigned_model,
                )
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
