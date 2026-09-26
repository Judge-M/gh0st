"""Trusted model metadata used for placement before gateway dispatch."""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    latency_rank: int = Field(default=100, ge=0,
                              description="Relative latency rank; lower values are faster")
    fallback_models: list[str] = Field(default_factory=list)
    pricing_model: str = ""
    input_cost_per_million_usd: float | None = Field(default=None, ge=0)
    output_cost_per_million_usd: float | None = Field(default=None, ge=0)
    image_generation_cost_usd: float | None = Field(default=None, gt=0)
    placement: ComputePlacement = ComputePlacement.CLOUD_API

    @model_validator(mode="after")
    def validate_custom_pricing(self):
        if (self.input_cost_per_million_usd is None) != (self.output_cost_per_million_usd is None):
            raise ValueError("Configure both input and output rates for custom model pricing")
        return self


class ModelCatalog:
    def __init__(self, targets: list[ModelTarget]):
        names = [target.name for target in targets]
        if len(names) != len(set(names)):
            raise ValueError("Model target names must be unique")
        self.targets = targets

    def resolve_fallbacks(self, target: ModelTarget) -> list[ModelTarget]:
        resolved = []
        for configured in target.fallback_models:
            match = next((item for item in self.targets
                          if configured in {item.name, item.gateway_model}), None)
            if match is None:
                raise ValueError(f"Unknown fallback model {configured!r} for {target.name}")
            if match.gateway_model == target.gateway_model:
                raise ValueError("A model cannot fall back to itself")
            resolved.append(match)
        return resolved

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
               domain: str, tier: ExecutionTier, override: str | None = None,
               strategy: str = "smart") -> ModelTarget:
        strategies = {"smart", "cost", "quality", "speed", "local"}
        if strategy not in strategies:
            raise ValueError(f"Unknown routing strategy {strategy!r}; choose from {', '.join(sorted(strategies))}")
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
        domain_miss = lambda target: domain not in target.domains
        quality_rank = lambda target: 0 if target.quality == "frontier" else 1
        placement_rank = lambda target: {
            ComputePlacement.LOCAL_GPU: 0,
            ComputePlacement.RENTED_GPU: 1,
            ComputePlacement.CLOUD_API: 2,
        }[target.placement]
        sort_keys = {
            "smart": lambda target: (domain_miss(target), quality_rank(target),
                                     target.latency_rank, target.cost_rank, target.name),
            "cost": lambda target: (target.cost_rank, domain_miss(target),
                                    quality_rank(target), target.latency_rank, target.name),
            "quality": lambda target: (quality_rank(target), domain_miss(target),
                                       target.latency_rank, target.cost_rank, target.name),
            "speed": lambda target: (target.latency_rank, domain_miss(target),
                                     quality_rank(target), target.cost_rank, target.name),
            "local": lambda target: (placement_rank(target), domain_miss(target),
                                     target.latency_rank, quality_rank(target), target.cost_rank, target.name),
        }
        return min(candidates, key=sort_keys[strategy])
