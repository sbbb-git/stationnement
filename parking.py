"""
Achète un ticket Flowbird via l'API directement (pas de navigateur).
Usage : python parking.py --zone 75016
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import unquote

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BASE = "https://my.flowbirdapp.com"
VERSION = "2.42.0+1771"

ZONES = {
    "75016": {
        "pos": "http://api.whooshstore.com/tm/whooshstore.com/parkFacility/v1/1966/PoS/v1/36961401/",
        "posLabel": "77 AVENUE FOCH (16F)",
    },
    "75008": {
        "pos": os.getenv("FLOWBIRD_POS_75008", ""),
        "posLabel": os.getenv("FLOWBIRD_POSLABEL_75008", ""),
    },
}


def make_session():
    s = requests.Session()
    user_cookie = unquote(os.environ["FLOWBIRD_USER_COOKIE"])
    phpsessid = os.environ["FLOWBIRD_PHPSESSID"]
    s.cookies.set("user", user_cookie, domain="my.flowbirdapp.com")
    s.cookies.set("PHPSESSID", phpsessid, domain="my.flowbirdapp.com")
    s.cookies.set("serverflb", "apachen1", domain="my.flowbirdapp.com")
    s.headers.update({
        "X-Mpp-Brand": "flowbird",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://my.flowbirdapp.com",
        "Referer": "https://my.flowbirdapp.com/",
        "Accept-Language": "fr",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    })
    log.info("Session prête avec PHPSESSID=%s…", phpsessid[:8])
    return s


def rt():
    return int(time.time() * 1000)


def create_ticket(s, zone):
    cfg = ZONES[zone]
    if not cfg["pos"]:
        raise ValueError(f"Zone {zone} non configurée — ajoute FLOWBIRD_POS_{zone} dans les secrets.")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "author": os.environ["FLOWBIRD_EMAIL"],
        "channel": "web",
        "class": "hourly",
        "duration": "PT1M",
        "freeDuration": "PT0S",
        "paidDuration": "PT0S",
        "platform": "europe",
        "pos": cfg["pos"],
        "posLabel": cfg["posLabel"],
        "preferredLanguage": "fr",
        "space": None,
        "startTime": now,
        "usertype": "9",
        "usertypeLabel": "HANDI",
        "vehicle": {
            "id": int(os.environ["FLOWBIRD_VEHICLE_ID"]),
            "plate": os.environ["FLOWBIRD_PLATE"],
            "default": True,
            "category": "pmr",
            "country-plate": "FR",
        },
    }

    log.info("Création du ticket — zone %s…", zone)
    r = s.post(
        f"{BASE}/order/create",
        params={"platform": "europe", "rt": rt(), "version": VERSION},
        json=payload,
    )
    log.info("Réponse serveur : %s — %s", r.status_code, r.text[:500])
    r.raise_for_status()
    data = r.json()
    order_id = data["id"]
    log.info("Ticket créé, id=%s", order_id)
    return order_id


def confirm_ticket(s, order_id):
    log.info("Confirmation du ticket %s…", order_id)
    r = s.post(
        f"{BASE}/order/confirm",
        params={
            "id": order_id,
            "platform": "europe",
            "reminderDelay": "PT5M",
            "rt": rt(),
            "version": VERSION,
        },
        json={"alertProposals": {}},
    )
    r.raise_for_status()
    log.info("✅ Ticket confirmé !")
    return r.json()


def run(zone):
    s = make_session()
    order_id = create_ticket(s, zone)
    confirm_ticket(s, order_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", required=True, choices=["75016", "75008"])
    args = parser.parse_args()

    try:
        run(args.zone)
    except Exception as e:
        log.error("❌ Échec : %s", e)
        sys.exit(1)
