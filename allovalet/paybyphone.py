"""Client PayByPhone — le moteur réel, celui de l'application.

Comment ces appels ont été établis
----------------------------------
L'application PayByPhone (m.paybyphone.com) est une app Flutter : son bundle
`main.dart.js` contient en clair les noms d'opérations GraphQL, les types
d'entrée et les champs de réponse. Tout ce qui suit en vient, plus une sonde
des endpoints :

    POST auth.paybyphoneapis.com/token          → 400 invalid_grant  (vivant)
    POST consumer.paybyphoneapis.com/uapi/graphql → 401              (vivant)
    GET  consumer.paybyphoneapis.com/parking/accounts → 404 page not found

La troisième ligne est la raison pour laquelle la version précédente ne pouvait
pas marcher : **l'API REST n'existe plus**. Tout passe par GraphQL.

Le vrai enchaînement d'un ticket
--------------------------------
    createQuotesV1        → un devis, et surtout un quoteId
    startParkingSessionV1 → l'achat, à partir de ce quoteId
    getParkingSessionsV1  → vérification que le ticket existe

Attention au faux ami : `getOpenSessionsV1` renvoie un `AutopaySessionResponse`,
c'est-à-dire les parkings en ouvrage, pas les tickets de voirie.

Un devis seul n'achète rien : c'est là que s'arrêtait l'ancien script.

Renouvellement
--------------
Chaque session dit elle-même `isRenewable` et `renewableAfter`. Quand une
session en cours est renouvelable, on passe par `parkingQuoteOperation: Renew`
puis `renewParkingSessionV1` plutôt que de créer une seconde session : c'est le
mécanisme prévu par l'API, et celui qu'utilisent les services du genre
AlloValet.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import ApiError, AuthError, NotEligibleError
from .http import HttpClient
from .models import ParkingSession, Quote, RateOption, Vehicle, parse_dt, utcnow

log = logging.getLogger("allovalet.paybyphone")

AUTH_URL = "https://auth.paybyphoneapis.com/token"
API_BASE = "https://consumer.paybyphoneapis.com"
GRAPHQL_PATH = "/uapi/graphql"
CLIENT_ID = "paybyphone_web"
WEB_ORIGIN = "https://m.paybyphone.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TOKEN_SKEW = timedelta(seconds=90)

# --------------------------------------------------------------------- GraphQL

# Sous-ensemble de la sélection réellement envoyée par l'application.
SESSION_FIELDS = """
  parkingSessionId
  status
  type
  locationId
  startTime
  expireTime
  stall
  isStoppable
  isExtendable
  isRenewable
  renewableAfter
  vehicle { licensePlate countryCode }
  ratePolicy { ratePolicyId type }
  totalCost { amount currency }
  location { advertisedLocationId name isStallBased }
