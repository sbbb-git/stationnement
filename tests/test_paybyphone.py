"""Client PayByPhone testé contre le faux serveur (flux réel de bout en bout)."""

import pytest

from allovalet.errors import ApiError, AuthError, NotEligibleError
from allovalet.models import utcnow
from allovalet.paybyphone import Duration, PayByPhoneClient, best_duration
from tests.fake_pbp import PLATE


def test_connexion_et_compte(client, server):
    assert client.account_id()
    assert server.token_calls[0]["grant_type"] == "password"


def test_connexion_refusee_sans_identifiants(server):
    with pytest.raises(AuthError):
        PayByPhoneClient().authenticate()


def test_refresh_puis_repli_sur_mot_de_passe(server):
    client = PayByPhoneClient(
        username="+33600000000", password="secret", refresh_token="périmé"
    )
    client.authenticate()  # le refresh échoue → on doit retomber sur le login
    grants = [c["grant_type"] for c in server.token_calls]
    assert grants == ["refresh_token", "password"]


def test_vehicules(client):
    plates = [v.plate for v in client.vehicles()]
    assert plates == [PLATE]


def test_tarifs_et_selection(client):
    options = client.rate_options("75016", PLATE)
    assert {o.type for o in options} == {"CMI", "VIS"}

    cmi = client.pick_rate_option("75016", PLATE, "CMI")
    assert cmi.id == "1085252721"
    assert cmi.max_stay_minutes == 24 * 60

    par_defaut = client.pick_rate_option("75016", PLATE, None)
    assert par_defaut.type == "CMI"  # sans `rate:`, le premier tarif de la zone

    par_nom = client.pick_rate_option("75016", PLATE, "mobilité")
    assert par_nom.type == "CMI"


def test_tarif_inexistant(client):
    with pytest.raises(NotEligibleError) as exc:
        client.pick_rate_option("75016", PLATE, "RESIDENT")
    assert "Tarifs proposés" in str(exc.value)


def test_zone_inconnue(client):
    with pytest.raises(NotEligibleError):
        client.pick_rate_option("99999", PLATE, None)


def test_devis(client):
    gratuit = client.quote("75016", PLATE, Duration(24, "Hours"), rate_option_id="1085252721")
    assert gratuit.cost == 0.0
    assert gratuit.minutes == 24 * 60

    payant = client.quote("75016", PLATE, Duration(2, "Hours"), rate_option_id="75016")
    assert payant.cost == 12.0


def test_achat_cree_un_ticket_verifie(client, server):
    session = client.start_session(
        location_id="75016", plate=PLATE, duration=Duration(24, "Hours"),
        rate_option_id="1085252721",
    )
    assert session.id
    assert session.plate == PLATE
    assert session.expiry > utcnow()
    assert len(server.active()) == 1


def test_achat_sans_ticket_leve_une_erreur(client, server):
    """Le piège de l'ancien script : 202 renvoyé mais aucun ticket créé."""
    server.swallow_purchases = True
    with pytest.raises(ApiError) as exc:
        client.start_session(
            location_id="75016", plate=PLATE, duration=Duration(1, "Hours"),
            rate_option_id="75016",
        )
    assert "non confirmé" in str(exc.value)
    assert server.active() == []


def test_ticket_preexistant_nest_pas_confondu(client, server):
    server.add_session(minutes=30)
    server.swallow_purchases = True
    with pytest.raises(ApiError):
        client.start_session(
            location_id="75016", plate=PLATE, duration=Duration(1, "Hours"),
            rate_option_id="75016",
        )


def test_recherche_du_ticket_actif(client, server):
    server.add_session(minutes=10, location="75016")
    server.add_session(minutes=120, location="75016")
    active = client.find_active(PLATE, "75016")
    assert int(active.remaining.total_seconds() // 60) >= 118  # le plus long
    assert client.find_active(PLATE, "75008") is None
    assert client.find_active("XX000XX", "75016") is None


def test_prolongation(client, server):
    session = server.add_session(minutes=30)
    client.extend_session(session["parkingSessionId"], Duration(1, "Hours"))
    active = client.find_active(PLATE, "75016")
    assert int(active.remaining.total_seconds() // 60) >= 88


def test_choix_de_lunite_de_duree():
    assert str(best_duration(1440, ["Hours", "Days"])) == "1 Days"
    assert str(best_duration(1440, ["Hours"])) == "24 Hours"
    assert str(best_duration(90, ["Minutes", "Hours"])) == "90 Minutes"
    assert str(best_duration(120, ["Minutes", "Hours"])) == "2 Hours"
    assert str(best_duration(45, ["Hours"])) == "1 Hours"  # arrondi au-dessus


# ------------------------------------- le filtre anti-robot de PayByPhone

def test_un_403_du_pare_feu_est_reessaye(client, server, monkeypatch):
    """PayByPhone renvoie parfois une page HTML « 403 ERROR » à une IP de
    datacenter. Ce n'est pas un refus d'identifiants : ça retombe tout seul,
    et abandonner à la première laisse la voiture sans ticket toute la nuit."""
    monkeypatch.setattr(PayByPhoneClient, "AUTH_PAUSES", (0, 0, 0))
    server.filtre_cloudfront = 2  # deux rejets, puis ça passe

    client.authenticate()

    assert client.member_id
    assert len(server.token_calls) == 3


def test_un_403_persistant_ne_se_fait_pas_passer_pour_un_mot_de_passe_faux(
    client, server, monkeypatch
):
    """Le message doit désigner le vrai coupable, sinon on passe la soirée à
    vérifier des identifiants qui étaient bons."""
    monkeypatch.setattr(PayByPhoneClient, "AUTH_PAUSES", (0, 0, 0))
    server.filtre_cloudfront = 99

    with pytest.raises(AuthError) as echec:
        client.authenticate()

    assert "403" in str(echec.value)
    assert "pas un problème d'identifiants" in str(echec.value)


def test_un_vrai_mauvais_mot_de_passe_nest_pas_reessaye(server, monkeypatch):
    """Réessayer trois fois un mot de passe faux ne sert qu'à se faire bloquer."""
    monkeypatch.setattr(PayByPhoneClient, "AUTH_PAUSES", (0, 0, 0))
    muet = PayByPhoneClient(username="+33600000000", password="mauvais")

    with pytest.raises(AuthError):
        muet.authenticate()

    assert len(server.token_calls) == 1
