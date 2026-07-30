"""SmartPark : le découpage doit vraiment être le moins cher."""

from allovalet.smartpark import candidate_durations, cheapest_plan, next_chunk
from tests.fake_pbp import price


def paris_curve() -> dict[int, float]:
    """Barème progressif zone 1 (celui du faux serveur)."""
    return {m: price("75016", m) for m in [30, 60, 90, 120, 180, 240, 300, 360]}


def test_decoupage_bat_le_ticket_unique():
    curve = paris_curve()
    plan = cheapest_plan(360, curve)

    assert plan.total_minutes >= 360
    assert plan.cost < curve[360]  # 6 h d'un bloc = 75 €
    assert plan.cost == 36.0  # 3 × 2 h à 12 €
    assert plan.chunks == [120, 120, 120]
    assert plan.savings == 39.0
    assert plan.savings_pct == 52.0


def test_optimum_verifie_par_force_brute():
    curve = paris_curve()
    total = 300
    plan = cheapest_plan(total, curve)

    best = float("inf")
    durations = sorted(curve)

    def explore(remaining: int, cost: float):
        nonlocal best
        if cost >= best:
            return
        if remaining <= 0:
            best = cost
            return
        for d in durations:
            explore(remaining - d, cost + curve[d])

    explore(total, 0.0)
    assert plan.cost == round(best, 2)


def test_depassement_autorise_si_moins_cher():
    # 20 min coûtent plus cher qu'une heure : on doit prendre l'heure.
    curve = {20: 5.0, 60: 3.0}
    plan = cheapest_plan(20, curve)
    assert plan.chunks == [60]
    assert plan.cost == 3.0


def test_tarif_lineaire_ne_decoupe_pas_inutilement():
    curve = {60: 2.0, 120: 4.0, 180: 6.0}
    plan = cheapest_plan(180, curve)
    assert plan.cost == 6.0
    assert plan.total_minutes == 180


def test_prochain_ticket():
    assert next_chunk(360, paris_curve()) == 120
    assert next_chunk(0, paris_curve()) is None
    assert next_chunk(120, {}) is None


def test_durees_candidates_bornees_par_duree_max():
    durations = candidate_durations(150)
    assert max(durations) == 150
    assert all(d <= 150 for d in durations)


def test_plan_vide_sans_bareme():
    plan = cheapest_plan(120, {})
    assert plan.chunks == []
    assert "aucun ticket" in plan.describe()
