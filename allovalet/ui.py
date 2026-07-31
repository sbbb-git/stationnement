"""Interface locale : voir l'état, modifier les automatisations.

    python -m allovalet ui        →  http://127.0.0.1:8787

Volontairement bâtie sur la bibliothèque standard — aucune dépendance de plus,
aucun service à héberger. Elle tourne sur la machine, avec les identifiants du
`.env` : rien n'est exposé sur Internet, et le fichier modifié est bien
`config.yml`, celui que lit GitHub Actions.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from .config import Config
from .errors import AlloValetError
from .etat import snapshot
from .notify import Notifier
from .providers import build_client
from .runner import Runner
from .state import State

log = logging.getLogger("allovalet.ui")


def serve(config_path: str, port: int = 8787, ouvrir: bool = True) -> int:
    adresse = ("127.0.0.1", port)
    serveur = ThreadingHTTPServer(adresse, _handler(Path(config_path)))
    url = f"http://{adresse[0]}:{serveur.server_address[1]}"
    print(f"\nInterface : {url}   (Ctrl-C pour arrêter)\n")
    if ouvrir:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        serveur.server_close()
    return 0


# ------------------------------------------------------------------- routes


def _handler(config_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "allovalet"

        def log_message(self, *args):  # pas de bruit dans le terminal
            pass

        # -- lecture ----------------------------------------------------

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/etat"):
                return self._json(self._etat())
            if self.path.startswith("/api/config"):
                return self._json({"texte": config_path.read_text(encoding="utf-8")})
            if self.path in ("/", "/index.html"):
                return self._html(PAGE)
            self.send_error(404)

        # -- écriture ---------------------------------------------------

        def do_POST(self):  # noqa: N802
            corps = self._corps()
            if self.path.startswith("/api/config"):
                return self._json(self._enregistrer(corps.get("texte", "")))
            if self.path.startswith("/api/passage"):
                return self._json(self._passage(bool(corps.get("simulation", True))))
            self.send_error(404)

        # -- actions ----------------------------------------------------

        def _etat(self) -> dict:
            try:
                cfg = Config.load(config_path)
            except AlloValetError as exc:
                return {"erreur": f"config.yml : {exc}", "regles": [], "tickets": [],
                        "passages": []}
            state = State()
            try:
                client = build_client(cfg, state)
            except AlloValetError as exc:
                return {"erreur": str(exc), "regles": [], "tickets": [], "passages": []}
            return snapshot(cfg, client, state)

        def _enregistrer(self, texte: str) -> dict:
            """Relit la config avant d'écrire : une config cassée n'est jamais
            enregistrée, sinon le prochain passage automatique échouerait."""
            try:
                yaml.safe_load(texte)
            except yaml.YAMLError as exc:
                return {"ok": False, "erreur": f"YAML invalide : {exc}"}
            brouillon = config_path.with_suffix(".verif.yml")
            try:
                brouillon.write_text(texte, encoding="utf-8")
                cfg = Config.load(brouillon)
            except AlloValetError as exc:
                return {"ok": False, "erreur": str(exc)}
            finally:
                brouillon.unlink(missing_ok=True)

            config_path.write_text(texte, encoding="utf-8")
            return {
                "ok": True,
                "message": f"Enregistré — {len(cfg.rules)} règle(s). "
                           "À pousser sur GitHub pour que l'automatisation en tienne compte.",
            }

        def _passage(self, simulation: bool) -> dict:
            try:
                cfg = Config.load(config_path)
                state = State()
                client = build_client(cfg, state)
                rapport = Runner(cfg, client, state, Notifier(cfg.notify),
                                 dry_run=simulation).tick()
            except AlloValetError as exc:
                return {"ok": False, "lignes": [str(exc)]}
            return {
                "ok": not rapport.failures,
                "lignes": [r.line() for r in rapport.results] or ["aucune règle active"],
            }

        # -- plomberie --------------------------------------------------

        def _corps(self) -> dict:
            taille = int(self.headers.get("Content-Length") or 0)
            if not taille:
                return {}
            try:
                return json.loads(self.rfile.read(taille) or b"{}")
            except ValueError:
                return {}

        def _json(self, payload: dict):
            self._repondre(json.dumps(payload).encode("utf-8"), "application/json")

        def _html(self, texte: str):
            self._repondre(texte.encode("utf-8"), "text/html; charset=utf-8")

        def _repondre(self, corps: bytes, mime: str):
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

    return Handler


# --------------------------------------------------------------------- page

PAGE = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stationnement</title>
<style>
  :root {
    --fond: #0f1115; --carte: #171a21; --trait: #262b36;
    --texte: #e8eaf0; --doux: #9aa3b2; --ok: #35c98a; --alerte: #ff6b6b;
    --repli: #f0b429; --accent: #5b8def;
  }
  @media (prefers-color-scheme: light) {
    :root { --fond:#f5f6f8; --carte:#fff; --trait:#e2e5ea; --texte:#1a1d23; --doux:#6b7280; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--fond); color:var(--texte); font:15px/1.5 -apple-system,
         BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .page { max-width: 900px; margin: 0 auto; padding: 24px 18px 60px; }
  h1 { font-size: 20px; margin: 0 0 2px; }
  .sous { color: var(--doux); font-size: 13px; margin-bottom: 22px; }
  .carte { background:var(--carte); border:1px solid var(--trait); border-radius:12px;
           padding:16px 18px; margin-bottom:14px; }
  .titre { display:flex; align-items:center; gap:10px; font-weight:600; }
  .pastille { width:9px; height:9px; border-radius:50%; flex:none; }
  .gros { font-size:26px; font-weight:600; margin:10px 0 2px; }
  .doux { color: var(--doux); font-size:13px; }
  .zones { display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; }
  .zone { border:1px solid var(--trait); border-radius:999px; padding:3px 10px;
          font-size:12px; color:var(--doux); }
  .zone.pref { border-color:var(--accent); color:var(--texte); }
  .zone.active { background:var(--ok); border-color:var(--ok); color:#04140d; font-weight:600; }
  .fleche { color:var(--doux); font-size:12px; align-self:center; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--trait); }
  th { color:var(--doux); font-weight:500; font-size:12px; }
  textarea { width:100%; min-height:340px; background:var(--fond); color:var(--texte);
             border:1px solid var(--trait); border-radius:8px; padding:12px;
             font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; resize:vertical; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px;
           padding:9px 16px; font-size:14px; cursor:pointer; }
  button.doux { background:transparent; color:var(--texte); border:1px solid var(--trait); }
  button:disabled { opacity:.5; cursor:default; }
  .barre { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; }
  .msg { font-size:13px; }
  .msg.ok { color:var(--ok); } .msg.ko { color:var(--alerte); }
  pre { background:var(--fond); border:1px solid var(--trait); border-radius:8px;
        padding:12px; overflow:auto; font-size:12.5px; margin:12px 0 0; }
  .bandeau { border:1px solid var(--alerte); color:var(--alerte); border-radius:10px;
             padding:12px 14px; margin-bottom:16px; font-size:13.5px; }
</style>
</head><body><div class="page">

<h1>Stationnement</h1>
<div class="sous" id="sous">chargement…</div>
<div id="bandeau"></div>
<div id="regles"></div>

<div class="carte">
  <div class="titre">Tickets en cours</div>
  <table id="tickets"></table>
</div>

<div class="carte">
  <div class="titre">Automatisations</div>
  <div class="doux" style="margin:6px 0 10px">
    L'ordre des zones est l'ordre des replis : la première est celle qu'on veut,
    les suivantes servent si elle refuse.
  </div>
  <textarea id="config" spellcheck="false"></textarea>
  <div class="barre">
    <button id="save">Enregistrer</button>
    <button class="doux" id="reload">Annuler</button>
    <span class="msg" id="msgconf"></span>
  </div>
</div>

<div class="carte">
  <div class="titre">Lancer un passage</div>
  <div class="barre">
    <button class="doux" id="sim">Simuler</button>
    <button id="vrai">Prendre les tickets maintenant</button>
    <span class="msg" id="msgpass"></span>
  </div>
  <pre id="sortie" hidden></pre>
</div>

<div class="carte">
  <div class="titre">Derniers passages</div>
  <pre id="journal">—</pre>
</div>

<script>
const $ = (id) => document.getElementById(id);
const duree = (m) => m >= 60 ? `${Math.floor(m/60)} h ${String(m%60).padStart(2,"0")}` : `${m} min`;

async function charger() {
  const vue = await (await fetch("/api/etat")).json();
  $("sous").textContent = `${(vue.tickets||[]).length} ticket(s) en cours · relevé ${
      (vue.genere||"").slice(11,16) || "—"}`;
  $("bandeau").innerHTML = vue.erreur
    ? `<div class="bandeau">Lecture du compte impossible : ${vue.erreur}</div>` : "";

  $("regles").innerHTML = (vue.regles||[]).map(r => {
    const couleur = !r.activee ? "var(--doux)"
                  : !r.couvert ? "var(--alerte)"
                  : r.sur_la_preferee ? "var(--ok)" : "var(--repli)";
    const etat = !r.activee ? "désactivée"
               : r.couvert ? `couvert par la zone ${r.zone_couvrante}` : "aucun ticket";
    const zones = r.zones.map(z => {
      const classes = ["zone"];
      if (z === r.preferee) classes.push("pref");
      if (z === r.zone_couvrante) classes.push("active");
      return `<span class="${classes.join(" ")}">${z}</span>`;
    }).join('<span class="fleche">›</span>');
    return `<div class="carte">
      <div class="titre"><span class="pastille" style="background:${couleur}"></span>${r.nom}</div>
      <div class="gros">${r.couvert ? duree(r.reste_minutes) : "—"}</div>
      <div class="doux">${etat}${r.expire ? ` · expire ${r.expire}` : ""}</div>
      <div class="doux" style="margin-top:6px">Prochaine action : ${
        r.action ? "<b>"+r.action+"</b>" : "rien à faire"} · rendez-vous ${
        r.rendez_vous || "—"} · ${r.plaque}</div>
      <div class="zones">${zones}</div>
    </div>`;
  }).join("") || '<div class="carte doux">Aucune règle.</div>';

  $("tickets").innerHTML =
    "<tr><th>Plaque</th><th>Zone</th><th>Tarif</th><th>Expire</th><th>Reste</th></tr>" +
    ((vue.tickets||[]).map(t => `<tr><td>${t.plaque}</td><td>${t.zone}</td><td>${t.tarif}</td>
      <td>${t.expire||"?"}</td><td>${duree(t.reste_minutes)}</td></tr>`).join("")
     || '<tr><td colspan="5" class="doux">aucun</td></tr>');

  $("journal").textContent = (vue.passages||[]).map(
    p => `${p.at.replace("T"," ")}\\n  ${(p.lines||[]).join("\\n  ")}`).join("\\n\\n") || "—";
}

async function chargerConfig() {
  $("config").value = (await (await fetch("/api/config")).json()).texte;
  $("msgconf").textContent = "";
}

$("save").onclick = async () => {
  $("save").disabled = true;
  const rep = await (await fetch("/api/config", {method:"POST",
      body: JSON.stringify({texte: $("config").value})})).json();
  $("msgconf").textContent = rep.ok ? rep.message : rep.erreur;
  $("msgconf").className = "msg " + (rep.ok ? "ok" : "ko");
  $("save").disabled = false;
  if (rep.ok) charger();
};
$("reload").onclick = chargerConfig;

async function passage(simulation) {
  $("sim").disabled = $("vrai").disabled = true;
  $("msgpass").textContent = simulation ? "simulation en cours…" : "passage en cours…";
  $("msgpass").className = "msg";
  const rep = await (await fetch("/api/passage", {method:"POST",
      body: JSON.stringify({simulation})})).json();
  $("sortie").hidden = false;
  $("sortie").textContent = (rep.lignes||[]).join("\\n");
  $("msgpass").textContent = rep.ok ? "terminé" : "des règles ont échoué";
  $("msgpass").className = "msg " + (rep.ok ? "ok" : "ko");
  $("sim").disabled = $("vrai").disabled = false;
  charger();
}
$("sim").onclick = () => passage(true);
$("vrai").onclick = () => {
  if (confirm("Prendre les tickets maintenant, pour de vrai ?")) passage(false);
};

charger(); chargerConfig();
setInterval(charger, 60000);
</script>
</div></body></html>
"""
