"""Self-hostable, opaque WebSocket relay for paired gh0st devices.

The broker authenticates daemons and routes ciphertext. Pair secrets and task
contents never enter this process. Pair offers and connections are memory-only.
"""

import argparse
import asyncio
from collections import defaultdict, deque
import base64
import hmac
import json
from pathlib import Path
import re
import ssl
import time
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed


DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
PAIR_ID = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")
MAX_MESSAGE_BYTES = 65536
PAIR_TTL_SECONDS = 300
MAX_OFFERS_PER_DEVICE = 5
MAX_FRAMES_PER_TEN_SECONDS = 120


class RelayBroker:
    def __init__(self, device_tokens: dict[str, str]):
        if not device_tokens or any(not DEVICE_ID.fullmatch(device_id) or
                                    not isinstance(token, str) or len(token) < 32
                                    for device_id, token in device_tokens.items()):
            raise ValueError("Configure valid device IDs with tokens of at least 32 characters")
        self.device_tokens = device_tokens
        self.devices: dict[str, ServerConnection] = {}
        self.offers: dict[str, tuple[str, float]] = {}
        self.paired_clients: dict[str, str] = {}
        self.clients: dict[str, ServerConnection] = {}
        self.client_devices: dict[str, str] = {}
        self.device_clients: dict[str, set[str]] = defaultdict(set)
        self.recent_frames: dict[ServerConnection, deque[float]] = defaultdict(deque)

    @staticmethod
    def _valid_ciphertext(data: dict[str, Any], pair_id: str) -> bool:
        if set(data) != {"message_type", "pair_id", "nonce", "ciphertext"}:
            return False
        if data.get("message_type") != "ciphertext" or data.get("pair_id") != pair_id:
            return False
        try:
            nonce = base64.urlsafe_b64decode(data["nonce"] + "==")
            ciphertext = base64.urlsafe_b64decode(data["ciphertext"] + "==")
        except (TypeError, ValueError, base64.binascii.Error):
            return False
        return len(nonce) == 12 and 16 <= len(ciphertext) <= MAX_MESSAGE_BYTES

    def _within_rate_limit(self, socket: ServerConnection) -> bool:
        recent = self.recent_frames[socket]
        now = time.monotonic()
        while recent and recent[0] <= now - 10:
            recent.popleft()
        recent.append(now)
        return len(recent) <= MAX_FRAMES_PER_TEN_SECONDS

    @staticmethod
    async def _receive_json(socket: ServerConnection, timeout: float = 5) -> dict[str, Any]:
        raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
        if not isinstance(raw, str):
            raise ValueError("Only JSON text frames are supported")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    @staticmethod
    async def _send(socket: ServerConnection, data: dict[str, Any]) -> None:
        await socket.send(json.dumps(data, separators=(",", ":")))

    def _discard_expired_offers(self) -> None:
        now = time.monotonic()
        for pair_id, (_, expires_at) in list(self.offers.items()):
            if expires_at <= now:
                del self.offers[pair_id]

    async def handle(self, socket: ServerConnection) -> None:
        try:
            hello = await self._receive_json(socket)
            role = hello.get("message_type")
            if role == "device_register":
                await self._device(socket, hello)
            elif role == "client_register":
                await self._client(socket, hello)
            else:
                await socket.close(code=1008, reason="Unknown connection role")
        except (ValueError, json.JSONDecodeError, asyncio.TimeoutError):
            await socket.close(code=1008, reason="Invalid registration")
        except ConnectionClosed:
            pass
        finally:
            self.recent_frames.pop(socket, None)

    async def _device(self, socket: ServerConnection, hello: dict[str, Any]) -> None:
        device_id = hello.get("device_id")
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
            await socket.close(code=1008, reason="Invalid device ID")
            return
        expected = self.device_tokens.get(device_id)
        supplied = socket.request.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            await socket.close(code=1008, reason="Device authentication failed")
            return
        if device_id in self.devices:
            await socket.close(code=1008, reason="Device already connected")
            return
        self.devices[device_id] = socket
        try:
            await self._send(socket, {"message_type": "device_registered", "device_id": device_id})
            async for raw in socket:
                if not self._within_rate_limit(socket):
                    await socket.close(code=1008, reason="Rate limit exceeded")
                    break
                await self._device_message(device_id, socket, raw)
        finally:
            self.devices.pop(device_id, None)
            for pair_id, (offered_by, _) in list(self.offers.items()):
                if offered_by == device_id:
                    del self.offers[pair_id]
            for pair_id in list(self.device_clients.pop(device_id, set())):
                client = self.clients.get(pair_id)
                if client:
                    await client.close(code=1001, reason="Device disconnected")

    async def _device_message(self, device_id: str, socket: ServerConnection, raw: str | bytes) -> None:
        if not isinstance(raw, str):
            await socket.close(code=1003, reason="Binary frames are unsupported")
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            await socket.close(code=1008, reason="Invalid message")
            return
        kind = data.get("message_type")
        pair_id = data.get("pair_id")
        if kind == "pair_offer":
            self._discard_expired_offers()
            if not isinstance(pair_id, str) or not PAIR_ID.fullmatch(pair_id) or pair_id in self.offers or pair_id in self.clients or pair_id in self.paired_clients:
                await self._send(socket, {"message_type": "error", "reason": "Invalid or duplicate pair ID"})
                return
            if sum(offered_by == device_id for offered_by, _ in self.offers.values()) >= MAX_OFFERS_PER_DEVICE:
                await self._send(socket, {"message_type": "error", "reason": "Too many pair offers"})
                return
            self.offers[pair_id] = (device_id, time.monotonic() + PAIR_TTL_SECONDS)
            await self._send(socket, {"message_type": "offer_ready", "pair_id": pair_id,
                                      "expires_in": PAIR_TTL_SECONDS})
        elif kind == "ciphertext":
            client = self.clients.get(pair_id) if isinstance(pair_id, str) else None
            if client and self.client_devices.get(pair_id) == device_id and self._valid_ciphertext(data, pair_id):
                await self._send(client, data)
        elif kind == "pair_register":
            if isinstance(pair_id, str) and PAIR_ID.fullmatch(pair_id):
                registered_device = self.paired_clients.get(pair_id)
                if registered_device is None or registered_device == device_id:
                    self.paired_clients[pair_id] = device_id
                else:
                    await self._send(socket, {"message_type": "error", "reason": "Pair ID is already registered"})
        elif kind == "pair_revoke":
            if isinstance(pair_id, str) and self.paired_clients.get(pair_id) == device_id:
                self.paired_clients.pop(pair_id, None)
                client = self.clients.get(pair_id)
                if client:
                    await client.close(code=1008, reason="Pairing revoked")
        elif kind == "ping":
            await self._send(socket, {"message_type": "pong"})
        else:
            await self._send(socket, {"message_type": "error", "reason": "Unsupported message"})

    async def _client(self, socket: ServerConnection, hello: dict[str, Any]) -> None:
        self._discard_expired_offers()
        pair_id = hello.get("pair_id")
        offer = self.offers.get(pair_id) if isinstance(pair_id, str) else None
        paired_device = self.paired_clients.get(pair_id) if isinstance(pair_id, str) else None
        device_id = offer[0] if offer else paired_device
        if not device_id or device_id not in self.devices or pair_id in self.clients:
            await socket.close(code=1008, reason="Pair offer unavailable")
            return
        if offer:
            del self.offers[pair_id]
            self.paired_clients[pair_id] = device_id
        self.clients[pair_id] = socket
        self.client_devices[pair_id] = device_id
        self.device_clients[device_id].add(pair_id)
        try:
            await self._send(socket, {"message_type": "transport_connected", "pair_id": pair_id})
            await self._send(self.devices[device_id], {"message_type": "client_connected", "pair_id": pair_id})
            async for raw in socket:
                if not self._within_rate_limit(socket):
                    await socket.close(code=1008, reason="Rate limit exceeded")
                    break
                if not isinstance(raw, str):
                    await socket.close(code=1003, reason="Binary frames are unsupported")
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await socket.close(code=1008, reason="Invalid message")
                    break
                if not isinstance(data, dict) or not self._valid_ciphertext(data, pair_id):
                    await socket.close(code=1008, reason="Only paired ciphertext is accepted")
                    break
                device = self.devices.get(device_id)
                if not device:
                    break
                await self._send(device, data)
        finally:
            self.clients.pop(pair_id, None)
            self.client_devices.pop(pair_id, None)
            self.device_clients[device_id].discard(pair_id)
            device = self.devices.get(device_id)
            if device:
                try:
                    await self._send(device, {"message_type": "client_disconnected", "pair_id": pair_id})
                except ConnectionClosed:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the self-hosted gh0st relay broker")
    parser.add_argument("--devices", type=Path, required=True,
                        help="JSON file mapping device IDs to long random authentication tokens")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("Provide both --cert and --key")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.cert:
        parser.error("A non-loopback broker requires TLS via --cert and --key")
    tokens = json.loads(args.devices.read_text(encoding="utf-8"))
    if not isinstance(tokens, dict):
        parser.error("--devices must contain a JSON object")
    broker = RelayBroker(tokens)
    tls = None
    if args.cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)

    async def run() -> None:
        async with serve(broker.handle, args.host, args.port, ssl=tls,
                         max_size=MAX_MESSAGE_BYTES, compression=None):
            await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()
