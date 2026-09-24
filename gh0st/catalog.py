"""Trusted model metadata used for placement before gateway dispatch."""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import ComputePlacement, ExecutionTier


class ModelTarget(BaseModel):
    name: str
    gateway_model: str
    modalities: set[Literal["text", "code", "vision", "image"]] = Field(default_factory=lambda: {"text", "code"})
    context_window: int = Field(ge=512)
    max_payload_tokens: int = Field(ge=1)
    quality: Literal["basic", "frontier"] = "basic"
    domains: set[str] = Field(default_factory=set)
    cost_rank: int = Field(default=100, ge=0)
    placement: ComputePlacement = ComputePlacement.CLOUD_API


class ModelCatalog:
    def __init__(self, targets: list[ModelTarget]):
        names = [target.name for target in targets]
        if len(names) != len(set(names)):
            raise ValueError("Model target names must be unique")
        self.targets = targets

    @classmethod
    def from_environment(cls) -> "ModelCatalog":
        configured = os.getenv("GH0ST_MODEL_CATALOG", "")
        if configured:
            value = configured if configured.lstrip().startswith("[") else Path(configured).read_text(encoding="utf-8")
            raw = json.loads(value)
            if not isinstance(raw, list):
                raise ValueError("GH0ST_MODEL_CATALOG must be a JSON array or a path to one")
            return cls([ModelTarget.model_validate(item) for item in raw])
        default = os.getenv("GH0ST_DEFAULT_MODEL", "")
        if not default:
            return cls([])
        return cls([ModelTarget(
            name=default, gateway_model=default,
            context_window=int(os.getenv("GH0ST_DEFAULT_MODEL_CONTEXT", "8192")),
            max_payload_tokens=int(os.getenv("GH0ST_DEFAULT_MODEL_PAYLOAD", "4096")),
            quality=os.getenv("GH0ST_DEFAULT_MODEL_QUALITY", "basic"),
        )])

    def select(self, modality: str, payload_tokens: int, output_tokens: int,
               domain: str, tier: ExecutionTier, override: str | None = None) -> ModelTarget:
        candidates = [target for target in self.targets if (
            modality in target.modalities
            and payload_tokens <= target.max_payload_tokens
            and payload_tokens + output_tokens <= target.context_window
        )]
        if override:
            for target in candidates:
                if override in {target.name, target.gateway_model}:
                    return target
            raise ValueError(f"Model override {override!r} is unavailable or cannot fit this task")
        if tier == ExecutionTier.HIGH_COMPLEXITY:
            candidates = [target for target in candidates if target.quality == "frontier"]
        if not candidates:
            raise ValueError("No configured model meets the modality, context, and quality requirements")
        return min(candidates, key=lambda target: (domain not in target.domains, target.cost_rank, target.name))
