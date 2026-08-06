"""Verrouille ce qui est réellement demandé : 75016 et 75008, toujours couverts,
rendez-vous à 20h01.

Ces tests portent sur les fichiers livrés (config.yml et le workflow), pas sur
des exemples : si quelqu'un touche à une zone, à l'horaire ou au cron, ça casse
ici.
"""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from allovalet.config import Config, ConfigError

ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")


PLAQUE = "AB123CD"


@pytest.fixture(autouse=True)
def _plaque(monkeypatch):
    """La plaque vit dans un secret : sans lui, la config ne se lit pas."""
    monkeypatch.setenv("PBP_PLATE", PLAQUE)


def ma_config() -> Config:
    return Config.load(ROOT / "config.yml")


def test_la_plaque_nest_pas_ecrite_dans_le_depot(monkeypatch):
    """Elle vient du secret PBP_PLATE. Un dépôt qui devient public — ça arrive —
    ne doit pas révéler quelle voiture se gare où."""
    texte = (ROOT / "config.yml").read_text(encoding="utf-8")
    assert PLAQUE not in texte
    assert "${PBP_PLATE}" in texte
    assert {r.plate for r in ma_config().rules} == {PLAQUE}

    # Et sans le secret, l'erreur doit désigner le secret, pas un champ absent.
    monkeypatch.delenv("PBP_PLATE")
    with pytest.raises(ConfigError) as echec:
        ma_config()
    assert "PBP_PLATE" in str(echec.value)


def test_une_regle_par_secteur():
    """Deux secteurs, chacun visant sa zone : le 75008 et le 75016."""
    rules = ma_config().rules
    assert [r.location for r in rules] == ["75008", "75016"]
    assert {r.plate for r in rules} == {PLAQUE}
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


def test_rien_avant_20h():
    """Demandé explicitement : aucun ticket pris dans la journée. Le seul
    moment où un ticket finit est 20h00, donc c'est là que tout se joue."""
    for rule in ma_config().rules:
        for jour in range(7):
            for heure in (9, 11, 14, 17, 19):
                moment = datetime(2025, 1, 6 + jour, heure, 30, tzinfo=PARIS)
                assert not rule.window.contains(moment), f"{moment} : agirait"


def test_la_veille_couvre_le_soir_et_la_nuit_tous_les_jours():
    """Après 20h00 et jusqu'au matin, un trou reste rattrapable — dimanches
    et jours fériés compris."""
    for rule in ma_config().rules:
        assert rule.window.days == frozenset(range(7))
        for jour in range(7):
            for heure in (20, 21, 23, 2, 6, 8):
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
        for h in _expand(hours, 23):
            for m in _expand(minutes, 59):
                out.add((h, m))
    return sorted(out)


def _expand(field: str, maximum: int) -> list[int]:
    """Développe un champ cron : `5`, `1,31`, `18-20`, `*`, `*/5`."""
    values = []
    for part in field.split(","):
        pas = 1
        if "/" in part:
            part, saut = part.split("/")
            pas = int(saut)
        if part == "*":
            debut, fin = 0, maximum
        elif "-" in part:
            debut, fin = (int(x) for x in part.split("-"))
        else:
            values.append(int(part))
            continue
        values += list(range(debut, fin + 1, pas))
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

    # Le tableau de bord doit être réécrit même quand le passage échoue :
    # c'est justement là qu'on a besoin de le consulter.
    tableau = etapes["Tableau de bord"]
    assert tableau["if"] == "always()"
    ordre = [e.get("name") for e in workflow["jobs"]["tickets"]["steps"]]
    assert ordre.index("Résumé de l'état") < ordre.index("Tableau de bord")
    # Réécriture du corps, jamais de commentaire : une édition ne notifie pas.
    assert "issues.update" in tableau["with"]["script"]
    assert "createComment" not in tableau["with"]["script"]


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


def test_un_passage_au_moins_toutes_les_deux_heures():
    """C'est ce qui borne la durée d'un trou de couverture imprévu."""
    slots = _slots_utc()
    moments = sorted(datetime(2025, 1, 1, h, m, tzinfo=UTC) for h, m in slots)
    ecarts = [
        (b - a).total_seconds() / 60
        for a, b in zip(moments, moments[1:] + [moments[0] + timedelta(days=1)])
    ]
    assert max(ecarts) <= 120, f"trou de {max(ecarts):.0f} min entre deux passages"


def test_des_passages_en_avance_pouvant_attendre_le_relais():
    """Le mécanisme repose là-dessus : il suffit qu'**un** passage arrive dans
    la demi-heure qui précède 20h05 pour que le relais tombe pile à l'heure.

    Ils doivent tomber dans la fenêtre d'attente réellement configurée, sinon
    ils agiraient trop tôt (ticket encore valide) ou trop tard."""
    plafond = _plafond_dattente()
    for mois, saison in ((7, "été"), (1, "hiver")):
        debut = f"{20 - (plafond + 55) // 60:02d}:{(65 - plafond) % 60:02d}"
        avance = [h for h in _heures_paris(mois) if debut <= h <= "20:05"]
        assert len(avance) >= 4, f"{saison} : {sorted(avance)} (depuis {debut})"


