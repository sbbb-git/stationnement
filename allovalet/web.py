"""Tableau de bord local — l'équivalent de l'interface AlloValet.

    python -m allovalet web        →  http://127.0.0.1:8777

Le serveur n'écoute que sur la machine locale et ne stocke aucun identifiant :
il réutilise le client et la configuration déjà en place.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .config import Config
from .errors import AlloValetError
from .models import utcnow
from .notify import Notifier
from .paybyphone import best_duration
from .providers import build_client
from .runner import Runner
from .schedule import parse_duration
from .smartpark import build_curve, candidate_durations, cheapest_plan
from .state import State

log = logging.getLogger("allovalet.web")


class Dashboard:
    """Rassemble tout ce que l'interface affiche, en une passe."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.state = State()
        self.reload()

    def reload(self) -> None:
        self.cfg = Config.load(self.config_path)
        self.client = build_client(self.cfg, self.state)
        self.tz = ZoneInfo(self.cfg.timezone)

    # ------------------------------------------------------------------ data

    def snapshot(self) -> dict:
        self.reload()
        now_local = datetime.now(self.tz)
        error = None
        sessions = []
        try:
            sessions = self.client.current_sessions()
        except AlloValetError as exc:
            error = str(exc)

        rules = []
        for rule in self.cfg.rules:
            active = self.client.find_active(rule.plate, rule.location, sessions)
            margin = timedelta(minutes=self.cfg.margin_for(rule))
            disabled = self.state.is_disabled(rule.name)
            if disabled or not rule.enabled:
                status = "off"
            elif not rule.window.contains(now_local):
                status = "waiting"
            elif active and active.covers(utcnow(), margin):
                status = "covered"
            else:
                status = "due"
            rules.append({
                "name": rule.name,
                "plate": rule.plate,
                "location": rule.location,
                "rate": rule.rate or "(défaut)",
                "mode": rule.mode,
                "duration": rule.duration_minutes,
                "window": rule.window.describe(),
                "status": status,
                "enabled": rule.enabled and not disabled,
                "expiry": active.expiry.isoformat() if active and active.expiry else None,
            })

        return {
            "error": error,
            "generatedAt": now_local.isoformat(),
            "timezone": self.cfg.timezone,
            "provider": self.cfg.provider,
            "sessions": [
                {
                    "plate": s.plate,
                    "location": s.location_id,
                    "rate": s.rate_type or "?",
                    "start": s.start.isoformat() if s.start else None,
                    "expiry": s.expiry.isoformat() if s.expiry else None,
                    "cost": s.cost,
                }
                for s in sorted(sessions, key=lambda s: s.expiry or utcnow())
            ],
            "rules": rules,
            "savings": {
                "total": self.state.total_savings(),
                "entries": list(reversed(self.state.data.get("savings", [])))[:10],
            },
            "spend": {
                "total": self.state.total_spend(),
                "today": sum(
                    self.state.data.get("spend", {})
                    .get(datetime.now().date().isoformat(), {})
                    .values()
                ),
            },
            "journal": list(reversed(self.state.data.get("journal", [])))[:12],
        }

    # --------------------------------------------------------------- actions

    def run(self, dry_run: bool = False) -> dict:
        self.reload()
        runner = Runner(self.cfg, self.client, self.state, Notifier(self.cfg.notify),
                        dry_run=dry_run)
        report = runner.tick()
        return {"lines": [r.line() for r in report.results],
                "failures": len(report.failures),
                "purchases": len(report.purchases)}

    def toggle(self, name: str) -> dict:
        """Active/désactive une règle sans toucher à config.yml."""
        new_state = not self.state.is_disabled(name)
        self.state.set_disabled(name, new_state)
        return {"name": name, "enabled": not new_state}

    def park(self, zone: str, duration: str, rate: str | None, plate: str | None) -> dict:
        self.reload()
        plate = plate or self.cfg.rules[0].plate
        rate_option = self.client.pick_rate_option(zone, plate, rate)
        minutes = parse_duration(duration)
        dur = best_duration(minutes, rate_option.accepted_time_units)
        quote = self.client.quote(zone, plate, dur, rate_option_id=rate_option.id)
        payment = self.client.payment_account_id() if quote.cost else None
        session = self.client.start_session(
            location_id=zone, plate=plate, duration=dur,
            rate_option_id=rate_option.id, payment_account_id=payment,
        )
        if quote.cost:
            self.state.add_spend(f"{plate}@{zone}", quote.cost)
        return {
            "message": f"Ticket pris — {plate} zone {zone} ({rate_option.type or rate_option.name})",
            "expiry": session.expiry.isoformat() if session.expiry else None,
            "cost": quote.cost,
        }

    def plan(self, zone: str, minutes: int, rate: str | None, plate: str | None) -> dict:
        self.reload()
        plate = plate or self.cfg.rules[0].plate
        rate_option = self.client.pick_rate_option(zone, plate, rate)

        def price_of(mins: int):
            quote = self.client.quote(
                zone, plate, best_duration(mins, rate_option.accepted_time_units),
                rate_option_id=rate_option.id,
            )
            real = quote.minutes
            return None if (real and abs(real - mins) > 5) else quote.cost

        curve = build_curve(price_of, candidate_durations(rate_option.max_stay_minutes))
        if not curve:
            return {"error": "Aucun devis obtenu sur cette zone."}
        result = cheapest_plan(minutes, curve)
        return {
            "rate": rate_option.type or rate_option.name,
            "curve": [{"minutes": m, "cost": curve[m]} for m in sorted(curve)],
            "chunks": result.chunks,
            "cost": result.cost,
            "single": result.single_ticket_cost,
            "savings": result.savings,
            "savingsPct": result.savings_pct,
            "describe": result.describe(),
        }


