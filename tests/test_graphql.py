"""Le moteur GraphQL : enchaînement réel, renouvellement, et auto-diagnostic."""

import pytest

from allovalet.errors import ApiError
from allovalet.models import utcnow
from allovalet.paybyphone import Duration
from tests.fake_pbp import CMI_POLICY, PLATE


def test_enchainement_devis_puis_achat(client, server):
    """L'achat s'appuie sur le quoteId du devis — un devis seul n'achète rien."""
    client.start_session(
        location_id="75016", plate=PLATE, duration=Duration(24, "Hours"),
        rate_option_id=CMI_POLICY,
    )
    ordre = [op for op in server.operations if op in
             ("createQuotesV1", "startParkingSessionV1", "getOpenSessionsV1")]
    assert ordre.index("createQuotesV1") < ordre.index("startParkingSessionV1")
    assert ordre[-1] == "getOpenSessionsV1"  # vérification en dernier

    achat = server.purchases[0]
    assert achat["quoteId"] in server.quotes  # l'achat référence bien le devis


def test_le_devis_porte_la_bonne_operation(client, server):
    client.quote("75016", PLATE, Duration(1, "Hours"), rate_option_id=CMI_POLICY)
    quote = list(server.quotes.values())[-1]
    assert quote["operation"] == "Start"

    session = server.add_session(minutes=30)
    client.quote("75016", PLATE, Duration(1, "Hours"), rate_option_id=CMI_POLICY,
                 session_id=session["parkingSessionId"], operation="Renew")
    quote = list(server.quotes.values())[-1]
    assert quote["operation"] == "Renew"
    assert quote["sessionId"] == session["parkingSessionId"]


def test_session_renouvelable_est_renouvelee(client, server):
    """C'est le mécanisme d'AlloValet : l'API dit `isRenewable`, on renouvelle."""
    session = server.add_session(minutes=20, renewable=True)

    result = client.start_session(
        location_id="75016", plate=PLATE, duration=Duration(24, "Hours"),
        rate_option_id=CMI_POLICY,
    )
    assert result.id == session["parkingSessionId"]
    assert len(server.active()) == 1
    assert "renewParkingSessionV1" in server.operations
    assert "startParkingSessionV1" not in server.operations


def test_session_non_renouvelable_donne_un_nouveau_ticket(client, server):
    server.add_session(minutes=20, renewable=False, location="75008",
                       rate_policy_id="75008")
    client.start_session(
        location_id="75008", plate=PLATE, duration=Duration(1, "Hours"),
        rate_option_id="75008",
    )
    assert "startParkingSessionV1" in server.operations
    assert len(server.active()) == 2


def test_repli_sur_lautre_forme_dentree(client, server):
    """Si l'API attend `input: {request: {...}}`, le client s'adapte tout seul."""
    server.require_request_wrapper = True

    session = client.start_session(
        location_id="75016", plate=PLATE, duration=Duration(24, "Hours"),
        rate_option_id=CMI_POLICY,
    )
    assert session.id
    assert session.expiry > utcnow()
    assert len(server.active()) == 1


def test_introspection_donne_la_vraie_forme(client):
    fields = client.input_fields("StartParkingSessionV1Input")
    assert [name for name, _ in fields] == ["quoteId", "plate"]
    assert client.input_fields("TypeInexistantInput") == []


def test_erreur_graphql_expose_les_champs_attendus(client, server):
    """Une opération refusée doit dire ce que l'API accepte vraiment."""
    server.require_request_wrapper = True
    quote = client.quote("75016", PLATE, Duration(1, "Hours"), rate_option_id=CMI_POLICY)

    from allovalet.paybyphone import M_START

    with pytest.raises(ApiError) as exc:
        client.gql(M_START, {"input": {"quoteId": quote.quote_id}},
                   "startParkingSessionV1")
    message = str(exc.value)
    assert "was not provided" in message
    assert "StartParkingSessionV1Input" in message
    assert "quoteId" in message and "plate" in message


def test_zone_inconnue_remonte_le_refus_du_devis(client):
    from allovalet.errors import NotEligibleError

    with pytest.raises(NotEligibleError) as exc:
        client.quote("99999", PLATE, Duration(1, "Hours"), rate_option_id="x")
    assert "UnknownLocation" in str(exc.value)


def test_le_jeton_perime_est_renouvele_en_cours_de_route(client, server):
    client.vehicles()
    client._access_token = "Bearer-périmé.mais.présent"  # invalide côté serveur
    client._expires_at = utcnow().replace(year=2099)     # le client le croit valide

    assert [v.plate for v in client.vehicles()] == [PLATE]
    assert len(server.token_calls) >= 2  # il s'est reconnecté tout seul
