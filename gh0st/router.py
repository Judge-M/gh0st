"""Deterministic System-1 routing and budget admission."""

from abc import ABC, abstractmethod
import os
import re
import json
from typing import Optional

import httpx
import tiktoken
from pydantic import BaseModel, Field

from .catalog import ModelCatalog
from .db import StateLedger
from .models import ComputePlacement, ExecutionTier, System1Decision, Ticket, ToolSchema
from .reference import select_memory_route
from .workspace import MAX_IMAGE_COUNT


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
        workbench = ToolSchema(
            name="local_workbench", description="Execute inside the isolated task workspace or request an external grant",
            parameters={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["run_command", "request_grant"]},
                "command": {"type": "string", "description": "Shell command for an isolated local action"},
                "purpose": {"type": "string", "enum": ["inspect", "edit", "test", "lint", "git", "other"]},
                "action_type": {"type": "string", "description": "External action requiring a human grant"},
                "details": {"type": "object"},
            }, "required": ["action"], "additionalProperties": False},
        )
        planner = ToolSchema(
            name="emit_child_tickets", description="Propose bounded child tickets",
            parameters={"type": "object", "properties": {"child_proposals": {
                "type": "array", "items": {"type": "object", "properties": {
                    "capability": {"type": "string"}, "objective": {"type": "string"},
                    "context_slice": {"type": "object"},
                }, "required": ["capability", "objective"], "additionalProperties": False},
            }}, "required": ["child_proposals"], "additionalProperties": False},
        )
        image_generator = ToolSchema(
            name="generate_image",
            description="Generate one image and save it as an artifact in this isolated task workspace",
            parameters={"type": "object", "properties": {
                "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
                "filename": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\\.png$"},
            }, "required": ["prompt", "filename"], "additionalProperties": False},
        )
        for name, description, keywords, tier, tool in (
            ("git_ops", "Repository operations", ["git", "commit", "push", "branch", "diff", "checkout"], ExecutionTier.LOW_COMPLEXITY, workbench),
            ("code_refactor", "Code analysis and edits", ["refactor", "rewrite", "fix", "bug", "optimize", "architecture", "code"], ExecutionTier.HIGH_COMPLEXITY, workbench),
            ("unit_testing", "Run and analyze tests", ["test", "pytest", "lint", "coverage"], ExecutionTier.LOW_COMPLEXITY, workbench),
            ("planner", "Decompose an ambiguous objective", ["plan", "decompose", "roadmap", "milestone"], ExecutionTier.HIGH_COMPLEXITY, planner),
        ):
            self.register(CapabilityTemplate(
                capability_name=name, description=description, keywords=keywords,
                default_tier=tier,
                default_tool_schema=tool,
            ))
        self.register(CapabilityTemplate(
            capability_name="image_generation",
            description="Generate an image artifact from a prompt",
            keywords=["generate", "create", "draw", "illustrate", "image", "picture", "artwork"],
            default_tier=ExecutionTier.LOW_COMPLEXITY,
            default_tool_schema=image_generator,
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
            if template.capability_name in {"planner", "image_generation"}:
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
        with httpx.Client(timeout=0.09) as client:
            response = client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            decision = response.json()
        capability = str(decision["capability"])
        confidence = float(decision["confidence"])
        if not 0 <= confidence <= 1:
            raise ValueError("Classifier confidence must be between zero and one")
        return capability, confidence


class JevClassifier(EndpointClassifier):
    """Optional fast external classifier, with local fallback in System1Router."""


class System1Router:
    def __init__(self, db: StateLedger, classifier: Optional[System1Classifier] = None,
                 registry: Optional[TemplateRegistry] = None, catalog: Optional[ModelCatalog] = None,
                 reference_provider_names: Optional[list[str]] = None):
        self.db = db
        if classifier is None:
            selected = os.getenv("GH0ST_ROUTER_MODEL", "layla").casefold()
            if selected == "layla":
                classifier = LaylaClassifier()
            elif selected == "jev":
                endpoint = os.getenv("GH0ST_ROUTER_ENDPOINT", "")
                if not endpoint:
                    raise ValueError("GH0ST_ROUTER_ENDPOINT is required for the jev router")
                classifier = JevClassifier(endpoint, os.getenv("GH0ST_ROUTER_API_KEY", ""))
            else:
                raise ValueError(f"Unsupported router model: {selected}")
        self.classifier = classifier
        self.registry = registry or TemplateRegistry()
        self.catalog = catalog or ModelCatalog.from_environment()
        self.reference_provider_names = reference_provider_names

    def route(self, prompt: str, task_id: str, parent_ticket_id: Optional[str] = None,
              depth: int = 0, capability: Optional[str] = None,
              context_slice: Optional[dict] = None, workspace_path: Optional[str] = None,
              source_commit: Optional[str] = None, model_override: Optional[str] = None,
              modality: Optional[str] = None,
              routing_strategy: Optional[str] = None) -> tuple[System1Decision, Optional[Ticket]]:
        quota = self.db.get_quota_record(task_id)
        if quota is None:
            raise ValueError(f"Task {task_id} has no quota")
        if not quota.is_active or quota.remaining_tokens <= 0 or quota.remaining_usd <= 0:
            return self._halt(prompt, "Task quota is inactive or exhausted"), None
        if depth > int(os.getenv("GH0ST_MAX_DEPTH", "4")):
            return self._halt(prompt, "Maximum ticket depth reached"), None
        parent = self.db.get_ticket(parent_ticket_id) if parent_ticket_id else None
        if parent_ticket_id and parent is None:
            raise ValueError(f"Unknown parent ticket: {parent_ticket_id}")
        if parent and parent.task_id != task_id:
            raise ValueError("Parent ticket belongs to another task")
        workspace_path = workspace_path or (parent.workspace_path if parent else None)
        source_commit = source_commit or (parent.source_commit if parent else None)
        if not workspace_path:
            raise ValueError("A task-scoped workspace is required")

        if modality == "image":
            matched, confidence = "image_generation", 0.98
        elif capability is None:
            try:
                matched, confidence = self.classifier.classify(prompt, self.registry.list_all())
            except (httpx.HTTPError, ValueError, KeyError):
                matched, confidence = LaylaClassifier().classify(prompt, self.registry.list_all())
        else:
            matched, confidence = capability, 1.0
        if confidence < 0.90:
            matched = "planner"
        template = self.registry.get(matched)
        if template is None:
            return self._halt(prompt, f"Unknown capability: {matched}"), None
        tier = template.default_tier
        selected_modality = modality or ("code" if matched in {"code_refactor", "unit_testing", "git_ops"} else "text")
        selected_strategy = routing_strategy or os.getenv("GH0ST_ROUTING_STRATEGY", "smart")
        context = context_slice or {}
        image_inputs = context.get("image_inputs", [])
        if (not isinstance(image_inputs, list) or len(image_inputs) > MAX_IMAGE_COUNT
                or any(not isinstance(item, dict) for item in image_inputs)):
            return self._halt(prompt, "Ticket image inputs must contain at most eight valid records"), None
        image_token_reserve = 0
        for item in image_inputs:
            reserve = item.get("reserved_tokens")
            if not isinstance(reserve, int) or reserve < 1:
                return self._halt(prompt, "Ticket image input has no valid token reservation"), None
            image_token_reserve += reserve
        payload_tokens = (int(len(tiktoken.get_encoding("cl100k_base").encode(
            json.dumps({"prompt": prompt, "context": context}))) * 1.2) + 128
            + image_token_reserve)
        remaining_tokens = min(quota.remaining_tokens, template.default_max_tokens)
        remaining_usd = min(quota.remaining_usd, template.default_max_cost_usd)
        if remaining_tokens <= 0 or remaining_usd <= 0:
            return self._halt(prompt, "Insufficient task budget"), None
        try:
            memory_route = select_memory_route(prompt, matched, self.reference_provider_names)
            target = self.catalog.select(selected_modality, payload_tokens,
                                         min(remaining_tokens, 1024), matched, tier, model_override,
                                         selected_strategy)
            if selected_modality == "image" and target.image_generation_cost_usd is None:
                raise ValueError("Image model selection requires an explicit image_generation_cost_usd catalog price")
            fallback_targets = self.catalog.resolve_fallbacks(target)
        except ValueError as exc:
            return self._halt(prompt, str(exc)), None
        ticket = Ticket(
            task_id=task_id, parent_ticket_id=parent_ticket_id, depth=depth,
            capability=matched, objective=prompt,
            context_slice=({**context, "allow_image_generation": True}
                           if matched == "image_generation" else context),
            workspace_path=workspace_path, source_commit=source_commit,
            permitted_local_capabilities=["local_shell"],
            gateway_allowance={"enabled": True, "model": target.gateway_model,
                               "max_calls": 12, "context_window": target.context_window,
                               "fallback_models": [item.gateway_model for item in fallback_targets],
                               "fallback_pricing": {item.gateway_model: {
                                   "pricing_model": item.pricing_model,
                                   "input_cost_per_million_usd": item.input_cost_per_million_usd,
                                   "output_cost_per_million_usd": item.output_cost_per_million_usd,
                               } for item in fallback_targets},
                               "pricing_model": target.pricing_model,
                               "input_cost_per_million_usd": target.input_cost_per_million_usd,
                               "output_cost_per_million_usd": target.output_cost_per_million_usd,
                               "image_generation_cost_usd": target.image_generation_cost_usd},
            assigned_model=target.gateway_model,
            routing_strategy=selected_strategy,
            attached_tool_schema=template.default_tool_schema,
            memory_route=memory_route,
            max_tokens_allocated=remaining_tokens, max_cost_usd=remaining_usd,
        )
        try:
            self.db.create_ticket(ticket)
        except ValueError as exc:
            return self._halt(prompt, str(exc)), None
        return System1Decision(
            prompt=prompt, confidence_score=confidence, selected_capability=matched,
            selected_model=target.gateway_model,
            selected_routing_strategy=selected_strategy,
            execution_tier=tier, compute_placement=target.placement,
            attached_tool_schema=template.default_tool_schema,
            memory_route=memory_route,
            requires_planner=matched == "planner",
            reasoning_summary=(f"OAG selected {matched}, model {target.name}, tool "
                               f"{template.default_tool_schema.name}, and memory providers "
                               f"{memory_route.reference_providers or ['SQLite rules only']} "
                               f"using {selected_strategy} routing with {confidence:.2f} confidence"),
        ), ticket

    @staticmethod
    def _halt(prompt: str, reason: str) -> System1Decision:
        return System1Decision(
            prompt=prompt, confidence_score=0, selected_capability="none",
            execution_tier=ExecutionTier.LOW_COMPLEXITY,
            compute_placement=ComputePlacement.LOCAL_GPU,
            reasoning_summary=reason,
        )
