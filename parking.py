"""
Achète un ticket HANDI PayByPhone via l'API GraphQL.
Usage: python parking.py --zone 75016
"""

import argparse
import logging
import os
import subprocess
import sys
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

GRAPHQL_URL = "https://consumer.paybyphoneapis.com/uapi/graphql"
TOKEN_URL = "https://auth.paybyphoneapis.com/token"
REPO = "sachabitoun17-ctrl/stationnement"
RATE_POLICY = {
    "75016": "1085252721",
    "75007": "312941064",
}

MUTATION = """
mutation CreateQuotesV1($requests: [QuoteRequestInput!]!) {
  createQuotesV1(input: {requests: $requests}) {
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


def get_access_token():
    log.info("Connexion avec email/mot de passe…")
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "username": os.environ["PBP_EMAIL"],
            "password": os.environ["PBP_PASSWORD"],
            "client_id": "paybyphone_web",
            "scope": "paybyphone offline_access",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://m.paybyphone.com",
            "Referer": "https://m.paybyphone.com/",
            "X-Pbp-Clienttype": "WebApp",
            "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
        },
    )
    log.info("Token status : %s — %s", r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()["access_token"]


def start_parking(access_token, zone):
    log.info("Démarrage stationnement zone %s…", zone)
    payload = {
        "operationName": None,
        "variables": {
            "requests": [{
                "quoteRequestId": str(uuid.uuid4()),
                "product": "PARKING",
                "details": {
                    "locationId": zone,
                    "advertisedLocationId": zone,
                    "ratePolicyId": RATE_POLICY[zone],
                    "parkingQuoteOperation": "Start",
                    "durationTimeUnit": "Hours",
                    "durationQuantity": "1",
                    "licensePlate": os.environ["PBP_PLATE"],
                    "stall": "",
                    "parkingSessionId": "",
                    "paymentAccountId": "",
                    "paymentCardType": "",
                    "paymentScope": "Private",
                },
            }]
        },
        "query": MUTATION,
    }

    r = requests.post(
        GRAPHQL_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://m.paybyphone.com",
            "Referer": "https://m.paybyphone.com/",
            "X-Pbp-Clienttype": "WebApp",
        },
    )
    log.info("GraphQL status : %s", r.status_code)
    r.raise_for_status()
    data = r.json()

    response = data.get("data", {}).get("createQuotesV1", {}).get("createQuotesResponse", {})
    errors = response.get("quoteErrors", [])
    if errors:
        raise RuntimeError(f"Erreur PayByPhone : {errors}")

    quotes = response.get("quotes", [])
    if not quotes:
        log.error("Réponse complète : %s", data)
        raise RuntimeError("Aucun ticket dans la réponse.")

    details = quotes[0]["details"]
    log.info("✅ Ticket OK — zone %s, expire %s", zone, details.get("parkingExpiryTime"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", required=True, choices=["75016", "75007"])
    args = parser.parse_args()

    try:
        token = get_access_token()
        start_parking(token, args.zone)
    except Exception as e:
        log.error("❌ Échec : %s", e)
        sys.exit(1)
