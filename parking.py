"""
Achète un ticket HANDI PayByPhone via Playwright.
Usage: python parking.py --zone 75016
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

SCREENSHOTS = Path("screenshots")


async def shot(page, name):
    SCREENSHOTS.mkdir(exist_ok=True)
    path = SCREENSHOTS / f"{datetime.now():%H%M%S}_{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    log.info("📸 %s", path.name)


async def accept_cookies(page):
    """Clique sur le bouton de refus/acceptation des cookies si présent."""
    for label in ["Tout refuser", "Autoriser tous les cookies", "Accepter", "Refuser tout"]:
        try:
            await page.get_by_role("button", name=label).click(timeout=2000)
            log.info("🍪 Bandeau cookies : %s", label)
            await page.wait_for_load_state("networkidle", timeout=5000)
            return
        except PWTimeout:
            continue


async def buy_ticket(zone: str):
    username = os.environ["PBP_USERNAME"]
    password = os.environ["PBP_PASSWORD"]
    plate = os.environ["PBP_PLATE"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=os.getenv("HEADLESS", "true") == "true")
        ctx = await browser.new_context(
            locale="fr-FR",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        try:
            # Étape 1 — Page d'accueil
            log.info("Ouverture de PayByPhone…")
            await page.goto("https://m.paybyphone.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await shot(page, "01_accueil")

            # Étape 2 — Cookies
            await accept_cookies(page)
            await shot(page, "02_post_cookies")

            # Étape 3 — Cliquer sur Se connecter si nécessaire
            try:
                await page.get_by_role("button", name=re.compile(r"connect|sign in|se connecter", re.I)).first.click(timeout=3000)
            except PWTimeout:
                pass

            # Étape 4 — Saisir téléphone
            log.info("Saisie du numéro…")
            phone = page.locator("input[type='tel']").first
            await phone.wait_for(state="visible", timeout=15000)
            await phone.fill(username)
            await shot(page, "03_username")

            # Bouton suivant/continuer
            await page.get_by_role("button", name=re.compile(r"continuer|suivant|next", re.I)).first.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await shot(page, "04_apres_username")

            # Étape 5 — Mot de passe
            log.info("Saisie du mot de passe…")
            pw_input = page.locator("input[type='password']").first
            await pw_input.wait_for(state="visible", timeout=15000)
            await pw_input.fill(password)
            await shot(page, "05_password")

            await page.get_by_role("button", name=re.compile(r"connexion|sign in|se connecter|valider|continuer", re.I)).first.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
            await shot(page, "06_apres_login")

            # Étape 6 — Nouvelle session
            log.info("Nouvelle session…")
            await page.get_by_role("button", name=re.compile(r"stationner|nouvelle.*session|park", re.I)).first.click(timeout=10000)
            await page.wait_for_load_state("networkidle")
            await shot(page, "07_nouvelle")

            # Étape 7 — Zone
            log.info("Zone %s…", zone)
            zone_input = page.locator("input[type='search'], input[type='text']").first
            await zone_input.fill(zone)
            await asyncio.sleep(2)
            await shot(page, "08_zone_saisie")

            # Cliquer sur le résultat
            await page.get_by_text(re.compile(rf"\b{zone}\b")).first.click(timeout=10000)
            await page.wait_for_load_state("networkidle")
            await shot(page, "09_zone_choisie")

            # Étape 8 — Tarif HANDI
            log.info("Tarif HANDI…")
            await page.get_by_text(re.compile(r"handi|cmi", re.I)).first.click(timeout=10000)
            await shot(page, "10_handi")

            # Continuer
            await page.get_by_role("button", name=re.compile(r"continuer|suivant", re.I)).first.click()
            await page.wait_for_load_state("networkidle")
            await shot(page, "11_recap")

            # Étape 9 — Stationner
            log.info("Confirmation…")
            await page.get_by_role("button", name=re.compile(r"stationner", re.I)).first.click(timeout=10000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await shot(page, "12_done")

            log.info("✅ Ticket acheté — zone %s", zone)

        except Exception as e:
            log.error("❌ Échec : %s", e)
            try:
                await shot(page, "ERREUR")
            except Exception:
                pass
            await browser.close()
            sys.exit(1)

        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", required=True, choices=["75016", "75007"])
    args = parser.parse_args()
    asyncio.run(buy_ticket(args.zone))
