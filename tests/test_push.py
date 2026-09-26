import asyncio

import httpx

from gh0st.push import EXPO_PUSH_ENDPOINT, ExpoPushNotifier, valid_expo_push_token


def test_expo_push_token_validation():
    assert valid_expo_push_token("ExponentPushToken[abc_123-XYZ]")
    assert valid_expo_push_token("ExpoPushToken[abc]")
    assert not valid_expo_push_token("https://example.test/token")
    assert not valid_expo_push_token("ExpoPushToken[bad token]")
    assert not valid_expo_push_token(123)


def test_push_payload_is_generic_and_delivery_is_best_effort(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 5.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json, headers):
            sent.append((url, json, headers))
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  json={"data": {"status": "ok"}})

    monkeypatch.setattr("gh0st.push.httpx.AsyncClient", FakeClient)
    notifier = ExpoPushNotifier()
    assert asyncio.run(notifier.send("ExpoPushToken[secret123]", "grant"))
    url, payload, _ = sent[0]
    assert url == EXPO_PUSH_ENDPOINT
    assert payload == {
        "to": "ExpoPushToken[secret123]",
        "title": "gh0st needs your attention",
        "body": "Open gh0st to view the update.",
        "sound": None,
        "priority": "normal",
    }
    assert "secret123" not in payload["title"] + payload["body"]


def test_invalid_push_token_never_sends(monkeypatch):
    monkeypatch.setattr("gh0st.push.httpx.AsyncClient", lambda **kwargs: (_ for _ in ()).throw(AssertionError()))
    assert not asyncio.run(ExpoPushNotifier().send("not-a-token", "complete"))
