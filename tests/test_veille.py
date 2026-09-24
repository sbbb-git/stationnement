"""La veille, exécutée pour de vrai.

Ses deux scripts sont extraits du fichier YAML livré et exécutés dans Node,
contre un faux GitHub et une horloge réglable. Jusqu'ici, la veille n'était
vérifiée que par des recherches de texte dans son script — et elle a crié sept
fois pour rien entre le 27/08 et le 23/09 sans qu'aucun test ne le voie.

Les scénarios reprennent des situations réellement observées : ce sont elles
qui doivent rester vraies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from tests.test_ma_config import ROOT, _SansDoublon

PARIS = ZoneInfo("Europe/Paris")
HARNAIS = Path(__file__).with_name("veille_harness.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node absent")


def _veille() -> dict:
    return yaml.load((ROOT / ".github/workflows/veille.yml").read_text(), _SansDoublon)


def _script(job: str) -> str:
    return _veille()["jobs"][job]["steps"][0]["with"]["script"]


def paris(texte: str) -> datetime:
    """« 2026-09-24 22:30 » heure de Paris."""
    return datetime.strptime(texte, "%Y-%m-%d %H:%M").replace(tzinfo=PARIS)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def lancer(job: str, tmp_path, maintenant: datetime, **scenario) -> list[dict]:
    """Exécute le script du job et renvoie les actions qu'il a tentées."""
    # Par défaut, un dépôt vivant : dernier commit l'avant-veille.
    scenario.setdefault("dernier_commit", maintenant - timedelta(days=2))
    for cle in ("derniere_reussite", "dernier_commit"):
        if isinstance(scenario.get(cle), datetime):
            scenario[cle] = _iso(scenario[cle])
    scenario["maintenant"] = int(maintenant.timestamp() * 1000)

    script = tmp_path / f"{job}.js"
    script.write_text(_script(job), encoding="utf-8")
    fichier = tmp_path / "scenario.json"
    fichier.write_text(json.dumps(scenario), encoding="utf-8")

    sortie = subprocess.run(
        ["node", str(HARNAIS), str(script), str(fichier)],
        capture_output=True, text=True, timeout=30,
    )
    assert sortie.returncode == 0, sortie.stderr
    return json.loads(sortie.stdout or "[]")


def quoi(actions: list[dict]) -> list[str]:
    return [a["quoi"] for a in actions]


# ------------------------------------------------------------- le relais du soir


def test_pas_de_fausse_alerte_quand_github_espace_ses_passages(tmp_path):
    """La situation du 23/09 à 12h04, où l'ancienne veille a crié pour rien :
    dernier passage à 02h01, plus de 8 h avant — mais le relais de la veille au
    soir avait bien eu lieu, et la voiture était couverte."""
    actions = lancer("silence", tmp_path, paris("2026-09-23 12:04"),
                     derniere_reussite=paris("2026-09-23 02:01"))
    assert actions == []


def test_le_relais_manque_est_signale_dans_la_nuit(tmp_path):
    """Dernier passage à 15h22, rien après 20h00 : à 2h30 du matin, la
    voiture n'est pas couverte pour la journée — il faut le dire."""
    actions = lancer("silence", tmp_path, paris("2026-09-25 02:30"),
                     derniere_reussite=paris("2026-09-24 15:22"))

    assert quoi(actions) == ["ouvre", "échoue"]
    assert "relais" in actions[0]["title"]
    assert actions[0]["labels"] == ["veille"]


def test_on_laisse_a_github_le_temps_d_honorer_un_creneau(tmp_path):
    """À 22h30, pas encore de passage depuis 20h00 : c'est courant — GitHub a
    déjà mis jusqu'à 2h56 à en honorer un. Pas d'alerte avant minuit."""
    actions = lancer("silence", tmp_path, paris("2026-09-24 22:30"),
                     derniere_reussite=paris("2026-09-24 15:22"))
    assert actions == []


