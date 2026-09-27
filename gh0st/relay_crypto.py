"""End-to-end encryption for a one-time relay pairing session."""

import base64
import ipaddress
import json
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes


MAX_MESSAGES_PER_PAIR = 10000
PAIR_ID_FORMAT = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in value):
        raise ValueError("Invalid base64url value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_relay_url(relay_url: str) -> None:
    """Require TLS except when the WebSocket host is actually loopback."""
    if (not isinstance(relay_url, str) or not relay_url
            or relay_url != relay_url.strip()
            or any(ord(character) < 0x21 for character in relay_url)):
        raise ValueError("Relay URL must use WSS outside loopback")
    try:
        parsed = urlsplit(relay_url)
        hostname = parsed.hostname
        parsed.port  # Force validation of malformed ports.
    except ValueError as exc:
        raise ValueError("Relay URL is invalid") from exc
    if (parsed.scheme not in {"ws", "wss"} or not parsed.netloc or not hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment):
        raise ValueError("Relay URL must use WSS outside loopback")
    if parsed.scheme == "wss":
        return
    host = hostname.casefold().rstrip(".")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("Relay URL must use WSS outside loopback")


def new_pairing(relay_url: str, device_id: str) -> tuple[str, str, str]:
    """Return (pair_id, secret, QR URI); share the URI only with the operator."""
    validate_relay_url(relay_url)
    # argparse treats a leading dash in a positional ID as an option. Keep
    # generated IDs safely usable with `gh0st unpair <pair_id>`.
    pair_id = f"pair_{secrets.token_urlsafe(24)}"
    secret = _encode(os.urandom(32))
    uri = "gh0st://pair?" + urlencode({"relay": relay_url, "device": device_id,
                                      "pair": pair_id, "secret": secret})
    return pair_id, secret, uri


def _host_key_path() -> Path:
    configured = os.getenv("GH0ST_RELAY_KEY_FILE")
    return Path(configured).expanduser() if configured else Path.home() / ".gh0st" / "relay.key"


def _host_key() -> bytes:
    """Load or atomically create the local key used to encrypt pairing keys at rest."""
    path = _host_key_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            with os.fdopen(fd, "wb") as key_file:
                key_file.write(os.urandom(32))
                key_file.flush()
                os.fsync(key_file.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError("Relay key file permissions must be 0600")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Relay key file must contain exactly 32 bytes")
    return key


def encrypt_persistent_secret(pair_id: str, secret: str) -> str:
    key_material = _decode(secret)
    if len(key_material) != 32 or not PAIR_ID_FORMAT.fullmatch(pair_id):
        raise ValueError("Invalid persistent pairing key")
    nonce = os.urandom(12)
    encrypted = AESGCM(_host_key()).encrypt(
        nonce, key_material, f"gh0st-pair-at-rest-v1:{pair_id}".encode("ascii"))
    return "enc:v1:" + _encode(nonce + encrypted)


def decrypt_persistent_secret(pair_id: str, encrypted_secret: str) -> str:
    if not encrypted_secret.startswith("enc:v1:"):
        # Backward compatibility for a pending offer written by earlier releases.
        if len(_decode(encrypted_secret)) != 32:
            raise ValueError("Invalid legacy pairing key")
        return encrypted_secret
    blob = _decode(encrypted_secret.removeprefix("enc:v1:"))
    if len(blob) < 12 + 16 or not PAIR_ID_FORMAT.fullmatch(pair_id):
        raise ValueError("Invalid encrypted persistent pairing key")
    key_material = AESGCM(_host_key()).decrypt(
        blob[:12], blob[12:], f"gh0st-pair-at-rest-v1:{pair_id}".encode("ascii"))
    if len(key_material) != 32:
        raise ValueError("Invalid decrypted persistent pairing key")
    return _encode(key_material)


class PairCipher:
    """AEAD channel with separate directional keys and receive-side replay checks."""

    def __init__(self, secret: str, pair_id: str, role: str):
        if role not in {"device", "client"}:
            raise ValueError("Role must be device or client")
        key_material = _decode(secret)
        if len(key_material) != 32 or not PAIR_ID_FORMAT.fullmatch(pair_id):
            raise ValueError("Invalid pairing secret or ID")
        self.pair_id = pair_id
        self.role = role
        self.peer = "client" if role == "device" else "device"
        self.sender = AESGCM(self._derive(key_material, pair_id, role))
        self.receiver = AESGCM(self._derive(key_material, pair_id, self.peer))
        self.seen_nonces: set[bytes] = set()
        self.sent_messages = 0

    @staticmethod
    def _derive(secret: bytes, pair_id: str, sender: str) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32,
                    salt=pair_id.encode("ascii"),
                    info=f"gh0st-relay-v1:{sender}".encode("ascii")).derive(secret)

    def _aad(self, sender: str) -> bytes:
        return f"gh0st-relay-v1:{self.pair_id}:{sender}".encode("ascii")

    def encrypt(self, payload: dict[str, Any]) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise ValueError("Encrypted payload must be a JSON object")
        if self.sent_messages >= MAX_MESSAGES_PER_PAIR:
            raise ValueError("Pairing message limit reached; pair again")
        plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(plaintext) > 32768:
            raise ValueError("Encrypted payload exceeds 32 KiB")
        nonce = os.urandom(12)
        ciphertext = self.sender.encrypt(nonce, plaintext, self._aad(self.role))
        self.sent_messages += 1
        return {"message_type": "ciphertext", "pair_id": self.pair_id,
                "nonce": _encode(nonce), "ciphertext": _encode(ciphertext)}

    def decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(envelope, dict) or set(envelope) != {"message_type", "pair_id", "nonce", "ciphertext"}:
            raise ValueError("Invalid ciphertext envelope")
        if len(self.seen_nonces) >= MAX_MESSAGES_PER_PAIR:
            raise ValueError("Pairing message limit reached; pair again")
        if envelope["message_type"] != "ciphertext" or envelope["pair_id"] != self.pair_id:
            raise ValueError("Ciphertext belongs to another pairing")
        nonce = _decode(envelope["nonce"])
        ciphertext = _decode(envelope["ciphertext"])
        if len(nonce) != 12 or nonce in self.seen_nonces:
            raise ValueError("Invalid or replayed nonce")
        try:
            plaintext = self.receiver.decrypt(nonce, ciphertext, self._aad(self.peer))
        except InvalidTag as exc:
            raise ValueError("Ciphertext authentication failed") from exc
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise ValueError("Decrypted payload is not a JSON object")
        self.seen_nonces.add(nonce)
        return data
