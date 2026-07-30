"""Objets métier communs à tous les fournisseurs (PayByPhone, EasyPark…)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value) -> datetime | None:
    """Parse une date API (RFC 3339, ou epoch ms) → datetime aware UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    text = text.replace("Z", "+00:00")
    # PayByPhone renvoie parfois 7 chiffres de fraction (.1234567) : illégal pour fromisoformat < 3.11
    text = re.sub(r"\.(\d{1,6})\d*", r".\1", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Vehicle:
    id: str
    plate: str
    country: str | None = None
    type: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class RateOption:
    """Un tarif disponible sur une zone (VIS, RES, CMI/PMR, PRO, 2RM…)."""

    id: str
    name: str
    type: str | None = None
    is_default: bool = False
    max_stay_minutes: int | None = None
    accepted_time_units: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def matches(self, wanted: str) -> bool:
        """`wanted` peut être un id exact, un type (CMI) ou un bout de nom."""
        wanted = wanted.strip().lower()
        if not wanted:
            return False
        if wanted == str(self.id).lower():
            return True
        if self.type and wanted == self.type.lower():
            return True
        haystack = f"{self.name or ''} {self.type or ''}".lower()
        return wanted in haystack


@dataclass
class Quote:
    """Devis : combien coûte X minutes ici, et jusqu'à quand ça va."""

    cost: float
    currency: str
    start: datetime | None
    expiry: datetime | None
    quote_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def minutes(self) -> int | None:
        if self.start and self.expiry:
            return int((self.expiry - self.start).total_seconds() // 60)
        return None


@dataclass
class ParkingSession:
    """Un ticket actif."""

    id: str
    plate: str
    location_id: str
    start: datetime | None
    expiry: datetime | None
    rate_option_id: str | None = None
    rate_type: str | None = None
    cost: float | None = None
    currency: str | None = None
    raw: dict = field(default_factory=dict)

    def covers(self, moment: datetime, margin: timedelta = timedelta(0)) -> bool:
        """Le ticket est-il valide à `moment` (+ marge de sécurité) ?"""
        if not self.expiry:
            return False
        if self.start and moment + margin < self.start:
            return False
        return self.expiry >= moment + margin

    @property
    def remaining(self) -> timedelta:
        if not self.expiry:
            return timedelta(0)
        return max(timedelta(0), self.expiry - utcnow())

    def describe(self) -> str:
        exp = self.expiry.astimezone().strftime("%d/%m %H:%M") if self.expiry else "?"
        rate = self.rate_type or self.rate_option_id or "?"
        return f"{self.plate} · zone {self.location_id} · {rate} · expire {exp}"
