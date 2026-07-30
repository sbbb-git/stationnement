"""Client EasyPark (secondaire).

AlloValet ne pilote que PayByPhone ; ce module existe parce que le compte
peut être chez EasyPark. Il expose la même interface que `PayByPhoneClient`
pour que `Runner` fonctionne à l'identique.

Limite connue : l'authentification EasyPark passe par un code SMS, elle ne peut
donc pas être automatisée en tâche planifiée. On stocke l'`idToken` obtenu une
fois via `allovalet easypark-login` (il est de longue durée).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta

from .errors import ApiError, AuthError, NotEligibleError
from .http import HttpClient
from .models import ParkingSession, Quote, RateOption, Vehicle, parse_dt, utcnow
from .paybyphone import Duration

log = logging.getLogger("allovalet.easypark")

BASE_URL = "https://app-bff.easyparksystem.net"

ONGOING_PATHS = [
    "/android/api/parking/ongoing",
    "/android/api/parkings/ongoing",
    "/android/api/parking/active",
    "/android/api/parking",
]

PMR_HINTS = ("PMR", "HANDI", "DISABLED", "CMI", "GIG", "GIC")


class EasyParkClient:
    def __init__(
        self,
        id_token: str,
        parking_user_id: str,
        country: str = "FR",
        install_id: str | None = None,
    ):
        if not id_token or not parking_user_id:
            raise AuthError("EP_ID_TOKEN / EP_PARKING_USER_ID manquants.")
        self.id_token = id_token
        self.parking_user_id = str(parking_user_id)
        self.country = country
        self.install_id = install_id or str(uuid.uuid4())
        self.http = HttpClient(BASE_URL)
        self._ongoing_path: str | None = None

    # ------------------------------------------------------------------ base

    def _headers(self) -> dict:
        return {
            "easypark-application-channel-name": "Android",
            "easypark-application-device-os": "Android Mobile",
            "easypark-application-version-number": "16.5.0",
            "easypark-application-market-country": self.country,
            "easypark-application-phone-number-country": self.country,
            "easypark-application-preferred-language": "fr-FR",
            "easypark-application-install-id": self.install_id,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "okhttp/4.9.3",
            "X-Authorization": f"Bearer {self.id_token}",
        }

    def authenticate(self) -> None:
        """Le token est de longue durée : on vérifie juste qu'il répond."""
        self.current_sessions()

    def account_id(self) -> str:
        return self.parking_user_id

    def payment_account_id(self):
        return None  # EasyPark utilise le moyen de paiement par défaut du compte

    def vehicles(self) -> list[Vehicle]:
        resp = self.http.get("/android/api/vehicles", headers=self._headers())
        if not resp.ok:
            return []
        items = resp.json()
        items = items if isinstance(items, list) else items.get("vehicles", [])
        return [
            Vehicle(
                id=str(v.get("id", "")),
                plate=str(v.get("licenseNumber") or v.get("licensePlate") or "").upper(),
                country=v.get("countryCode"),
                raw=v,
            )
            for v in items
        ]

    # ------------------------------------------------------------- zones

    def _parking_information(self, location_id: str, plate: str, minutes: int) -> dict:
        now_ms = int(time.time() * 1000)
        end_ms = now_ms + minutes * 60 * 1000
        resp = self.http.post(
            f"/android/api/parkingarea/{self.country}/{location_id}/parkinginformation",
            headers=self._headers(),
            json={
                "carCountryCode": self.country,
                "carLicenseNumber": plate,
                "endDate": end_ms,
                "parkingAreaCountryCode": self.country,
                "parkingAreaNo": int(location_id),
                "parkingType": "NORMAL_TIME",
                "parkingUserId": int(self.parking_user_id),
                "startDate": str(now_ms),
            },
        )
        if not resp.ok:
            raise ApiError("parkinginformation", resp.status_code, resp.text)
        return resp.json()

    def rate_options(self, location_id: str, plate: str | None = None, start=None):
        info = self._parking_information(location_id, plate or "", 60)
        found: dict[str, RateOption] = {}
        for key in ("parkingTypes", "availableParkingTypes", "tariffs", "rates", "options"):
            for item in info.get(key, []) or []:
                if isinstance(item, str):
                    code, name = item, item
                elif isinstance(item, dict):
                    code = item.get("type") or item.get("code") or item.get("parkingType")
                    name = item.get("name") or item.get("description") or code
                else:
                    continue
                if code:
                    found[str(code)] = RateOption(
                        id=str(code), name=str(name), type=str(code), raw=item if isinstance(item, dict) else {}
                    )
        if not found:
            found["NORMAL_TIME"] = RateOption(
                id="NORMAL_TIME", name="Tarif standard", type="NORMAL_TIME", is_default=True
            )
        return list(found.values())

    def pick_rate_option(self, location_id: str, plate: str, wanted: str | None) -> RateOption:
        options = self.rate_options(location_id, plate)
        if wanted:
            for opt in options:
                if opt.matches(wanted):
                    return opt
            # un tarif PMR n'est pas toujours listé : on l'essaie quand même
            if any(h in wanted.upper() for h in PMR_HINTS):
                log.warning("Tarif « %s » non listé zone %s — tentative directe.", wanted, location_id)
                return RateOption(id=wanted.upper(), name=wanted, type=wanted.upper())
            listing = ", ".join(f"{o.id}" for o in options)
            raise NotEligibleError(
                f"Tarif « {wanted} » indisponible zone {location_id}. Proposés : {listing}"
            )
        for opt in options:
            if opt.is_default:
                return opt
        return options[0]

    def quote(self, location_id, plate, duration: Duration, rate_option_id=None, stall=None,
              session_id=None) -> Quote:
        info = self._parking_information(location_id, plate, duration.minutes)
        price = None
        for key in ("totalCost", "price", "totalPrice", "cost", "amount"):
            value = info.get(key)
            if isinstance(value, dict):
                value = value.get("amount")
            if isinstance(value, (int, float)):
                price = float(value)
                # EasyPark renvoie souvent des centimes
                if price > 1000:
                    price /= 100
                break
        return Quote(
            cost=price if price is not None else 0.0,
            currency=info.get("currency", "EUR"),
            start=utcnow(),
            expiry=utcnow() + timedelta(minutes=duration.minutes),
            raw=info,
        )

    # ------------------------------------------------------------ tickets

    def current_sessions(self) -> list[ParkingSession]:
        paths = [self._ongoing_path] if self._ongoing_path else ONGOING_PATHS
        last: ApiError | None = None
        for path in paths:
            resp = self.http.get(path, headers=self._headers(), retry=False)
            if resp.status_code == 404:
                continue
            if not resp.ok:
                last = ApiError(f"GET {path}", resp.status_code, resp.text)
                continue
            try:
                data = resp.json()
            except ValueError:
                continue
            self._ongoing_path = path
            return [self._to_session(item) for item in _as_list(data)]
        if last:
            raise last
        raise ApiError(
            "Impossible de lister les tickets EasyPark en cours "
            f"(essayé : {', '.join(ONGOING_PATHS)})."
        )

    @staticmethod
    def _to_session(item: dict) -> ParkingSession:
        return ParkingSession(
            id=str(item.get("id") or item.get("parkingId") or ""),
            plate=str(item.get("carLicenseNumber") or item.get("licenseNumber") or "").upper(),
            location_id=str(item.get("parkingAreaNo") or item.get("parkingArea") or ""),
            start=parse_dt(item.get("startDate") or item.get("startTime")),
            expiry=parse_dt(item.get("endDate") or item.get("endTime")),
            rate_type=item.get("parkingType"),
            raw=item,
        )

    def find_active(self, plate, location_id=None, sessions=None):
        plate = plate.upper().replace(" ", "")
        best = None
        for sess in sessions if sessions is not None else self.current_sessions():
            if sess.plate and sess.plate != plate:
                continue
            if location_id and sess.location_id and str(sess.location_id) != str(location_id):
                continue
            if not sess.expiry or sess.expiry <= utcnow():
                continue
            if best is None or sess.expiry > best.expiry:
                best = sess
        return best

    def start_session(self, location_id, plate, duration: Duration, rate_option_id,
                      stall=None, payment_account_id=None, start_time=None, verify=True):
        now_ms = int(time.time() * 1000)
        end_ms = now_ms + duration.minutes * 60 * 1000
        resp = self.http.post(
            "/android/api/parking/start?isAutomotive=false",
            headers=self._headers(),
            json={
                "carCountryCode": self.country,
                "carLicenseNumber": plate,
                "endDate": end_ms,
                "insufficientBalanceAllowed": False,
                "parkingAreaCountryCode": self.country,
                "parkingAreaNo": int(location_id),
                "parkingType": rate_option_id,
                "parkingUserId": int(self.parking_user_id),
            },
        )
        if not resp.ok:
            raise ApiError("Achat EasyPark refusé", resp.status_code, resp.text)

        if not verify:
            return ParkingSession(
                id="", plate=plate, location_id=str(location_id),
                start=utcnow(), expiry=utcnow() + timedelta(minutes=duration.minutes),
            )
        for attempt in range(6):
            time.sleep(2 if attempt else 1)
            found = self.find_active(plate, str(location_id))
            if found:
                log.info("Ticket confirmé ✅ %s", found.describe())
                return found
        raise ApiError(
            f"Ticket EasyPark non confirmé pour {plate} zone {location_id} après achat."
        )

    def extend_session(self, session_id, duration: Duration, payment_account_id=None):
        raise ApiError("EasyPark : prolongation non supportée — un nouveau ticket est pris.")


def _as_list(data):
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("parkings", "ongoing", "items", "sessions", "content"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        if data.get("id") or data.get("parkingId"):
            return [data]
    return []
