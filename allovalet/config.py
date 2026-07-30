"""Chargement et validation de config.yml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigError
from .schedule import Window, parse_duration, parse_time

ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")

MODES = {"renew", "smartpark"}


def _expand(value):
    """Remplace ${VAR} / ${VAR:-defaut} par les variables d'environnement."""
    if isinstance(value, str):
        return ENV_RE.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class Rule:
    name: str
    plate: str
    location: str
    rate: str | None = None
    mode: str = "renew"
    duration_minutes: int = 1440
    window: Window = field(default_factory=Window)
    enabled: bool = True
    stall: str | None = None
    max_cost_per_ticket: float | None = None
    max_cost_per_day: float | None = None
    renew_margin_minutes: int | None = None
    renew_at: str | None = None
    min_chunk_minutes: int = 15
    max_chunk_minutes: int | None = None

    @classmethod
    def parse(cls, data: dict, index: int) -> "Rule":
        if not isinstance(data, dict):
            raise ConfigError(f"règle #{index + 1} : doit être un objet")
        missing = [k for k in ("plate", "location") if not data.get(k)]
        if missing:
            raise ConfigError(
                f"règle #{index + 1} ({data.get('name', 'sans nom')}) : "
                f"champ(s) manquant(s) {missing}"
            )
        mode = str(data.get("mode", "renew")).lower()
        if mode not in MODES:
            raise ConfigError(f"règle « {data.get('name')} » : mode inconnu {mode!r} ({MODES})")

        duration = data.get("duration", "24h" if mode == "renew" else None)
        duration_minutes = parse_duration(duration) if duration else 0

        rule = cls(
            name=str(data.get("name") or f"règle {index + 1}"),
            plate=str(data["plate"]).upper().replace(" ", "").replace("-", ""),
            location=str(data["location"]).strip(),
            rate=str(data["rate"]).strip() if data.get("rate") else None,
            mode=mode,
            duration_minutes=duration_minutes,
            window=Window.parse(data.get("window")),
            enabled=bool(data.get("enabled", True)),
            stall=str(data["stall"]) if data.get("stall") else None,
            max_cost_per_ticket=_opt_float(data.get("max_cost_per_ticket")),
            max_cost_per_day=_opt_float(data.get("max_cost_per_day")),
            renew_margin_minutes=(
                int(data["renew_margin_minutes"]) if data.get("renew_margin_minutes") else None
            ),
            renew_at=(
                parse_time(data["renew_at"]).strftime("%H:%M") if data.get("renew_at") else None
            ),
            min_chunk_minutes=int(data.get("min_chunk_minutes", 15)),
            max_chunk_minutes=(
                parse_duration(data["max_chunk_minutes"]) if data.get("max_chunk_minutes") else None
            ),
        )
        if rule.mode == "renew" and rule.duration_minutes <= 0:
            raise ConfigError(f"règle « {rule.name} » : `duration` requis en mode renew")
        return rule

    def key(self) -> str:
        return f"{self.plate}@{self.location}"


def _opt_float(value):
    return None if value is None else float(value)


@dataclass
class NotifyConfig:
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None
    on_success: bool = True
    on_failure: bool = True

    @classmethod
    def parse(cls, data: dict | None) -> "NotifyConfig":
        data = data or {}
        return cls(
            ntfy_topic=data.get("ntfy_topic") or os.getenv("NTFY_TOPIC") or None,
            ntfy_server=data.get("ntfy_server") or os.getenv("NTFY_SERVER") or "https://ntfy.sh",
            telegram_token=data.get("telegram_token") or os.getenv("TELEGRAM_TOKEN") or None,
            telegram_chat_id=(
                data.get("telegram_chat_id") or os.getenv("TELEGRAM_CHAT_ID") or None
            ),
            webhook_url=data.get("webhook_url") or os.getenv("WEBHOOK_URL") or None,
            on_success=bool(data.get("on_success", True)),
            on_failure=bool(data.get("on_failure", True)),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.ntfy_topic or self.telegram_token or self.webhook_url)


@dataclass
class Config:
    provider: str = "paybyphone"
    timezone: str = "Europe/Paris"
    renew_margin_minutes: int = 20
    country: str = "FR"
    dry_run: bool = False
    rules: list[Rule] = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise ConfigError(
                f"Config introuvable : {path}\n"
                "Copie config.example.yml vers config.yml et adapte-le."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} : le fichier doit être un objet YAML")
        raw = _expand(raw)

        rules_raw = raw.get("rules") or []
        if not rules_raw:
            raise ConfigError(f"{path} : aucune règle définie (`rules:`)")

        cfg = cls(
            provider=str(raw.get("provider", "paybyphone")).lower(),
            timezone=str(raw.get("timezone", "Europe/Paris")),
            renew_margin_minutes=int(raw.get("renew_margin_minutes", 20)),
            country=str(raw.get("country", "FR")).upper(),
            dry_run=bool(raw.get("dry_run", False)),
            rules=[Rule.parse(r, i) for i, r in enumerate(rules_raw)],
            notify=NotifyConfig.parse(raw.get("notify")),
            path=path,
        )
        if cfg.provider not in ("paybyphone", "easypark"):
            raise ConfigError(f"provider inconnu : {cfg.provider}")

        seen = set()
        for rule in cfg.rules:
            if rule.name in seen:
                raise ConfigError(f"deux règles portent le même nom : « {rule.name} »")
            seen.add(rule.name)
        return cfg

    def margin_for(self, rule: Rule) -> int:
        return rule.renew_margin_minutes or self.renew_margin_minutes

    def active_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]
