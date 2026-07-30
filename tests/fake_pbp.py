"""Faux serveur PayByPhone : rejoue le vrai flux d'API en local.

Il sert à valider la chaîne complète (connexion → tarifs → devis → achat →
vérification → SmartPark) sans toucher au vrai service ni à un vrai compte.

Barème imité de la voirie parisienne (zone 1) : volontairement progressif,
c'est ce qui rend SmartPark intéressant.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACCOUNT_ID = "d6d1817e-98ee-4600-b82b-f1aace2abea5"
PLATE = "AB123CD"

# Prix cumulé (€) pour une durée continue, tarif visiteur.
PROGRESSIVE = {60: 6.0, 120: 12.0, 180: 32.5, 240: 52.5, 300: 63.75, 360: 75.0}

RATE_OPTIONS = {
    "75016": [
        {"name": "Carte Mobilité Inclusion", "type": "CMI", "rateOptionId": "1085252721",
         "maxStayDuration": {"durationType": "Hour", "quantity": 24},
         "acceptedTimeUnits": ["Hours", "Days"], "isDefault": False},
        {"name": "Visiteur", "type": "VIS", "rateOptionId": "75016",
         "maxStayDuration": {"durationType": "Minute", "quantity": 360},
         "acceptedTimeUnits": ["Minutes", "Hours"], "isDefault": True},
    ],
    "75008": [
        {"name": "Visiteur", "type": "VIS", "rateOptionId": "75008",
         "maxStayDuration": {"durationType": "Minute", "quantity": 360},
         "acceptedTimeUnits": ["Minutes", "Hours"], "isDefault": True},
    ],
}

UNIT_MINUTES = {"minutes": 1, "hours": 60, "days": 1440}


def price(rate_option_id: str, minutes: int) -> float:
    """0 € pour la CMI ; barème progressif interpolé sinon."""
    if rate_option_id == "1085252721":
        return 0.0
    steps = sorted(PROGRESSIVE)
    if minutes <= steps[0]:
        return round(PROGRESSIVE[steps[0]] * minutes / steps[0], 2)
    for low, high in zip(steps, steps[1:]):
        if minutes <= high:
            span = high - low
            ratio = (minutes - low) / span
            return round(PROGRESSIVE[low] + ratio * (PROGRESSIVE[high] - PROGRESSIVE[low]), 2)
    last = steps[-1]
    return round(PROGRESSIVE[last] * minutes / last, 2)


class FakePayByPhone:
    """Serveur jetable. `sessions` est l'état de vérité, comme chez PayByPhone."""

    def __init__(self):
        self.sessions: list[dict] = []
        self.purchases: list[dict] = []
        self.swallow_purchases = False  # simule un achat qui "réussit" sans ticket
        self.reject_duplicate = False   # simule une zone qui refuse un 2e ticket
        self.token_calls: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- helpers de test ------------------------------------------------
    def add_session(self, minutes: int, plate: str = PLATE, location: str = "75016",
                    rate_option_id: str = "1085252721") -> dict:
        now = datetime.now(timezone.utc)
        session = {
            "parkingSessionId": str(uuid.uuid4()),
            "locationId": location,
            "vehicle": {"licensePlate": plate, "countryCode": "FR"},
            "startTime": _iso(now),
            "expireTime": _iso(now + timedelta(minutes=minutes)),
            "rateOption": {"rateOptionId": rate_option_id, "type": "CMI"},
        }
        self.sessions.append(session)
        return session

    def active(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [s for s in self.sessions if _parse(s["expireTime"]) > now]

    def past(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [s for s in self.sessions if _parse(s["expireTime"]) <= now]

    def add_past_session(self, hours_ago: int = 24, cost: float = 6.0,
                         plate: str = PLATE, location: str = "75016") -> dict:
        end = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        session = {
            "parkingSessionId": str(uuid.uuid4()),
            "locationId": location,
            "vehicle": {"licensePlate": plate, "countryCode": "FR"},
            "startTime": _iso(end - timedelta(hours=1)),
            "expireTime": _iso(end),
            "rateOption": {"rateOptionId": "75016", "type": "VIS"},
            "totalCost": {"amount": cost, "currency": "EUR"},
        }
        self.sessions.append(session)
        return session


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _make_handler(state: FakePayByPhone):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence
            pass

        # ------------------------------------------------------------ utils
        def _json(self, payload, status: int = 200, headers: dict | None = None):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode() if length else ""
            if not raw:
                return {}
            if raw.lstrip().startswith("{"):
                return json.loads(raw)
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def _authorized(self) -> bool:
            return self.headers.get("Authorization", "").startswith("Bearer ")

        # -------------------------------------------------------------- GET
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            query = {k: v[0] for k, v in parse_qs(url.query).items()}

            if not self._authorized():
                return self._json({"error": "unauthorized"}, 401)

            if path == "/parking/accounts":
                return self._json([{"id": ACCOUNT_ID}])

            if path == f"/parking/accounts/{ACCOUNT_ID}/vehicles":
                return self._json([{"id": "1", "licensePlate": PLATE,
                                    "countryCode": "FR", "type": "car"}])

            if path.startswith("/parking/locations/") and path.endswith("/rateOptions"):
                location = path.split("/")[3]
                return self._json(RATE_OPTIONS.get(location, []))

            if path == f"/parking/accounts/{ACCOUNT_ID}/quote":
                return self._quote(query)

            if path == f"/parking/accounts/{ACCOUNT_ID}/sessions":
                if query.get("periodType") == "Historic":
                    return self._json(state.past()[: int(query.get("limit", 25))])
                return self._json(state.active())

            if path == "/parking/locations":
                number = query.get("advertisedLocationNumber", "")
                return self._json([
                    {"locationId": zone, "name": f"Paris {zone}", "countryCode": "FR",
                     "status": "lotOpen"}
                    for zone in RATE_OPTIONS if number in zone
                ])

            if path.startswith("/events/workflow/"):
                return self._json({"status": "Completed"})

            if path == "/payment/accounts":
                return self._json([{"paymentAccountId": "pay-123"}])

            return self._json({"error": "not found", "path": path}, 404)

        def _quote(self, query):
            unit = query.get("durationTimeUnit", "Hours").lower()
            quantity = int(query.get("durationQuantity", 1))
            minutes = quantity * UNIT_MINUTES.get(unit, 60)
            rate = query.get("rateOptionId", "75016")
            now = datetime.now(timezone.utc)
            return self._json({
                "locationId": query.get("locationId", "75016"),
                "quoteDate": _iso(now),
                "totalCost": {"amount": price(rate, minutes), "currency": "EUR"},
                "parkingStartTime": _iso(now),
                "parkingExpiryTime": _iso(now + timedelta(minutes=minutes)),
                "licensePlate": query.get("licensePlate", PLATE),
            })

        # ------------------------------------------------------------- POST
        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path

            if path == "/token":
                body = self._body()
                state.token_calls.append(body)
                if body.get("grant_type") == "password":
                    if not (body.get("username") and body.get("password")):
                        return self._json({"error": "invalid_grant"}, 400)
                elif body.get("grant_type") == "refresh_token":
                    if body.get("refresh_token") != "valid-refresh":
                        return self._json({"error": "invalid_grant"}, 400)
                return self._json({
                    "access_token": _fake_jwt(),
                    "refresh_token": "valid-refresh",
                    "token_type": "bearer",
                    "expires_in": 3600,
                })

            if not self._authorized():
                return self._json({"error": "unauthorized"}, 401)

            if path == f"/parking/accounts/{ACCOUNT_ID}/sessions":
                return self._start_session()

            return self._json({"error": "not found", "path": path}, 404)

        def _start_session(self):
            body = self._body()
            state.purchases.append(body)

            plate = body.get("licensePlate", "").upper()
            location = str(body.get("locationId"))
            duration = body.get("duration") or {}
            minutes = int(duration.get("quantity", 1)) * UNIT_MINUTES.get(
                str(duration.get("timeUnit", "Hours")).lower(), 60
            )

            if state.reject_duplicate and any(
                s["vehicle"]["licensePlate"] == plate and s["locationId"] == location
                for s in state.active()
            ):
                return self._json(
                    {"error": "An active session already exists for this vehicle"}, 409
                )

            workflow = f"http://127.0.0.1:{self.server.server_address[1]}/events/workflow/{uuid.uuid4()}"

            if state.swallow_purchases:
                # Le cas piégeux : 202 renvoyé, mais aucun ticket créé.
                return self._json({"accepted": True}, 202, {"Location": workflow})

            now = datetime.now(timezone.utc)
            state.sessions.append({
                "parkingSessionId": str(uuid.uuid4()),
                "locationId": location,
                "vehicle": {"licensePlate": plate, "countryCode": "FR"},
                "startTime": _iso(now),
                "expireTime": _iso(now + timedelta(minutes=minutes)),
                "rateOption": {"rateOptionId": str(body.get("rateOptionId")), "type": "VIS"},
                "totalCost": {"amount": price(str(body.get("rateOptionId")), minutes),
                              "currency": "EUR"},
            })
            return self._json({"accepted": True}, 202, {"Location": workflow})

        # -------------------------------------------------------------- PUT
        def do_PUT(self):  # noqa: N802
            url = urlparse(self.path)
            prefix = f"/parking/accounts/{ACCOUNT_ID}/sessions/"
            if not url.path.startswith(prefix):
                return self._json({"error": "not found"}, 404)
            session_id = url.path[len(prefix):]
            body = self._body()
            duration = body.get("duration") or {}
            minutes = int(duration.get("quantity", 1)) * UNIT_MINUTES.get(
                str(duration.get("timeUnit", "Hours")).lower(), 60
            )
            for session in state.sessions:
                if session["parkingSessionId"] == session_id:
                    session["expireTime"] = _iso(
                        _parse(session["expireTime"]) + timedelta(minutes=minutes)
                    )
                    return self._json({"accepted": True}, 202)
            return self._json({"error": "unknown session"}, 404)

    return Handler


def _fake_jwt() -> str:
    import base64

    def part(payload: dict) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'sub': 'member-1'})}.signature"
