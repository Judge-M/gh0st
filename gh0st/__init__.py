"""Public SDK for bounded, ephemeral gh0st task execution."""

from .db import StateLedger
from .reference import (
    Mem0Retriever,
    configured_retrievers,
    PineconeRetriever,
    QdrantRetriever,
    ReferenceHit,
    ReferenceRetriever,
)
from .router import System1Router
from .models import MemoryRoute
from .secrets import (
    EnvSecretResolver,
    FileSecretResolver,
    OnePasswordSecretResolver,
    SecretResolver,
    VaultSecretResolver,
)


def __getattr__(name: str):
    """Load execution classes lazily so `python -m gh0st.worker` stays clean."""
    if name in {"DispatchResult", "Gh0stClient"}:
        from . import sdk

        return getattr(sdk, name)
    if name == "EphemeralWorker":
        from .worker import EphemeralWorker

        return EphemeralWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "DispatchResult", "EphemeralWorker", "EnvSecretResolver", "FileSecretResolver",
    "Gh0stClient", "Mem0Retriever", "OnePasswordSecretResolver", "PineconeRetriever",
    "MemoryRoute", "QdrantRetriever", "ReferenceHit", "ReferenceRetriever", "SecretResolver",
    "StateLedger", "System1Router", "VaultSecretResolver", "configured_retrievers",
]
