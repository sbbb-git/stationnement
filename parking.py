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
    log.info("Renouvellement du token…")
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["PBP_REFRESH_TOKEN"],
            "client_id": "paybyphone_web",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://m.paybyphone.com",
            "Referer": "https://m.paybyphone.com/",
            "X-Pbp-Clienttype": "WebApp",
            "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
        },
    )
    log.info("Token status : %s", r.status_code)
    r.raise_for_status()
    data = r.json()

    # Sauvegarder le nouveau refresh token dans GitHub Secrets
    new_refresh = data.get("refresh_token")
    if new_refresh and os.getenv("GH_PAT"):
        try:
            subprocess.run(
                ["gh", "secret", "set", "PBP_REFRESH_TOKEN",
                 "--repo", REPO, "--body", new_refresh],
                check=True,
                env={**os.environ, "GH_TOKEN": os.environ["GH_PAT"]},
                capture_output=True,
            )
            log.info("Refresh token mis à jour dans GitHub Secrets ✅")
        except Exception as e:
            log.warning("Impossible de mettre à jour le secret : %s", e)

    log.info("Token OK ✅")
    return data["access_token"]


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
