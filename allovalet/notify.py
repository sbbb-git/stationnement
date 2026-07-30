"""Notifications : ntfy, Telegram, webhook générique. Aucune dépendance en plus."""

from __future__ import annotations

import logging

import requests

from .config import NotifyConfig

log = logging.getLogger("allovalet.notify")


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    def send(self, title: str, message: str, success: bool = True) -> None:
        if not self.cfg.enabled:
            return
        if success and not self.cfg.on_success:
            return
        if not success and not self.cfg.on_failure:
            return

        prefix = "✅" if success else "❌"
        text = f"{prefix} {title}\n{message}".strip()

        if self.cfg.ntfy_topic:
            self._safe(
                "ntfy",
                lambda: requests.post(
                    f"{self.cfg.ntfy_server.rstrip('/')}/{self.cfg.ntfy_topic}",
                    data=text.encode("utf-8"),
                    headers={
                        "Title": f"{prefix} {title}".encode("utf-8"),
                        "Priority": "default" if success else "high",
                        "Tags": "parking" if success else "warning",
                    },
                    timeout=15,
                ),
            )

        if self.cfg.telegram_token and self.cfg.telegram_chat_id:
            self._safe(
                "telegram",
                lambda: requests.post(
                    f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                    json={"chat_id": self.cfg.telegram_chat_id, "text": text},
                    timeout=15,
                ),
            )

        if self.cfg.webhook_url:
            self._safe(
                "webhook",
                lambda: requests.post(
                    self.cfg.webhook_url,
                    json={"title": title, "message": message, "success": success},
                    timeout=15,
                ),
            )

    @staticmethod
    def _safe(name: str, call) -> None:
        try:
            resp = call()
            if not resp.ok:
                log.warning("Notification %s → HTTP %s", name, resp.status_code)
        except Exception as exc:  # une notif ratée ne doit jamais casser un ticket
            log.warning("Notification %s impossible : %s", name, exc)
