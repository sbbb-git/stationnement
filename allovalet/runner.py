"""Le cœur : à chaque passage, s'assurer qu'un ticket est en cours.

C'est la promesse d'AlloValet, réduite à l'essentiel : « vos tickets se
renouvellent tout seuls, avant qu'ils n'expirent ».

La décision ne dépend pas de l'heure qu'il est mais de l'état réel du compte,
lu à chaque passage. Un passage raté est donc rattrapé au suivant, au lieu
d'être perdu jusqu'au lendemain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config, Rule
from .errors import ApiError, NotEligibleError
from .models import ParkingSession, money
from .notify import Notifier
from .paybyphone import best_duration
from .state import State

log = logging.getLogger("allovalet.runner")

OK = "ok"
SKIPPED = "hors-créneau"
PURCHASED = "acheté"
PLANNED = "simulé"
BLOCKED = "bloqué"
FAILED = "échec"

ICONS = {OK: "·", SKIPPED: "·", PURCHASED: "✅", PLANNED: "🧪", BLOCKED: "⛔", FAILED: "❌"}


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
        return f"{ICONS[self.status]} [{self.rule}] {self.message}"


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

        self.state.log_run([r.line() for r in report.results])
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

        reason = self._why_act(rule, active, now_local)
        if not reason:
            reste = active.expiry - now_local.astimezone(timezone.utc)
            return RuleResult(
                rule.name, OK,
                f"couvert jusqu'à {self._local(active.expiry)} (reste {_fmt_delta(reste)})",
                session=active,
            )
        return self._take_ticket(rule, reason)

    def _why_act(
        self, rule: Rule, active: ParkingSession | None, now_local: datetime
    ) -> str | None:
        """Faut-il un ticket, et pourquoi ? `None` = rien à faire.

        Trois cas, du plus impératif au plus confortable :

        1. plus rien d'actif      → on prend immédiatement, quelle que soit l'heure
        2. le ticket va expirer   → on le reprend **avant** le trou
        3. rendez-vous quotidien  → à `renew_at`, si le ticket ne tient pas
                                    jusqu'au rendez-vous du lendemain
        """
        if active is None:
            return "aucun ticket en cours"

        # Une seule source de temps : celle passée en paramètre. Sans ça, la
        # décision dépendrait à la fois de `now_local` et de l'horloge réelle.
        now_utc = now_local.astimezone(timezone.utc)
        margin = timedelta(minutes=self.cfg.margin_for(rule))
        if not active.covers(now_utc, margin):
            restant = (active.expiry - now_utc) if active.expiry else timedelta(0)
            return f"expire dans {_fmt_delta(max(timedelta(0), restant))}"

        if not rule.renew_at or not active.expiry:
            return None

        hour, minute = (int(x) for x in rule.renew_at.split(":"))
        anchor = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_local < anchor:
            return None  # le rendez-vous du jour n'est pas encore arrivé
        if active.expiry.astimezone(self.tz) >= anchor + timedelta(days=1):
            return None  # le ticket tient déjà jusqu'au prochain rendez-vous

        # Une seule reprise par rendez-vous, sinon on recommencerait à chaque passage.
        key = f"{rule.name}@{anchor.isoformat()}"
        if self.dry_run:  # une simulation ne doit pas consommer le rendez-vous
            return None if self.state.done(key) else f"rendez-vous de {rule.renew_at}"
        if not self.state.once(key):
            return None
        return f"rendez-vous de {rule.renew_at}"

    # ----------------------------------------------------------------- achat

    def _take_ticket(self, rule: Rule, reason: str) -> RuleResult:
        rate = self.client.pick_rate_option(rule.location, rule.plate, rule.rate)
        duration = best_duration(rule.duration_minutes, rate.accepted_time_units)

        quote = None
        try:
            quote = self.client.quote(
                rule.location, rule.plate, duration, rate_option_id=rate.id, stall=rule.stall
            )
        except ApiError as exc:
            log.warning("Devis indisponible (%s) — garde-fous de config seuls.", exc)

        cost = quote.cost if quote else None
        if (
            cost is not None
            and rule.max_cost_per_ticket is not None
            and cost > rule.max_cost_per_ticket + 1e-9
        ):
            return RuleResult(
                rule.name, BLOCKED,
                f"{reason} — refusé : ticket à {money(cost)} > plafond "
                f"{money(rule.max_cost_per_ticket)} (`max_cost_per_ticket`)",
                cost=cost,
            )

        detail = (
            f"zone {rule.location} · {rate.type or rate.name} · {duration} · "
            f"{money(cost, quote.currency) if quote else 'prix inconnu'}"
        )
        if self.dry_run:
            return RuleResult(rule.name, PLANNED, f"{reason} → achèterait : {detail}",
                              cost=cost or 0.0)

        payment = self.client.payment_account_id() if cost else None
        session = self.client.start_session(
            location_id=rule.location,
            plate=rule.plate,
            duration=duration,
            rate_option_id=rate.id,
            stall=rule.stall,
            payment_account_id=payment,
        )
        self._sessions = None  # l'état a changé
        if cost:
            self.state.add_spend(rule.key(), cost)
        return RuleResult(
            rule.name, PURCHASED,
            f"{reason} → ticket pris : {detail} · expire {self._local(session.expiry)}",
            session=session, cost=cost or 0.0,
        )

    def _local(self, moment: datetime | None) -> str:
        return moment.astimezone(self.tz).strftime("%d/%m %H:%M") if moment else "?"


def _fmt_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds() // 60)
    hours, minutes = divmod(max(0, total), 60)
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}min"
