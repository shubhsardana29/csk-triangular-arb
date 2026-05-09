import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(
        self,
        webhook_url: str,
        enabled: bool = True,
        cooldown_seconds: int = 60,
        username: str = "OmniArb",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.webhook_url = webhook_url.strip()
        self.enabled = enabled and bool(self.webhook_url)
        self.cooldown_seconds = max(cooldown_seconds, 0)
        self.username = username
        self.session = session
        self._owned_session = False
        self._last_sent: dict[str, float] = {}

    async def __aenter__(self):
        if self.enabled and self.session is None:
            self.session = aiohttp.ClientSession()
            self._owned_session = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._owned_session and self.session:
            await self.session.close()

    def should_send(self, key: str) -> bool:
        if not self.enabled:
            return False

        now = time.time()
        last_sent = self._last_sent.get(key, 0.0)
        if now - last_sent < self.cooldown_seconds:
            return False

        self._last_sent[key] = now
        return True

    async def send(self, text: str, key: Optional[str] = None) -> bool:
        if not self.enabled:
            return False

        if key and not self.should_send(key):
            return False

        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._owned_session = True

        payload = {
            "text": text,
            "username": self.username,
        }

        try:
            async with self.session.post(self.webhook_url, json=payload, timeout=5) as response:
                if response.status in {200, 204}:
                    return True

                body = await response.text()
                logger.error("Slack webhook error %s: %s", response.status, body[:200])
                return False
        except Exception as exc:
            logger.error("Slack notification failed: %s", exc)
            return False
