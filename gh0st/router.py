"""Deterministic System-1 routing and budget admission."""

from abc import ABC, abstractmethod
import os
import re
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from .db import StateLedger
from .models import ComputePlacement, ExecutionTier, System1Decision, Ticket, ToolSchema


class CapabilityTemplate(BaseModel):
    capability_name: str
    description: str
    keywords: list[str]
    default_tier: ExecutionTier
    default_tool_schema: ToolSchema
    default_max_tokens: int = Field(default=4096, ge=1)
    default_max_cost_usd: float = Field(default=0.50, ge=0)


class TemplateRegistry:
    def __init__(self):
        self._templates: dict[str, CapabilityTemplate] = {}
        for name, description, keywords, tier, tool in (
            ("git_ops", "Repository operations", ["git", "commit", "push", "branch", "diff", "checkout"], ExecutionTier.LOW_COMPLEXITY, "git_status"),
            ("code_refactor", "Code analysis and edits", ["refactor", "rewrite", "fix", "bug", "optimize", "architecture", "code"], ExecutionTier.HIGH_COMPLEXITY, "file_patch_proposal"),
            ("unit_testing", "Run and analyze tests", ["test", "pytest", "lint", "coverage"], ExecutionTier.LOW_COMPLEXITY, "test_result_proposal"),
            ("planner", "Decompose an ambiguous objective", ["plan", "decompose", "roadmap", "milestone"], ExecutionTier.HIGH_COMPLEXITY, "emit_child_tickets"),
        ):
            self.register(CapabilityTemplate(
                capability_name=name, description=description, keywords=keywords,
                default_tier=tier,
                default_tool_schema=ToolSchema(name=tool, description=description, parameters={"type": "object", "properties": {}}),
            ))

    def register(self, template: CapabilityTemplate) -> None:
        self._templates[template.capability_name] = template

    def get(self, capability: str) -> Optional[CapabilityTemplate]:
        return self._templates.get(capability)

    def list_all(self) -> list[CapabilityTemplate]:
        return list(self._templates.values())


class System1Classifier(ABC):
    @abstractmethod
    def classify(self, prompt: str, templates: list[CapabilityTemplate]) -> tuple[str, float]:
        """Return a capability and confidence between zero and one."""


class LaylaClassifier(System1Classifier):
    """Local deterministic classifier; no model download or network call."""

    def classify(self, prompt: str, templates: list[CapabilityTemplate]) -> tuple[str, float]:
        value = prompt.casefold()
        ranked: list[tuple[int, str]] = []
        for template in templates:
            if template.capability_name == "planner":
                continue
            count = sum(bool(re.search(r"\b" + re.escape(word.casefold()) + r"\b", value)) for word in template.keywords)
            ranked.append((count, template.capability_name))
        ranked.sort(reverse=True)
        if not ranked or ranked[0][0] == 0:
            return "planner", 0.5
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return "planner", 0.7
        return ranked[0][1], min(0.98, 0.85 + ranked[0][0] * 0.05)


class EndpointClassifier(System1Classifier):
    """Adapter for a separately hosted classification endpoint."""

    def __init__(self, endpoint: str, api_key: str = ""):
        if not endpoint.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
            raise ValueError("Router endpoint must use HTTPS or loopback HTTP")
        self.endpoint = endpoint
        self.api_key = api_key

    def classify(self, prompt: str, templates: list[CapabilityTemplate]) -> tuple[str, float]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {"prompt": prompt, "templates": [
            {"capability_name": item.capability_name, "description": item.description}
            for item in templates]}
        with httpx.Client(timeout=2.0) as client:
            response = client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            decision = response.json()
        capability = str(decision["capability"])
        confidence = float(decision["confidence"])
        if not 0 <= confidence <= 1:
            raise ValueError("Classifier confidence must be between zero and one")
        return capability, confidence


class System1Router:
    def __init__(self, db: StateLedger, classifier: Optional[System1Classifier] = None, registry: Optional[TemplateRegistry] = None):
        self.db = db
        if classifier is None:
            selected = os.getenv("GH0ST_ROUTER_MODEL", "layla").casefold()
            if selected == "layla":
                classifier = LaylaClassifier()
            elif selected == "jev":
                endpoint = os.getenv("GH0ST_ROUTER_ENDPOINT", "")
                if not endpoint:
                    raise ValueError("GH0ST_ROUTER_ENDPOINT is required for the jev router")
                classifier = EndpointClassifier(endpoint, os.getenv("GH0ST_ROUTER_API_KEY", ""))
            else:
                raise ValueError(f"Unsupported router model: {selected}")
        self.classifier = classifier
        self.registry = registry or TemplateRegistry()

    def route(self, prompt: str, task_id: str, parent_ticket_id: Optional[str] = None,
              depth: int = 0, capability: Optional[str] = None,
              context_slice: Optional[dict] = None) -> tuple[System1Decision, Optional[Ticket]]:
        quota = self.db.get_quota_record(task_id)
        if quota is None:
            raise ValueError(f"Task {task_id} has no quota")
        if not quota.is_active or quota.remaining_tokens <= 0 or quota.remaining_usd <= 0:
            return self._halt(prompt, "Task quota is inactive or exhausted"), None
        if depth > int(os.getenv("GH0ST_MAX_DEPTH", "4")):
            return self._halt(prompt, "Maximum ticket depth reached"), None
        if parent_ticket_id and self.db.get_ticket(parent_ticket_id) is None:
            raise ValueError(f"Unknown parent ticket: {parent_ticket_id}")

        if capability is None:
            matched, confidence = self.classifier.classify(prompt, self.registry.list_all())
        else:
            matched, confidence = capability, 1.0
        if confidence < 0.90:
            matched = "planner"
        template = self.registry.get(matched)
        if template is None:
            return self._halt(prompt, f"Unknown capability: {matched}"), None
        tier = template.default_tier
        placement = ComputePlacement.CLOUD_API if tier == ExecutionTier.HIGH_COMPLEXITY else ComputePlacement.LOCAL_GPU
        remaining_tokens = min(quota.remaining_tokens, template.default_max_tokens)
        remaining_usd = min(quota.remaining_usd, template.default_max_cost_usd)
        if remaining_tokens <= 0 or remaining_usd <= 0:
            return self._halt(prompt, "Insufficient task budget"), None
        ticket = Ticket(
            task_id=task_id, parent_ticket_id=parent_ticket_id, depth=depth,
            capability=matched, objective=prompt, context_slice=context_slice or {},
            attached_tool_schema=template.default_tool_schema,
            max_tokens_allocated=remaining_tokens, max_cost_usd=remaining_usd,
        )
        self.db.create_ticket(ticket)
        return System1Decision(
            prompt=prompt, confidence_score=confidence, selected_capability=matched,
            execution_tier=tier, compute_placement=placement,
            attached_tool_schema=template.default_tool_schema,
            requires_planner=matched == "planner",
            reasoning_summary=f"Selected {matched} with {confidence:.2f} confidence",
        ), ticket

    @staticmethod
    def _halt(prompt: str, reason: str) -> System1Decision:
        return System1Decision(
            prompt=prompt, confidence_score=0, selected_capability="none",
            execution_tier=ExecutionTier.LOW_COMPLEXITY,
            compute_placement=ComputePlacement.LOCAL_GPU,
            reasoning_summary=reason,
        )
