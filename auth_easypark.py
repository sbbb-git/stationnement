"""
Authentification EasyPark one-time — à lancer manuellement.
Génère idToken + parkingUserId et les pousse dans GitHub Secrets.

Usage:
    python auth_easypark.py
"""
import json
import os
import subprocess
import sys
import uuid

import requests

BASE_URL = "https://app-bff.easyparksystem.net"
REPO = "sachabitoun17-ctrl/stationnement"

HEADERS = {
    "easypark-application-channel-name": "Android",
    "easypark-application-device-os": "Android Mobile",
    "easypark-application-version-number": "16.5.0",
    "easypark-application-build-number": "1605001",
    "easypark-application-device-os-version": "29",
    "easypark-application-market-country": "FR",
    "easypark-application-phone-number-country": "FR",
    "easypark-application-preferred-language": "fr-FR",
    "easypark-application-install-id": str(uuid.uuid4()),
    "Content-Type": "application/json; charset=UTF-8",
    "Host": "app-bff.easyparksystem.net",
    "Connection": "Keep-Alive",
    "Accept-Encoding": "gzip",
    "User-Agent": "okhttp/4.9.3",
}

SECURE_INSTALL_ID = str(uuid.uuid4())


def check_account(phone: str):
    r = requests.post(
        BASE_URL + "/android/api/account/exists",
        headers=HEADERS,
        json={"phoneNumber": phone, "canSplitTerms": True},
    )
    r.raise_for_status()
    data = r.json()
    print(f"Compte trouvé : {data}")
    return data.get("isKnownUser", False)


def request_code(phone: str):
    r = requests.post(
        BASE_URL + "/android/api/account/requestVerificationCode",
        headers=HEADERS,
        json={"loginId": "", "phoneNumber": phone},
    )
    r.raise_for_status()
    print(f"Code SMS envoyé au {phone}")


def login_with_code(phone: str, code: str):
    r = requests.post(
        BASE_URL + "/android/api/account/loginWithVerificationCode",
        headers=HEADERS,
        json={
            "countryCode": "FR",
            "phoneNumber": phone,
            "secureInstallId": SECURE_INSTALL_ID,
            "verificationCode": code,
        },
    )
    r.raise_for_status()
    data = r.json()
    print(f"Action : {data.get('action')}")

    action = data.get("action", "")

    # Login direct sans 2FA
    if "main" in action:
        return extract_credentials(data)

    # 2FA par plaque d'immatriculation
    if "multiFactorVerification" in action:
        pending = action.split("pendingAccessToken=")[-1].split("&")[0]
        plate = input("Vérification supplémentaire — entrez la plaque d'immatriculation : ").upper()
        return verify_with_plate(phone, plate, pending)

    print(f"Action inconnue : {action}")
    print(f"Réponse complète : {json.dumps(data, indent=2)}")
    sys.exit(1)


def verify_with_plate(phone: str, plate: str, pending_token: str):
    r = requests.post(
        BASE_URL + "/account/verifyAccountWithLicensePlateNumber",
        headers=HEADERS,
        json={
            "licensePlateNumber": plate,
            "pendingAccessToken": pending_token,
            "phoneNumber": phone,
        },
    )
    r.raise_for_status()
    return extract_credentials(r.json())


def extract_credentials(data: dict) -> dict:
    id_token = data["sso"]["idToken"]
    parking_user_id = str(data["status"]["accounts"][0]["parkingUserId"])
    return {"idToken": id_token, "parkingUserId": parking_user_id}


def push_secrets(creds: dict):
    gh_token = os.getenv("GH_TOKEN") or os.getenv("GH_PAT")
    if not gh_token:
        print("\nPas de GH_TOKEN/GH_PAT — secrets non poussés sur GitHub.")
        print("Sauvegarde locale dans easypark_creds.json")
        with open("easypark_creds.json", "w") as f:
            json.dump(creds, f)
        return

    env = {**os.environ, "GH_TOKEN": gh_token}
    for key, value in [
        ("EP_ID_TOKEN", creds["idToken"]),
        ("EP_PARKING_USER_ID", creds["parkingUserId"]),
    ]:
        subprocess.run(
            ["gh", "secret", "set", key, "--repo", REPO, "--body", value],
            check=True,
            env=env,
        )
        print(f"Secret {key} mis à jour ✅")


def main():
    phone = input("Numéro de téléphone EasyPark (ex: +33619878096) : ").strip()

    if not check_account(phone):
        print("Compte introuvable pour ce numéro.")
        sys.exit(1)

    request_code(phone)
    code = input("Code SMS reçu : ").strip()

    creds = login_with_code(phone, code)
    print(f"\n✅ Authentifié — parkingUserId : {creds['parkingUserId']}")

    push_secrets(creds)
    print("\nTerminé. Vous pouvez maintenant lancer parking.py")


if __name__ == "__main__":
    main()
