"""
Achète un ticket de stationnement Flowbird automatiquement.
Usage : python parking.py --zone 75016
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

FLOWBIRD_URL = "https://my.flowbirdapp.com/#/Parking"
SCREENSHOTS = Path("screenshots")


async def screenshot(page, nom):
    SCREENSHOTS.mkdir(exist_ok=True)
    path = SCREENSHOTS / f"{datetime.now():%H%M%S}_{nom}.png"
    await page.screenshot(path=str(path), full_page=True)
    log.info("📸 Screenshot : %s", path)


async def acheter_ticket(zone: str):
    email = os.environ["FLOWBIRD_EMAIL"]
    password = os.environ["FLOWBIRD_PASSWORD"]
    headless = os.getenv("HEADLESS", "true") == "true"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        page = await browser.new_page(
            viewport={"width": 1280, "height": 800},
            locale="fr-FR",
        )

        try:
            # 1. Ouvrir Flowbird
            log.info("Ouverture de Flowbird…")
            await page.goto(FLOWBIRD_URL, wait_until="networkidle")
            await screenshot(page, "01_accueil")

            # 2. Connexion
            log.info("Connexion…")
            await page.fill("input[type='email']", email)
            await page.fill("input[type='password']", password)
            await page.click("button[type='submit']")
            await page.wait_for_load_state("networkidle")
            await screenshot(page, "02_apres_login")

            # 3. Sélectionner la voiture (la première = ta seule voiture)
            log.info("Sélection de la voiture…")
            await page.click(".vehicle-item:first-child, [data-testid='vehicle']:first-child", timeout=8000)
            await screenshot(page, "03_voiture")

            # 4. Saisir la zone
            log.info("Zone : %s", zone)
            await page.fill("input[placeholder*='zone'], input[name*='zone']", zone)
            await screenshot(page, "04_zone")

            # 5. Mettre 1 minute
            log.info("Durée : 1 minute")
            await page.fill("input[name*='duration'], input[placeholder*='durée']", "1")
            await screenshot(page, "05_duree")

            # 6. Valider
            log.info("Validation…")
            await page.click("button[type='submit'], button:has-text('Confirmer'), button:has-text('Payer')")
            await page.wait_for_load_state("networkidle")
            await screenshot(page, "06_confirmation")

            log.info("✅ Ticket acheté — zone %s", zone)

        except Exception as e:
            await screenshot(page, "ERREUR")
            log.error("❌ Échec : %s", e)
            await browser.close()
            sys.exit(1)

        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", required=True, help="Ex: 75016 ou 75008")
    args = parser.parse_args()
    asyncio.run(acheter_ticket(args.zone))
