"""Interactive terminal shell; each entered turn still gets a fresh worker."""

import subprocess
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.text import Text

from .cli import Gh0stCLI


ROUTING_STRATEGIES = ("smart", "cost", "quality", "speed", "local")


def default_source_repository() -> Optional[str]:
    """Use the current repository as a read-only clone source when inside Git."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True,
        text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


class InteractiveShell:
    def __init__(self, cli: Gh0stCLI, *, source_repo: Optional[str] = None,
                 budget_usd: float = 5.0, budget_tokens: int = 100000,
                 model: str = "", routing_strategy: str = "smart",
                 session_id: Optional[str] = None, gateway_url: Optional[str] = None,
                 console: Optional[Console] = None):
        self.cli = cli
        self.console = console or Console()
        self.gateway_url = gateway_url
        if session_id:
            self.session = self.cli.db.get_interactive_session(session_id)
            if self.session is None:
                raise ValueError(f"Unknown interactive session: {session_id}")
            self.cli.db.update_interactive_session(session_id, closed=False)
        else:
            self.session = self.cli.create_interactive_session(
                source_repo or default_source_repository(), budget_usd, budget_tokens,
                model, routing_strategy,
            )
        self.session_id = self.session["session_id"]

    def _budget_text(self) -> str:
        quota = self.cli.db.get_quota_record(self.session["task_id"])
        return f"${quota.remaining_usd:.2f} remaining" if quota else "unavailable"

    def _show_banner(self) -> None:
        session = self.cli.db.get_interactive_session(self.session_id)
        model = session["selected_model"] or f"auto ({session['routing_strategy']})"
        self.console.print(Text("👻 gh0st v1.0 (Interactive Session)", style="bold cyan"))
        self.console.print(f"Model: {model} | Budget: {self._budget_text()} | Session: {self.session_id}")
        self.console.print("Type /help for commands, or start typing below.\n")

    def _handle_command(self, line: str) -> bool:
        parts = line.strip().split(maxsplit=1)
        command = parts[0].casefold()
        argument = parts[1].strip() if len(parts) > 1 else ""
        session = self.cli.db.get_interactive_session(self.session_id)
        if command in {"/exit", "/quit"}:
            self.cli.db.update_interactive_session(self.session_id, closed=True)
            return False
        if command == "/help":
            self.console.print(
                "/model [provider:model|auto]  Select a fixed model or return to routing\n"
                "/routing [smart|cost|quality|speed|local]  Select a placement policy\n"
                "/budget [USD]  Show or raise the session budget\n"
                "/clear  Clear conversation context; keep task files and ledger records\n"
                "/exit  Close this terminal session"
            )
        elif command == "/model":
            if not argument:
                self.console.print(f"Model override: {session['selected_model'] or 'automatic routing'}")
            else:
                model = "" if argument.casefold() == "auto" else argument
                self.cli.db.update_interactive_session(self.session_id, selected_model=model)
                self.console.print(f"Model set to: {model or 'automatic routing'}")
        elif command == "/routing":
            if not argument:
                self.console.print(f"Routing strategy: {session['routing_strategy']}")
            elif argument not in ROUTING_STRATEGIES:
                self.console.print(f"Choose one of: {', '.join(ROUTING_STRATEGIES)}")
            else:
                self.cli.db.update_interactive_session(self.session_id, routing_strategy=argument)
                self.console.print(f"Routing strategy set to: {argument}")
        elif command == "/budget":
            quota = self.cli.db.get_quota_record(session["task_id"])
            if not argument:
                self.console.print(f"Budget: ${quota.max_cost_usd_budget:.2f} total; {self._budget_text()}")
            else:
                try:
                    total = float(argument.removeprefix("$"))
                    self.cli.db.set_session_cost_budget(self.session_id, total)
                    self.console.print(f"Session budget set to ${total:.2f}")
                except ValueError as exc:
                    self.console.print(f"Budget unchanged: {exc}", style="yellow")
        elif command == "/clear":
            self.cli.db.clear_session_history(self.session_id)
            self.console.print("Conversation context cleared. Task workspace and audit history remain.")
        else:
            self.console.print(f"Unknown command: {command}. Type /help for commands.", style="yellow")
        return True

    def process_prompt(self, prompt: str) -> None:
        if prompt.startswith("/"):
            self._handle_command(prompt)
            return
        try:
            streamed_parts = []

            def show_route(decision, ticket):
                self.console.print(
                    f"[System-1 Router] Layla: '{decision.selected_capability}' "
                    f"| strategy: {decision.selected_routing_strategy} "
                    f"| model: {decision.selected_model} "
                    f"| tool: {decision.attached_tool_schema.name if decision.attached_tool_schema else 'none'}"
                )
                self.console.print("[Worker] Spawning ephemeral worker...")

            decision, report = self.cli.run_session_turn(
                self.session_id, prompt, self.gateway_url, on_routed=show_route,
                on_text_delta=lambda text: (streamed_parts.append(text), self.console.print(
                    text, end="", markup=False, highlight=False)),
            )
            if streamed_parts:
                self.console.print()
            elif report.output.get("content"):
                self.console.print()
                self.console.print(str(report.output["content"]), markup=False)
            for command in report.commands_executed:
                purpose = command.get("purpose", "action")
                result = "✓" if command.get("exit_code") == 0 else "✗"
                self.console.print(f"  {result} {purpose}")
            if report.stderr:
                self.console.print(report.stderr, style="yellow")
            self.console.print(
                f"[Worker] Turn {report.status}. Cost: ${report.cost_usd:.4f} "
                "| worker process exited.\n"
            )
            self.session = self.cli.db.get_interactive_session(self.session_id)
        except ValueError as exc:
            self.console.print(f"Turn halted: {exc}", style="yellow")

    def run(self) -> str:
        self._show_banner()
        prompts: PromptSession = PromptSession(history=InMemoryHistory())
        try:
            while True:
                try:
                    line = prompts.prompt("gh0st> ")
                except KeyboardInterrupt:
                    self.console.print("\nUse /exit to close the session.")
                    continue
                except EOFError:
                    self.cli.db.update_interactive_session(self.session_id, closed=True)
                    break
                if line.strip() == "/exit" or line.strip() == "/quit":
                    self._handle_command(line)
                    break
                if line.strip():
                    self.process_prompt(line.strip())
        finally:
            self.console.print(f"Session {self.session_id} saved.")
        return self.session_id