# --------------------------------------------------------------------- serveur


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, body: bytes, content_type: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200):
            self._send(json.dumps(payload).encode(), "application/json; charset=utf-8", status)

        def _guard(self, call):
            try:
                return self._json(call())
            except AlloValetError as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001 — l'interface ne doit jamais tomber
                log.exception("erreur dans le tableau de bord")
                return self._json({"error": str(exc)}, 500)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}

            if url.path in ("/", "/index.html"):
                return self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._guard(dash.snapshot)
            if url.path == "/api/plan":
                return self._guard(lambda: dash.plan(
                    query["zone"], int(query.get("minutes", 360)),
                    query.get("rate") or None, query.get("plate") or None,
                ))
            return self._json({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")

            if url.path == "/api/run":
                return self._guard(lambda: dash.run(bool(body.get("dryRun"))))
            if url.path == "/api/toggle":
                return self._guard(lambda: dash.toggle(body["name"]))
            if url.path == "/api/park":
                return self._guard(lambda: dash.park(
                    str(body["zone"]), str(body.get("duration", "1h")),
                    body.get("rate") or None, body.get("plate") or None,
                ))
            return self._json({"error": "not found"}, 404)

    return Handler


def serve(config_path: str, port: int = 8777, open_browser: bool = True) -> None:
    dash = Dashboard(config_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(dash))
    url = f"http://127.0.0.1:{port}"
    print(f"\nTableau de bord : {url}   (Ctrl-C pour arrêter)\n")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêté.")
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AlloValet perso</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #fff; --ink: #16181d; --muted: #6b7280;
    --line: #e5e7eb; --accent: #2563eb; --ok: #15803d; --warn: #b45309;
    --bad: #b91c1c; --off: #9ca3af; --okbg: #dcfce7; --warnbg: #fef3c7;
    --badbg: #fee2e2; --offbg: #f3f4f6;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --panel: #171a21; --ink: #e8eaed; --muted: #9aa3b2;
      --line: #262b35; --accent: #60a5fa; --ok: #4ade80; --warn: #fbbf24;
      --bad: #f87171; --off: #6b7280; --okbg: #14311f; --warnbg: #3b2c0a;
      --badbg: #3b1414; --offbg: #21252e;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 980px; margin: 0 auto; padding: 28px 20px 64px; }
  header { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline;
           justify-content: space-between; margin-bottom: 22px; }
  h1 { font-size: 21px; margin: 0; letter-spacing: -0.01em; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
       color: var(--muted); margin: 30px 0 12px; font-weight: 600; }
  .sub { color: var(--muted); font-size: 13px; }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 12px; padding: 16px 18px; margin-bottom: 10px; }
  .row { display: flex; gap: 14px; align-items: center; justify-content: space-between;
         flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
  .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
         padding: 14px 16px; }
  .kpi .v { font-size: 26px; font-weight: 650; letter-spacing: -0.02em; }
  .kpi .l { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
  .chip { font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 999px;
          white-space: nowrap; }
  .covered { background: var(--okbg); color: var(--ok); }
  .due { background: var(--badbg); color: var(--bad); }
  .waiting { background: var(--warnbg); color: var(--warn); }
  .off { background: var(--offbg); color: var(--off); }
  .plate { font-weight: 650; letter-spacing: .04em; }
  .meta { color: var(--muted); font-size: 13px; }
  .bar { height: 5px; background: var(--line); border-radius: 999px; margin-top: 11px;
         overflow: hidden; }
  .bar > i { display: block; height: 100%; background: var(--accent); border-radius: 999px; }
  button { font: inherit; font-size: 13.5px; padding: 7px 13px; border-radius: 8px;
           border: 1px solid var(--line); background: var(--panel); color: var(--ink);
           cursor: pointer; }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { opacity: .9; color: #fff; }
  button:disabled { opacity: .5; cursor: default; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  pre { margin: 0; white-space: pre-wrap; font: 12.5px/1.6 ui-monospace, SFMono-Regular,
        Menlo, monospace; color: var(--muted); }
  .err { background: var(--badbg); color: var(--bad); border-color: transparent; }
  input, select { font: inherit; font-size: 13.5px; padding: 7px 9px; border-radius: 8px;
                  border: 1px solid var(--line); background: var(--bg); color: var(--ink); }
  .empty { color: var(--muted); font-size: 13.5px; }
  .toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
           background: var(--ink); color: var(--bg); padding: 11px 18px; border-radius: 10px;
           font-size: 13.5px; box-shadow: 0 8px 28px rgba(0,0,0,.22); z-index: 9; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>AlloValet perso</h1>
      <div class="sub" id="sub">chargement…</div>
    </div>
    <div class="actions">
      <button id="simulate">Simuler</button>
      <button id="run" class="primary">Lancer un passage</button>
    </div>
  </header>

  <div id="alert"></div>

  <div class="grid" id="kpis"></div>

  <h2>Tickets en cours</h2>
  <div id="sessions"></div>

  <h2>Règles</h2>
  <div id="rules"></div>

  <h2>Ticket manuel</h2>
  <div class="card">
    <div class="row">
      <div class="actions">
        <input id="p-zone" placeholder="zone (75016)" size="10">
        <input id="p-duration" placeholder="durée (2h)" size="8" value="2h">
        <input id="p-rate" placeholder="tarif (CMI)" size="8">
        <input id="p-plate" placeholder="plaque" size="10">
      </div>
      <div class="actions">
        <button id="simulatePlan">Simuler le découpage</button>
        <button id="park" class="primary">Prendre le ticket</button>
      </div>
    </div>
    <div id="planout" style="margin-top:12px"></div>
  </div>

  <h2>Économies SmartPark</h2>
  <div id="savings"></div>

  <h2>Journal</h2>
  <div id="journal"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const euro = (n) => (n ?? 0).toFixed(2).replace('.', ',') + ' €';

function human(ms) {
  if (ms <= 0) return 'expiré';
  const m = Math.floor(ms / 60000), h = Math.floor(m / 60);
  return h ? `${h} h ${String(m % 60).padStart(2, '0')}` : `${m} min`;
}

function toast(text, bad) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = text;
  if (bad) el.style.background = '#b91c1c', el.style.color = '#fff';
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}

const LABEL = { covered: 'couvert', due: 'à prendre', waiting: 'en attente', off: 'désactivée' };

function render(s) {
  $('sub').textContent =
    `${s.provider} · ${s.timezone} · actualisé à ` +
    new Date(s.generatedAt).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });

  $('alert').innerHTML = s.error
    ? `<div class="card err"><strong>Problème de connexion</strong><br>${s.error}</div>` : '';

  const due = s.rules.filter(r => r.status === 'due').length;
  $('kpis').innerHTML = `
    <div class="kpi"><div class="v">${s.sessions.length}</div><div class="l">ticket(s) actif(s)</div></div>
    <div class="kpi"><div class="v" style="color:${due ? 'var(--bad)' : 'var(--ok)'}">${due || 'OK'}</div>
      <div class="l">${due ? 'règle(s) à traiter' : 'tout est couvert'}</div></div>
    <div class="kpi"><div class="v">${euro(s.savings.total)}</div><div class="l">économisé (SmartPark)</div></div>
    <div class="kpi"><div class="v">${euro(s.spend.total)}</div><div class="l">dépensé au total</div></div>`;

  $('sessions').innerHTML = s.sessions.length ? s.sessions.map(x => {
    const end = new Date(x.expiry), start = new Date(x.start || x.expiry);
    const left = end - Date.now(), span = Math.max(1, end - start);
    const pct = Math.max(0, Math.min(100, 100 * left / span));
    return `<div class="card">
      <div class="row">
        <div><span class="plate">${x.plate}</span>
          <span class="meta">· zone ${x.location} · ${x.rate}</span></div>
        <div><span class="chip ${left > 0 ? 'covered' : 'due'}">${human(left)}</span></div>
      </div>
      <div class="meta" style="margin-top:4px">jusqu'au ${end.toLocaleString('fr-FR',
        { weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}
        ${x.cost ? '· ' + euro(x.cost) : ''}</div>
      <div class="bar"><i style="width:${pct}%"></i></div>
    </div>`;
  }).join('') : '<div class="card empty">Aucun ticket en cours.</div>';

  $('rules').innerHTML = s.rules.map(r => `<div class="card">
    <div class="row">
      <div>
        <span class="plate">${r.name}</span>
        <div class="meta">${r.plate} · zone ${r.location} · ${r.rate} · ${r.mode} · ${r.window}</div>
      </div>
      <div class="actions">
        <span class="chip ${r.status}">${LABEL[r.status]}</span>
        <button data-toggle="${r.name}">${r.enabled ? 'Désactiver' : 'Activer'}</button>
      </div>
    </div>
  </div>`).join('');

  $('savings').innerHTML = s.savings.entries.length
    ? s.savings.entries.map(e => `<div class="card">
        <div class="row"><div><span class="plate">${euro(e.amount)}</span>
        <span class="meta">· ${e.rule}</span></div>
        <span class="meta">${new Date(e.at).toLocaleDateString('fr-FR')}</span></div>
        <div class="meta" style="margin-top:4px">${e.detail}</div></div>`).join('')
    : '<div class="card empty">Rien encore — le mode SmartPark n\'a pas encore tourné.</div>';

  $('journal').innerHTML = s.journal.length ? s.journal.map(j => `<div class="card">
      <div class="meta">${new Date(j.at).toLocaleString('fr-FR')}</div>
      <pre>${j.lines.join('\n')}</pre></div>`).join('')
    : '<div class="card empty">Aucun passage enregistré.</div>';

  document.querySelectorAll('[data-toggle]').forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try { await api('/api/toggle', post({ name: b.dataset.toggle })); await refresh(); }
      catch (e) { toast(e.message, true); b.disabled = false; }
    };
  });
}

