"""
Achète un ticket HANDI PayByPhone via Playwright (vrai navigateur).
Usage: python parking.py --zone 75016
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

SCREENSHOTS = Path("screenshots")


async def shot(page, name):
    SCREENSHOTS.mkdir(exist_ok=True)
    path = SCREENSHOTS / f"{datetime.now():%H%M%S}_{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    log.info("📸 %s", path.name)


async def buy_ticket(zone: str):
    username = os.environ["PBP_USERNAME"]
    password = os.environ["PBP_PASSWORD"]
    plate    = os.environ["PBP_PLATE"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=os.getenv("HEADLESS", "true") == "true")
        ctx = await browser.new_context(
            locale="fr-FR",
            viewport={"width": 412, "height": 915},
            user_agent="Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
        )
        page = await ctx.new_page()

        try:
            # 1. Aller sur PayByPhone
            log.info("Ouverture de PayByPhone…")
            await page.goto("https://m.paybyphone.com/", wait_until="networkidle", timeout=30000)
            await shot(page, "01_accueil")

            # 2. Accepter les cookies si bannière
            try:
                await page.get_by_role("button", name="Accepter").click(timeout=3000)
            except PlaywrightTimeoutError:
                pass

            # 3. Connexion : entrer le numéro de téléphone
            log.info("Saisie du numéro…")
            phone_input = page.locator("input[type='tel'], input[name*='phone'], input[id*='phone'], input[name*='username']").first
            await phone_input.fill(username, timeout=10000)
            await shot(page, "02_username")
            await page.get_by_role("button", name=lambda n: n and any(x in n.lower() for x in ["continuer", "suivant", "next", "connexion"])).first.click()

            # 4. Entrer le mot de passe
            log.info("Saisie du mot de passe…")
            pw_input = page.locator("input[type='password']").first
            await pw_input.fill(password, timeout=10000)
            await shot(page, "03_password")
            await page.get_by_role("button", name=lambda n: n and any(x in n.lower() for x in ["connexion", "sign in", "se connecter", "valider"])).first.click()

            await page.wait_for_load_state("networkidle", timeout=20000)
            await shot(page, "04_apres_login")

            # 5. Démarrer une session de stationnement
            log.info("Nouvelle session…")
            await page.get_by_role("button", name=lambda n: n and "stationner" in n.lower()).first.click(timeout=10000)
            await shot(page, "05_nouvelle_session")

            # 6. Entrer la zone
            log.info("Zone %s…", zone)
            zone_input = page.locator("input[type='search'], input[placeholder*='zone'], input[placeholder*='code']").first
            await zone_input.fill(zone, timeout=10000)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle")
            await shot(page, "06_zone")

            # 7. Sélectionner le premier résultat
            await page.get_by_text(zone).first.click(timeout=10000)
            await shot(page, "07_zone_selected")

            # 8. Sélectionner le tarif HANDI
            log.info("Sélection HANDI…")
            await page.get_by_text(lambda t: t and "handi" in t.lower()).first.click(timeout=10000)
            await shot(page, "08_handi")

            # 9. Continuer
            await page.get_by_role("button", name=lambda n: n and any(x in n.lower() for x in ["continuer", "suivant", "next"])).first.click()
            await page.wait_for_load_state("networkidle")
            await shot(page, "09_summary")

            # 10. Stationner
            log.info("Confirmation…")
            await page.get_by_role("button", name=lambda n: n and "stationner" in n.lower()).first.click(timeout=10000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await shot(page, "10_done")

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
