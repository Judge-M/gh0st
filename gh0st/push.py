"""Best-effort, content-free Expo Push delivery for paired mobile clients."""

import asyncio
import re

import httpx


EXPO_PUSH_ENDPOINT = "https://exp.host/--/api/v2/push/send"
_TOKEN_RE = re.compile(r"^(?:Exponent|Expo)PushToken\[[A-Za-z0-9_-]{1,200}\]$")


def valid_expo_push_token(token: object) -> bool:
    return isinstance(token, str) and bool(_TOKEN_RE.fullmatch(token))


class ExpoPushNotifier:
    """Send generic notices without allowing task or grant data into push payloads."""

    async def send(self, token: str, kind: str) -> bool:
        if not valid_expo_push_token(token):
            return False
        title = "gh0st needs your attention" if kind == "grant" else "gh0st task finished"
        payload = {"to": token, "title": title, "body": "Open gh0st to view the update.",
                   "sound": None, "priority": "normal"}
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                    response = await client.post(EXPO_PUSH_ENDPOINT, json=payload,
                                                 headers={"Accept": "application/json",
                                                          "Content-Type": "application/json"})
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(0.25 * (2 ** attempt))
                        continue
                response.raise_for_status()
                data = response.json().get("data", {})
                return data.get("status") == "ok"
            except (httpx.HTTPError, ValueError):
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2 ** attempt))
        return False
