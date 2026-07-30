"""Petit état local : tokens en cache + dépenses du jour.

Rien de critique n'y est stocké : l'état de vérité, ce sont les tickets actifs
côté PayByPhone. Si le fichier disparaît (runner GitHub sans cache), tout
continue de fonctionner.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
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
        self.data.setdefault("savings", [])
        self.data.setdefault("journal", [])

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

    def total_spend(self) -> float:
        return round(
            sum(sum(day.values()) for day in self.data["spend"].values()), 2
        )

    # ------------------------------------------------- règles mises en pause

    def is_disabled(self, rule_name: str) -> bool:
        return rule_name in self.data.get("disabled", [])

    def set_disabled(self, rule_name: str, disabled: bool) -> None:
        paused = set(self.data.get("disabled", []))
        paused.add(rule_name) if disabled else paused.discard(rule_name)
        self.data["disabled"] = sorted(paused)
        self.save()

    # ------------------------------------------------- actions à faire une fois

    def done(self, key: str) -> bool:
        """Lecture seule : l'action a-t-elle déjà été faite ?"""
        return key in self.data.get("once", {})

    def once(self, key: str) -> bool:
        """Vrai la première fois seulement — empêche de refaire la même action.

        Sert au rendez-vous quotidien : sans ça, un ticket qui reste largement
        valide déclencherait un renouvellement à chaque passage du soir.
        """
        marks = self.data.setdefault("once", {})
        if key in marks:
            return False
        marks[key] = datetime.now().isoformat(timespec="seconds")
        if len(marks) > 200:
            for old, _ in sorted(marks.items(), key=lambda kv: kv[1])[:100]:
                marks.pop(old, None)
        self.save()
        return True

    # -------------------------------------------------------------- SmartPark

    def credit_savings(self, key: str, rule: str, amount: float, detail: str) -> bool:
        """Crédite l'économie d'un découpage, une seule fois par créneau couvert."""
        if amount <= 0:
            return False
        if any(entry.get("key") == key for entry in self.data["savings"]):
            return False
        self.data["savings"].append({
            "key": key,
            "at": datetime.now().isoformat(timespec="seconds"),
            "rule": rule,
            "amount": round(float(amount), 2),
            "detail": detail,
        })
        del self.data["savings"][:-200]
        self.save()
        return True

    def total_savings(self) -> float:
        return round(sum(float(e.get("amount", 0)) for e in self.data["savings"]), 2)

    # --------------------------------------------------------------- journal

    def log_run(self, lines: list[str]) -> None:
        self.data["journal"].append({
            "at": datetime.now().isoformat(timespec="seconds"),
            "lines": lines,
        })
        del self.data["journal"][:-50]
        self.save()