def test_le_relais_est_dense_apres_20h():
    """Et si aucun passage en avance n'est honoré, il faut rattraper vite."""
    for mois, saison in ((7, "été"), (1, "hiver")):
        proches = [h for h in _heures_paris(mois) if "20:00" <= h <= "20:59"]
        assert len(proches) >= 6, f"{saison} : {sorted(proches)}"


def test_pas_trop_de_passages_rapproches():
    """Deux échecs ont la même cause : trop de déclenchements. Ils se sont
    annulés les uns les autres dans la file d'attente le 06/08, et PayByPhone
    a fini par filtrer les connexions trop rapprochées (403 CloudFront)."""
    slots = _slots_utc()
    assert len(slots) <= 40, f"{len(slots)} passages demandés par jour"


def test_un_passage_qui_attend_cede_la_place_au_suivant():
    """Sinon les passages du soir s'empilent, et GitHub annule ceux qui
    patientent avant même de leur donner une machine — la panne du 06/08."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    assert workflow["concurrency"]["cancel-in-progress"] is True


def _plafond_dattente() -> int:
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    for etape in workflow["jobs"]["tickets"]["steps"]:
        if "--max-minutes" in (etape.get("run") or ""):
            return int(etape["run"].split("--max-minutes")[1].split()[0])
    raise AssertionError("aucune étape d'attente")


def test_letape_dattente_vise_bien_le_relais():
    """L'heure exacte est tenue par l'attente, pas par le planificateur."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    job = workflow["jobs"]["tickets"]
    etapes = {e.get("name"): e for e in job["steps"] if e.get("name")}
    attente = next(e for nom, e in etapes.items() if nom.startswith("Attendre"))

    assert "--at 20:05" in attente["run"]
    # Une modification poussée ne doit pas rester bloquée une demi-heure.
    assert attente["if"] == "github.event_name == 'schedule'"

    ordre = [e.get("name") for e in job["steps"]]
    assert ordre.index(attente["name"]) < ordre.index("Vérifier la couverture")

    # Le passage doit pouvoir attendre sans être tué par le délai maximum.
    plafond = int(attente["run"].split("--max-minutes")[1].split()[0])
    assert job["timeout-minutes"] > plafond + 5

    # Le rendez-vous quotidien doit déjà être passé quand l'attente se termine,
    # sinon on se réveillerait pile trop tôt pour qu'il se déclenche.
    for rule in ma_config().rules:
        assert rule.renew_at <= "20:05"


# ------------------------------------------------- installation par un tiers


def test_la_decouverte_nachete_jamais_rien():
    """Ce workflow sert à configurer le sien : il tourne avec les identifiants
    de quelqu'un qui n'a encore rien vérifié. Il doit être en lecture seule."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/decouverte.yml").read_text())
    declencheurs = workflow[True] if True in workflow else workflow["on"]

    # À la demande uniquement : ni horaire, ni déclenchement par un commit.
    assert list(declencheurs) == ["workflow_dispatch"]

    commandes = " ".join(
        etape.get("run") or "" for etape in workflow["jobs"]["tarifs"]["steps"]
    )
    for achat in ("allovalet run", "allovalet park", "allovalet sweep"):
        assert achat not in commandes, achat
    assert "allovalet rates" in commandes and "allovalet doctor" in commandes


def test_le_mode_demploi_reste_juste():
    """Un mode d'emploi qui ment coûte plus cher que pas de mode d'emploi."""
    texte = (ROOT / "INSTALLATION.md").read_text(encoding="utf-8")
    assert "PBP_USERNAME" in texte and "PBP_PASSWORD" in texte
    assert "PBP_PLATE" in texte                   # la plaque aussi est un secret
    assert "1321271030" in texte                  # le tarif Handi, déjà rempli
    assert "mobilité inclusion" in texte.lower()  # le vrai prérequis
    assert "max_cost_per_ticket: 0" in texte      # le garde-fou doit être expliqué
    assert "Découverte" in texte                  # le nom réel du workflow
    for champ in ("zones:", "rate:", "renew_at:", "window:"):
        assert champ in texte, champ


def test_le_ticket_a_la_demande_ne_peut_pas_couter():
    """Un marqueur dans un message de commit déclenche un achat réel. Deux
    choses doivent donc être vraies : il ne peut être ni payant, ni détourné."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    etapes = {e.get("name"): e for e in workflow["jobs"]["tickets"]["steps"] if e.get("name")}
    etape = etapes["Ticket à la demande"]

    assert "github.event_name == 'push'" in etape["if"]
    assert "contains(github.event.head_commit.message, '[ticket " in etape["if"]

    # Le message de commit est du texte libre : il passe par l'environnement,
    # jamais interpolé dans la commande.
    assert "${{ github.event.head_commit.message }}" not in etape["run"]
    assert etape["env"]["MESSAGE"] == "${{ github.event.head_commit.message }}"

    # `park` sans `--max-cost` plafonne à 0 € : aucun achat payant possible.
    assert "--max-cost" not in etape["run"]
