import os
from types import SimpleNamespace

import pytest

from gh0st.secrets import (
    EnvSecretResolver,
    FileSecretResolver,
    OnePasswordSecretResolver,
    VaultSecretResolver,
    get_secret_resolver,
)


def test_environment_and_file_secret_resolvers(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_TOKEN", "env-value")
    assert EnvSecretResolver().get_secret("PROVIDER_TOKEN") == "env-value"
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "provider-token").write_text("file-value\n", encoding="utf-8")
    resolver = FileSecretResolver(root)
    assert resolver.get_secret("provider-token") == "file-value"
    with pytest.raises(ValueError, match="unavailable"):
        resolver.get_secret("../outside")


def test_vault_kv_v2_resolves_only_requested_field(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"data": {"OPENAI_API_KEY": "vault-value"}}}

    calls = []
    monkeypatch.setattr("gh0st.secrets.httpx.get", lambda *args, **kwargs: (calls.append((args, kwargs)) or Response()))
    resolver = VaultSecretResolver(address="https://vault.test", token="vault-token",
                                   mount="secret", secret_path="gh0st/providers", kv_version=2)
    assert resolver.get_secret("OPENAI_API_KEY") == "vault-value"
    assert calls[0][0][0] == "https://vault.test/v1/secret/data/gh0st/providers"
    assert calls[0][1]["headers"] == {"X-Vault-Token": "vault-token"}


def test_vault_requires_tls_outside_loopback():
    with pytest.raises(ValueError, match="HTTPS or loopback"):
        VaultSecretResolver(address="http://vault.test", token="token")


def test_onepassword_resolves_field_by_label_or_id():
    client = SimpleNamespace(get_item=lambda item, vault: SimpleNamespace(fields=[
        SimpleNamespace(label="ANTHROPIC_API_KEY", id="api-key-field", value="op-value"),
    ]))
    resolver = OnePasswordSecretResolver(client=client, vault_id="vault", item_id="item")
    assert resolver.get_secret("ANTHROPIC_API_KEY") == "op-value"
    assert resolver.get_secret("api-key-field") == "op-value"


@pytest.mark.parametrize("provider, expected", [
    ("env", EnvSecretResolver),
    ("file", FileSecretResolver),
    ("vault", VaultSecretResolver),
    ("onepassword", OnePasswordSecretResolver),
])
def test_secret_resolver_factory(provider, expected, monkeypatch, tmp_path):
    if provider == "file":
        monkeypatch.setenv("GH0ST_SECRET_DIR", str(tmp_path))
    if provider == "vault":
        monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
        monkeypatch.setenv("VAULT_TOKEN", "token")
    if provider == "onepassword":
        monkeypatch.setenv("GH0ST_1PASSWORD_VAULT_ID", "vault")
        monkeypatch.setenv("GH0ST_1PASSWORD_ITEM_ID", "item")
    assert isinstance(get_secret_resolver(provider), expected)
