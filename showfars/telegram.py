"""Minimal Telegram Bot API publisher with ordered reply threading."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

LOG = logging.getLogger("showfars.telegram")


class TelegramPublisher:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.session = requests.Session()

    def _post(self, method: str, data: dict, files: dict | None = None) -> dict:
        response = self.session.post(f"{self.base}/{method}", data=data, files=files, timeout=(30, 300))
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('description', 'unknown error')}")
        return payload["result"]

    def publish_movie(self, movie: dict, caption: str, poster: bytes | None,
                      outputs: dict[str, Path]) -> dict:
        if poster:
            result = self._post("sendPhoto", {
                "chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML",
            }, {"photo": ("poster.jpg", poster, "image/jpeg")})
        else:
            result = self._post("sendMessage", {
                "chat_id": self.chat_id, "text": caption, "parse_mode": "HTML",
            })
        lead_id = result["message_id"]
        ids = {"lead": lead_id}
        for quality in ("1080p", "720p", "480p"):
            path = outputs[quality]
            LOG.info("Uploading %s (%d bytes)", quality, path.stat().st_size)
            with path.open("rb") as handle:
                result = self._post("sendVideo", {
                    "chat_id": self.chat_id,
                    "reply_to_message_id": lead_id,
                    "caption": f"{movie.get('title', 'Film')} · {quality} · @ShowFars",
                    "supports_streaming": "true",
                }, {"video": (path.name, handle, "video/mp4")})
            ids[quality] = result["message_id"]
            time.sleep(1)
        return ids
