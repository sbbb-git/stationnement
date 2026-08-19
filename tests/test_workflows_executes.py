"""Les workflows sont exécutés, pas seulement relus.

C'est la leçon des trois pannes d'août 2026. Toutes venaient du même angle
mort : mes tests lisaient le YAML et affirmaient des choses dessus — l'ordre
des étapes, la présence d'un secret, la valeur d'un `if` — sans jamais lancer
une seule des commandes qu'il contient. Un workflow était donc du texte non
testé, et trois défauts invisibles ont coûté quatorze jours sans ticket :

- 06/08 — `Historique récent` sans `PBP_PLATE` : le passage échouait après
  avoir pourtant pris les tickets ;
- 07/08 — une clé YAML en double : GitHub refusait le fichier, silence total ;
- 07→19/08 — `Attendre le relais` sans `PBP_PLATE` : l'étape échouait **avant**
  la prise de tickets et la bloquait. Douze jours.

Le test ci-dessous rejoue chaque commande d'un workflow **avec pour seul
environnement celui que l'étape déclare**, contre le faux PayByPhone. C'est
exactement ce qui manquait : un `env:` oublié fait désormais échouer les tests
au lieu de la voiture.
"""

from __future__ import annotations

import os
import re
import shlex

import pytest
import yaml

from allovalet import cli
from tests.fake_pbp import PLATE
from tests.test_ma_config import ROOT, _SansDoublon

# Ce que le vrai coffre-fort de GitHub fournirait.
SECRETS = {
    "PBP_USERNAME": "+33600000000",
    "PBP_PASSWORD": "secret",
    "PBP_PLATE": PLATE,
    "NTFY_TOPIC": "",
}

# Les entrées d'un `workflow_dispatch`, telles qu'on les saisirait.
ENTREES = {"zone": "75016", "plaque": PLATE, "dry_run": "false"}

# Les diagnostics ont le droit de conclure « il y a des problèmes » : sur le
# faux serveur, la plupart des zones n'existent pas. Ce qu'on leur demande,
# c'est de tourner — pas d'être contents.
TOLERENT_UN_ECHEC = {"doctor", "probe", "rates"}


def _resoudre(texte: str) -> str:
    """Remplace les expressions GitHub par ce qu'elles vaudraient à l'exécution."""
    texte = re.sub(r"\$\{\{\s*secrets\.(\w+)\s*\}\}", lambda m: SECRETS.get(m.group(1), ""), texte)
    texte = re.sub(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", lambda m: ENTREES.get(m.group(1), ""), texte)
    # `${{ inputs.dry_run && '--dry-run' || '' }}` — le cas par défaut : rien.
    texte = re.sub(r"\$\{\{[^}]*\}\}", "", texte)
    return texte


def _commandes(etape: dict, sortie: str) -> list[list[str]]:
    """Les appels `allovalet` d'une étape, prêts à être passés à `main()`."""
    brut = _resoudre(etape.get("run") or "")
    appels = []
    for ligne in brut.replace("\\\n", " ").splitlines():
        if "-m allovalet" not in ligne:
            continue
        argv = ligne.split("-m allovalet", 1)[1]
        for coupure in ("|", ">>", "2>&1"):        # on s'arrête au tuyau shell
            argv = argv.split(coupure)[0]
        argv = argv.replace('"$GITHUB_STEP_SUMMARY"', sortie).replace("etat.md", sortie)
        argv = argv.replace('"$ZONE"', ENTREES["zone"])
        appels.append([m for m in shlex.split(argv) if m])
    return appels


def _etapes_executables():
    """Chaque (fichier, étape, commande) réellement lancée par un workflow."""
    for fichier in sorted(ROOT.glob(".github/workflows/*.yml")):
        workflow = yaml.load(fichier.read_text(encoding="utf-8"), _SansDoublon)
        for job in workflow["jobs"].values():
            for etape in job["steps"]:
                if "-m allovalet" in (etape.get("run") or ""):
                    yield fichier.name, etape


CAS = [
    pytest.param(nom, etape, id=f"{nom}::{etape.get('name') or 'sans nom'}")
    for nom, etape in _etapes_executables()
]


@pytest.fixture
def sans_secrets(monkeypatch, tmp_path):
    """Un environnement nu : seuls les secrets déclarés par l'étape existeront.

    C'est le cœur du test. Le coureur de GitHub ne fournit rien d'autre que le
    bloc `env:` de l'étape ; les tests, eux, héritaient de tout et ne voyaient
    donc jamais un oubli.
    """
    for variable in list(os.environ):
        if variable.startswith(("PBP_", "NTFY_", "ALLOVALET_")):
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ALLOVALET_STATE", str(tmp_path / "state.json"))
    # `load_dotenv()` lit le .env du dossier courant : il ne doit pas
    # ressusciter un secret que l'étape ne déclare pas.
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)


@pytest.mark.parametrize("fichier,etape", CAS)
def test_chaque_etape_tourne_avec_le_seul_environnement_quelle_declare(
    fichier, etape, sans_secrets, server, tmp_path, monkeypatch, capsys
):
    for variable, valeur in (etape.get("env") or {}).items():
        monkeypatch.setenv(variable, _resoudre(str(valeur)))

    sortie = str(tmp_path / "resume.md")
    for argv in _commandes(etape, sortie):
        code = cli.main(["--config", str(ROOT / "config.yml"), *argv])
        texte = capsys.readouterr().out

        assert "pas de plaque" not in texte, (
            f"{fichier} · « {etape.get('name')} » : `allovalet {' '.join(argv)}` "
            "ne peut pas lire la config — il manque un secret dans son bloc `env:`"
        )
        if argv and argv[0] not in TOLERENT_UN_ECHEC:
            assert code == 0, (
                f"{fichier} · « {etape.get('name')} » : "
                f"`allovalet {' '.join(argv)}` a échoué (code {code})\n{texte[-1500:]}"
            )


def test_les_workflows_appellent_des_commandes_qui_existent():
    """Une commande mal orthographiée ne se verrait qu'en production."""
    connues = set(cli.build_parser()._subparsers._group_actions[0].choices)

    appelees = set()
    for _, etape in _etapes_executables():
        for argv in _commandes(etape, "/dev/null"):
            if argv:
                appelees.add(argv[0])

    assert appelees, "aucune commande trouvée — l'extraction est cassée"
    assert appelees <= connues, f"commandes inconnues : {sorted(appelees - connues)}"