const post = (body) => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

async function refresh() {
  try { render(await api('/api/state')); }
  catch (e) { $('alert').innerHTML = `<div class="card err">${e.message}</div>`; }
}

async function launch(dryRun, button) {
  button.disabled = true;
  const label = button.textContent;
  button.textContent = 'en cours…';
  try {
    const r = await api('/api/run', post({ dryRun }));
    toast(r.lines.join(' | ') || 'rien à faire', r.failures > 0);
    await refresh();
  } catch (e) { toast(e.message, true); }
  finally { button.disabled = false; button.textContent = label; }
}

$('run').onclick = (e) => launch(false, e.target);
$('simulate').onclick = (e) => launch(true, e.target);

$('park').onclick = async (e) => {
  const zone = $('p-zone').value.trim();
  if (!zone) return toast('Indique une zone.', true);
  e.target.disabled = true;
  try {
    const r = await api('/api/park', post({
      zone, duration: $('p-duration').value.trim() || '1h',
      rate: $('p-rate').value.trim(), plate: $('p-plate').value.trim(),
    }));
    toast(`${r.message} — ${euro(r.cost)}`);
    await refresh();
  } catch (err) { toast(err.message, true); }
  finally { e.target.disabled = false; }
};

$('simulatePlan').onclick = async (e) => {
  const zone = $('p-zone').value.trim();
  if (!zone) return toast('Indique une zone.', true);
  e.target.disabled = true;
  $('planout').innerHTML = '<div class="meta">calcul des devis réels…</div>';
  try {
    const minutes = Math.round(toMinutes($('p-duration').value.trim() || '6h'));
    const params = new URLSearchParams({ zone, minutes,
      rate: $('p-rate').value.trim(), plate: $('p-plate').value.trim() });
    const p = await api('/api/plan?' + params);
    $('planout').innerHTML = `
      <div><strong>${p.describe}</strong></div>
      <div class="meta" style="margin-top:6px">Barème constaté (${p.rate}) : ` +
      p.curve.map(c => `${Math.round(c.minutes / 60 * 10) / 10} h → ${euro(c.cost)}`).join(' · ') +
      '</div>';
  } catch (err) { $('planout').innerHTML = `<div class="meta">${err.message}</div>`; }
  finally { e.target.disabled = false; }
};

function toMinutes(text) {
  const m = /^(?:(\d+)\s*h)?\s*(\d+)?\s*(?:m|min)?$/i.exec(text);
  if (!m) return 360;
  return (parseInt(m[1] || 0, 10) * 60) + parseInt(m[2] || 0, 10) || 360;
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""
