"""Loopback protocol checks for the opaque broker and encrypted pairing."""

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from gh0st.broker import MAX_MESSAGE_BYTES, RelayBroker
from gh0st.cli import write_pair_qr
from gh0st.db import StateLedger
from gh0st.models import GrantRequest, QuotaRecord, Ticket
from gh0st.relay import CloudRelayClient
from gh0st.relay_crypto import PairCipher, new_pairing


def test_pair_cipher_authenticates_direction_and_rejects_replay():
    pair_id, secret, uri = new_pairing("ws://127.0.0.1:8765", "dev_test")
    device = PairCipher(secret, pair_id, "device")
    client = PairCipher(secret, pair_id, "client")
    assert pair_id in uri and secret in uri
    envelope = client.encrypt({"action": "status", "task_id": "task_one"})
    assert secret not in json.dumps(envelope)
    assert device.decrypt(envelope) == {"action": "status", "task_id": "task_one"}
    with pytest.raises(ValueError, match="replayed"):
        device.decrypt(envelope)
    reply = device.encrypt({"event": "ready"})
    assert client.decrypt(reply) == {"event": "ready"}
    with pytest.raises(ValueError, match="authentication"):
        PairCipher(new_pairing("ws://127.0.0.1:8765", "dev_test")[1], pair_id,
                   "device").decrypt(envelope)
    altered = dict(device.encrypt({"event": "unchanged"}))
    raw = bytearray(base64.urlsafe_b64decode(altered["ciphertext"] + "=="))
    raw[0] ^= 1
    altered["ciphertext"] = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    with pytest.raises(ValueError):
        client.decrypt(altered)


def test_python_matches_mobile_encryption_vector():
    vector_path = Path(__file__).resolve().parents[1] / "interfaces" / "mobile" / "tests" / "relay-vector.json"
    vector = json.loads(vector_path.read_text(encoding="utf-8"))
    pair_id = vector["pairing"]["pair"]
    secret = vector["pairing"]["secret"]
    assert PairCipher(secret, pair_id, "device").decrypt(vector["envelope"]) == vector["payload"]
    with patch("gh0st.relay_crypto.os.urandom", return_value=bytes(range(12))):
        assert PairCipher(secret, pair_id, "client").encrypt(vector["payload"]) == vector["envelope"]


def test_pair_qr_is_svg_and_never_overwrites(tmp_path):
    path = tmp_path / "nested" / "pair.svg"
    write_pair_qr("gh0st://pair?relay=wss%3A%2F%2Frelay.example&secret=abc", path)
    content = path.read_text(encoding="utf-8")
    assert "<svg" in content and "<path" in content
    with pytest.raises(FileExistsError):
        write_pair_qr("different", path)


def test_broker_routes_only_paired_ciphertext():
    async def scenario():
        broker = RelayBroker({"dev_test": "t" * 48})
        async with serve(broker.handle, "127.0.0.1", 0,
                         max_size=MAX_MESSAGE_BYTES, compression=None) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            pair_id, secret, _ = new_pairing(url, "dev_test")
            device_cipher = PairCipher(secret, pair_id, "device")
            client_cipher = PairCipher(secret, pair_id, "client")
            async with connect(url, additional_headers={"Authorization": "Bearer " + "t" * 48}) as device:
                await device.send(json.dumps({"message_type": "device_register", "device_id": "dev_test"}))
                assert json.loads(await device.recv())["message_type"] == "device_registered"
                await device.send(json.dumps({"message_type": "pair_offer", "pair_id": pair_id}))
                assert json.loads(await device.recv())["message_type"] == "offer_ready"
                async with connect(url) as client:
                    await client.send(json.dumps({"message_type": "client_register", "pair_id": pair_id}))
                    assert json.loads(await client.recv())["message_type"] == "transport_connected"
                    assert json.loads(await device.recv())["message_type"] == "client_connected"

                    request = client_cipher.encrypt({"action": "status"})
                    await client.send(json.dumps(request))
                    assert device_cipher.decrypt(json.loads(await device.recv())) == {"action": "status"}
                    response = device_cipher.encrypt({"event": "ready"})
                    await device.send(json.dumps(response))
                    assert client_cipher.decrypt(json.loads(await client.recv())) == {"event": "ready"}

                    async with connect(url) as duplicate:
                        await duplicate.send(json.dumps({"message_type": "client_register", "pair_id": pair_id}))
                        with pytest.raises(ConnectionClosed):
                            await duplicate.recv()
                    await client.send(json.dumps({**client_cipher.encrypt({"action": "status"}),
                                                  "payload": {"plaintext": "forbidden"}}))
                    with pytest.raises(ConnectionClosed):
                        await client.recv()

    asyncio.run(scenario())


