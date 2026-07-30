"""Petit état local : tokens en cache + dépenses du jour.

Rien de critique n'y est stocké : l'état de vérité, ce sont les tickets actifs
côté PayByPhone. Si le fichier disparaît (runner GitHub sans cache), tout
continue de fonctionner.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

log = logging.getLogger("allovalet.state")

DEFAULT_PATH = ".allovalet_state.json"


class State:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.getenv("ALLOVALET_STATE", DEFAULT_PATH))
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                log.warning("État illisible (%s) — on repart de zéro.", exc)
                self.data = {}
        self.data.setdefault("tokens", {})
        self.data.setdefault("spend", {})

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            log.warning("Impossible d'écrire l'état : %s", exc)

    # ---------------------------------------------------------------- tokens

    def tokens(self, provider: str) -> dict:
        return self.data["tokens"].get(provider, {})

    def set_tokens(self, provider: str, tokens: dict) -> None:
        self.data["tokens"][provider] = tokens
        self.save()

    # -------------------------------------------------------------- dépenses

    def _today(self) -> str:
        return date.today().isoformat()

    def spent_today(self, rule_key: str) -> float:
        day = self.data["spend"].get(self._today(), {})
        return float(day.get(rule_key, 0.0))

    def add_spend(self, rule_key: str, amount: float) -> None:
        today = self._today()
        spend = self.data["spend"]
        # on ne garde que les 7 derniers jours
        for old in [d for d in spend if d < today][:-6]:
            spend.pop(old, None)
        spend.setdefault(today, {})
        spend[today][rule_key] = round(spend[today].get(rule_key, 0.0) + float(amount), 2)
        self.save()
