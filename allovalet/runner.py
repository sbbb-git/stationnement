"""Le cœur : à chaque passage, on regarde ce qui doit être fait et on le fait.

Principe important : la condition d'achat n'est pas « il est 20h01 » mais
« la règle est dans son créneau ET aucun ticket ne couvre l'instant présent ».
Un passage raté (runner GitHub en retard, panne réseau) est donc rattrapé au
passage suivant, au lieu d'être perdu jusqu'au lendemain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config, Rule
from .errors import ApiError, NotEligibleError
from .models import ParkingSession, utcnow
from .notify import Notifier
from .paybyphone import best_duration
from .smartpark import build_curve, candidate_durations, cheapest_plan
from .state import State

log = logging.getLogger("allovalet.runner")

CURVE_TTL = timedelta(days=7)

OK = "ok"
SKIPPED = "hors-créneau"
PURCHASED = "acheté"
PLANNED = "simulé"
BLOCKED = "bloqué"
FAILED = "échec"


@dataclass
class RuleResult:
    rule: str
    status: str
    message: str
    session: ParkingSession | None = None
    cost: float = 0.0

    @property
    def is_failure(self) -> bool:
        return self.status in (FAILED, BLOCKED)

    def line(self) -> str:
        icon = {
            OK: "·", SKIPPED: "·", PURCHASED: "✅", PLANNED: "🧪",
            BLOCKED: "⛔", FAILED: "❌",
        }[self.status]
        return f"{icon} [{self.rule}] {self.message}"


@dataclass
class TickReport:
    results: list[RuleResult] = field(default_factory=list)

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if r.is_failure]

    @property
    def purchases(self) -> list[RuleResult]:
        return [r for r in self.results if r.status == PURCHASED]

    def text(self) -> str:
        return "\n".join(r.line() for r in self.results)


class Runner:
    def __init__(
        self,
        config: Config,
        client,
        state: State | None = None,
        notifier: Notifier | None = None,
        dry_run: bool = False,
    ):
        self.cfg = config
        self.client = client
        self.state = state or State()
        self.notifier = notifier or Notifier(config.notify)
        self.dry_run = dry_run or config.dry_run
        self.tz = ZoneInfo(config.timezone)
        self._sessions: list[ParkingSession] | None = None

    # ------------------------------------------------------------------ tick

    def tick(self) -> TickReport:
        report = TickReport()
        self._sessions = None

        for rule in self.cfg.active_rules():
            try:
                result = self._apply(rule)
            except NotEligibleError as exc:
                result = RuleResult(rule.name, BLOCKED, str(exc))
            except Exception as exc:  # noqa: BLE001 — une règle en échec n'arrête pas les autres
                log.exception("règle « %s » en échec", rule.name)
                result = RuleResult(rule.name, FAILED, str(exc))
            report.results.append(result)
            log.info(result.line())

        self._notify(report)
        return report

    def _notify(self, report: TickReport) -> None:
        if report.failures:
            self.notifier.send(
                "Stationnement — action requise",
                "\n".join(r.line() for r in report.failures + report.purchases),
                success=False,
            )
        elif report.purchases:
            self.notifier.send(
                "Stationnement — ticket pris",
                "\n".join(r.line() for r in report.purchases),
                success=True,
            )

    # ------------------------------------------------------------ une règle

    def sessions(self) -> list[ParkingSession]:
        if self._sessions is None:
            self._sessions = self.client.current_sessions()
        return self._sessions

    def _apply(self, rule: Rule) -> RuleResult:
        now_local = datetime.now(self.tz)
        margin = timedelta(minutes=self.cfg.margin_for(rule))

        active = self.client.find_active(rule.plate, rule.location, self.sessions())

        if not rule.window.contains(now_local):
            if active:
                return RuleResult(
                    rule.name, SKIPPED,
                    f"hors créneau ({rule.window.describe()}) — ticket en cours jusqu'à "
                    f"{self._local(active.expiry)}",
                    session=active,
                )
            return RuleResult(rule.name, SKIPPED, f"hors créneau ({rule.window.describe()})")

        if active and active.covers(utcnow(), margin):
            return RuleResult(
                rule.name, OK,
                f"couvert jusqu'à {self._local(active.expiry)} "
                f"(reste {_fmt_delta(active.remaining)})",
                session=active,
            )

        minutes, plan_note = self._target_minutes(rule, now_local)
        if minutes <= 0:
            return RuleResult(rule.name, SKIPPED, "rien à couvrir sur ce créneau")

        return self._buy(rule, minutes, plan_note, active)

    def _target_minutes(self, rule: Rule, now_local: datetime) -> tuple[int, str]:
        if rule.mode == "renew":
            return rule.duration_minutes, ""

        # --- SmartPark : couvrir jusqu'à la fin du créneau, au meilleur prix
        end = rule.window.end_after(now_local)
        remaining = int((end - now_local).total_seconds() // 60)
        if remaining <= 0:
            return 0, ""

        rate = self._rate_option(rule)
        curve = self._price_curve(rule, rate, now_local)
        if not curve:
            fallback = min(remaining, rule.max_chunk_minutes or 60)
            return fallback, "aucun devis obtenu — durée par défaut"

        plan = cheapest_plan(remaining, curve)
        if not plan.chunks:
            fallback = min(remaining, rule.max_chunk_minutes or 60)
            return fallback, "découpage impossible — durée par défaut"

        chunk = plan.chunks[0]
        if rule.max_chunk_minutes:
            chunk = min(chunk, rule.max_chunk_minutes)
        chunk = max(chunk, rule.min_chunk_minutes)
        note = (
            f"SmartPark jusqu'à {end.strftime('%H:%M')} → {plan.describe()}"
        )
        return chunk, note

    def _rate_option(self, rule: Rule):
        return self.client.pick_rate_option(rule.location, rule.plate, rule.rate)

    def _price_curve(self, rule: Rule, rate, now_local: datetime) -> dict[int, float]:
        cache_key = f"{rule.location}:{rate.id}:{now_local.weekday()}:{now_local.hour}"
        cached = self.state.data.setdefault("curves", {}).get(cache_key)
        if cached and _fresh(cached.get("at")):
            return {int(k): float(v) for k, v in cached["curve"].items()}

        durations = candidate_durations(rate.max_stay_minutes)
        durations = [d for d in durations if d >= rule.min_chunk_minutes]
        if rule.max_chunk_minutes:
            durations = [d for d in durations if d <= rule.max_chunk_minutes]

        def price_of(minutes: int):
            quote = self.client.quote(
                rule.location,
                rule.plate,
                best_duration(minutes, rate.accepted_time_units),
                rate_option_id=rate.id,
                stall=rule.stall,
            )
            real = quote.minutes
            # le vendeur peut arrondir la durée : on garde le prix à la durée réelle
            if real and abs(real - minutes) > 5:
                log.debug("devis %s min → durée réelle %s min", minutes, real)
                return None
            return quote.cost

        curve = build_curve(price_of, durations)
        if curve:
            self.state.data["curves"][cache_key] = {
                "at": utcnow().isoformat(),
                "curve": {str(k): v for k, v in curve.items()},
            }
            self.state.save()
        return curve

    def _buy(
        self, rule: Rule, minutes: int, note: str, active: ParkingSession | None
    ) -> RuleResult:
        rate = self._rate_option(rule)
        duration = best_duration(minutes, rate.accepted_time_units)

        quote = None
        try:
            quote = self.client.quote(
                rule.location, rule.plate, duration, rate_option_id=rate.id, stall=rule.stall
            )
        except ApiError as exc:
            log.warning("Devis indisponible (%s) — on continue avec les garde-fous config.", exc)

        cost = quote.cost if quote else None
        guard = self._check_budget(rule, cost)
        if guard:
            return RuleResult(rule.name, BLOCKED, guard, cost=cost or 0.0)

        price_txt = f"{cost:.2f} {quote.currency}" if quote else "prix inconnu"
        detail = (
            f"zone {rule.location} · {rate.type or rate.name} · {duration} · {price_txt}"
        )
        if note:
            detail += f"\n   ↳ {note}"

        if self.dry_run:
            return RuleResult(rule.name, PLANNED, f"achèterait : {detail}", cost=cost or 0.0)

        payment = None
        if cost:  # tarif payant → il faut un moyen de paiement enregistré
            payment = self.client.payment_account_id()

        try:
            session = self.client.start_session(
                location_id=rule.location,
                plate=rule.plate,
                duration=duration,
                rate_option_id=rate.id,
                stall=rule.stall,
                payment_account_id=payment,
            )
        except ApiError as exc:
            session = self._maybe_extend(rule, duration, payment, active, exc)

        self._sessions = None  # l'état a changé
        if cost:
            self.state.add_spend(rule.key(), cost)
        return RuleResult(
            rule.name, PURCHASED,
            f"ticket pris — {detail} · expire {self._local(session.expiry)}",
            session=session, cost=cost or 0.0,
        )

    def _maybe_extend(self, rule, duration, payment, active, original: ApiError):
        """Certaines zones refusent un 2e ticket : on prolonge celui en cours."""
        blocking = active or self.client.find_active(rule.plate, rule.location)
        text = (original.body or str(original)).lower()
        looks_duplicate = any(
            k in text for k in ("already", "existing", "active session", "en cours", "duplicate")
        )
        if not (blocking and blocking.id and looks_duplicate):
            raise original
        log.info("Ticket déjà actif — prolongation de %s", blocking.id)
        self.client.extend_session(blocking.id, duration, payment_account_id=payment)
        self._sessions = None
        extended = self.client.find_active(rule.plate, rule.location)
        if not extended:
            raise original
        return extended

    def _check_budget(self, rule: Rule, cost: float | None) -> str | None:
        if cost is None:
            return None
        if rule.max_cost_per_ticket is not None and cost > rule.max_cost_per_ticket + 1e-9:
            return (
                f"refusé : ticket à {cost:.2f} € > plafond {rule.max_cost_per_ticket:.2f} € "
                "(`max_cost_per_ticket`)"
            )
        if rule.max_cost_per_day is not None:
            spent = self.state.spent_today(rule.key())
            if spent + cost > rule.max_cost_per_day + 1e-9:
                return (
                    f"refusé : {spent:.2f} € déjà dépensés aujourd'hui + {cost:.2f} € "
                    f"> plafond {rule.max_cost_per_day:.2f} € (`max_cost_per_day`)"
                )
        return None

    def _local(self, moment: datetime | None) -> str:
        if not moment:
            return "?"
        return moment.astimezone(self.tz).strftime("%d/%m %H:%M")


def _fresh(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        return utcnow() - datetime.fromisoformat(iso) < CURVE_TTL
    except ValueError:
        return False


def _fmt_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds() // 60)
    hours, minutes = divmod(max(0, total), 60)
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}min"
