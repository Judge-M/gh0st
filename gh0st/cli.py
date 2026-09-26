"""Command line entry points for dispatch and status."""

import argparse
import os
import subprocess
import sys
import threading
import uuid
from typing import Optional
from pathlib import Path

from .db import StateLedger
from .models import QuotaRecord, TicketStatus
from .router import System1Router
from .relay_crypto import new_pairing
from .workspace import WorkspaceManager
from .sdk import Gh0stClient


class Gh0stCLI:
    def __init__(self, db_url: Optional[str] = None, workspace_root: Optional[str] = None):
        self.client = Gh0stClient(db_url, workspace_root)
        self.db = self.client.ledger
        self.workspaces = self.client.workspaces

    def run_prompt(self, prompt: str, task_id: Optional[str] = None, budget_usd: float = 5.0,
                   budget_tokens: int = 100000, gateway_url: Optional[str] = None,
                   source_repo: Optional[str] = None, model: Optional[str] = None,
                   modality: Optional[str] = None,
                   routing_strategy: Optional[str] = None,
                   image_paths: Optional[list[Path]] = None) -> str:
        if not prompt.strip() or budget_usd <= 0 or budget_tokens < 1:
            raise ValueError("A prompt and positive budgets are required")
        dispatched = self.client.dispatch(prompt, task_id=task_id, source_repo=source_repo,
                                          budget_usd=budget_usd, budget_tokens=budget_tokens,
                                          model=model, modality=modality,
                                          routing_strategy=routing_strategy or "smart",
                                          image_paths=[str(path) for path in (image_paths or [])])
        task_id, decision, ticket = dispatched.task_id, dispatched.decision, dispatched.ticket
        print(f"Task {task_id}: {decision.selected_capability} ({decision.confidence_score:.2f})")
        if ticket is None:
            self.db.set_task_status(task_id, "halted")
            print(decision.reasoning_summary, file=sys.stderr)
            return task_id

        memory_targets = ["SQLite rules", *ticket.memory_route.reference_providers]
        print(f"[System-1 OAG] Model: {ticket.assigned_model} | "
              f"Tool: {ticket.attached_tool_schema.name} | "
              f"Memory: {' + '.join(memory_targets)}")

        self.process_queue(task_id, gateway_url)
        return task_id

    def process_queue(self, task_id: str, gateway_url: Optional[str] = None,
                      quiet: bool = False, on_text_delta=None) -> None:
        task = self.db.get_task(task_id)
        if task is None or task.status != "active":
            raise ValueError(f"Task is not active: {task_id}")
        if self.db.recover_expired_claims(task_id):
            return
        if self.db.get_task(task_id).status != "active":
            return
        if any(item.status == TicketStatus.RUNNING for item in self.db.list_task_tickets(task_id)):
            print("A worker claim is still live; retry after its lease expires", file=sys.stderr)
            return
        environment = os.environ.copy()
        environment["GH0ST_WORKSPACE_ROOT"] = str(self.workspaces.root)
        if gateway_url:
            environment["GH0ST_GATEWAY_URL"] = gateway_url
            environment["GH0ST_MODEL_EXECUTION"] = "gateway"
        else:
            environment["GH0ST_MODEL_EXECUTION"] = "aisuite"
        if on_text_delta:
            environment["GH0ST_STREAM_WORKER"] = "1"
        processed = 0
        while processed < 100:
            pending = [item for item in self.db.list_task_tickets(task_id) if item.status == TicketStatus.PENDING]
            if not pending:
                break
            next_ticket = pending[0]
            owner = uuid.uuid4().hex
            if not self.db.claim_ticket(next_ticket.ticket_id, owner):
                return
            try:
                command = [sys.executable, "-m", "gh0st.worker", next_ticket.ticket_id,
                           "--db-url", self.db.db_url, "--claim-owner", owner]
                if on_text_delta:
                    process = subprocess.Popen(
                        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                    )
                    stderr_parts: list[str] = []
                    stderr_thread = threading.Thread(
                        target=lambda: stderr_parts.extend(process.stderr.readlines()), daemon=True,
                    )
                    stderr_thread.start()
                    report_payload = None
                    for line in process.stdout:
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            if not quiet:
                                print(line.rstrip())
                            continue
                        if message.get("type") == "event" and message.get("event") == "text_delta":
                            on_text_delta(str(message.get("text") or ""))
                        elif message.get("type") == "report":
                            report_payload = message.get("report")
                    return_code = process.wait()
                    stderr_thread.join()
                    stderr_text = "".join(stderr_parts)
                    if stderr_text and not quiet:
                        print(stderr_text.rstrip(), file=sys.stderr)
                else:
                    result = subprocess.run(command, env=environment, check=False,
                                            capture_output=True, text=True)
                    if result.stdout and not quiet:
                        print(result.stdout.strip())
                    if result.stderr and not quiet:
                        print(result.stderr.strip(), file=sys.stderr)
                    return_code = result.returncode
                    report_payload = True
            except OSError as exc:
                self.db.recover_abandoned_ticket(
                    next_ticket.ticket_id, f"Worker could not start: {exc}",
                    worker_exited=True, expected_owner=owner,
                )
                return
            processed += 1
            if return_code != 0:
                report = self.db.get_worker_report(next_ticket.ticket_id)
                if report is None:
                    self.db.recover_abandoned_ticket(
                        next_ticket.ticket_id, "Worker exited without a report",
                        worker_exited=True, expected_owner=owner,
                    )
                    report = self.db.get_worker_report(next_ticket.ticket_id)
                self.db.set_task_status(task_id, "halted" if report and report.status == "halted" else "failed")
                return
            if report_payload is None or self.db.get_worker_report(next_ticket.ticket_id) is None:
                self.db.recover_abandoned_ticket(
                    next_ticket.ticket_id, "Worker exited without a report",
                    worker_exited=True, expected_owner=owner,
                )
                return

        tickets = self.db.list_task_tickets(task_id)
        if processed == 100 and any(item.status == TicketStatus.PENDING for item in tickets):
            self.db.set_task_status(task_id, "halted")
        elif any(
            (report := self.db.get_worker_report(item.ticket_id))
            and report.child_ticket_proposals
            and sum(child.parent_ticket_id == item.ticket_id for child in tickets) < len(report.child_ticket_proposals)
            for item in tickets
        ):
            self.db.set_task_status(task_id, "halted")
        elif tickets and all(item.status == TicketStatus.COMPLETED for item in tickets):
            self.db.set_task_status(task_id, "completed")
        elif any(item.status == TicketStatus.FAILED for item in tickets):
            self.db.set_task_status(task_id, "failed")
        elif any(item.status == TicketStatus.HALTED for item in tickets):
            self.db.set_task_status(task_id, "halted")
        return

    def create_interactive_session(self, source_repo: Optional[str] = None,
                                   budget_usd: float = 5.0, budget_tokens: int = 100000,
                                   model: str = "", routing_strategy: str = "smart") -> dict:
        if budget_usd <= 0 or budget_tokens < 1:
            raise ValueError("Session budgets must be positive")
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        workspace_path, source_commit = self.workspaces.create(task_id, source_repo)
        self.db.create_task(task_id, f"Interactive session {session_id}")
        self.db.create_quota_record(QuotaRecord(
            task_id=task_id, max_tokens_budget=budget_tokens,
            max_cost_usd_budget=budget_usd,
        ))
        self.db.create_interactive_session(session_id, task_id, workspace_path, source_commit)
        self.db.update_interactive_session(session_id, selected_model=model,
                                           routing_strategy=routing_strategy)
        return self.db.get_interactive_session(session_id)

    def run_session_turn(self, session_id: str, prompt: str,
                         gateway_url: Optional[str] = None, on_routed=None, on_text_delta=None,
                         modality: Optional[str] = None):
        session = self.db.get_interactive_session(session_id)
        if session is None:
            raise ValueError(f"Unknown interactive session: {session_id}")
        if not prompt.strip():
            raise ValueError("A non-empty prompt is required")
        task_id = session["task_id"]
        self.db.set_task_status(task_id, "active")
        history = session["history"][-20:]
        decision, ticket = System1Router(self.db).route(
            prompt, task_id, workspace_path=session["workspace_path"],
            source_commit=session["source_commit"],
            model_override=session["selected_model"] or None,
            modality=modality,
            routing_strategy=session["routing_strategy"],
            context_slice={"conversation_history": history},
        )
        if ticket is None:
            self.db.set_task_status(task_id, "halted")
            raise ValueError(decision.reasoning_summary)
        if on_routed:
            on_routed(decision, ticket)
        self.process_queue(task_id, gateway_url, quiet=True, on_text_delta=on_text_delta)
        report = self.db.get_worker_report(ticket.ticket_id)
        if report is None:
            raise ValueError("Worker exited without saving a report")
        assistant_text = str(report.output.get("content") or "")
        if report.stderr:
            assistant_text = (assistant_text + "\n" if assistant_text else "") + report.stderr
        updated_history = session["history"] + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": assistant_text},
        ]
        self.db.update_interactive_session(session_id, history=updated_history[-200:])
        return decision, report

    def show_status(self, task_id: str) -> dict:
        task = self.db.get_task(task_id)
        quota = self.db.get_quota_record(task_id)
        if task is None or quota is None:
            raise ValueError(f"Unknown task: {task_id}")
        tickets = self.db.list_task_tickets(task_id)
        status = {"task_id": task_id, "status": task.status,
                  "tokens_consumed": quota.tokens_consumed, "tokens_reserved": quota.tokens_reserved,
                  "tokens_uncertain": quota.tokens_uncertain,
                  "remaining_tokens": quota.remaining_tokens,
                  "cost_usd_consumed": quota.cost_usd_consumed, "cost_usd_reserved": quota.cost_usd_reserved,
                  "cost_usd_uncertain": quota.cost_usd_uncertain,
                  "remaining_usd": quota.remaining_usd,
                  "pending_tickets_count": sum(t.status == TicketStatus.PENDING for t in tickets),
                  "trusted_memory_count": len(self.db.get_trusted_valid_claims(task_id)),
                  "pending_grants_count": sum(g.status == "pending" for g in self.db.list_grant_requests(task_id))}
        for key, value in status.items():
            print(f"{key}: {value}")
        return status


