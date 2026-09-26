"""Untrusted, read-only reference retrieval adapters.

Retrieved material is never copied into the SQLite ledger. It is injected into
the current worker prompt as reference data and must not be treated as policy.
"""

from abc import ABC, abstractmethod
import json
import os
import re
from typing import Any

from pydantic import BaseModel, Field

from .models import MemoryRoute


_PROVIDER_NAMES = {"mem0", "pinecone", "qdrant"}
_ONTOLOGY_TERMS = {
    "codebase": ("code", "repo", "repository", "source", "function", "module", "bug", "refactor", "implementation"),
    "testing": ("test", "tests", "pytest", "coverage", "lint", "assertion"),
    "documentation": ("doc", "docs", "documentation", "readme", "guide", "manual", "reference"),
    "architecture": ("architecture", "architectural", "design", "component", "diagram", "system"),
    "operations": ("deploy", "deployment", "release", "build", "ci", "pipeline", "workflow", "configuration"),
    "planning": ("plan", "planning", "roadmap", "milestone", "decompose"),
    "project_memory": ("earlier", "previously", "remember", "preference", "decision", "we decided", "project context"),
}
_CAPABILITY_DOMAINS = {
    "code_refactor": ("codebase", "project_memory"),
    "unit_testing": ("testing", "codebase", "project_memory"),
    "git_ops": ("codebase", "operations", "project_memory"),
    "planner": ("planning", "project_memory"),
    "image_generation": (),
}


class ReferenceHit(BaseModel):
    text: str
    source: str = ""
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReferenceRetriever(ABC):
    provider_name = "external"

    @abstractmethod
    def retrieve(self, query: str, limit: int = 5) -> list[ReferenceHit]:
        """Return relevant unverified text without persisting it in gh0st."""


def ontology_domains(prompt: str, capability: str) -> list[str]:
    """Walk the local OAG memory taxonomy for the routed capability and prompt."""
    if capability == "image_generation":
        return []
    domains = set(_CAPABILITY_DOMAINS.get(capability, ()))
    value = prompt.casefold()
    for domain, terms in _ONTOLOGY_TERMS.items():
        if any(re.search(r"\b" + re.escape(term) + r"\b", value) for term in terms):
            domains.add(domain)
    if not domains:
        domains.add("general")
    return sorted(domains)