def test_broker_rejects_unauthorized_device_and_unknown_offer():
    async def scenario():
        broker = RelayBroker({"dev_test": "t" * 48})
        async with serve(broker.handle, "127.0.0.1", 0,
                         max_size=MAX_MESSAGE_BYTES, compression=None) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(url, additional_headers={"Authorization": "Bearer wrong"}) as device:
                await device.send(json.dumps({"message_type": "device_register", "device_id": "dev_test"}))
                with pytest.raises(ConnectionClosed):
                    await device.recv()
            async with connect(url) as client:
                await client.send(json.dumps({"message_type": "client_register",
                                              "pair_id": "unknown_pair_identifier_12345"}))
                with pytest.raises(ConnectionClosed):
                    await client.recv()

    asyncio.run(scenario())


def test_paired_daemon_handles_encrypted_status_grant_and_dispatch(tmp_path, monkeypatch):
    async def scenario():
        db = StateLedger(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
        db.init_db()
        db.create_task("task_one", "Fix code")
        db.create_quota_record(QuotaRecord(
            task_id="task_one", max_tokens_budget=10000, max_cost_usd_budget=5,
        ))
        ticket = Ticket(task_id="task_one", capability="code_refactor",
                        objective="Fix code", workspace_path=str(tmp_path))
        db.create_ticket(ticket)
        grant = db.save_grant_request(GrantRequest(
            task_id="task_one", ticket_id=ticket.ticket_id,
            action_type="git_push", details={"ref": "main"},
        ))
        broker = RelayBroker({"dev_test": "t" * 48})
        async with serve(broker.handle, "127.0.0.1", 0,
                         max_size=MAX_MESSAGE_BYTES, compression=None) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            pair_id, secret, _ = new_pairing(url, "dev_test")
            db.create_relay_pair(pair_id, "dev_test", secret, url)
            daemon = CloudRelayClient(url, "dev_test", "t" * 48, db,
                                      str(tmp_path / "workspaces"))
            configured_repo = str(tmp_path / "allowed-source")
            monkeypatch.setenv("GH0ST_RELAY_SOURCE_REPO", configured_repo)
            dispatched = []

            def fake_run_prompt(prompt, **kwargs):
                dispatched.append((prompt, kwargs))
                db.create_task(kwargs["task_id"], prompt)
                db.create_quota_record(QuotaRecord(
                    task_id=kwargs["task_id"], max_tokens_budget=kwargs["budget_tokens"],
                    max_cost_usd_budget=kwargs["budget_usd"],
                ))
                db.set_task_status(kwargs["task_id"], "completed")
                return kwargs["task_id"]

            daemon.cli.run_prompt = fake_run_prompt
            daemon_task = asyncio.create_task(daemon.run())
            try:
                for _ in range(100):
                    if pair_id in broker.offers:
                        break
                    await asyncio.sleep(0.05)
                assert pair_id in broker.offers
                async with connect(url) as client:
                    await client.send(json.dumps({"message_type": "client_register", "pair_id": pair_id}))
                    assert json.loads(await asyncio.wait_for(client.recv(), 5))["message_type"] == "transport_connected"
                    cipher = PairCipher(secret, pair_id, "client")
                    assert cipher.decrypt(json.loads(await asyncio.wait_for(client.recv(), 5)))["message_type"] == "paired"
                    assert db.get_relay_pair(pair_id)["secret"] == ""
                    await client.send(json.dumps(cipher.encrypt({
                        "action_id": "act_status", "action_type": "status", "task_id": "task_one",
                    })))
                    result = cipher.decrypt(json.loads(await asyncio.wait_for(client.recv(), 5)))
                    assert result["ok"] is True
                    assert result["result"]["task_id"] == "task_one"
                    assert result["result"]["grant_requests"][0]["status"] == "pending"
                    await client.send(json.dumps(cipher.encrypt({
                        "action_id": "act_approve", "action_type": "approve_grant",
                        "task_id": "task_one", "grant_id": grant.grant_id,
                    })))
                    result = cipher.decrypt(json.loads(await asyncio.wait_for(client.recv(), 5)))
                    assert result["result"]["status"] == "approved"
                    assert db.list_grant_requests("task_one")[0].status == "approved"
                    await client.send(json.dumps(cipher.encrypt({
                        "action_id": "act_dispatch", "action_type": "dispatch_task",
                        "prompt": "Fix a parser bug", "budget_usd": 2,
                        "budget_tokens": 5000,
                    })))
                    responses = [cipher.decrypt(json.loads(await asyncio.wait_for(client.recv(), 5)))
                                 for _ in range(2)]
                    accepted = next(item for item in responses if item["message_type"] == "action_result")
                    assert accepted["ok"] is True
                    assert any(item["message_type"] == "task_update" and item["status"] == "completed"
                               for item in responses)
                    assert dispatched[0][0] == "Fix a parser bug"
                    assert dispatched[0][1]["source_repo"] == configured_repo
                    await client.send(json.dumps(cipher.encrypt({
                        "action_id": "act_overbudget", "action_type": "dispatch_task",
                        "prompt": "Too expensive", "budget_usd": 100,
                    })))
                    rejected = cipher.decrypt(json.loads(await asyncio.wait_for(client.recv(), 5)))
                    assert rejected["ok"] is False
                    assert len(dispatched) == 1
            finally:
                daemon_task.cancel()
                await asyncio.gather(daemon_task, return_exceptions=True)

    asyncio.run(scenario())
