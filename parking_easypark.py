"""
Achète un ticket HANDI EasyPark via l'API.
Usage: python parking_easypark.py --zone 75016
"""
import argparse
import logging
import os
import sys
import time
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://app-bff.easyparksystem.net"
PLATE = "AB123CD"
COUNTRY = "FR"

# Durée HANDI : 24h en millisecondes
DURATION_MS = 24 * 60 * 60 * 1000

# Types à essayer dans l'ordre pour trouver le tarif HANDI/PMR
PMR_TYPES = ["PMR_TICKET", "PMR", "HANDI", "HANDICAP", "DISABLED_BADGE", "FREE_PMR"]


def _headers(id_token: str) -> dict:
    return {
        "easypark-application-channel-name": "Android",
        "easypark-application-device-os": "Android Mobile",
        "easypark-application-version-number": "16.5.0",
        "easypark-application-market-country": COUNTRY,
        "easypark-application-phone-number-country": COUNTRY,
        "easypark-application-preferred-language": "fr-FR",
        "easypark-application-install-id": str(uuid.uuid4()),
        "Content-Type": "application/json; charset=UTF-8",
        "Host": "app-bff.easyparksystem.net",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "User-Agent": "okhttp/4.9.3",
        "X-Authorization": f"Bearer {id_token}",
    }


def get_parking_info(id_token: str, zone: str, parking_user_id: str) -> dict:
    now_ms = int(time.time() * 1000)
    end_ms = now_ms + DURATION_MS
    r = requests.post(
        BASE_URL + f"/android/api/parkingarea/{COUNTRY}/{zone}/parkinginformation",
        headers=_headers(id_token),
        json={
            "carCountryCode": COUNTRY,
            "carLicenseNumber": PLATE,
            "endDate": end_ms,
            "parkingAreaCountryCode": COUNTRY,
            "parkingAreaNo": int(zone),
            "parkingType": "NORMAL_TIME",
            "parkingUserId": int(parking_user_id),
            "startDate": str(now_ms),
        },
    )
    log.info("parkingInformation status : %s", r.status_code)
    if r.status_code != 200:
        log.warning("Réponse brute parkingInformation : %s", r.text)
    return r.json() if r.ok else {}


def find_pmr_type(info: dict) -> str | None:
    """Cherche le type HANDI/PMR dans la réponse parkingInformation."""
    for key in ["parkingTypes", "tariffs", "rates", "options"]:
        items = info.get(key, [])
        for item in items:
            val = str(item).upper()
            if any(t in val for t in ["PMR", "HANDI", "DISABLED", "HANDICAP", "FREE"]):
                log.info("Type PMR trouvé : %s", item)
                return item if isinstance(item, str) else item.get("type") or item.get("code")
    log.warning("Type PMR non trouvé dans parkingInformation. Réponse : %s", info)
    return None


def start_parking(id_token: str, zone: str, parking_user_id: str, parking_type: str) -> bool:
    now_ms = int(time.time() * 1000)
    end_ms = now_ms + DURATION_MS
    r = requests.post(
        BASE_URL + "/android/api/parking/start?isAutomotive=false",
        headers=_headers(id_token),
        json={
            "carCountryCode": COUNTRY,
            "carLicenseNumber": PLATE,
            "endDate": end_ms,
            "insufficientBalanceAllowed": False,
            "parkingAreaCountryCode": COUNTRY,
            "parkingAreaNo": int(zone),
            "parkingType": parking_type,
            "parkingUserId": int(parking_user_id),
        },
    )
    log.info("parking/start status : %s | type=%s", r.status_code, parking_type)
    if r.ok:
        data = r.json()
        parking_id = data.get("id") or data.get("parkingId")
        log.info("✅ Ticket OK — zone %s | id=%s | expire +24h", zone, parking_id)
        return True
    log.warning("Réponse brute parking/start : %s", r.text)
    return False


def main():
    id_token = os.environ["EP_ID_TOKEN"]
    parking_user_id = os.environ["EP_PARKING_USER_ID"]

    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", required=True, choices=["75016", "75007"])
    args = parser.parse_args()
    zone = args.zone

    # 1. Récupérer les infos de la zone pour trouver le type HANDI
    log.info("Récupération des infos zone %s…", zone)
    info = get_parking_info(id_token, zone, parking_user_id)
    log.info("parkingInformation : %s", info)

    # 2. Chercher le type PMR dans la réponse, sinon tester les types connus
    pmr_type = find_pmr_type(info)
    if pmr_type:
        if not start_parking(id_token, zone, parking_user_id, pmr_type):
            log.error("❌ Échec avec type %s", pmr_type)
            sys.exit(1)
        return

    # Fallback : essayer les types connus un par un
    log.info("Tentative avec les types PMR connus : %s", PMR_TYPES)
    for t in PMR_TYPES:
        if start_parking(id_token, zone, parking_user_id, t):
            return

    log.error("❌ Aucun type PMR n'a fonctionné pour la zone %s", zone)
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error("❌ Échec : %s", e)
        sys.exit(1)
