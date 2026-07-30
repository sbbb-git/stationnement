"""Client PayByPhone — l'API utilisée par AlloValet.

Flux réel d'un ticket (c'est là que l'ancien script se plantait) :

    1. token         POST  auth.paybyphoneapis.com/token
    2. compte        GET   /parking/accounts
    3. tarifs        GET   /parking/locations/{zone}/rateOptions
    4. devis         GET   /parking/accounts/{id}/quote          ← ne réserve RIEN
    5. ACHAT         POST  /parking/accounts/{id}/sessions       ← le vrai ticket
    6. VÉRIFICATION  GET   /parking/accounts/{id}/sessions?periodType=Current

Un devis (`quote` / `createQuotesV1`) n'est qu'une estimation de prix : sans
l'étape 5 aucun ticket n'existe, et sans l'étape 6 on ne peut pas l'affirmer.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import ApiError, AuthError, NotEligibleError
from .http import HttpClient
from .models import ParkingSession, Quote, RateOption, Vehicle, parse_dt, utcnow

log = logging.getLogger("allovalet.paybyphone")

AUTH_URL = "https://auth.paybyphoneapis.com/token"
API_BASE = "https://consumer.paybyphoneapis.com"
GRAPHQL_URL = f"{API_BASE}/uapi/graphql"
CLIENT_ID = "paybyphone_web"
WEB_ORIGIN = "https://m.paybyphone.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Marge avant expiration du token pour le renouveler d'office.
TOKEN_SKEW = timedelta(seconds=90)


@dataclass
class Duration:
    """Une durée exprimée dans une unité acceptée par PayByPhone."""

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
    # la zone n'accepte ni minutes ni heures rondes : on arrondit à l'heure supérieure
    return Duration(max(1, -(-minutes // 60)), "Hours")


def _jwt_claims(token: str) -> dict:
    """Décode le payload d'un JWT sans vérifier la signature (lecture seule)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # token opaque ou malformé
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
    ):
        self.username = username
        self.password = password
        self.refresh_token = refresh_token
        self._access_token = access_token
        self._expires_at = expires_at
        self.on_token_refresh = on_token_refresh
        self.http = HttpClient(API_BASE)
        self._account_id: str | None = None

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
            self.on_token_refresh(
                {
                    "access_token": self._access_token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self._expires_at.isoformat(),
                }
            )

    def login(self) -> None:
        """Connexion par identifiants — pas de rotation de token à gérer."""
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
            raise AuthError(
                "Connexion refusée — vérifie identifiant (numéro de téléphone ou email) "
                f"et mot de passe.\nHTTP {resp.status_code} : {resp.text[:500]}"
            )
        self._store_tokens(resp.json())
        log.info("Connecté ✅")

    def refresh(self) -> None:
        if not self.refresh_token:
            raise AuthError("Pas de refresh_token.")
        log.info("Renouvellement du token…")
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
            raise AuthError(f"Refresh refusé — HTTP {resp.status_code} : {resp.text[:300]}")
        self._store_tokens(resp.json())

    def authenticate(self) -> None:
        """Obtient un access_token valide, par le chemin le plus fiable disponible."""
        if self._access_token and self._expires_at and utcnow() + TOKEN_SKEW < self._expires_at:
            return
        if self.refresh_token:
            try:
                self.refresh()
                return
            except AuthError as exc:
                log.warning("Refresh impossible (%s) — bascule sur identifiant/mot de passe.", exc)
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

    # ------------------------------------------------------------------ base

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
            "X-Pbp-ClientType": "WebApp",
            "X-Pbp-Version": "2",
            "User-Agent": USER_AGENT,
        }

    def _get_json(self, path: str, params: dict | None = None):
        resp = self.http.get(path, headers=self._headers(), params=params)
        if resp.status_code == 401:
            # token périmé côté serveur : on force une reconnexion complète
            self._access_token = None
            self._expires_at = None
            resp = self.http.get(path, headers=self._headers(), params=params)
        if not resp.ok:
            raise ApiError(f"GET {path}", resp.status_code, resp.text)
        return resp.json() if resp.content else None

    # -------------------------------------------------------------- comptes

    def account_id(self) -> str:
        if self._account_id:
            return self._account_id
        data = self._get_json("/parking/accounts")
        accounts = data if isinstance(data, list) else data.get("accounts", [])
        if not accounts:
            raise ApiError("Aucun compte de stationnement sur ce login PayByPhone.")
        self._account_id = str(accounts[0].get("id") or accounts[0].get("accountId"))
        return self._account_id

    def vehicles(self) -> list[Vehicle]:
        data = self._get_json(f"/parking/accounts/{self.account_id()}/vehicles") or []
        out = []
        for item in data:
            out.append(
                Vehicle(
                    id=str(item.get("id", "")),
                    plate=str(item.get("licensePlate", "")).upper().replace(" ", ""),
                    country=item.get("countryCode"),
                    type=item.get("type") or item.get("vehicleType"),
                    raw=item,
                )
            )
        return out

    def payment_account_id(self) -> str | None:
        """Carte enregistrée. Inutile pour un tarif gratuit (CMI/PMR), requis sinon."""
        member = self.member_id
        candidates = []
        if member:
            candidates.append(f"/identity/profileservice/v1/members/{member}/paymentaccounts")
        candidates.append("/payment/accounts")
        if self._account_id:
            candidates.append(f"/parking/accounts/{self._account_id}/paymentaccounts")
        for path in candidates:
            try:
                data = self._get_json(path)
            except ApiError:
                continue
            items = data if isinstance(data, list) else (data or {}).get("paymentAccounts", [])
            for item in items or []:
                pid = item.get("paymentAccountId") or item.get("id")
                if pid:
                    log.debug("Moyen de paiement trouvé via %s", path)
                    return str(pid)
        log.debug("Aucun moyen de paiement enregistré trouvé (normal si tarif gratuit).")
        return None

    # ---------------------------------------------------------------- zones

    def location(self, location_id: str) -> dict:
        return self._get_json(f"/parking/locations/{location_id}") or {}

    def search_location(self, advertised_number: str, country: str = "FR") -> list[dict]:
        data = self._get_json(
            "/parking/locations",
            params={"advertisedLocationNumber": advertised_number, "countryCode": country},
        )
        return data if isinstance(data, list) else []

    def rate_options(
        self, location_id: str, plate: str | None = None, start: datetime | None = None
    ) -> list[RateOption]:
        params: dict = {"parkingAccountId": self.account_id()}
        if plate:
            params["licensePlate"] = plate
        if start:
            params["startTime"] = start.isoformat().replace("+00:00", "Z")
        data = self._get_json(f"/parking/locations/{location_id}/rateOptions", params=params) or []
        out = []
        for item in data:
            max_stay = item.get("maxStayDuration") or {}
            qty = max_stay.get("quantity")
            unit = str(max_stay.get("durationType", "")).lower()
            minutes = None
            if isinstance(qty, (int, float)):
                minutes = int(qty) * {"minute": 1, "hour": 60, "day": 1440}.get(unit, 1)
            out.append(
                RateOption(
                    id=str(item.get("rateOptionId", "")),
                    name=item.get("name", ""),
                    type=item.get("type") or item.get("eligibilityType"),
                    is_default=bool(item.get("isDefault")),
                    max_stay_minutes=minutes,
                    accepted_time_units=item.get("acceptedTimeUnits") or [],
                    raw=item,
                )
            )
        return out

    def pick_rate_option(
        self, location_id: str, plate: str, wanted: str | None
    ) -> RateOption:
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
        for opt in options:
            if opt.is_default:
                return opt
        return options[0]

    # --------------------------------------------------------------- devis

    def quote(
        self,
        location_id: str,
        plate: str,
        duration: Duration,
        rate_option_id: str | None = None,
        stall: str | None = None,
        session_id: str | None = None,
    ) -> Quote:
        params = {
            "durationTimeUnit": duration.unit,
            "durationQuantity": str(duration.quantity),
            "licensePlate": plate,
        }
        if session_id:
            params["parkingSessionId"] = session_id
        else:
            params["locationId"] = location_id
        if rate_option_id:
            params["rateOptionId"] = rate_option_id
        if stall:
            params["stall"] = stall

        data = self._get_json(f"/parking/accounts/{self.account_id()}/quote", params=params) or {}
        total = data.get("totalCost") or {}
        return Quote(
            cost=float(total.get("amount", 0) or 0),
            currency=total.get("currency", "EUR"),
            start=parse_dt(data.get("parkingStartTime")),
            expiry=parse_dt(data.get("parkingExpiryTime")),
            raw=data,
        )

    # -------------------------------------------------------------- tickets

    def current_sessions(self) -> list[ParkingSession]:
        return self._sessions("Current")

    def history(self, limit: int = 25) -> list[ParkingSession]:
        """Tickets passés — sert de justificatif et de suivi de dépense."""
        return self._sessions("Historic", limit=limit)

    def _sessions(self, period: str, limit: int | None = None) -> list[ParkingSession]:
        params: dict = {"periodType": period}
        if limit:
            params["limit"] = min(limit, 49)  # borne imposée par l'API
        data = self._get_json(
            f"/parking/accounts/{self.account_id()}/sessions", params=params
        ) or []
        if isinstance(data, dict):
            data = data.get("sessions", data.get("items", []))
        out = []
        for item in data:
            vehicle = item.get("vehicle") or {}
            rate = item.get("rateOption") or {}
            cost = (item.get("totalCost") or item.get("cost") or {})
            out.append(
                ParkingSession(
                    id=str(item.get("parkingSessionId") or item.get("id") or ""),
                    plate=str(vehicle.get("licensePlate") or item.get("licensePlate") or "")
                    .upper()
                    .replace(" ", ""),
                    location_id=str(item.get("locationId") or ""),
                    start=parse_dt(item.get("startTime")),
                    expiry=parse_dt(item.get("expireTime") or item.get("expiryTime")
                                    or item.get("endTime")),
                    rate_option_id=str(rate.get("rateOptionId")) if rate.get("rateOptionId") else None,
                    rate_type=rate.get("type") or rate.get("name"),
                    cost=float(cost["amount"]) if isinstance(cost.get("amount"), (int, float)) else None,
                    currency=cost.get("currency"),
                    raw=item,
                )
            )
        return out

    def find_active(
        self, plate: str, location_id: str | None = None, sessions: list[ParkingSession] | None = None
    ) -> ParkingSession | None:
        plate = plate.upper().replace(" ", "")
        best: ParkingSession | None = None
        for sess in sessions if sessions is not None else self.current_sessions():
            if sess.plate != plate:
                continue
            if location_id and sess.location_id and str(sess.location_id) != str(location_id):
                continue
            if not sess.expiry or sess.expiry <= utcnow():
                continue
            if best is None or (sess.expiry > best.expiry):
                best = sess
        return best

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
        """Achète réellement un ticket, puis VÉRIFIE qu'il existe côté serveur."""
        body: dict = {
            "locationId": str(location_id),
            "licensePlate": plate,
            "rateOptionId": str(rate_option_id),
            "duration": {"timeUnit": duration.unit, "quantity": str(duration.quantity)},
        }
        if stall:
            body["stall"] = stall
        if start_time:
            body["startTime"] = start_time.isoformat().replace("+00:00", "Z")
        if payment_account_id:
            body["paymentMethod"] = {
                "paymentMethodType": "PaymentAccount",
                "payload": {"paymentAccountId": payment_account_id},
            }

        before = self.current_sessions() if verify else []
        known_ids = {s.id for s in before}

        log.info("Achat ticket : zone %s · %s · %s · tarif %s",
                 location_id, plate, duration, rate_option_id)
        resp = self.http.post(
            f"/parking/accounts/{self.account_id()}/sessions",
            headers=self._headers(),
            json=body,
        )
        if resp.status_code not in (200, 201, 202):
            raise ApiError("Achat du ticket refusé", resp.status_code, resp.text)

        workflow = resp.headers.get("Location")
        if workflow:
            self._check_workflow(workflow)

        if not verify:
            return ParkingSession(
                id="", plate=plate, location_id=str(location_id), start=utcnow(),
                expiry=utcnow() + timedelta(minutes=duration.minutes),
            )
        return self._wait_for_session(plate, str(location_id), known_ids)

    def extend_session(
        self,
        session_id: str,
        duration: Duration,
        payment_account_id: str | None = None,
    ) -> None:
        body: dict = {"duration": {"timeUnit": duration.unit, "quantity": str(duration.quantity)}}
        if payment_account_id:
            body["paymentMethod"] = {
                "paymentMethodType": "PaymentAccount",
                "payload": {"paymentAccountId": payment_account_id},
            }
        resp = self.http.put(
            f"/parking/accounts/{self.account_id()}/sessions/{session_id}",
            headers=self._headers(),
            json=body,
        )
        if resp.status_code not in (200, 202, 204):
            raise ApiError("Prolongation refusée", resp.status_code, resp.text)
        workflow = resp.headers.get("Location")
        if workflow:
            self._check_workflow(workflow)

    def _check_workflow(self, url: str) -> None:
        """L'achat est asynchrone : le workflow dit si ça a échoué et pourquoi."""
        for _ in range(5):
            try:
                resp = self.http.get(url, headers=self._headers(), retry=False)
            except ApiError:
                return
            if resp.status_code == 404:
                time.sleep(1)
                continue
            if not resp.ok:
                return
            try:
                data = resp.json()
            except ValueError:
                return
            status = str(data.get("status") or data.get("$type") or "").lower()
            log.debug("workflow → %s", data)
            if "fail" in status or "error" in status or data.get("errors"):
                raise ApiError(f"Achat refusé par PayByPhone : {json.dumps(data)[:500]}")
            if "complete" in status or "created" in status or "success" in status:
                return
            time.sleep(1)

    def _wait_for_session(
        self, plate: str, location_id: str, known_ids: set[str], attempts: int = 10
    ) -> ParkingSession:
        plate = plate.upper().replace(" ", "")
        for attempt in range(attempts):
            time.sleep(2 if attempt else 1)
            for sess in self.current_sessions():
                if sess.plate != plate:
                    continue
                if sess.location_id and str(sess.location_id) != location_id:
                    continue
                if not sess.expiry or sess.expiry <= utcnow():
                    continue
                if sess.id and sess.id in known_ids:
                    continue  # ticket déjà là avant l'achat : ce n'est pas le nôtre
                log.info("Ticket confirmé ✅ %s", sess.describe())
                return sess
        raise ApiError(
            f"Ticket non confirmé : aucune session active pour {plate} zone {location_id} "
            f"après {attempts} vérifications. L'achat n'a pas abouti."
        )

    # ------------------------------------------------------------ diagnostic

    def graphql_mutations(self) -> list[str]:
        """Introspection GraphQL — utile si l'API REST change un jour."""
        query = {
            "query": "{ __schema { mutationType { fields { name } } } }",
        }
        resp = self.http.post(GRAPHQL_URL, headers=self._headers(), json=query)
        if not resp.ok:
            raise ApiError("Introspection GraphQL", resp.status_code, resp.text)
        data = resp.json()
        fields = (
            data.get("data", {}).get("__schema", {}).get("mutationType", {}) or {}
        ).get("fields") or []
        return [f["name"] for f in fields]
