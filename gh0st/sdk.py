"""Embeddable public API for creating and executing gh0st work tickets."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Optional

from .db import StateLedger
from .models import QuotaRecord, System1Decision, Ticket, WorkerReport
from .reference import ReferenceRetriever
from .router import System1Router
from .worker import EphemeralWorker
from .workspace import WorkspaceManager
from .secrets import SecretResolver


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    decision: System1Decision
    ticket: Optional[Ticket]


class Gh0stClient:
    """Small synchronous SDK for bounded local task dispatch and execution."""

    def __init__(self, db_url: str | None = None, workspace_root: str | None = None,
                 reference_retriever: ReferenceRetriever | list[ReferenceRetriever] | Mapping[str, ReferenceRetriever] | None = None,
                 secret_resolver: SecretResolver | None = None):
        self.ledger = StateLedger(db_url)
        self.ledger.init_db()
        self.workspaces = WorkspaceManager(workspace_root)
        if reference_retriever is None:
            self.reference_retrievers: dict[str, ReferenceRetriever] = {}
        elif isinstance(reference_retriever, Mapping):
            self.reference_retrievers = {str(name).casefold(): retriever
                                         for name, retriever in reference_retriever.items()}
        elif isinstance(reference_retriever, list):
            self.reference_retrievers = {retriever.provider_name.casefold(): retriever
                                         for retriever in reference_retriever}
        else:
            self.reference_retrievers = {reference_retriever.provider_name.casefold(): reference_retriever}
        self.secret_resolver = secret_resolver

    def dispatch(self, prompt: str, *, source_repo: str | None = None,
                 budget_usd: float = 5.0, budget_tokens: int = 100_000,
                 model: str | None = None, modality: str | None = None,
                 routing_strategy: str = "smart", task_id: str | None = None,
                 image_paths: list[str] | None = None) -> DispatchResult:
        if not prompt.strip() or budget_usd <= 0 or budget_tokens < 1:
            raise ValueError("A prompt and positive token and cost budgets are required")
        image_paths = image_paths or []
        if image_paths and modality not in {None, "vision"}:
            raise ValueError("Image inputs require the vision modality")
        prepared_images = self.workspaces.validate_image_inputs(image_paths)
        selected_modality = "vision" if prepared_images else modality
        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        if self.ledger.get_task(task_id):
            raise ValueError(f"Task already exists: {task_id}")
        workspace_path, source_commit = self.workspaces.create(task_id, source_repo)
        image_records = self.workspaces.store_image_inputs(task_id, workspace_path, prepared_images)
        context_slice = {}
        if image_records:
            context_slice = {"image_inputs": image_records}
        self.ledger.create_task(task_id, prompt)
        self.ledger.create_quota_record(QuotaRecord(task_id=task_id,
            max_tokens_budget=budget_tokens, max_cost_usd_budget=budget_usd))
        decision, ticket = System1Router(
            self.ledger, reference_provider_names=list(self.reference_retrievers) or None
        ).route(
            prompt, task_id, workspace_path=workspace_path, source_commit=source_commit,
            model_override=model, modality=selected_modality, routing_strategy=routing_strategy,
            context_slice=context_slice)
        if ticket is None:
            self.ledger.set_task_status(task_id, "halted")
        return DispatchResult(task_id, decision, ticket)

    def execute_turn(self, ticket_id: str) -> WorkerReport:
        return EphemeralWorker(self.ledger, reference_retriever=self.reference_retrievers,
                               secret_resolver=self.secret_resolver,
                               workspace_root=str(self.workspaces.root)).execute_turn(ticket_id)


__all__ = ["DispatchResult", "Gh0stClient"]
