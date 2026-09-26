"""Runtime secret lookup adapters for provider credentials."""

from abc import ABC, abstractmethod
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


class SecretResolver(ABC):
    @abstractmethod
    def get_secret(self, secret_name: str) -> str:
        """Return one named secret without logging or persisting its value."""


class EnvSecretResolver(SecretResolver):
    def get_secret(self, secret_name: str) -> str:
        value = os.getenv(secret_name)
        if not value:
            raise ValueError(f"Required environment secret is unavailable: {secret_name}")
        return value


class FileSecretResolver(SecretResolver):
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("GH0ST_SECRET_DIR", "/run/secrets")).expanduser().resolve()

    def get_secret(self, secret_name: str) -> str:
        if not secret_name or "\x00" in secret_name:
            raise ValueError("Invalid secret name")
        path = (self.root / secret_name).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError(f"Secret file is unavailable: {secret_name}")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
        if not value:
            raise ValueError(f"Secret file is empty: {secret_name}")
        return value


class VaultSecretResolver(SecretResolver):
    """HashiCorp Vault KV resolver using the HTTP API (KV v1 or v2)."""

    def __init__(self, *, address: str | None = None, token: str | None = None,
                 mount: str | None = None, secret_path: str | None = None,
                 kv_version: int | None = None, timeout: float = 5.0):
        self.address = (address or os.getenv("VAULT_ADDR", "")).rstrip("/")
        self.token = token or os.getenv("VAULT_TOKEN", "")
        self.mount = (mount or os.getenv("GH0ST_VAULT_MOUNT", "secret")).strip("/")
        self.secret_path = (secret_path or os.getenv("GH0ST_VAULT_SECRET_PATH", "provider-keys")).strip("/")
        self.kv_version = int(kv_version or os.getenv("GH0ST_VAULT_KV_VERSION", "2"))
        self.timeout = timeout
        if not self.address.startswith(("https://", "http://127.0.0.1", "http://localhost", "http://[::1]")):
            raise ValueError("Vault address must use HTTPS or loopback HTTP")
        if not self.token or self.kv_version not in {1, 2} or not self.mount or not self.secret_path:
            raise ValueError("Vault token, mount, secret path, and KV version 1 or 2 are required")

    def get_secret(self, secret_name: str) -> str:
        api_path = f"{self.mount}/data/{self.secret_path}" if self.kv_version == 2 else f"{self.mount}/{self.secret_path}"
        response = httpx.get(
            f"{self.address}/v1/{quote(api_path, safe='/')}",
            headers={"X-Vault-Token": self.token}, timeout=self.timeout,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        values = payload.get("data", {}).get("data", {}) if self.kv_version == 2 else payload.get("data", {})
        value = values.get(secret_name) if isinstance(values, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError(f"Vault secret field is unavailable: {secret_name}")
        return value


class OnePasswordSecretResolver(SecretResolver):
    """Resolver backed by the official 1Password Connect Python SDK."""

    def __init__(self, *, client: Any = None, vault_id: str | None = None,
                 item_id: str | None = None):
        self.client = client
        self.vault_id = vault_id or os.getenv("GH0ST_1PASSWORD_VAULT_ID", "")
        self.item_id = item_id or os.getenv("GH0ST_1PASSWORD_ITEM_ID", "")
        if not self.vault_id or not self.item_id:
            raise ValueError("1Password vault and item IDs are required")

    def _client(self) -> Any:
        if self.client is None:
            try:
                from onepasswordconnectsdk.client import new_client_from_environment
            except ImportError as exc:
                raise RuntimeError("Install the 1Password extra to use this secret resolver") from exc
            self.client = new_client_from_environment()
        return self.client

    def get_secret(self, secret_name: str) -> str:
        item = self._client().get_item(self.item_id, self.vault_id)
        for field in getattr(item, "fields", []) or []:
            if secret_name in {getattr(field, "label", None), getattr(field, "id", None)}:
                value = getattr(field, "value", None)
                if isinstance(value, str) and value:
                    return value
        raise ValueError(f"1Password field is unavailable: {secret_name}")


def get_secret_resolver(provider: str | None = None) -> SecretResolver:
    selected = (provider or os.getenv("GH0ST_SECRETS_PROVIDER", "env")).casefold()
    if selected == "env":
        return EnvSecretResolver()
    if selected == "file":
        return FileSecretResolver()
    if selected == "vault":
        return VaultSecretResolver()
    if selected in {"1password", "onepassword"}:
        return OnePasswordSecretResolver()
    raise ValueError(f"Unsupported secret provider: {selected}")
