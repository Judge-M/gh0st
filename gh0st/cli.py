"""Command line entry points for dispatch and status."""

import argparse
import os
import subprocess
import sys
import uuid
from typing import Optional
from pathlib import Path

from .db import StateLedger
from .models import QuotaRecord, TicketStatus
from .router import System1Router
from .relay_crypto import new_pairing
from .workspace import WorkspaceManager


class Gh0stCLI:
    def __init__(self, db_url: Optional[str] = None, workspace_root: Optional[str] = None):
        self.db = StateLedger(db_url)
        self.db.init_db()
        self.workspaces = WorkspaceManager(workspace_root)

    def run_prompt(self, prompt: str, task_id: Optional[str] = None, budget_usd: float = 5.0,
                   budget_tokens: int = 100000, gateway_url: Optional[str] = None,
                   source_repo: Optional[str] = None, model: Optional[str] = None,
                   modality: Optional[str] = None) -> str:
        if not prompt.strip() or budget_usd <= 0 or budget_tokens < 1:
            raise ValueError("A prompt and positive budgets are required")
        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        if self.db.get_task(task_id):
            raise ValueError(f"Task already exists: {task_id}")
        workspace_path, source_commit = self.workspaces.create(task_id, source_repo)
        self.db.create_task(task_id, prompt)
        self.db.create_quota_record(QuotaRecord(task_id=task_id, max_tokens_budget=budget_tokens,
                                                max_cost_usd_budget=budget_usd))
        decision, ticket = System1Router(self.db).route(
            prompt, task_id, workspace_path=workspace_path,
            source_commit=source_commit, model_override=model, modality=modality,
        )
        print(f"Task {task_id}: {decision.selected_capability} ({decision.confidence_score:.2f})")
        if ticket is None:
            self.db.set_task_status(task_id, "halted")
            print(decision.reasoning_summary, file=sys.stderr)
            return task_id

        self.process_queue(task_id, gateway_url)
        return task_id

    def process_queue(self, task_id: str, gateway_url: Optional[str] = None) -> None:
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
                result = subprocess.run([sys.executable, "-m", "gh0st.worker", next_ticket.ticket_id,
                                         "--db-url", self.db.db_url, "--claim-owner", owner],
                                        env=environment, check=False, capture_output=True, text=True)
            except OSError as exc:
                self.db.recover_abandoned_ticket(
                    next_ticket.ticket_id, f"Worker could not start: {exc}",
                    worker_exited=True, expected_owner=owner,
                )
                return
            if result.stdout:
                print(result.stdout.strip())
            if result.stderr:
                print(result.stderr.strip(), file=sys.stderr)
            processed += 1
            if result.returncode != 0:
                report = self.db.get_worker_report(next_ticket.ticket_id)
                if report is None:
                    self.db.recover_abandoned_ticket(
                        next_ticket.ticket_id, "Worker exited without a report",
                        worker_exited=True, expected_owner=owner,
                    )
                    report = self.db.get_worker_report(next_ticket.ticket_id)
                self.db.set_task_status(task_id, "halted" if report and report.status == "halted" else "failed")
                return
            if self.db.get_worker_report(next_ticket.ticket_id) is None:
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
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--task-id")
    run.add_argument("--budget", "--budget-usd", dest="budget_usd", type=float, default=5.0)
    run.add_argument("--tokens", "--budget-tokens", dest="budget_tokens", type=int, default=100000)
    run.add_argument("--gateway-url")
    run.add_argument("--source-repo", help="Repository to clone into this task's workspace")
    run.add_argument("--model", help="Explicit model target override")
    run.add_argument("--modality", choices=["text", "code", "vision", "image"])
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
        if args.command == "run":
            task_id = cli.run_prompt(args.prompt, args.task_id, args.budget_usd, args.budget_tokens,
                                     args.gateway_url, args.source_repo, args.model, args.modality)
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
