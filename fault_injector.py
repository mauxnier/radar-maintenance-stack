#!/usr/bin/env python3
"""
Fault Injector — Radar Maintenance Dashboard
Interface web pour déclencher des scénarios de pannes à la demande.
Port : 5001
"""

import os
import psycopg2
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

DSN      = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/radar")
RADAR_ID = os.getenv("RADAR_ID", "RADAR-01")

# (icône, label, description, durée_défaut_s, classe_css)
SCENARIOS = {
    "gen1_fault":   ("🔴", "Panne génératrice 1",     "Génératrice 1 en défaut — coupure puissance + alarme",          60,  "danger"),
    "gen2_fault":   ("🔴", "Panne génératrice 2",     "Génératrice 2 en défaut — coupure puissance + alarme",          60,  "danger"),
    "blackout":     ("⚡", "Coupure alimentation",    "Gen1 + Gen2 en défaut simultané — radar en état FAULT",         30,  "danger"),
    "overheat":     ("🌡️", "Surchauffe cabine",       "Température cabine +20 °C — franchit le seuil d'alarme",       120, "warning"),
    "vswr_spike":   ("📡", "Pic VSWR antenne",        "VSWR forcé > 2.5 — alarme hyperfréquence",                      30,  "warning"),
    "antenna_stop": ("📻", "Arrêt rotation antenne",  "Rotation forcée en mode FIXE — RPM = 0",                        90,  "warning"),
    "maintenance":  ("🔧", "Mode maintenance",        "État opérationnel forcé en MAINTENANCE",                       300,  "info"),
}

app = FastAPI(title="Radar Fault Injector")


# ----------------------------------------------------------------
#  DB helpers
# ----------------------------------------------------------------

def get_conn():
    return psycopg2.connect(DSN)


def init_db():
    """Crée la table radar_commands si elle n'existe pas (idempotent)."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS radar_commands (
                id          SERIAL      PRIMARY KEY,
                radar_id    TEXT        NOT NULL DEFAULT 'RADAR-01',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                command     TEXT        NOT NULL,
                duration_s  INTEGER     NOT NULL DEFAULT 60,
                description TEXT,
                applied_at  TIMESTAMPTZ,
                expires_at  TIMESTAMPTZ
            )
        """)
    conn.commit()
    conn.close()


def get_active_fault():
    """Retourne la panne active (commande, description, expires_at) ou None."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT command, description, expires_at
                FROM radar_commands
                WHERE radar_id = %s AND expires_at > NOW()
                ORDER BY created_at DESC LIMIT 1
            """, (RADAR_ID,))
            row = cur.fetchone()
        conn.close()
        return row
    except Exception:
        return None


