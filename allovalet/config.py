"""Chargement et validation de config.yml.

Le modèle est celui d'AlloValet : une règle par véhicule × zone × type de
ticket, avec un rendez-vous de renouvellement. Rien de plus.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigError
from .schedule import Window, parse_duration, parse_time

ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


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
    """« Sur cette plaque, dans ce groupe de zones, toujours un ticket. »

    `zones` est une **liste ordonnée de replis** : la première est celle qu'on
    veut, les suivantes servent quand elle refuse. Elles appartiennent au même
    secteur de stationnement, donc un ticket sur n'importe laquelle couvre la
    règle — c'est ce qui permet de garantir « toujours un ticket actif » même
    quand la zone préférée dit non.
    """

    name: str
    plate: str
    zones: list[str]
    rate: str | None = None
    duration_minutes: int = 1440
    renew_at: str | None = None
    window: Window = field(default_factory=Window)
    enabled: bool = True
    stall: str | None = None
    toutes_zones: bool = False
    max_cost_per_ticket: float | None = None
    renew_margin_minutes: int | None = None

    @classmethod
    def parse(cls, data: dict, index: int) -> "Rule":
        if not isinstance(data, dict):
            raise ConfigError(f"règle #{index + 1} : doit être un objet")
        nom = data.get("name", "sans nom")
        if not data.get("plate"):
            raise ConfigError(f"règle #{index + 1} ({nom}) : champ `plate` manquant")
        zones = cls._zones(data, index, nom)
        rule = cls(
            name=str(data.get("name") or f"règle {index + 1}"),
            plate=str(data["plate"]).upper().replace(" ", "").replace("-", ""),
            zones=zones,
            rate=str(data["rate"]).strip() if data.get("rate") else None,
            duration_minutes=parse_duration(data.get("duration", "24h")),
            renew_at=(
                parse_time(data["renew_at"]).strftime("%H:%M") if data.get("renew_at") else None
            ),
            window=Window.parse(data.get("window")),
            enabled=bool(data.get("enabled", True)),
            stall=str(data["stall"]) if data.get("stall") else None,
            toutes_zones=bool(data.get("toutes_zones", False)),
            max_cost_per_ticket=(
                None if data.get("max_cost_per_ticket") is None
                else float(data["max_cost_per_ticket"])
            ),
            renew_margin_minutes=(
                int(data["renew_margin_minutes"]) if data.get("renew_margin_minutes") else None
            ),
        )
        if rule.duration_minutes <= 0:
            raise ConfigError(f"règle « {rule.name} » : `duration` doit être positive")
        return rule

    @staticmethod
    def _zones(data: dict, index: int, nom) -> list[str]:
        """`zones: [...]` ou, forme courte d'une seule zone, `location:`."""
        brut = data.get("zones", data.get("location"))
        if brut is None or brut == []:
            raise ConfigError(
                f"règle #{index + 1} ({nom}) : il faut `zones:` (liste ordonnée, "
                "la première est la zone préférée) ou `location:` pour une seule zone"
            )
        if not isinstance(brut, (list, tuple)):
            brut = [brut]
        zones: list[str] = []
        for zone in brut:
            zone = str(zone).strip()
            if zone and zone not in zones:  # l'ordre porte le sens : on le garde
                zones.append(zone)
        if not zones:
            raise ConfigError(f"règle #{index + 1} ({nom}) : `zones` est vide")
        return zones

    @property
    def location(self) -> str:
        """La zone préférée — celle qu'on essaie en premier."""
        return self.zones[0]

    @property
    def fallbacks(self) -> list[str]:
        return self.zones[1:]

    def key(self) -> str:
        return f"{self.plate}@{self.zones[0]}"


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
    timezone: str = "Europe/Paris"
    country: str = "FR"
    renew_margin_minutes: int = 25
    dry_run: bool = False
    rules: list[Rule] = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"Config introuvable : {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} : le fichier doit être un objet YAML")
        raw = _expand(raw)

        if not raw.get("rules"):
            raise ConfigError(f"{path} : aucune règle définie (`rules:`)")

        cfg = cls(
            timezone=str(raw.get("timezone", "Europe/Paris")),
            country=str(raw.get("country", "FR")).upper(),
            renew_margin_minutes=int(raw.get("renew_margin_minutes", 25)),
            dry_run=bool(raw.get("dry_run", False)),
            rules=[Rule.parse(r, i) for i, r in enumerate(raw["rules"])],
            notify=NotifyConfig.parse(raw.get("notify")),
            path=path,
        )
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
