"""Command line entry points for dispatch and status."""

import argparse
import os
import subprocess
import sys
import uuid
from typing import Optional

from .db import StateLedger
from .models import QuotaRecord, TicketStatus
from .router import System1Router


class Gh0stCLI:
    def __init__(self, db_url: Optional[str] = None):
        self.db = StateLedger(db_url)
        self.db.init_db()

    def run_prompt(self, prompt: str, task_id: Optional[str] = None, budget_usd: float = 5.0,
                   budget_tokens: int = 100000, gateway_url: Optional[str] = None) -> str:
        if not prompt.strip() or budget_usd <= 0 or budget_tokens < 1:
            raise ValueError("A prompt and positive budgets are required")
        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        if self.db.get_task(task_id):
            raise ValueError(f"Task already exists: {task_id}")
        self.db.create_task(task_id, prompt)
        self.db.create_quota_record(QuotaRecord(task_id=task_id, max_tokens_budget=budget_tokens,
                                                max_cost_usd_budget=budget_usd))
        decision, ticket = System1Router(self.db).route(prompt, task_id)
        print(f"Task {task_id}: {decision.selected_capability} ({decision.confidence_score:.2f})")
        if ticket is None:
            self.db.set_task_status(task_id, "halted")
            print(decision.reasoning_summary, file=sys.stderr)
            return task_id

        self.process_queue(task_id, gateway_url)
        return task_id

    def process_queue(self, task_id: str, gateway_url: Optional[str] = None) -> None:
        environment = os.environ.copy()
        if gateway_url:
            environment["GH0ST_GATEWAY_URL"] = gateway_url
        processed = 0
        while processed < 100:
            pending = [item for item in self.db.list_task_tickets(task_id) if item.status == TicketStatus.PENDING]
            if not pending:
                break
            next_ticket = pending[0]
            result = subprocess.run([sys.executable, "-m", "gh0st.worker", next_ticket.ticket_id,
                                     "--db-url", self.db.db_url], env=environment, check=False,
                                    capture_output=True, text=True)
            if result.stdout:
                print(result.stdout.strip())
            if result.stderr:
                print(result.stderr.strip(), file=sys.stderr)
            processed += 1
            if result.returncode != 0:
                self.db.set_task_status(task_id, "failed")
                return

        if processed == 100 and any(item.status == TicketStatus.PENDING for item in self.db.list_task_tickets(task_id)):
            self.db.set_task_status(task_id, "halted")
        else:
            self.db.set_task_status(task_id, "completed")
        return

    def show_status(self, task_id: str) -> dict:
        task = self.db.get_task(task_id)
        quota = self.db.get_quota_record(task_id)
        if task is None or quota is None:
            raise ValueError(f"Unknown task: {task_id}")
        tickets = self.db.list_task_tickets(task_id)
        status = {"task_id": task_id, "status": task.status,
                  "tokens_consumed": quota.tokens_consumed, "remaining_tokens": quota.remaining_tokens,
                  "cost_usd_consumed": quota.cost_usd_consumed, "remaining_usd": quota.remaining_usd,
                  "pending_tickets_count": sum(t.status == TicketStatus.PENDING for t in tickets),
                  "trusted_memory_count": len(self.db.get_trusted_valid_claims(task_id))}
        for key, value in status.items():
            print(f"{key}: {value}")
        return status


def main() -> None:
    parser = argparse.ArgumentParser(prog="gh0st")
    parser.add_argument("--db-url", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--task-id")
    run.add_argument("--budget", "--budget-usd", dest="budget_usd", type=float, default=5.0)
    run.add_argument("--tokens", "--budget-tokens", dest="budget_tokens", type=int, default=100000)
    run.add_argument("--gateway-url")
    status = commands.add_parser("status")
    status.add_argument("task_id")
    api = commands.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8383)
    args = parser.parse_args()
    cli = Gh0stCLI(args.db_url)
    try:
        if args.command == "run":
            task_id = cli.run_prompt(args.prompt, args.task_id, args.budget_usd, args.budget_tokens, args.gateway_url)
            sys.exit(0 if cli.db.get_task(task_id).status == "completed" else 1)
        if args.command == "status":
            cli.show_status(args.task_id)
        if args.command == "api":
            if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("GH0ST_API_TOKEN"):
                raise ValueError("Set GH0ST_API_TOKEN before binding the API beyond loopback")
            import uvicorn
            from .relay import create_app
            uvicorn.run(create_app(cli.db), host=args.host, port=args.port)
    except ValueError as exc:
        parser.exit(2, f"gh0st: {exc}\n")


if __name__ == "__main__":
    main()