def _provider_route_config() -> dict[str, list[str]] | None:
    raw = os.getenv("GH0ST_MEMORY_ONTOLOGY_ROUTES", "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("GH0ST_MEMORY_ONTOLOGY_ROUTES must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("GH0ST_MEMORY_ONTOLOGY_ROUTES must be a JSON object")
    valid_domains = set(_ONTOLOGY_TERMS) | {"general", "default"}
    routes: dict[str, list[str]] = {}
    for domain, configured in value.items():
        key = str(domain).casefold()
        if key not in valid_domains:
            raise ValueError(f"Unknown memory ontology domain: {domain}")
        names = [configured] if isinstance(configured, str) else configured
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise ValueError(f"Memory routes for {domain} must be a provider name or list of names")
        normalized = [item.strip().casefold() for item in names if item.strip()]
        if any(name not in _PROVIDER_NAMES for name in normalized):
            raise ValueError(f"Memory routes for {domain} support mem0, pinecone, and qdrant providers")
        routes[key] = sorted(set(normalized))
    return routes


def _provider_names_from_environment() -> list[str]:
    raw = os.getenv("GH0ST_REFERENCE_PROVIDERS", "") or os.getenv("GH0ST_REFERENCE_PROVIDER", "")
    names = [name.strip().casefold() for name in raw.split(",") if name.strip()]
    if any(name not in _PROVIDER_NAMES for name in names):
        raise ValueError("Reference providers must be mem0, pinecone, or qdrant")
    return list(dict.fromkeys(names))


def select_memory_route(prompt: str, capability: str,
                        available_providers: list[str] | None = None) -> MemoryRoute:
    """Select trusted local rules and ontology-matched external reference adapters."""
    domains = ontology_domains(prompt, capability)
    if capability == "image_generation":
        return MemoryRoute(system_rules=True, ontology_domains=domains, reference_providers=[])

    routes = _provider_route_config()
    if routes is not None:
        selected: set[str] = set(routes.get("default", []))
        for domain in domains:
            selected.update(routes.get(domain, []))
        return MemoryRoute(system_rules=True, ontology_domains=domains,
                           reference_providers=sorted(selected))

    names = available_providers if available_providers is not None else _provider_names_from_environment()
    normalized = list(dict.fromkeys(name.strip().casefold() for name in names if name.strip()))
    if any(name not in _PROVIDER_NAMES for name in normalized):
        raise ValueError("Reference providers must be mem0, pinecone, or qdrant")
    if len(normalized) > 1:
        raise ValueError("Configure GH0ST_MEMORY_ONTOLOGY_ROUTES to route between multiple memory providers")
    # Backwards compatibility: a single configured adapter remains the default
    # for all eligible turns; multiple adapters require explicit OAG routes.
    return MemoryRoute(system_rules=True, ontology_domains=domains,
                       reference_providers=normalized)


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


class Mem0Retriever(ReferenceRetriever):
    provider_name = "Mem0"

    def __init__(self, client: Any = None, user_id: str | None = None):
        if client is None:
            try:
                from mem0 import MemoryClient
            except ImportError as exc:
                raise RuntimeError("Install gh0st-core[mem0] to use Mem0 retrieval") from exc
            api_key = os.getenv("MEM0_API_KEY")
            if not api_key:
                raise ValueError("MEM0_API_KEY is required for Mem0 retrieval")
            client = MemoryClient(api_key=api_key)
        self.client = client
        self.user_id = user_id or os.getenv("GH0ST_MEM0_USER_ID", "")
        if not self.user_id:
            raise ValueError("GH0ST_MEM0_USER_ID is required to scope Mem0 retrieval")

    def retrieve(self, query: str, limit: int = 5) -> list[ReferenceHit]:
        response = self.client.search(query, filters={"user_id": self.user_id}, top_k=limit)
        records = _field(response, "results", response)
        return [ReferenceHit(
            text=str(_field(item, "memory", _field(item, "text", ""))),
            source=str(_field(item, "id", "Mem0")),
            score=_field(item, "score"),
            metadata=dict(_field(item, "metadata", {}) or {}),
        ) for item in records if _field(item, "memory", _field(item, "text", ""))]


class PineconeRetriever(ReferenceRetriever):
    provider_name = "Pinecone"

    def __init__(self, index: Any = None, *, index_name: str | None = None,
                 namespace: str | None = None, text_field: str | None = None):
        self.namespace = namespace or os.getenv("GH0ST_PINECONE_NAMESPACE", "__default__")
        self.text_field = text_field or os.getenv("GH0ST_PINECONE_TEXT_FIELD", "text")
        if index is None:
            try:
                from pinecone import Pinecone
            except ImportError as exc:
                raise RuntimeError("Install gh0st-core[pinecone] to use Pinecone retrieval") from exc
            key = os.getenv("PINECONE_API_KEY")
            name = index_name or os.getenv("GH0ST_PINECONE_INDEX", "")
            if not key or not name:
                raise ValueError("PINECONE_API_KEY and GH0ST_PINECONE_INDEX are required")
            index = Pinecone(api_key=key).index(name)
        self.index = index

    def retrieve(self, query: str, limit: int = 5) -> list[ReferenceHit]:
        result = self.index.search(namespace=self.namespace, inputs={"text": query},
                                   top_k=limit, fields=[self.text_field])
        records = _field(_field(result, "result", {}), "hits", [])
        hits: list[ReferenceHit] = []
        for record in records:
            fields = _field(record, "fields", {}) or {}
            text = fields.get(self.text_field, "") if isinstance(fields, dict) else ""
            if text:
                hits.append(ReferenceHit(text=str(text), source=str(_field(record, "id", "Pinecone")),
                                         score=_field(record, "score"), metadata=fields))
        return hits


class QdrantRetriever(ReferenceRetriever):
    provider_name = "Qdrant"

    def __init__(self, client: Any = None, *, collection: str | None = None,
                 text_field: str | None = None, vector_name: str | None = None):
        self.collection = collection or os.getenv("GH0ST_QDRANT_COLLECTION", "")
        self.text_field = text_field or os.getenv("GH0ST_QDRANT_TEXT_FIELD", "text")
        self.vector_name = vector_name or os.getenv("GH0ST_QDRANT_VECTOR_NAME") or None
        if client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:
                raise RuntimeError("Install gh0st-core[qdrant] to use Qdrant retrieval") from exc
            url = os.getenv("GH0ST_QDRANT_URL", "http://localhost:6333")
            client = QdrantClient(url=url, api_key=os.getenv("QDRANT_API_KEY") or None)
        if not self.collection:
            raise ValueError("GH0ST_QDRANT_COLLECTION is required")
        self.client = client

    def retrieve(self, query: str, limit: int = 5) -> list[ReferenceHit]:
        result = self.client.query_points(collection_name=self.collection, query=query,
                                          using=self.vector_name, limit=limit, with_payload=True)
        points = _field(result, "points", [])
        hits: list[ReferenceHit] = []
        for point in points:
            payload = _field(point, "payload", {}) or {}
            text = payload.get(self.text_field, "") if isinstance(payload, dict) else ""
            if text:
                hits.append(ReferenceHit(text=str(text), source=str(_field(point, "id", "Qdrant")),
                                         score=_field(point, "score"), metadata=payload))
        return hits


def configured_retrievers(provider_names: list[str]) -> dict[str, ReferenceRetriever]:
    retrievers: dict[str, ReferenceRetriever] = {}
    factories = {"mem0": Mem0Retriever, "pinecone": PineconeRetriever, "qdrant": QdrantRetriever}
    for name in provider_names:
        key = name.casefold()
        if key not in factories:
            raise ValueError(f"Unsupported reference provider: {name}")
        retrievers[key] = factories[key]()
    return retrievers


def configured_retriever() -> ReferenceRetriever | None:
    """Compatibility helper for callers using the original single-provider setting."""
    names = _provider_names_from_environment()
    if not names:
        return None
    if len(names) != 1:
        raise ValueError("Use configured_retrievers with OAG-selected providers")
    return configured_retrievers(names)[names[0]]