def write_pair_qr(uri: str, output_path: Path) -> None:
    """Write a one-time pairing URI as an SVG file without overwriting it."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    qr = qrcode.QRCode(border=4, box_size=8)
    qr.add_data(uri)
    qr.make(fit=True)
    svg = qr.make_image(image_factory=SvgPathImage).to_string(encoding="unicode")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as output:
        output.write(svg)
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="gh0st")
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--workspace-root", default=None)
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--task-id")
    run.add_argument("--budget", "--budget-usd", dest="budget_usd", type=float, default=5.0)
    run.add_argument("--tokens", "--budget-tokens", dest="budget_tokens", type=int, default=100000)
    run.add_argument("--gateway-url")
    run.add_argument("--source-repo", help="Repository to clone into this task's workspace")
    run.add_argument("--model", help="Explicit model target override")
    run.add_argument("--modality", choices=["text", "code", "vision", "image"])
    run.add_argument("--image", action="append", type=Path,
                     help="Attach a PNG, JPEG, or WebP input image (repeatable; selects vision routing)")
    run.add_argument("--routing-strategy", choices=["smart", "cost", "quality", "speed", "local"],
                     help="Select model placement policy; --model overrides this choice")
    chat = commands.add_parser("chat", help="Open an interactive gh0st session")
    chat.add_argument("--session-id", help="Resume a prior interactive session")
    chat.add_argument("--budget", "--budget-usd", dest="budget_usd", type=float, default=5.0)
    chat.add_argument("--tokens", "--budget-tokens", dest="budget_tokens", type=int, default=100000)
    chat.add_argument("--source-repo", default=os.getenv("GH0ST_SOURCE_REPO"))
    chat.add_argument("--model", default="", help="Static model override for this session")
    chat.add_argument("--routing-strategy", choices=["smart", "cost", "quality", "speed", "local"], default="smart")
    chat.add_argument("--gateway-url")
    status = commands.add_parser("status")
    status.add_argument("task_id")
    resume = commands.add_parser("resume", help="Continue an active task and recover expired worker claims")
    resume.add_argument("task_id")
    resume.add_argument("--gateway-url")
    pair = commands.add_parser("pair", help="Create a one-time encrypted relay pairing URI")
    pair.add_argument("--relay-url", default=os.getenv("GH0ST_RELAY_URL", ""))
    pair.add_argument("--device-id", default=os.getenv("GH0ST_DEVICE_ID", "local"))
    pair.add_argument("--qr-output", type=Path,
                      help="Write a scan-ready SVG containing the one-time pairing secret")
    pairings = commands.add_parser("pairings", help="List mobile devices paired with this gh0st instance")
    pairings.add_argument("--device-id", default=os.getenv("GH0ST_DEVICE_ID", "local"))
    unpair = commands.add_parser("unpair", help="Revoke a mobile device pairing")
    unpair.add_argument("pair_id", help="Pairing ID shown by `gh0st pairings`")
    unpair.add_argument("--device-id", default=os.getenv("GH0ST_DEVICE_ID", "local"))
    grant = commands.add_parser("grant")
    grant.add_argument("grant_id")
    decision = grant.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    grant.add_argument("--operator", required=True)
    api = commands.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8383)
    args = parser.parse_args()
    cli = Gh0stCLI(args.db_url, args.workspace_root)
    try:
        if args.command in {None, "chat"}:
            from .repl import InteractiveShell
            shell = InteractiveShell(
                cli, source_repo=getattr(args, "source_repo", None),
                budget_usd=getattr(args, "budget_usd", 5.0),
                budget_tokens=getattr(args, "budget_tokens", 100000),
                model=getattr(args, "model", ""),
                routing_strategy=getattr(args, "routing_strategy", "smart"),
                session_id=getattr(args, "session_id", None),
                gateway_url=getattr(args, "gateway_url", None),
            )
            shell.run()
            return
        if args.command == "run":
            task_id = cli.run_prompt(args.prompt, args.task_id, args.budget_usd, args.budget_tokens,
                                     args.gateway_url, args.source_repo, args.model, args.modality,
                                     args.routing_strategy, args.image)
            sys.exit(0 if cli.db.get_task(task_id).status == "completed" else 1)
        if args.command == "status":
            cli.show_status(args.task_id)
        if args.command == "resume":
            cli.process_queue(args.task_id, args.gateway_url)
        if args.command == "pair":
            pair_id, secret, uri = new_pairing(args.relay_url, args.device_id)
            cli.db.create_relay_pair(pair_id, args.device_id, secret, args.relay_url)
            if args.qr_output:
                write_pair_qr(uri, args.qr_output)
                print(f"One-time QR code written to {args.qr_output}; treat that file as a pairing secret.")
            print(uri)
        if args.command == "pairings":
            pairings = cli.db.list_paired_devices(args.device_id)
            if not pairings:
                print("No paired mobile devices")
            for paired in pairings:
                print(f"pair_id: {paired['pair_id']}\nrelay: {paired['relay_url']}\ncreated_at: {paired['created_at']}\nlast_seen_at: {paired['last_seen_at']}\n")
        if args.command == "unpair":
            if not cli.db.revoke_paired_device(args.pair_id, args.device_id):
                raise ValueError(f"No active pairing found: {args.pair_id}")
            print(f"Revoked pairing {args.pair_id}")
        if args.command == "grant":
            recorded = cli.db.decide_grant_request(args.grant_id, args.approve, args.operator)
            print(f"{recorded.grant_id}: {recorded.status} by {recorded.decided_by}")
        if args.command == "api":
            if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("GH0ST_API_TOKEN"):
                raise ValueError("Set GH0ST_API_TOKEN before binding the API beyond loopback")
            import uvicorn
            from .relay import create_app
            uvicorn.run(create_app(cli.db, str(cli.workspaces.root)), host=args.host, port=args.port)
    except ValueError as exc:
        parser.exit(2, f"gh0st: {exc}\n")


if __name__ == "__main__":
    main()
