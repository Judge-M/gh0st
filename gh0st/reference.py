"""Untrusted, read-only reference retrieval adapters.

Retrieved material is never copied into the SQLite ledger. It is injected into
the current worker prompt as reference data and must not be treated as policy.
"""

from abc import ABC, abstractmethod
import os
from typing import Any

from pydantic import BaseModel, Field


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


def configured_retriever() -> ReferenceRetriever | None:
    provider = os.getenv("GH0ST_REFERENCE_PROVIDER", "").casefold()
    if not provider:
        return None
    if provider == "mem0":
        return Mem0Retriever()
    if provider == "pinecone":
        return PineconeRetriever()
    if provider == "qdrant":
        return QdrantRetriever()
    raise ValueError("GH0ST_REFERENCE_PROVIDER must be mem0, pinecone, or qdrant")