def insert_command(command: str, duration_s: int, description: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO radar_commands (radar_id, command, duration_s, description)
            VALUES (%s, %s, %s, %s)
        """, (RADAR_ID, command, duration_s, description))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------
#  HTML builder
# ----------------------------------------------------------------

def build_page(active) -> str:
    # --- Barre de statut ---
    if active:
        cmd, desc, expires_at = active
        # psycopg2 retourne un datetime timezone-aware pour TIMESTAMPTZ
        now_utc = datetime.now(timezone.utc)
        remaining = max(0, int((expires_at - now_utc).total_seconds()))
        status_html = f"""
        <div class="status-bar status-active">
          <div class="status-dot dot-active"></div>
          <div>
            <div class="status-label">PANNE ACTIVE&nbsp;: {desc}</div>
            <div class="status-sub">Expire dans <strong>{remaining}s</strong>&nbsp;· commande&nbsp;: <code>{cmd}</code></div>
          </div>
        </div>"""
    else:
        status_html = """
        <div class="status-bar status-ok">
          <div class="status-dot dot-ok"></div>
          <div class="status-label">Système nominal — aucune panne active</div>
        </div>"""

    # --- Boutons de scénarios ---
    btns = ""
    for key, (icon, label, desc, duration, css) in SCENARIOS.items():
        btns += f"""
        <form method="post" action="/inject/{key}">
          <button type="submit" class="btn btn-{css}">
            <span class="btn-icon">{icon}</span>
            <span class="btn-label">{label}</span>
            <span class="btn-desc">{desc}</span>
            <span class="btn-duration">Durée : {duration} s</span>
          </button>
        </form>"""

    now_str = datetime.now().strftime("%H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>Fault Injector — {RADAR_ID}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: #111217;
      color: #d8d9da;
      font-family: 'Segoe UI', system-ui, sans-serif;
      padding: 28px 24px;
      max-width: 940px;
      margin: 0 auto;
    }}

    h1 {{ color: #f57c00; font-size: 1.5rem; margin-bottom: 4px; }}
    .subtitle {{ color: #888; font-size: .85rem; margin-bottom: 22px; }}
    .subtitle a {{ color: #5794f2; text-decoration: none; }}
    .subtitle a:hover {{ text-decoration: underline; }}

    /* Status bar */
    .status-bar {{
      display: flex; align-items: center; gap: 14px;
      border-radius: 8px; padding: 14px 18px; margin-bottom: 28px;
      border: 1px solid #383a40;
    }}
    .status-ok   {{ background: #1a2a1a; border-color: #73bf69; }}
    .status-active {{ background: #2a1520; border-color: #f2495c; }}
    .status-dot  {{ width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }}
    .dot-ok      {{ background: #73bf69; }}
    .dot-active  {{ background: #f2495c; animation: pulse 1s infinite; }}
    @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.25 }} }}
    .status-label {{ font-weight: 600; font-size: .95rem; }}
    .status-sub   {{ font-size: .82rem; color: #aaa; margin-top: 4px; }}
    code {{ background: #2c3038; padding: 1px 6px; border-radius: 3px; font-size: .8rem; }}

    /* Section headings */
    h2 {{
      font-size: .73rem; text-transform: uppercase; letter-spacing: .12em;
      color: #888; margin-bottom: 14px;
    }}

    /* Button grid */
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}

    .btn {{
      background: #1e2128; border: 1px solid #383a40; border-radius: 8px;
      padding: 16px; cursor: pointer; text-align: left; width: 100%;
      color: #d8d9da; transition: background .15s, border-color .15s;
      display: flex; flex-direction: column; gap: 0;
    }}
    .btn:hover {{ background: #272b33; }}

    .btn-danger  {{ border-left: 3px solid #f2495c; }}
    .btn-danger:hover  {{ border-color: #f2495c; }}
    .btn-warning {{ border-left: 3px solid #ff9900; }}
    .btn-warning:hover {{ border-color: #ff9900; }}
    .btn-info    {{ border-left: 3px solid #5794f2; }}
    .btn-info:hover    {{ border-color: #5794f2; }}

    .btn-icon     {{ font-size: 1.4rem; margin-bottom: 8px; }}
    .btn-label    {{ font-weight: 700; font-size: .95rem; margin-bottom: 6px; }}
    .btn-desc     {{ font-size: .78rem; color: #888; margin-bottom: 8px; line-height: 1.45; }}
    .btn-duration {{ font-size: .73rem; color: #555; }}

    /* Reset button */
    .btn-reset {{
      background: #1a2a1c; border: 2px solid #73bf69; border-radius: 8px;
      padding: 14px; cursor: pointer; color: #73bf69;
      font-size: 1rem; font-weight: 700; width: 100%;
      transition: background .15s, color .15s;
    }}
    .btn-reset:hover {{ background: #73bf69; color: #111217; }}

    .footer {{ margin-top: 22px; font-size: .72rem; color: #444; }}
  </style>
</head>
<body>
  <h1>⚡ Injecteur de pannes radar</h1>
  <p class="subtitle">
    Radar&nbsp;: <strong>{RADAR_ID}</strong>
    &nbsp;·&nbsp;<a href="http://localhost:3000" target="_blank">Ouvrir Grafana →</a>
    &nbsp;·&nbsp;auto-refresh 5 s
  </p>

  {status_html}

  <h2>Scénarios disponibles</h2>
  <div class="grid">
    {btns}
  </div>

  <form method="post" action="/inject/reset">
    <button type="submit" class="btn-reset">✅&nbsp;&nbsp;Réinitialisation — Retour immédiat à l'état nominal</button>
  </form>

  <p class="footer">Mise à jour : {now_str}</p>
</body>
</html>"""


# ----------------------------------------------------------------
#  Routes FastAPI
# ----------------------------------------------------------------

@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def index():
    active = get_active_fault()
    return build_page(active)


@app.post("/inject/{command}", response_class=RedirectResponse)
def inject(command: str):
    if command == "reset":
        insert_command("reset", 0, "Réinitialisation — retour à l'état nominal")
    elif command in SCENARIOS:
        _, label, desc, duration, _ = SCENARIOS[command]
        insert_command(command, duration, f"{label} — {desc}")
    # Redirect → GET pour éviter le re-POST au refresh
    return RedirectResponse("/", status_code=303)


# ----------------------------------------------------------------
#  Entrypoint
# ----------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5001, log_level="warning")