"""

Q_VEHICLES = """
query GetVehiclesV3($input: GetVehiclesInput!) {
  getVehiclesV3(input: $input) { id licensePlate countryCode type jurisdiction }
}
"""

Q_SESSIONS = """
query GetParkingSessionsV1($input: GetParkingSessionsInput!) {
  getParkingSessionsV1(input: $input) { %s }
}
""" % SESSION_FIELDS

Q_RATE_OPTIONS = """
query GetRateOptionsV1($input: GetRateOptionsInput!) {
  getRateOptionsV1(input: $input) {
    name
    type
    ratePolicyId
    maxStayStatus
    acceptedTimeUnits
    effectiveMaxStayDuration { quantity timeUnit }
  }
}
"""

Q_PAYMENT_ACCOUNTS = """
query GetPaymentAccountsV1($input: GetPaymentAccountsInput!) {
  getPaymentAccountsV1(input: $input) { paymentAccountId }
}
"""

M_CREATE_QUOTES = """
mutation CreateQuotesV1($requests: [QuoteRequestInput!]!) {
  createQuotesV1(input: { requests: $requests }) {
    createQuotesResponse {
      quotes {
        quoteId
        details {
          locationId
          parkingStartTime
          parkingExpiryTime
          totalCost { amount currency }
        }
      }
      quoteErrors { quoteRequestId status reason }
    }
  }
}
"""

M_START = """
mutation StartParkingSessionV1($input: StartParkingSessionV1Input!) {
  startParkingSessionV1(input: $input) {
    parkingSessionResponse {
      parkingSessionId
      expireTime
      segmentTotalCost { amount currency }
    }
  }
}
"""

M_RENEW = """
mutation RenewParkingSessionV1($input: RenewParkingSessionV1Input!) {
  renewParkingSessionV1(input: $input) {
    parkingSessionResponse {
      parkingSessionId
      expireTime
      segmentTotalCost { amount currency }
    }
  }
}
"""

M_EXTEND = """
mutation ExtendParkingSessionV1($input: ExtendParkingSessionV1Input!) {
  extendParkingSessionV1(input: $input) {
    parkingSessionResponse { parkingSessionId expireTime }
  }
}
"""

Q_INTROSPECT_INPUT = """
query Introspect($name: String!) {
  __type(name: $name) {
    name
    inputFields { name type { name kind ofType { name kind } } }
  }
}
"""

# Décrit un type dans les deux sens : ce qu'il accepte et ce qu'il renvoie.
# C'est ce qui aurait signalé tout de suite qu'une opération ne rend pas le
# type attendu, au lieu de l'apprendre par un refus.
Q_DESCRIBE_TYPE = """
query Describe($name: String!) {
  __type(name: $name) {
    name
    kind
    enumValues { name }
    inputFields { name type { name kind ofType { name kind } } }
    fields { name type { name kind ofType { name kind ofType { name kind } } } }
  }
}
"""

Q_ROOT_FIELDS = """
query RootFields {
  __schema {
    queryType { fields { name type { name kind ofType { name kind ofType { name } } } } }
  }
}
"""

# Les opérations et leurs types d'entrée, telles que l'application les utilise.
OPERATION_INPUTS = {
    "getVehiclesV3": "GetVehiclesInput",
    "getParkingSessionsV1": "GetParkingSessionsInput",
    "getRateOptionsV1": "GetRateOptionsInput",
    "getPaymentAccountsV1": "GetPaymentAccountsInput",
    "createQuotesV1": "QuoteRequestInput",
    "startParkingSessionV1": "StartParkingSessionV1Input",
    "renewParkingSessionV1": "RenewParkingSessionV1Input",
    "extendParkingSessionV1": "ExtendParkingSessionV1Input",
}


@dataclass
class Duration:
    quantity: int
    unit: str  # Minutes | Hours | Days

    @property
    def minutes(self) -> int:
        return self.quantity * {"Minutes": 1, "Hours": 60, "Days": 1440}[self.unit]

    def __str__(self) -> str:
        return f"{self.quantity} {self.unit}"


def best_duration(minutes: int, accepted: list[str] | None = None) -> Duration:
    """Convertit des minutes vers l'unité la plus « propre » acceptée par la zone."""
    accepted = [u.lower() for u in (accepted or [])] or ["minutes", "hours", "days"]
    if minutes % 1440 == 0 and "days" in accepted:
        return Duration(minutes // 1440, "Days")
    if minutes % 60 == 0 and "hours" in accepted:
        return Duration(minutes // 60, "Hours")
    if "minutes" in accepted:
        return Duration(minutes, "Minutes")
    if minutes % 60 == 0 and "hours" in accepted:
        return Duration(minutes // 60, "Hours")
    return Duration(max(1, -(-minutes // 60)), "Hours")


def _type_name(node: dict | None) -> str:
    """Déplie NON_NULL / LIST jusqu'au nom réel du type."""
    while isinstance(node, dict):
        if node.get("name"):
            return node["name"]
        node = node.get("ofType")
    return "?"


def _enum_variants(value: str) -> list[str]:
    """« Current », « CURRENT », « current » — on essaie les orthographes usuelles."""
    return list(dict.fromkeys([value, value.upper(), value.lower()]))


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


class PayByPhoneClient:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        refresh_token: str | None = None,
        access_token: str | None = None,
        expires_at: datetime | None = None,
        on_token_refresh=None,
        country: str = "FR",
        schema_cache: dict | None = None,
    ):
        self.username = username
        self.password = password
        self.refresh_token = refresh_token
        self._access_token = access_token
        self._expires_at = expires_at
        self.on_token_refresh = on_token_refresh
        self.country = country
        self.schema_cache = schema_cache if schema_cache is not None else {}
        self.http = HttpClient(API_BASE)

    # ------------------------------------------------------------------ auth

    def _auth_headers(self) -> dict:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
            "X-Pbp-ClientType": "WebApp",
            "User-Agent": USER_AGENT,
        }

    def _store_tokens(self, data: dict) -> None:
        self._access_token = data.get("access_token")
        if not self._access_token:
            raise AuthError(f"Pas d'access_token dans la réponse : {data}")
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
        self._expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))
        if self.on_token_refresh:
            self.on_token_refresh({
                "access_token": self._access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self._expires_at.isoformat(),
            })

    def login(self) -> None:
        if not (self.username and self.password):
            raise AuthError("PBP_USERNAME / PBP_PASSWORD manquants.")
        log.info("Connexion PayByPhone (%s)…", self.username)
        resp = self.http.post(
            AUTH_URL,
            headers=self._auth_headers(),
            data={
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "client_id": CLIENT_ID,
            },
        )
        if resp.status_code != 200:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("error_description") or body.get("error") or ""
            except ValueError:
                detail = resp.text[:300]
            raise AuthError(
                "Connexion refusée — vérifie l'identifiant (numéro avec indicatif "
                f"« +336… » ou email) et le mot de passe.\n↳ {detail}"
            )
        self._store_tokens(resp.json())
        log.info("Connecté ✅")

    def refresh(self) -> None:
        if not self.refresh_token:
            raise AuthError("Pas de refresh_token.")
        resp = self.http.post(
            AUTH_URL,
            headers=self._auth_headers(),
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": CLIENT_ID,
            },
        )
        if resp.status_code != 200:
            raise AuthError(f"Refresh refusé — HTTP {resp.status_code}")
        self._store_tokens(resp.json())

    def authenticate(self) -> None:
        if self._access_token and self._expires_at and utcnow() + TOKEN_SKEW < self._expires_at:
            return
        if self.refresh_token:
            try:
                self.refresh()
                return
            except AuthError as exc:
                log.warning("Refresh impossible (%s) — bascule sur mot de passe.", exc)
        self.login()

    @property
    def access_token(self) -> str:
        self.authenticate()
        assert self._access_token
        return self._access_token

    @property
    def member_id(self) -> str | None:
        claims = _jwt_claims(self._access_token or "")
        for key in ("userAccountId", "sub", "https://paybyphone.com/userAccountId", "uid"):
            if claims.get(key):
                return str(claims[key])
        return None

    def account_id(self) -> str:
        """Identifiant du compte = l'identifiant membre porté par le jeton."""
        self.authenticate()
        member = self.member_id
        if not member:
            raise ApiError("Impossible de lire l'identifiant membre dans le jeton.")
        return member

    # --------------------------------------------------------------- GraphQL

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
            "X-Pbp-ClientType": "WebApp",
            "User-Agent": USER_AGENT,
        }

    def gql(self, query: str, variables: dict, field: str, retried: bool = False):
        """Un appel GraphQL. Renvoie directement le contenu de `field`."""
        resp = self.http.post(
            GRAPHQL_PATH,
            headers=self._headers(),
            json={"query": query, "variables": variables},
        )
        if resp.status_code == 401 and not retried:
            self._access_token = self._expires_at = None  # jeton périmé côté serveur
            return self.gql(query, variables, field, retried=True)
        if not resp.ok:
            raise ApiError(f"GraphQL {field}", resp.status_code, resp.text)

        payload = resp.json()
        if payload.get("errors"):
            raise ApiError(self._explain(field, payload["errors"]))
        data = payload.get("data") or {}
        if field not in data:
            raise ApiError(f"Réponse GraphQL sans champ {field} : {json.dumps(payload)[:400]}")
        return data[field]

    def _explain(self, field: str, errors: list) -> str:
        """Une erreur GraphQL est bavarde : on y ajoute la vraie forme attendue."""
        messages = "; ".join(str(e.get("message", e)) for e in errors)
        text = f"GraphQL {field} a répondu : {messages}"

        type_name = OPERATION_INPUTS.get(field)
        looks_like_shape = any(
            k in messages.lower()
            for k in ("not defined", "unknown field", "required", "expected type", "argument")
        )
        if type_name and looks_like_shape:
            try:
                fields = self.input_fields(type_name)
            except Exception:  # noqa: BLE001 — le diagnostic ne doit rien casser
                fields = []
            if fields:
                text += (
                    f"\n↳ champs réellement acceptés par {type_name} : "
                    + ", ".join(f"{n} ({t})" for n, t in fields)
                )
        return text

    def accepted_fields(self, type_name: str) -> set[str] | None:
        """Champs réellement acceptés par un type d'entrée. `None` si inconnu.

        Le résultat est mis en cache : une seule introspection, réutilisée
        ensuite. C'est ce qui permet de ne jamais deviner la forme d'un input.
        """
        cached = self.schema_cache.get(type_name)
        if cached is not None:
            return set(cached) if cached else None
        try:
            fields = [name for name, _ in self.input_fields(type_name)]
        except Exception as exc:  # noqa: BLE001 — l'introspection peut être fermée
            log.debug("introspection de %s impossible : %s", type_name, exc)
            self.schema_cache[type_name] = []
            return None
        self.schema_cache[type_name] = fields
        return set(fields) or None

    def prune_details(self, details: dict) -> dict:
        """Élague le `details` d'un devis contre son vrai type.

        Le type est trouvé en demandant à l'API la forme de `QuoteRequestInput`,
        puis celle du type de son champ `details`.
        """
        nom = self.schema_cache.get("__details_type")
        if nom is None:
            try:
                champs = dict(self.input_fields("QuoteRequestInput"))
            except Exception:  # noqa: BLE001
                champs = {}
            nom = champs.get("details") or ""
            self.schema_cache["__details_type"] = nom
        return self.prune(details, nom) if nom else details

    def prune(self, payload: dict, type_name: str) -> dict:
        """Ne garde que les clés que l'API connaît, sans rien inventer.

        On peut donc proposer plusieurs orthographes plausibles (`plate` et
        `licensePlate`, par exemple) : seules celles qui existent partent.
        """
        accepted = self.accepted_fields(type_name)
        if accepted is None:
            return payload
        kept = {k: v for k, v in payload.items() if k in accepted}
        ignored = sorted(set(payload) - set(kept))
        if ignored:
            log.debug("%s : champs ignorés %s", type_name, ignored)
        return kept

    def describe_type(self, type_name: str) -> dict:
        """Tout ce que l'API sait dire d'un type : entrées, sorties, énumération."""
        data = self.gql(Q_DESCRIBE_TYPE, {"name": type_name}, "__type") or {}
        return {
            "name": data.get("name"),
            "kind": data.get("kind"),
            "enum": [e["name"] for e in data.get("enumValues") or []],
            "inputs": [(f["name"], _type_name(f.get("type"))) for f in data.get("inputFields") or []],
            "outputs": [(f["name"], _type_name(f.get("type"))) for f in data.get("fields") or []],
        }

    def root_fields(self) -> list[tuple[str, str]]:
        """Les opérations de lecture et le type que chacune renvoie."""
        data = self.gql(Q_ROOT_FIELDS, {}, "__schema") or {}
        fields = ((data.get("queryType") or {}).get("fields")) or []
        return [(f["name"], _type_name(f.get("type"))) for f in fields]

    def input_fields(self, type_name: str) -> list[tuple[str, str]]:
        """Introspection : la forme exacte d'un type d'entrée, telle qu'elle est."""
        data = self.gql(Q_INTROSPECT_INPUT, {"name": type_name}, "__type")
        out = []
        for item in (data or {}).get("inputFields") or []:
            kind = item.get("type") or {}
            name = kind.get("name") or (kind.get("ofType") or {}).get("name") or kind.get("kind")
            out.append((item["name"], str(name)))
        return out

    # ------------------------------------------------------------- véhicules

    def vehicles(self) -> list[Vehicle]:
        data = self.gql(Q_VEHICLES, {"input": {}}, "getVehiclesV3") or []
        return [
            Vehicle(
                id=str(v.get("id", "")),
                plate=str(v.get("licensePlate", "")).upper().replace(" ", ""),
                country=v.get("countryCode"),
                type=v.get("type"),
                raw=v,
            )
            for v in data
        ]

    def payment_account_id(self) -> str | None:
        """Carte enregistrée. Inutile pour un tarif gratuit (CMI/PMR)."""
        try:
            data = self.gql(Q_PAYMENT_ACCOUNTS, {"input": {}}, "getPaymentAccountsV1") or []
        except ApiError as exc:
            log.debug("Moyens de paiement non listés : %s", exc)
            return None
        for item in data:
            if item.get("paymentAccountId"):
                return str(item["paymentAccountId"])
        return None

    # ----------------------------------------------------------------- zones

    def rate_options(
        self, location_id: str, plate: str | None = None, start: datetime | None = None
    ) -> list[RateOption]:
        # Forme relevée dans l'application : {locationId, licensePlate}.
        payload = self.prune(
            {k: v for k, v in {
                "locationId": str(location_id),
                "licensePlate": plate,
                "startTime": start.isoformat().replace("+00:00", "Z") if start else None,
            }.items() if v is not None},
            "GetRateOptionsInput",
        )
        data = self.gql(Q_RATE_OPTIONS, {"input": payload}, "getRateOptionsV1") or []
        out = []
        for item in data:
            max_stay = item.get("effectiveMaxStayDuration") or {}
            qty, unit = max_stay.get("quantity"), str(max_stay.get("timeUnit", "")).lower()
            minutes = None
            if isinstance(qty, (int, float)):
                minutes = int(qty) * {
                    "minute": 1, "minutes": 1, "hour": 60, "hours": 60,
                    "day": 1440, "days": 1440,
                }.get(unit, 1)
            out.append(
                RateOption(
                    id=str(item.get("ratePolicyId", "")),
                    name=item.get("name") or "",
                    type=item.get("type"),
                    is_default=False,
                    max_stay_minutes=minutes,
                    accepted_time_units=item.get("acceptedTimeUnits") or [],
                    raw=item,
                )
            )
        return out

    def pick_rate_option(self, location_id: str, plate: str, wanted: str | None) -> RateOption:
        options = self.rate_options(location_id, plate)
        if not options:
            raise NotEligibleError(
                f"Aucun tarif disponible zone {location_id} pour {plate}. "
                "Zone inconnue, ou plaque non enregistrée sur le compte."
            )
        if wanted:
            for opt in options:
                if opt.matches(wanted):
                    return opt
            listing = ", ".join(f"{o.type or '?'}/{o.name} (id {o.id})" for o in options)
            raise NotEligibleError(
                f"Tarif « {wanted} » indisponible zone {location_id} pour {plate}.\n"
                f"Tarifs proposés : {listing}"
            )
        # getRateOptionsV1 ne marque aucun tarif comme « par défaut » :
        # sans `rate:` explicite, on prend le premier proposé par la zone.
        return options[0]

    # ----------------------------------------------------------------- devis

    def quote(
        self,
        location_id: str,
        plate: str,
        duration: Duration,
        rate_option_id: str | None = None,
        stall: str | None = None,
        session_id: str | None = None,
        operation: str = "Start",
        payment_account_id: str | None = None,
    ) -> Quote:
        """createQuotesV1 — un prix **et** un quoteId, indispensable pour l'achat."""
        # On n'envoie que ce qui a une valeur. Un champ vide n'est pas neutre :
        # `parkingSessionId: ""` sur un démarrage, ou un moyen de paiement vide
        # sur un tarif gratuit, peuvent suffire à faire refuser le devis.
        details: dict = {
            "locationId": str(location_id),
            "advertisedLocationId": str(location_id),
            "ratePolicyId": str(rate_option_id or ""),
            "parkingQuoteOperation": operation,
            "durationTimeUnit": duration.unit,
            "durationQuantity": str(duration.quantity),
            "licensePlate": plate,
        }
        if stall:
            details["stall"] = stall
        if session_id:
            details["parkingSessionId"] = session_id
        if payment_account_id:  # inutile sur un tarif gratuit
            details["paymentAccountId"] = payment_account_id
            details["paymentScope"] = "Private"
        if operation == "Renew":
            details["isRenewal"] = True
        details = self.prune_details(details)

        request = {
            "quoteRequestId": str(uuid.uuid4()),
            "product": "PARKING",
            "details": details,
        }
        data = self.gql(M_CREATE_QUOTES, {"requests": [request]}, "createQuotesV1") or {}
        response = data.get("createQuotesResponse") or {}

        errors = response.get("quoteErrors") or []
        if errors:
            reasons = "; ".join(
                f"{e.get('status', '')} {e.get('reason', '')}".strip() for e in errors
            )
            raise NotEligibleError(f"Devis refusé zone {location_id} : {reasons}")

        quotes = response.get("quotes") or []
        if not quotes:
            raise ApiError(f"Aucun devis renvoyé pour la zone {location_id}.")

        first = quotes[0]
        detail = first.get("details") or {}
        cost = detail.get("totalCost") or {}
        return Quote(
            cost=float(cost.get("amount") or 0),
            currency=cost.get("currency") or "EUR",
            start=parse_dt(detail.get("parkingStartTime")),
            expiry=parse_dt(detail.get("parkingExpiryTime")),
            quote_id=first.get("quoteId"),
            raw=first,
        )

    # --------------------------------------------------------------- tickets

    def _to_session(self, item: dict) -> ParkingSession:
        vehicle = item.get("vehicle") or {}
        rate = item.get("ratePolicy") or {}
        cost = item.get("totalCost") or {}
        lieu = item.get("location") or {}
        return ParkingSession(
            advertised_location_id=(
                str(lieu["advertisedLocationId"]) if lieu.get("advertisedLocationId") else None
            ),
            id=str(item.get("parkingSessionId") or ""),
            plate=str(vehicle.get("licensePlate") or "").upper().replace(" ", ""),
            location_id=str(item.get("locationId") or ""),
            start=parse_dt(item.get("startTime")),
            expiry=parse_dt(item.get("expireTime")),
            rate_option_id=str(rate["ratePolicyId"]) if rate.get("ratePolicyId") else None,
            rate_type=rate.get("type"),
            cost=float(cost["amount"]) if isinstance(cost.get("amount"), (int, float)) else None,
            currency=cost.get("currency"),
            raw=item,
        )

    def current_sessions(self) -> list[ParkingSession]:
        """Tickets en cours.

        C'est `getParkingSessionsV1`, pas `getOpenSessionsV1` : ce dernier
        renvoie un `AutopaySessionResponse` — les parkings en ouvrage, pas la
        voirie. L'API l'a dit elle-même au premier essai contre le vrai compte.
        """
        sessions = self._sessions("CURRENT")
        maintenant = utcnow()
        return [s for s in sessions if s.expiry and s.expiry > maintenant]

    def history(self, limit: int = 25) -> list[ParkingSession]:
        return self._sessions("HISTORIC", limit=limit)

    def _sessions(self, period: str, limit: int | None = None) -> list[ParkingSession]:
        # L'application envoie periodType en majuscules, avec offset et limit.
        payload: dict = {"periodType": period, "offset": 0, "limit": min(limit or 50, 50)}
        payload = self.prune(payload, "GetParkingSessionsInput") or {"periodType": period}

        # `periodType` est une énumération : l'orthographe exacte peut différer.
        derniere: ApiError | None = None
        for valeur in _enum_variants(period):
            try:
                data = self.gql(
                    Q_SESSIONS, {"input": {**payload, "periodType": valeur}},
                    "getParkingSessionsV1",
                ) or []
            except ApiError as exc:
                derniere = exc
                if "enum" not in str(exc).lower() and "periodtype" not in str(exc).lower():
                    raise
                continue
            return [self._to_session(item) for item in data]
        raise derniere or ApiError("getParkingSessionsV1 : aucune période acceptée.")

    def find_active(self, plate, location_id=None, sessions=None):
        plate = plate.upper().replace(" ", "")
        best = None
        for sess in sessions if sessions is not None else self.current_sessions():
            if sess.plate != plate:
                continue
            if location_id and not sess.at_location(location_id):
                continue
            if not sess.expiry or sess.expiry <= utcnow():
                continue
            if best is None or sess.expiry > best.expiry:
                best = sess
        return best

    # ----------------------------------------------------------------- achat

    def start_session(
        self,
        location_id: str,
        plate: str,
        duration: Duration,
        rate_option_id: str,
        stall: str | None = None,
        payment_account_id: str | None = None,
        start_time: datetime | None = None,
        verify: bool = True,
    ) -> ParkingSession:
        """Prend un ticket : devis → achat → vérification.

        Si une session renouvelable existe déjà sur cette plaque et cette zone,
        on la renouvelle — c'est ce que prévoit l'API, et ça évite le refus
        « session déjà active ».
        """
        plate = plate.upper().replace(" ", "")
        before = self.current_sessions()
        existing = self.find_active(plate, str(location_id), before)

        if existing and existing.raw.get("isRenewable"):
            log.info("Session renouvelable trouvée (%s) — renouvellement.", existing.id)
            return self.renew_session(existing, duration, rate_option_id,
                                      payment_account_id, verify=verify)

        quote = self.quote(
            location_id, plate, duration, rate_option_id, stall,
            payment_account_id=payment_account_id,
        )
        log.info("Achat : zone %s · %s · %s · %.2f %s",
                 location_id, plate, duration, quote.cost, quote.currency)

        result = self._mutate_session(M_START, "startParkingSessionV1", quote, plate)
        if not verify:
            return result
        return self._verify(plate, str(location_id), {s.id for s in before}, result)

    def renew_session(
        self,
        session: ParkingSession,
        duration: Duration,
        rate_option_id: str | None = None,
        payment_account_id: str | None = None,
        verify: bool = True,
    ) -> ParkingSession:
        quote = self.quote(
            session.location_id,
            session.plate,
            duration,
            rate_option_id or session.rate_option_id,
            session_id=session.id,
            operation="Renew",
            payment_account_id=payment_account_id,
        )
        result = self._mutate_session(M_RENEW, "renewParkingSessionV1", quote, session.plate,
                                      session_id=session.id)
        if not verify:
            return result
        return self._verify(session.plate, session.location_id, set(), result,
                            previous_expiry=session.expiry)

    def extend_session(
        self, session_id: str, duration: Duration, payment_account_id: str | None = None
    ) -> None:
        current = next((s for s in self.current_sessions() if s.id == session_id), None)
        if not current:
            raise ApiError(f"Session {session_id} introuvable — impossible de prolonger.")
        quote = self.quote(
            current.location_id, current.plate, duration, current.rate_option_id,
            session_id=session_id, operation="Extend",
            payment_account_id=payment_account_id,
        )
        self._mutate_session(M_EXTEND, "extendParkingSessionV1", quote, current.plate,
                             session_id=session_id)

    def _mutate_session(
        self, query: str, field: str, quote: Quote, plate: str, session_id: str | None = None
    ) -> ParkingSession:
        """Achat/renouvellement à partir du quoteId, avec repli sur la forme d'entrée."""
        if not quote.quote_id:
            raise ApiError("Le devis n'a pas renvoyé de quoteId — achat impossible.")

        # Forme relevée dans l'application : input.request ne porte que le
        # quoteId. Tout le contexte (zone, plaque, durée, tarif) est déjà
        # attaché au devis côté serveur.
        requete: dict = {"quoteId": quote.quote_id}
        if session_id:
            requete["parkingSessionId"] = session_id

        shapes = [{"request": requete}, requete, {"request": {"quoteId": quote.quote_id}}]
        last: ApiError | None = None
        for shape in shapes:
            try:
                data = self.gql(query, {"input": shape}, field) or {}
            except ApiError as exc:
                last = exc
                log.debug("forme d'entrée %s refusée : %s", list(shape), exc)
                continue
            response = data.get("parkingSessionResponse") or {}
            cost = response.get("segmentTotalCost") or {}
            return ParkingSession(
                id=str(response.get("parkingSessionId") or ""),
                plate=plate,
                location_id="",
                start=utcnow(),
                expiry=parse_dt(response.get("expireTime")),
                cost=float(cost["amount"]) if isinstance(cost.get("amount"), (int, float)) else None,
                currency=cost.get("currency"),
                raw=response,
            )
        raise last or ApiError(f"{field} : aucune forme d'entrée acceptée.")

    def _verify(
        self,
        plate: str,
        location_id: str,
        known_ids: set[str],
        claimed: ParkingSession,
        previous_expiry: datetime | None = None,
        attempts: int = 8,
    ) -> ParkingSession:
        """Le ticket n'est acquis que s'il est relu dans les sessions ouvertes."""
        for attempt in range(attempts):
            time.sleep(2 if attempt else 1)
            for sess in self.current_sessions():
                if sess.plate != plate:
                    continue
                if location_id and sess.location_id and str(sess.location_id) != str(location_id):
                    continue
                if not sess.expiry or sess.expiry <= utcnow():
                    continue
                if claimed.id and sess.id == claimed.id:
                    if previous_expiry and sess.expiry <= previous_expiry:
                        continue  # renouvellement pas encore pris en compte
                    log.info("Ticket confirmé ✅ %s", sess.describe())
                    return sess
                if not claimed.id and sess.id not in known_ids:
                    log.info("Ticket confirmé ✅ %s", sess.describe())
                    return sess
        raise ApiError(
            f"Ticket non confirmé : rien d'actif pour {plate} zone {location_id} après "
            f"{attempts} vérifications. L'achat n'a pas abouti."
        )
