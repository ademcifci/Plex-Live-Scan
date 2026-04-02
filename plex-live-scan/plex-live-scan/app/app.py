import os
import json
import sqlite3
import requests
import logging
from datetime import datetime
from flask import Flask, request, jsonify, render_template, g

app = Flask(__name__)
DB_PATH = "/data/config.db"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("plex-live-scan")


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_path TEXT NOT NULL,
            plex_path TEXT NOT NULL,
            section_id TEXT,
            enabled INTEGER DEFAULT 1
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_paths (
            path TEXT PRIMARY KEY,
            last_seen TEXT NOT NULL
        )
    """)
    db.commit()
    db.close()


def cfg_get(key, default=None):
    row = get_db().execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def cfg_set(key, value):
    get_db().execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, value))
    get_db().commit()


def add_log(level, message):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    get_db().execute(
        "INSERT INTO activity_log(ts, level, message) VALUES(?,?,?)",
        (ts, level, message)
    )
    get_db().commit()
    log.info("[%s] %s", level, message)


# ── Plex helpers ──────────────────────────────────────────────────────────────

def plex_request(path, params=None):
    base = cfg_get("plex_url", "http://localhost:32400")
    token = cfg_get("plex_token", "")
    p = dict(params or {})
    p["X-Plex-Token"] = token
    r = requests.get(f"{base}{path}", params=p, timeout=10)
    r.raise_for_status()
    return r


def get_plex_sections():
    try:
        r = plex_request("/library/sections")
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        sections = []
        for d in root.findall("Directory"):
            locations = [loc.get("path") for loc in d.findall("Location")]
            sections.append({
                "id": d.get("key"),
                "title": d.get("title"),
                "type": d.get("type"),
                "locations": locations
            })
        return sections, None
    except Exception as e:
        return [], str(e)


def trigger_scan(plex_path, section_id):
    try:
        plex_request(
            f"/library/sections/{section_id}/refresh",
            {"path": plex_path}
        )
        return True, None
    except Exception as e:
        return False, str(e)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify({
        "plex_url": cfg_get("plex_url", "http://localhost:32400"),
        "plex_token": cfg_get("plex_token", ""),
        "webhook_secret": cfg_get("webhook_secret", ""),
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.json
    for key in ("plex_url", "plex_token", "webhook_secret"):
        if key in data:
            cfg_set(key, data[key])
    add_log("INFO", "Configuration updated")
    return jsonify({"ok": True})


@app.route("/api/sections")
def api_sections():
    sections, err = get_plex_sections()
    if err:
        return jsonify({"error": err}), 500
    return jsonify(sections)


@app.route("/api/mappings", methods=["GET"])
def api_get_mappings():
    rows = get_db().execute("SELECT * FROM mappings").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/mappings", methods=["POST"])
def api_add_mapping():
    data = request.json
    db = get_db()
    db.execute(
        "INSERT INTO mappings(agent_path, plex_path, section_id, enabled) VALUES(?,?,?,1)",
        (data["agent_path"], data["plex_path"], data.get("section_id", ""))
    )
    db.commit()
    add_log("INFO", f"Mapping added: {data['agent_path']} → {data['plex_path']}")
    return jsonify({"ok": True})


@app.route("/api/mappings/<int:mid>", methods=["DELETE"])
def api_del_mapping(mid):
    get_db().execute("DELETE FROM mappings WHERE id=?", (mid,))
    get_db().commit()
    add_log("INFO", f"Mapping #{mid} removed")
    return jsonify({"ok": True})


@app.route("/api/mappings/<int:mid>", methods=["PATCH"])
def api_patch_mapping(mid):
    data = request.json
    for col in ("agent_path", "plex_path", "section_id", "enabled"):
        if col in data:
            get_db().execute(f"UPDATE mappings SET {col}=? WHERE id=?", (data[col], mid))
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/api/log")
def api_log():
    limit = int(request.args.get("limit", 100))
    rows = get_db().execute(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/log", methods=["DELETE"])
def api_clear_log():
    get_db().execute("DELETE FROM activity_log")
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify secret if configured
    secret = cfg_get("webhook_secret", "")
    if secret:
        incoming = request.headers.get("X-Webhook-Secret", "")
        if incoming != secret:
            add_log("WARN", "Webhook rejected: invalid secret")
            return jsonify({"error": "unauthorized"}), 401

    data = request.json
    if not data or "path" not in data:
        return jsonify({"error": "missing path"}), 400

    changed_path = data["path"]
    add_log("INFO", f"Change detected on agent: {changed_path}")

    # Find matching mapping
    mappings = get_db().execute(
        "SELECT * FROM mappings WHERE enabled=1"
    ).fetchall()

    matched = False
    for m in mappings:
        agent_base = m["agent_path"].rstrip("/")
        if changed_path.startswith(agent_base):
            # Translate path
            relative = changed_path[len(agent_base):]
            plex_path = m["plex_path"].rstrip("/") + relative
            section_id = m["section_id"]

            if not section_id:
                add_log("WARN", f"No section ID for mapping {agent_base}, skipping scan")
                continue

            add_log("INFO", f"Triggering Plex scan: section={section_id} path={plex_path}")
            ok, err = trigger_scan(plex_path, section_id)
            if ok:
                add_log("OK", f"Scan triggered successfully for {plex_path}")
            else:
                add_log("ERROR", f"Scan failed for {plex_path}: {err}")
            matched = True

    if not matched:
        add_log("WARN", f"No mapping found for path: {changed_path}")

    return jsonify({"ok": True})


@app.route("/api/test-plex")
def api_test_plex():
    try:
        plex_request("/identity")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/export")
def api_export():
    mappings = get_db().execute(
        "SELECT agent_path, plex_path, section_id, enabled FROM mappings"
    ).fetchall()
    payload = {
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Plex token is intentionally excluded. Re-enter it after importing.",
        "config": {
            "plex_url":       cfg_get("plex_url", ""),
            # plex_token is deliberately omitted — it is a credential
            "webhook_secret": cfg_get("webhook_secret", ""),
        },
        "mappings": [dict(r) for r in mappings]
    }
    from flask import Response
    import json as _json
    return Response(
        _json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=plex-live-scan-settings.json"}
    )


@app.route("/api/import", methods=["POST"])
def api_import():
    data = request.json
    if not data or data.get("version") != 1:
        return jsonify({"error": "Invalid or missing settings file"}), 400

    imported_config   = 0
    imported_mappings = 0
    skipped_mappings  = 0

    # Restore config values — plex_token is never imported (not in export)
    cfg = data.get("config", {})
    for key in ("plex_url", "webhook_secret"):
        if key in cfg and cfg[key]:
            cfg_set(key, cfg[key])
            imported_config += 1

    # Restore mappings — skip exact duplicates (same agent_path + plex_path)
    db = get_db()
    for m in data.get("mappings", []):
        existing = db.execute(
            "SELECT id FROM mappings WHERE agent_path=? AND plex_path=?",
            (m.get("agent_path", ""), m.get("plex_path", ""))
        ).fetchone()
        if existing:
            skipped_mappings += 1
            continue
        db.execute(
            "INSERT INTO mappings(agent_path, plex_path, section_id, enabled) VALUES(?,?,?,?)",
            (m.get("agent_path", ""), m.get("plex_path", ""),
             m.get("section_id", ""), m.get("enabled", 1))
        )
        imported_mappings += 1
    db.commit()

    summary = f"Imported {imported_config} config value(s), {imported_mappings} mapping(s)"
    if skipped_mappings:
        summary += f", skipped {skipped_mappings} duplicate(s)"
    summary += ". Re-enter your Plex token."
    add_log("INFO", f"Settings imported: {summary}")
    return jsonify({"ok": True, "summary": summary})


@app.route("/api/agent/paths", methods=["POST"])
def api_post_agent_paths():
    """Receive the list of paths the agent is currently watching."""
    secret = cfg_get("webhook_secret", "")
    if secret:
        incoming = request.headers.get("X-Webhook-Secret", "")
        if incoming != secret:
            return jsonify({"error": "unauthorized"}), 401
    data = request.json
    if not data or "paths" not in data:
        return jsonify({"error": "missing paths"}), 400
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    for path in data["paths"]:
        db.execute(
            "INSERT OR REPLACE INTO agent_paths(path, last_seen) VALUES(?,?)",
            (path, ts)
        )
    db.commit()
    log.info("Agent announced %d path(s)", len(data["paths"]))
    return jsonify({"ok": True})


@app.route("/api/agent/paths", methods=["GET"])
def api_get_agent_paths():
    """Return known agent paths, each annotated with its mapping if one exists."""
    db = get_db()
    paths = db.execute(
        "SELECT * FROM agent_paths ORDER BY path"
    ).fetchall()
    mappings = db.execute("SELECT * FROM mappings").fetchall()
    mapping_by_agent = {m["agent_path"]: dict(m) for m in mappings}
    return jsonify([{
        "path": p["path"],
        "last_seen": p["last_seen"],
        "mapping": mapping_by_agent.get(p["path"])
    } for p in paths])


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=7077, debug=False)
