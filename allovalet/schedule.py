"""Fenêtres horaires : quand une règle doit être active."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .errors import ConfigError

DAYS = {
    "mon": 0, "lun": 0, "lundi": 0, "monday": 0,
    "tue": 1, "mar": 1, "mardi": 1, "tuesday": 1,
    "wed": 2, "mer": 2, "mercredi": 2, "wednesday": 2,
    "thu": 3, "jeu": 3, "jeudi": 3, "thursday": 3,
    "fri": 4, "ven": 4, "vendredi": 4, "friday": 4,
    "sat": 5, "sam": 5, "samedi": 5, "saturday": 5,
    "sun": 6, "dim": 6, "dimanche": 6, "sunday": 6,
}
ALL_DAYS = frozenset(range(7))
GROUPS = {
    "all": ALL_DAYS,
    "tous": ALL_DAYS,
    "daily": ALL_DAYS,
    "weekdays": frozenset({0, 1, 2, 3, 4}),
    "semaine": frozenset({0, 1, 2, 3, 4}),
    "weekend": frozenset({5, 6}),
    "workdays": frozenset({0, 1, 2, 3, 4, 5}),  # lun-sam : jours payants à Paris
    "ouvrables": frozenset({0, 1, 2, 3, 4, 5}),
}

_DURATION_RE = re.compile(
    r"^\s*(?:(?P<d>\d+)\s*(?:d|j|jours?|days?))?\s*"
    r"(?:(?P<h>\d+)\s*(?:h|heures?|hours?))?\s*"
    r"(?:(?P<m>\d+)\s*(?:m|min|minutes?)?)?\s*$",
    re.IGNORECASE,
)


def parse_duration(value) -> int:
    """« 24h », « 2h30 », « 90m », « 1d », 120 → minutes."""
    if isinstance(value, (int, float)):
        return int(value)
    match = _DURATION_RE.match(str(value))
    if not match or not any(match.groupdict().values()):
        raise ConfigError(f"Durée illisible : {value!r} (ex. « 24h », « 2h30 », « 45m »)")
    days = int(match.group("d") or 0)
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    total = days * 1440 + hours * 60 + minutes
    if total <= 0:
        raise ConfigError(f"Durée nulle : {value!r}")
    return total


def parse_time(value) -> time:
    text = str(value).strip()
    match = re.match(r"^(\d{1,2})[h:.]?(\d{2})?$", text)
    if not match:
        raise ConfigError(f"Heure illisible : {value!r} (ex. « 20:00 », « 9h30 »)")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"Heure hors bornes : {value!r}")
    if (hour, minute) == (23, 59):
        return END_OF_DAY
    return time(hour, minute)


def parse_days(value) -> frozenset[int]:
    if value is None:
        return ALL_DAYS
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    out: set[int] = set()
    for item in value:
        key = str(item).strip().lower()
        if key in GROUPS:
            out |= GROUPS[key]
        elif key in DAYS:
            out.add(DAYS[key])
        elif "-" in key:  # « lun-sam »
            start, _, end = key.partition("-")
            if start not in DAYS or end not in DAYS:
                raise ConfigError(f"Jour inconnu dans {key!r}")
            a, b = DAYS[start], DAYS[end]
            out |= {d % 7 for d in range(a, b + 1 if b >= a else b + 8)}
        else:
            raise ConfigError(f"Jour inconnu : {item!r}")
    return frozenset(out)


#  « 23:59 » veut dire « jusqu'à la fin de la journée » : sans ça, un passage
#  tombant entre 23:59:00 et minuit serait considéré hors créneau.
END_OF_DAY = time(23, 59, 59, 999999)


@dataclass
class Window:
    """Créneau récurrent, exprimé en heure locale."""

    start: time = time(0, 0)
    end: time = END_OF_DAY
    days: frozenset[int] = field(default_factory=lambda: ALL_DAYS)

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    @classmethod
    def parse(cls, data) -> "Window":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError(f"`window` doit être un objet, reçu : {data!r}")
        return cls(
            start=parse_time(data.get("from", "00:00")),
            end=parse_time(data.get("to", "23:59")),
            days=parse_days(data.get("days")),
        )

    def contains(self, moment: datetime) -> bool:
        """`moment` doit être en heure locale (tz-aware)."""
        clock = moment.time()
        if self.overnight:
            # ex. 20:00 → 08:00 : le jour de référence est celui du démarrage
            if clock >= self.start:
                return moment.weekday() in self.days
            return (moment - timedelta(days=1)).weekday() in self.days
        return moment.weekday() in self.days and self.start <= clock < self.end

    def end_after(self, moment: datetime) -> datetime:
        """Fin du créneau en cours (ou du prochain) à partir de `moment`."""
        end_today = moment.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0
        )
        if self.overnight:
            return end_today if moment.time() < self.end else end_today + timedelta(days=1)
        return end_today if end_today > moment else end_today + timedelta(days=1)

    def describe(self) -> str:
        names = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
        days = "tous les jours" if self.days == ALL_DAYS else ",".join(
            names[d] for d in sorted(self.days)
        )
        return f"{days} {self.start.strftime('%H:%M')}→{self.end.strftime('%H:%M')}"