def test_un_passage_lance_avant_20h_qui_a_attendu_compte(tmp_path):
    """Un passage lancé à 19h58 attend 20h05 pour agir : il finit après 20h00.
    C'est la fin du passage qui compte, pas son lancement."""
    actions = lancer("silence", tmp_path, paris("2026-09-25 03:00"),
                     derniere_reussite=paris("2026-09-24 20:06"))
    assert actions == []


def test_le_relais_est_compte_en_heure_d_hiver_aussi(tmp_path):
    """Le 20h00 de Paris tombe à 19h00 UTC l'hiver, 18h00 l'été."""
    ok = lancer("silence", tmp_path, paris("2026-11-11 03:00"),
                derniere_reussite=paris("2026-11-10 20:10"))
    manque = lancer("silence", tmp_path, paris("2026-11-11 03:00"),
                    derniere_reussite=paris("2026-11-10 19:50"))
    assert ok == []
    assert quoi(manque) == ["ouvre", "échoue"]


def test_la_panne_totale_est_signalee_a_toute_heure(tmp_path):
    actions = lancer("silence", tmp_path, paris("2026-09-24 21:00"),
                     derniere_reussite=paris("2026-09-23 17:00"))
    assert quoi(actions) == ["ouvre", "échoue"]
    assert "plus aucun passage" in actions[0]["title"]


def test_aucun_passage_reussi_du_tout(tmp_path):
    actions = lancer("silence", tmp_path, paris("2026-09-24 12:00"))
    assert quoi(actions) == ["ouvre", "échoue"]


# ------------------------------------------------------- une alerte qui insiste


ALERTE = {"number": 7, "labels": ["veille"], "created_at": "2026-09-22T00:00:00Z"}


def test_une_alerte_ouverte_se_rappelle_chaque_jour(tmp_path):
    actions = lancer("silence", tmp_path, paris("2026-09-25 02:30"),
                     derniere_reussite=paris("2026-09-21 15:00"),
                     ouvertes=[ALERTE],
                     commentaires=[{"created_at": "2026-09-23T23:00:00Z"}])
    assert quoi(actions) == ["commente", "échoue"]
    assert "Toujours en panne" in actions[0]["body"]


def test_mais_pas_plus_d_une_fois_par_jour(tmp_path):
    actions = lancer("silence", tmp_path, paris("2026-09-25 02:30"),
                     derniere_reussite=paris("2026-09-21 15:00"),
                     ouvertes=[ALERTE],
                     commentaires=[{"created_at": "2026-09-24T20:00:00Z"}])
    assert actions == []


def test_la_veille_referme_elle_meme_son_alerte(tmp_path):
    """Un passage réussi en pleine journée n'a rien acheté : il ne prouve pas
    que la voiture est couverte. C'est donc la veille, et non n'importe quel
    passage, qui décide que c'est rétabli — au premier relais réussi."""
    actions = lancer("silence", tmp_path, paris("2026-09-25 03:00"),
                     derniere_reussite=paris("2026-09-24 20:20"),
                     ouvertes=[ALERTE])
    assert quoi(actions) == ["commente", "modifie"]
    assert actions[1]["state"] == "closed"


def test_parking_ne_referme_pas_les_alertes_de_la_veille():
    """Sinon un passage de l'après-midi, qui ne prend aucun ticket, refermerait
    l'alerte d'une nuit sans relais — et la voiture resterait découverte en
    silence."""
    texte = (ROOT / ".github/workflows/parking.yml").read_text(encoding="utf-8")
    assert 'labels: "veille"' not in texte
    assert 'labels: "stationnement"' in texte


def test_l_epreuve_leve_l_alerte_sur_commande(tmp_path):
    actions = lancer("silence", tmp_path, paris("2026-09-24 12:00"),
                     derniere_reussite=paris("2026-09-24 11:00"),
                     message="test: éprouver la veille [test-veille]")
    assert quoi(actions) == ["ouvre", "échoue"]


# ------------------------------------------------------------- le signe de vie


