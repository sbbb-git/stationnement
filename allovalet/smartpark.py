"""SmartPark — payer moins cher en découpant la durée.

Le tarif de voirie est *progressif* : à Paris (1er-11e), 6 h d'affilée coûtent
75 € alors que 3 tickets de 2 h coûtent 3 × 12 € = 36 €, parce que chaque
nouveau ticket repart au bas du barème.

On ne code aucun barème en dur : la courbe de prix est construite avec de vrais
devis (`quote`) sur la zone et le tarif concernés, puis on cherche le découpage
optimal par programmation dynamique (problème du rendu de monnaie).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from math import gcd
from typing import Callable, Iterable

log = logging.getLogger("allovalet.smartpark")

# Durées candidates par défaut (minutes) — filtrées ensuite par la durée max de la zone.
DEFAULT_STEPS = [15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 480, 600, 720, 1440]

MAX_GRID = 20_000  # garde-fou sur la taille de la DP


@dataclass
class Plan:
    chunks: list[int] = field(default_factory=list)  # durées en minutes, dans l'ordre
    cost: float = 0.0
    currency: str = "EUR"
    single_ticket_cost: float | None = None  # prix d'un seul ticket couvrant tout

    @property
    def total_minutes(self) -> int:
        return sum(self.chunks)

    @property
    def savings(self) -> float:
        if self.single_ticket_cost is None:
            return 0.0
        return round(self.single_ticket_cost - self.cost, 2)

    @property
    def savings_pct(self) -> float:
        if not self.single_ticket_cost:
            return 0.0
        return round(100 * self.savings / self.single_ticket_cost, 1)

    def describe(self) -> str:
        if not self.chunks:
            return "aucun ticket nécessaire"
        parts = " + ".join(_fmt_minutes(c) for c in self.chunks)
        line = f"{len(self.chunks)} ticket(s) : {parts} = {self.cost:.2f} {self.currency}"
        if self.single_ticket_cost is not None and self.savings > 0:
            line += (
                f"  (au lieu de {self.single_ticket_cost:.2f} {self.currency} "
                f"en un seul ticket → -{self.savings:.2f} {self.currency}, "
                f"-{self.savings_pct:.0f} %)"
            )
        return line


def _fmt_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h{mins:02d}"
    if hours:
        return f"{hours}h"
    return f"{mins}min"


def candidate_durations(
    max_stay_minutes: int | None, steps: Iterable[int] | None = None
) -> list[int]:
    steps = sorted(set(steps or DEFAULT_STEPS))
    if max_stay_minutes:
        steps = [s for s in steps if s <= max_stay_minutes]
        if max_stay_minutes not in steps:
            steps.append(max_stay_minutes)
    return sorted(set(s for s in steps if s > 0))


def build_curve(
    price_of: Callable[[int], float | None], durations: Iterable[int]
) -> dict[int, float]:
    """Interroge le tarificateur pour chaque durée candidate. `None` = refusé."""
    curve: dict[int, float] = {}
    for minutes in durations:
        try:
            price = price_of(minutes)
        except Exception as exc:  # une durée non vendue ne doit pas tout casser
            log.debug("devis %s min impossible : %s", minutes, exc)
            continue
        if price is None:
            continue
        curve[minutes] = round(float(price), 2)
        log.debug("devis %s min → %.2f", minutes, curve[minutes])
    return curve


def cheapest_plan(total_minutes: int, curve: dict[int, float], currency: str = "EUR") -> Plan:
    """Découpage le moins cher couvrant *au moins* `total_minutes`.

    Un ticket plus long que le reste à couvrir est autorisé s'il est moins cher
    (fréquent : le dernier quart d'heure coûte parfois plus qu'une heure pleine).
    """
    if total_minutes <= 0 or not curve:
        return Plan(currency=currency)

    durations = sorted(curve)
    step = 0
    for d in durations:
        step = gcd(step, d)
    step = max(step, 1)

    size = -(-total_minutes // step)  # ceil
    if size > MAX_GRID:  # granularité de secours
        step = max(step, -(-total_minutes // MAX_GRID))
        size = -(-total_minutes // step)

    inf = float("inf")
    dp = [inf] * (size + 1)
    count = [0] * (size + 1)  # à prix égal, on veut le moins de tickets possible
    choice = [0] * (size + 1)
    dp[0] = 0.0

    units = [(max(1, d // step), d) for d in durations]
    for cell in range(1, size + 1):
        for unit, minutes in units:
            prev = max(0, cell - unit)
            if dp[prev] == inf:
                continue
            cost = dp[prev] + curve[minutes]
            tickets = count[prev] + 1
            better = cost < dp[cell] - 1e-9 or (
                abs(cost - dp[cell]) <= 1e-9 and tickets < count[cell]
            )
            if better:
                dp[cell] = cost
                count[cell] = tickets
                choice[cell] = minutes

    if dp[size] == inf:
        return Plan(currency=currency)

    chunks: list[int] = []
    cell = size
    while cell > 0:
        minutes = choice[cell]
        chunks.append(minutes)
        cell = max(0, cell - max(1, minutes // step))
    chunks.reverse()

    single = curve.get(total_minutes)
    if single is None:
        # prix du plus petit ticket unique couvrant toute la durée, s'il existe
        covering = [curve[d] for d in durations if d >= total_minutes]
        single = min(covering) if covering else None

    return Plan(
        chunks=chunks,
        cost=round(dp[size], 2),
        currency=currency,
        single_ticket_cost=single,
    )


def next_chunk(remaining_minutes: int, curve: dict[int, float]) -> int | None:
    """Durée du prochain ticket à acheter, recalculée à chaque passage."""
    plan = cheapest_plan(remaining_minutes, curve)
    return plan.chunks[0] if plan.chunks else None
