"""Faux PayByPhone GraphQL — rejoue le moteur réel en local.

Les opérations, les types d'entrée et les champs de réponse sont ceux relevés
dans le bundle Flutter de l'application (`main.dart.js`) :
createQuotesV1 → startParkingSessionV1 / renewParkingSessionV1 → getOpenSessionsV1.

Barème imité de la voirie parisienne (zone 1), volontairement progressif.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MEMBER_ID = "d6d1817e-98ee-4600-b82b-f1aace2abea5"
PLATE = "AB123CD"

CMI_POLICY = "1085252721"

PROGRESSIVE = {60: 6.0, 120: 12.0, 180: 32.5, 240: 52.5, 300: 63.75, 360: 75.0}

RATE_OPTIONS = {
    "75016": [
        {"name": "Carte Mobilité Inclusion", "type": "CMI", "ratePolicyId": CMI_POLICY,
         "maxStayStatus": "ParkingAllowed", "acceptedTimeUnits": ["Hours", "Days"],
         "effectiveMaxStayDuration": {"quantity": 24, "timeUnit": "Hours"}},
        {"name": "Visiteur", "type": "VIS", "ratePolicyId": "75016",
         "maxStayStatus": "ParkingAllowed", "acceptedTimeUnits": ["Minutes", "Hours"],
         "effectiveMaxStayDuration": {"quantity": 360, "timeUnit": "Minutes"}},
    ],
    "75008": [
        {"name": "Visiteur", "type": "VIS", "ratePolicyId": "75008",
         "maxStayStatus": "ParkingAllowed", "acceptedTimeUnits": ["Minutes", "Hours"],
         "effectiveMaxStayDuration": {"quantity": 360, "timeUnit": "Minutes"}},
    ],
}

UNIT_MINUTES = {"minutes": 1, "hours": 60, "days": 1440}

# Formes d'entrée du serveur. Tout champ hors de cette liste est rejeté, comme
# le ferait un vrai GraphQL : c'est ce qui vérifie que le client élague.
INPUT_FIELDS = {
    "StartParkingSessionV1Input": ["quoteId", "plate"],
    "RenewParkingSessionV1Input": ["quoteId", "plate", "parkingSessionId"],
    "GetRateOptionsInput": ["locationId", "plate"],
    "GetVehiclesInput": [],
    "GetPaymentAccountsInput": [],
    "GetParkingSessionsInput": ["periodType", "offset", "limit"],
}


def price(rate_policy_id: str, minutes: int) -> float:
    """0 € pour la CMI ; barème progressif interpolé sinon."""
    if rate_policy_id == CMI_POLICY:
        return 0.0
    steps = sorted(PROGRESSIVE)
    if minutes <= steps[0]:
        return round(PROGRESSIVE[steps[0]] * minutes / steps[0], 2)
    for low, high in zip(steps, steps[1:]):
        if minutes <= high:
            ratio = (minutes - low) / (high - low)
            return round(PROGRESSIVE[low] + ratio * (PROGRESSIVE[high] - PROGRESSIVE[low]), 2)
    last = steps[-1]
    return round(PROGRESSIVE[last] * minutes / last, 2)


class FakePayByPhone:
    def __init__(self):
        self.sessions: list[dict] = []
        self.quotes: dict[str, dict] = {}
        self.purchases: list[dict] = []
        self.operations: list[str] = []
        self.swallow_purchases = False   # « acheté » mais aucune session créée
        self.reject_duplicate = False    # zone refusant une 2e session
        self.require_request_wrapper = False  # variante input: {request: {...}}
        self.token_calls: list[dict] = []
        self.issued_tokens: set[str] = set()  # un jeton inconnu = 401, comme en vrai
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ------------------------------------------------------ aides de test

    def add_session(self, minutes: int, plate: str = PLATE, location: str = "75016",
                    rate_policy_id: str = CMI_POLICY, renewable: bool = True) -> dict:
        now = datetime.now(timezone.utc)
        session = {
            "parkingSessionId": str(uuid.uuid4()),
            "locationId": location,
            "startTime": _iso(now),
            "expireTime": _iso(now + timedelta(minutes=minutes)),
            "stall": None,
            "status": "Active",
            "type": "Parking",
            "isStoppable": True,
            "totalCost": {"amount": 0.0, "currency": "EUR"},
            "location": {"advertisedLocationId": location, "name": f"Paris {location}",
                         "isStallBased": False},
            "isRenewable": renewable,
            "renewableAfter": _iso(now),
            "isExtendable": True,
            "vehicle": {"licensePlate": plate, "countryCode": "FR"},
            "ratePolicy": {"ratePolicyId": rate_policy_id, "type": "CMI"},
        }
        self.sessions.append(session)
        return session

    def add_past_session(self, hours_ago: int = 24, cost: float = 6.0,
                         plate: str = PLATE, location: str = "75016") -> dict:
        end = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        session = {
            "parkingSessionId": str(uuid.uuid4()),
            "locationId": location,
            "startTime": _iso(end - timedelta(hours=1)),
            "expireTime": _iso(end),
            "status": "Expired",
            "type": "Parking",
            "location": {"advertisedLocationId": location, "name": f"Paris {location}",
                         "isStallBased": False},
            "isRenewable": False,
            "vehicle": {"licensePlate": plate, "countryCode": "FR"},
            "ratePolicy": {"ratePolicyId": "75016", "type": "VIS"},
            "totalCost": {"amount": cost, "currency": "EUR"},
        }
        self.sessions.append(session)
        return session

    def active(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [s for s in self.sessions if _parse(s["expireTime"]) > now]

    def past(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [s for s in self.sessions if _parse(s["expireTime"]) <= now]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _make_handler(state: FakePayByPhone):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # ------------------------------------------------------------ utils
        def _json(self, payload, status: int = 200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _data(self, field, value):
            return self._json({"data": {field: value}})

        def _error(self, message):
            return self._json({"errors": [{"message": message}]})

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode() if length else ""
            if not raw:
                return {}
            if raw.lstrip().startswith("{"):
                return json.loads(raw)
            return {k: v[0] for k, v in parse_qs(raw).items()}

        # ------------------------------------------------------------- POST
        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/token":
                return self._token()
            if path != "/uapi/graphql":
                return self._json({"error": "not found"}, 404)
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if token not in state.issued_tokens:
                return self._json({"errors": [{"message": "unauthorized"}]}, 401)
            return self._graphql()

        def do_GET(self):  # noqa: N802
            return self._json({"error": "not found"}, 404)

        def _token(self):
            body = self._body()
            state.token_calls.append(body)
            grant = body.get("grant_type")
            if grant == "password" and not (body.get("username") and body.get("password")):
                return self._json({"error": "invalid_grant"}, 400)
            if grant == "refresh_token" and body.get("refresh_token") != "valid-refresh":
                return self._json(
                    {"error": "invalid_grant", "error_description": "expired"}, 400
                )
            token = _fake_jwt()
            state.issued_tokens.add(token)
            return self._json({
                "access_token": token,
                "refresh_token": "valid-refresh",
                "token_type": "bearer",
                "expires_in": 3600,
            })

        # ---------------------------------------------------------- GraphQL
        def _graphql(self):
            body = self._body()
            query = body.get("query", "")
            variables = body.get("variables") or {}
            field = _operation_field(query)
            state.operations.append(field)

            handler = {
                "__type": self._introspect,
                "getVehiclesV3": self._vehicles,
                "getPaymentAccountsV1": self._payment_accounts,
                "getRateOptionsV1": self._rate_options,
                "getParkingSessionsV1": self._sessions,
                "createQuotesV1": self._create_quotes,
                "startParkingSessionV1": self._start,
                "renewParkingSessionV1": self._renew,
                "extendParkingSessionV1": self._extend,
            }.get(field)
            if not handler:
                return self._error(f"Cannot query field \"{field}\"")
            return handler(variables)

        def _introspect(self, variables):
            name = variables.get("name", "")
            fields = INPUT_FIELDS.get(name)
            if fields is None:
                return self._data("__type", None)
            return self._data("__type", {
                "name": name,
                "inputFields": [
                    {"name": f, "type": {"name": "String", "kind": "SCALAR", "ofType": None}}
                    for f in fields
                ],
            })

        def _vehicles(self, variables):
            return self._data("getVehiclesV3", [
                {"id": "1", "licensePlate": PLATE, "countryCode": "FR",
                 "type": "car", "jurisdiction": None}
            ])

        def _payment_accounts(self, variables):
            return self._data("getPaymentAccountsV1", [{"paymentAccountId": "pay-123"}])

        def _rate_options(self, variables):
            payload = variables.get("input") or {}
            refus = self._champs_inconnus(payload, "GetRateOptionsInput")
            if refus:
                return refus
            return self._data(
                "getRateOptionsV1", RATE_OPTIONS.get(str(payload.get("locationId", "")), [])
            )

        def _champs_inconnus(self, payload, type_name):
            """Rejette comme le ferait un vrai GraphQL si un champ n'existe pas."""
            inconnus = sorted(set(payload) - set(INPUT_FIELDS.get(type_name, [])))
            if not inconnus:
                return None
            return self._error(
                f'Field "{inconnus[0]}" is not defined by type "{type_name}".'
            )

        def _sessions(self, variables):
            payload = variables.get("input") or {}
            refus = self._champs_inconnus(payload, "GetParkingSessionsInput")
            if refus:
                return refus
            periode = payload.get("periodType")
            if periode not in ("CURRENT", "HISTORIC"):  # énumération stricte, comme en vrai
                return self._error(
                    f'Expected type "PeriodType", found {periode}. '
                    "Valid values: CURRENT, HISTORIC."
                )
            if periode == "HISTORIC":
                return self._data(
                    "getParkingSessionsV1", state.past()[: int(payload.get("limit", 25))]
                )
            return self._data("getParkingSessionsV1", state.active())

        def _create_quotes(self, variables):
            request = (variables.get("requests") or [{}])[0]
            details = request.get("details") or {}
            unit = str(details.get("durationTimeUnit", "Hours")).lower()
            minutes = int(details.get("durationQuantity", 1)) * UNIT_MINUTES.get(unit, 60)
            policy = str(details.get("ratePolicyId") or "")
            location = str(details.get("locationId") or "")

            if location not in RATE_OPTIONS:
                return self._data("createQuotesV1", {"createQuotesResponse": {
                    "quotes": [],
                    "quoteErrors": [{"quoteRequestId": request.get("quoteRequestId"),
                                     "status": "Rejected", "reason": "UnknownLocation"}],
                }})

            quote_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            state.quotes[quote_id] = {
                "minutes": minutes, "policy": policy, "location": location,
                "plate": details.get("licensePlate", PLATE),
                "operation": details.get("parkingQuoteOperation", "Start"),
                "sessionId": details.get("parkingSessionId") or "",
            }
            return self._data("createQuotesV1", {"createQuotesResponse": {
                "quotes": [{
                    "quoteId": quote_id,
                    "details": {
                        "locationId": location,
                        "parkingStartTime": _iso(now),
                        "parkingExpiryTime": _iso(now + timedelta(minutes=minutes)),
                        "totalCost": {"amount": price(policy, minutes), "currency": "EUR"},
                    },
                }],
                "quoteErrors": [],
            }})

        def _quote_of(self, variables):
            payload = variables.get("input") or {}
            if state.require_request_wrapper and "request" not in payload:
                return None, None
            if "request" in payload:  # forme alternative
                payload = payload["request"]
            return payload, state.quotes.get(str(payload.get("quoteId", "")))

        def _start(self, variables):
            payload, quote = self._quote_of(variables)
            if not quote:
                return self._error(
                    'Field "request" of required type "StartParkingRequest!" '
                    "was not provided."
                )
            state.purchases.append({**payload, **quote})

            if state.reject_duplicate and any(
                s["vehicle"]["licensePlate"] == quote["plate"]
                and s["locationId"] == quote["location"]
                for s in state.active()
            ):
                return self._error("An active session already exists for this vehicle")

            if state.swallow_purchases:
                return self._data("startParkingSessionV1", {"parkingSessionResponse": {
                    "parkingSessionId": str(uuid.uuid4()),
                    "expireTime": _iso(datetime.now(timezone.utc)
                                       + timedelta(minutes=quote["minutes"])),
                    "segmentTotalCost": {"amount": 0.0, "currency": "EUR"},
                }})

            session = state.add_session(
                minutes=quote["minutes"], plate=quote["plate"],
                location=quote["location"], rate_policy_id=quote["policy"],
            )
            return self._data("startParkingSessionV1", {"parkingSessionResponse": {
                "parkingSessionId": session["parkingSessionId"],
                "expireTime": session["expireTime"],
                "segmentTotalCost": {
                    "amount": price(quote["policy"], quote["minutes"]), "currency": "EUR"},
            }})

        def _renew(self, variables):
            payload, quote = self._quote_of(variables)
            if not quote:
                return self._error('Field "quoteId" was not provided.')
            state.purchases.append({**payload, **quote})

            target = next(
                (s for s in state.active()
                 if s["parkingSessionId"] == (quote["sessionId"] or payload.get("parkingSessionId"))),
                None,
            )
            if not target:
                return self._error("Session not found or not renewable")
            if state.swallow_purchases:
                return self._data("renewParkingSessionV1", {"parkingSessionResponse": {
                    "parkingSessionId": target["parkingSessionId"],
                    "expireTime": target["expireTime"],
                }})
            target["expireTime"] = _iso(
                datetime.now(timezone.utc) + timedelta(minutes=quote["minutes"])
            )
            return self._data("renewParkingSessionV1", {"parkingSessionResponse": {
                "parkingSessionId": target["parkingSessionId"],
                "expireTime": target["expireTime"],
                "segmentTotalCost": {
                    "amount": price(quote["policy"], quote["minutes"]), "currency": "EUR"},
            }})

        def _extend(self, variables):
            payload, quote = self._quote_of(variables)
            if not quote:
                return self._error('Field "quoteId" was not provided.')
            target = next((s for s in state.active()
                           if s["parkingSessionId"] == quote["sessionId"]), None)
            if not target:
                return self._error("Session not found")
            target["expireTime"] = _iso(
                _parse(target["expireTime"]) + timedelta(minutes=quote["minutes"])
            )
            return self._data("extendParkingSessionV1", {"parkingSessionResponse": {
                "parkingSessionId": target["parkingSessionId"],
                "expireTime": target["expireTime"],
            }})

    return Handler


def _operation_field(query: str) -> str:
    """Nom du champ racine demandé — c'est ce qui identifie l'opération."""
    match = re.search(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*[({]", query)
    return match.group(1) if match else ""


def _fake_jwt() -> str:
    import base64

    def part(payload: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'sub': MEMBER_ID})}.signature"