def test_un_signe_de_vie_avant_que_github_n_eteigne_tout(tmp_path):
    """GitHub désactive les workflows programmés d'un dépôt public inactif
    depuis 60 jours. Sans ce job, tout se serait arrêté le 18/10/2026."""
    actions = lancer("signe-de-vie", tmp_path, paris("2026-09-24 12:00"),
                     dernier_commit=paris("2026-08-19 23:18"))
    assert quoi(actions) == ["commit"]
    assert actions[0]["path"] == ".github/signe-de-vie"
    assert actions[0]["branch"] == "main"


def test_pas_de_commit_inutile(tmp_path):
    actions = lancer("signe-de-vie", tmp_path, paris("2026-09-24 12:00"),
                     dernier_commit=paris("2026-09-10 12:00"))
    assert actions == []


def test_le_signe_de_vie_met_a_jour_le_fichier_existant(tmp_path):
    actions = lancer("signe-de-vie", tmp_path, paris("2026-09-24 12:00"),
                     dernier_commit=paris("2026-08-19 23:18"), fichier_sha="abc123")
    assert actions[0]["sha"] == "abc123"


def test_le_signe_de_vie_s_eprouve_sur_commande(tmp_path):
    """Il ne sert qu'une fois tous les 25 jours : sans épreuve, on ne saurait
    qu'il est cassé qu'au moment où GitHub aurait déjà tout éteint."""
    actions = lancer("signe-de-vie", tmp_path, paris("2026-09-24 12:00"),
                     dernier_commit=paris("2026-09-24 11:55"),
                     message="test: éprouver le signe de vie [test-signe-de-vie]")
    assert quoi(actions) == ["commit"]


def test_la_veille_voit_quand_le_signe_de_vie_a_echoue(tmp_path):
    """Si le signe de vie échoue, aucun passage n'échoue pour autant : le
    compte à rebours des 60 jours reprend en silence. La veille le surveille
    donc, avec un seuil qui laisse le temps de réagir."""
    actions = lancer("silence", tmp_path, paris("2026-09-24 12:00"),
                     derniere_reussite=paris("2026-09-24 11:00"),
                     dernier_commit=paris("2026-08-05 12:00"))
    assert quoi(actions) == ["ouvre", "échoue"]
    assert "éteindre" in actions[0]["title"]
    assert "60 jours" in actions[0]["body"]


def test_pas_d_alerte_d_extinction_tant_que_le_signe_de_vie_fonctionne(tmp_path):
    actions = lancer("silence", tmp_path, paris("2026-09-24 12:00"),
                     derniere_reussite=paris("2026-09-24 11:00"),
                     dernier_commit=paris("2026-09-01 12:00"))
    assert actions == []


def test_le_seuil_laisse_de_la_marge_avant_les_60_jours():
    """Assez tôt pour que des semaines de créneaux sautés ne suffisent pas à
    rater l'échéance ; assez tard pour ne pas encombrer l'historique."""
    script = _script("signe-de-vie")
    seuil = int(script.split("SEUIL_JOURS = ")[1].split(": ")[1].split(";")[0])
    assert 14 <= seuil <= 30
    # et l'alerte de la veille tombe entre le signe de vie raté et l'échéance
    alerte = int(_script("silence").split("joursSansCommit > ")[1].split(")")[0])
    assert seuil < alerte < 60


# ------------------------------------------------------ ce que la veille ne doit pas


def test_la_veille_ne_partage_rien_avec_ce_qu_elle_surveille():
    """Pas de Python, pas d'identifiants, pas de `checkout` : tout point commun
    avec `parking.yml` serait une panne commune, donc un angle mort."""
    texte = (ROOT / ".github/workflows/veille.yml").read_text(encoding="utf-8")
    for interdit in ("PBP_USERNAME", "PBP_PASSWORD", "PBP_PLATE",
                     "requirements.txt", "allovalet", "actions/checkout"):
        assert interdit not in texte, interdit


def test_chaque_job_n_a_que_les_droits_dont_il_a_besoin():
    veille = _veille()
    assert veille["permissions"] == {}
    assert veille["jobs"]["silence"]["permissions"] == {
        "actions": "read", "contents": "read", "issues": "write"}
    assert veille["jobs"]["signe-de-vie"]["permissions"] == {"contents": "write"}
