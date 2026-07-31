"""Verrouille ce qui est réellement demandé : 75016 et 75008, toujours couverts,
rendez-vous à 20h01.

Ces tests portent sur les fichiers livrés (config.yml et le workflow), pas sur
des exemples : si quelqu'un touche à une zone, à l'horaire ou au cron, ça casse
ici.
"""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from allovalet.config import Config

ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")


def ma_config() -> Config:
    return Config.load(ROOT / "config.yml")


def test_une_regle_par_secteur():
    """Deux secteurs, chacun visant sa zone : le 75008 et le 75016."""
    rules = ma_config().rules
    assert [r.location for r in rules] == ["75008", "75016"]
    assert {r.plate for r in rules} == {"AB123CD"}
    assert {r.rate for r in rules} == {"1321271030"}
    assert not any(r.toutes_zones for r in rules)
    assert all(r.enabled for r in rules)


def test_chaque_secteur_a_ses_zones_de_repli_dans_le_bon_ordre():
    """« Si 75008 échoue → 75007, puis 75006… ; si 75016 échoue → 75017… »

    Les deux secteurs sont disjoints : un repli ne doit jamais empiéter sur
    l'autre, sinon les deux règles se disputeraient la même zone.
    """
    huit, seize = ma_config().rules

    assert huit.zones[:4] == ["75008", "75007", "75006", "75005"]
    assert seize.zones[:4] == ["75016", "75017", "75018", "75019"]

    assert {int(z) for z in huit.zones} <= set(range(75001, 75012))
    assert {int(z) for z in seize.zones} <= set(range(75012, 75021))
    assert not set(huit.zones) & set(seize.zones)

    # Le secteur entier doit être disponible : c'est ce qui rend le trou de
    # couverture improbable — il faudrait que toutes les zones refusent.
    assert len(huit.zones) == 11 and len(seize.zones) == 9


def test_ticket_de_24h_et_gratuit_seulement():
    for rule in ma_config().rules:
        assert rule.duration_minutes == 24 * 60
        assert rule.max_cost_per_ticket == 0  # ne dépensera jamais un centime


def test_rendez_vous_a_20h01():
    for rule in ma_config().rules:
        assert rule.renew_at == "20:01"


def test_les_regles_veillent_24h_sur_24():
    """Pas de créneau restreint : un trou de couverture doit pouvoir être
    rattrapé à n'importe quelle heure, y compris la nuit et le dimanche."""
    for rule in ma_config().rules:
        assert rule.window.days == frozenset(range(7))
        for jour in range(7):
            for heure in (0, 3, 11, 17, 20, 23):
                moment = datetime(2025, 1, 6 + jour, heure, 30, tzinfo=PARIS)
                assert rule.window.contains(moment), f"{moment} non couvert"


def test_la_marge_reste_sous_lintervalle_des_passages():
    """Sinon le filet de sécurité se déclencherait avant le rendez-vous de 20h01
    et ferait dériver l'horaire d'un jour sur l'autre."""
    cfg = ma_config()
    for rule in cfg.rules:
        assert cfg.margin_for(rule) < 30


# ----------------------------------------------------------------------- cron


def _slots_utc() -> list[tuple[int, int]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]
    out = set()
    for entry in triggers["schedule"]:
        minutes, hours = entry["cron"].split()[:2]
        heures = range(24) if hours == "*" else _expand(hours)
        for h in heures:
            for m in _expand(minutes):
                out.add((h, m))
    return sorted(out)


def _expand(field: str) -> list[int]:
    values = []
    for part in field.split(","):
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            values += list(range(a, b + 1))
        else:
            values.append(int(part))
    return values


def _heures_paris(mois: int) -> set[str]:
    return {
        datetime(2025, mois, 15, h, m, tzinfo=UTC).astimezone(PARIS).strftime("%H:%M")
        for h, m in _slots_utc()
    }


def test_lalerte_souvre_et_se_referme_toute_seule():
    """Pas de notification : une issue qui persiste tant que la panne dure."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    job = workflow["jobs"]["tickets"]
    assert (job.get("permissions") or workflow.get("permissions"))["issues"] == "write"

    etapes = {e.get("name"): e for e in job["steps"] if e.get("name")}
    assert etapes["Ouvrir l'alerte"]["if"] == "failure()"
    assert etapes["Refermer l'alerte"]["if"] == "success()"
    # l'alerte doit venir après le diagnostic, pour que le log le contienne
    ordre = [e.get("name") for e in job["steps"]]
    assert ordre.index("Sonde de diagnostic") < ordre.index("Ouvrir l'alerte")


def test_letat_est_consultable_depuis_le_telephone():
    """Le résumé doit être publié à chaque passage, réussi ou non — sinon on ne
    peut plus consulter l'état justement quand ça va mal."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    etapes = {e.get("name"): e for e in workflow["jobs"]["tickets"]["steps"] if e.get("name")}
    resume = etapes["Résumé de l'état"]
    assert resume["if"] == "always()"
    assert "summary" in resume["run"] and "GITHUB_STEP_SUMMARY" in resume["run"]


def test_lepreuve_de_lalarme_ne_peut_pas_se_declencher_toute_seule():
    """L'échec volontaire ne doit se produire que sur demande explicite,
    et jamais avant la prise de tickets."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    job = workflow["jobs"]["tickets"]
    etapes = {e.get("name"): e for e in job["steps"] if e.get("name")}
    epreuve = etapes["Épreuve de l'alarme"]
    assert epreuve["if"] == "contains(github.event.head_commit.message, '[test-alerte]')"

    ordre = [e.get("name") for e in job["steps"]]
    assert ordre.index("Vérifier la couverture") < ordre.index("Épreuve de l'alarme")


def test_le_cron_passe_a_20h01_ete_comme_hiver():
    for mois, saison in ((7, "été"), (1, "hiver")):
        assert "20:01" in _heures_paris(mois), saison


def test_un_passage_au_moins_toutes_les_30_minutes():
    """C'est ce qui borne la durée d'un éventuel trou de couverture."""
    slots = _slots_utc()
    moments = sorted(datetime(2025, 1, 1, h, m, tzinfo=UTC) for h, m in slots)
    ecarts = [
        (b - a).total_seconds() / 60
        for a, b in zip(moments, moments[1:] + [moments[0] + timedelta(days=1)])
    ]
    assert max(ecarts) <= 30, f"trou de {max(ecarts):.0f} min entre deux passages"


def test_renfort_autour_du_rendez_vous():
    """Le moment qui compte mérite plus qu'un seul passage."""
    for mois in (1, 7):
        proches = [h for h in _heures_paris(mois) if "20:00" <= h <= "20:59"]
        assert len(proches) >= 5, f"mois {mois} : {sorted(proches)}"
