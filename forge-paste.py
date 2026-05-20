#!/usr/bin/env python3
"""
Forge Paste — A lightweight, self-hosted paste server.
Hastebin-compatible API with a clean web UI.

Usage:
    python3 forge-paste.py [--port PORT] [--host HOST] [--db PATH] [--max-age DAYS]

API:
    POST /api/paste         — raw text body → {"key": "abc123", "url": "..."}
    POST /api/documents     — hastebin-compat → {"key": "abc123"}
    GET  /api/raw/<slug>    — raw text output
    GET  /api/documents/<slug> — {"key": "...", "data": "..."}
    GET  /<slug>            — web viewer
    GET  /                  — web editor
"""

import sqlite3
import string
import random
import os
import json
import argparse
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime
import html

DB_PATH = os.environ.get("FORGE_PASTE_DB", "/var/lib/forge-paste/pastes.db")
HOST = os.environ.get("FORGE_PASTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("FORGE_PASTE_PORT", "7890"))
MAX_SIZE = 512 * 1024  # 512KB
MAX_AGE_DAYS = int(os.environ.get("FORGE_PASTE_MAX_AGE", "30"))  # Default 30 days
_CLEANUP_INTERVAL = 3600  # Run cleanup at most once per hour
_last_cleanup = 0

def get_db():
    """Get a thread-local database connection."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db

def init_db():
    """Initialize the database schema."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS pastes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            language TEXT,
            created_at REAL NOT NULL DEFAULT (unixepoch()),
            views INTEGER NOT NULL DEFAULT 0,
            ip TEXT,
            expires_at REAL
        )
    """)
    # Migrate: add expires_at column if missing (existing installs)
    cols = [row[1] for row in db.execute("PRAGMA table_info(pastes)").fetchall()]
    if "expires_at" not in cols:
        db.execute("ALTER TABLE pastes ADD COLUMN expires_at REAL")
        # Backfill existing pastes: expire them MAX_AGE_DAYS from creation
        db.execute(
            "UPDATE pastes SET expires_at = created_at + ? WHERE expires_at IS NULL",
            (MAX_AGE_DAYS * 86400,),
        )
        db.commit()

    db.execute("CREATE INDEX IF NOT EXISTS idx_slug ON pastes(slug)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_created ON pastes(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_expires ON pastes(expires_at)")
    db.commit()

    db.close()

def generate_slug(length=8):
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choices(chars, k=length))

def cleanup_expired():
    """Delete expired pastes. Runs at most once per _CLEANUP_INTERVAL."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    db = get_db()
    deleted = db.execute(
        "DELETE FROM pastes WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
    ).rowcount
    if deleted:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.commit()
    db.close()

def create_paste(content, language=None, ip=None):
    cleanup_expired()  # Opportunistic cleanup
    db = get_db()
    slug = generate_slug()
    # Ensure unique
    while db.execute("SELECT 1 FROM pastes WHERE slug=?", (slug,)).fetchone():
        slug = generate_slug()
    now = time.time()
    expires_at = now + (MAX_AGE_DAYS * 86400) if MAX_AGE_DAYS > 0 else None
    db.execute(
        "INSERT INTO pastes (slug, content, language, created_at, views, ip, expires_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (slug, content, language, now, ip, expires_at)
    )
    db.commit()
    db.close()
    return slug

def get_paste(slug):
    db = get_db()
    # Exclude expired pastes from reads
    row = db.execute(
        "SELECT * FROM pastes WHERE slug=? AND (expires_at IS NULL OR expires_at > ?)",
        (slug, time.time()),
    ).fetchone()
    if row:
        db.execute("UPDATE pastes SET views = views + 1 WHERE slug=?", (slug,))
        db.commit()
    db.close()
    return dict(row) if row else None

def get_stats():
    db = get_db()
    row = db.execute("SELECT COUNT(*) as total FROM pastes").fetchone()
    db.close()
    return row["total"]

def time_ago(ts):
    seconds = int(time.time() - ts)
    if seconds < 60: return "just now"
    if seconds < 3600: return f"{seconds // 60}m ago"
    if seconds < 86400: return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"

# ── HTML Templates ──────────────────────────────────────────

STYLE = """
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0a1628; color: white; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; min-height: 100vh; display: flex; flex-direction: column; }
  a { color: #2aa4dd; text-decoration: none; }
  a:hover { color: #5bcaee; }
  .header { border-bottom: 1px solid #1a2d42; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  .header .left { display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 20px; font-weight: 700; }
  .header .slug { font-size: 12px; color: rgba(91,202,238,0.4); font-family: monospace; margin-left: 8px; }
  .header .right { display: flex; align-items: center; gap: 12px; font-size: 14px; }
  .header .meta { font-size: 12px; color: rgba(91,202,238,0.4); }
  .btn { padding: 6px 16px; border-radius: 8px; border: none; cursor: pointer; font-size: 14px; font-weight: 500; transition: all 0.15s; color: white; }
  .btn-primary { background: #2aa4dd; }
  .btn-primary:hover { background: #5bcaee; }
  .btn-secondary { background: #1a2d42; }
  .btn-secondary:hover { background: #253d55; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .editor { flex: 1; display: flex; flex-direction: column; padding: 16px; }
  .editor textarea { flex: 1; width: 100%; background: #0e2132; color: white; font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 14px; padding: 24px; border-radius: 12px; border: 1px solid #1a2d42; resize: none; outline: none; }
  .editor textarea:focus { border-color: rgba(42,164,221,0.5); }
  .editor textarea::placeholder { color: rgba(91,202,238,0.3); }
  .editor .bar { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; }
  .editor .chars { font-size: 14px; color: rgba(91,202,238,0.4); }
  .viewer { flex: 1; padding: 16px; }
  .viewer pre { background: #0e2132; padding: 24px; border-radius: 12px; border: 1px solid #1a2d42; overflow: auto; white-space: pre-wrap; word-break: break-word; font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 14px; line-height: 1.6; }
  .line { display: flex; }
  .line-num { user-select: none; color: rgba(91,202,238,0.2); margin-right: 24px; text-align: right; min-width: 3ch; }
  .footer { border-top: 1px solid #1a2d42; padding: 12px 24px; text-align: center; font-size: 12px; color: rgba(91,202,238,0.3); }
  .footer code { color: rgba(91,202,238,0.5); }
  .result { display: flex; align-items: center; justify-content: center; flex: 1; padding: 16px; }
  .result-box { max-width: 500px; width: 100%; text-align: center; }
  .result-box .success { color: #5bcaee; font-size: 18px; font-weight: 500; margin-bottom: 16px; }
  .result-box .url-box { background: #0a1628; border: 1px solid rgba(42,164,221,0.3); border-radius: 12px; padding: 24px; margin-bottom: 16px; }
  .result-box .url { color: #2aa4dd; font-size: 18px; font-family: monospace; word-break: break-all; }
  .result-box .actions { display: flex; gap: 12px; justify-content: center; }
  .notfound { display: flex; align-items: center; justify-content: center; flex: 1; text-align: center; }
  .notfound .emoji { font-size: 60px; margin-bottom: 16px; }
  .notfound h2 { font-size: 20px; margin-bottom: 8px; }
  .notfound p { color: rgba(91,202,238,0.5); margin-bottom: 16px; }
  .hint { font-size: 14px; color: rgba(91,202,238,0.5); }
  kbd { padding: 2px 6px; background: #1a2d42; border-radius: 4px; font-size: 12px; }
  .icon { width: 28px; height: 28px; }
</style>
"""

ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="28" height="28"><rect width="64" height="64" rx="12" fill="#0a1628"/><path d="M17 35L47 35L50 33L48 31L16 31L14 33Z" fill="#b8d4e3"/><path d="M24 35L24 40L21 45L17 45L17 50L47 50L47 45L43 45L40 40L40 35Z" fill="#b8d4e3" opacity="0.9"/><path d="M32 12C36 19 35 23 34 26Q33 28 32 29Q31 28 30 26C29 23 28 19 32 12Z" fill="#2aa4dd"/><path d="M27 17C29 21 28.5 24 27.5 26Q27 28 27 29Q26.5 27.5 26 26C25 24 24.5 21 27 17Z" fill="#5bcaee" opacity="0.8"/><path d="M37 17C35 21 35.5 24 36.5 26Q37 28 37 29Q37.5 27.5 38 26C39 24 39.5 21 37 17Z" fill="#5bcaee" opacity="0.8"/></svg>'

def page_editor(host):
    total = get_stats()
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forge Paste</title>{STYLE}</head><body>
<div class="header"><div class="left">{ICON_SVG}<h1>Forge Paste</h1>
<span class="meta">{total} paste{'s' if total != 1 else ''}</span></div>
<div class="right hint"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> to save</div></div>
<form class="editor" method="POST" action="/api/paste" id="form">
<textarea name="content" placeholder="Paste your logs, code, or text here..." autofocus id="ta"></textarea>
<div class="bar"><span class="chars" id="chars"></span><button type="submit" class="btn btn-primary" id="btn">Save Paste</button></div>
</form>
<div class="footer">Forge Paste &mdash; API: <code>curl -X POST -d "your text" {html.escape(host)}/api/paste</code></div>
<script>
const ta=document.getElementById('ta'),ch=document.getElementById('chars'),form=document.getElementById('form'),btn=document.getElementById('btn');
ta.addEventListener('input',()=>{{if(ta.value.length>0)ch.textContent=ta.value.length.toLocaleString()+' characters';else ch.textContent='';}});
document.addEventListener('keydown',e=>{{if(e.ctrlKey&&e.key==='Enter')form.submit();}});
form.addEventListener('submit',async e=>{{e.preventDefault();if(!ta.value.trim())return;btn.disabled=true;btn.textContent='Saving...';
const r=await fetch('/api/paste',{{method:'POST',body:ta.value}});const d=await r.json();
if(d.key)window.location='/'+d.key+'?new=1';else{{btn.disabled=false;btn.textContent='Save Paste';}}}});
</script></body></html>"""

def page_viewer(paste, host, is_new=False):
    lines = paste["content"].split("\n")
    gw = len(str(len(lines)))
    line_html = ""
    for i, line in enumerate(lines, 1):
        line_html += f'<div class="line"><span class="line-num" style="min-width:{gw}ch">{i}</span><span>{html.escape(line)}</span></div>'
    
    ta = time_ago(paste["created_at"])
    v = paste["views"]
    slug = paste["slug"]
    url = f"{host}/{slug}"
    expires_at = paste.get("expires_at")
    if expires_at:
        expires_in = expires_at - time.time()
        if expires_in > 86400:
            expiry_text = f"expires in {int(expires_in // 86400)}d"
        elif expires_in > 3600:
            expiry_text = f"expires in {int(expires_in // 3600)}h"
        elif expires_in > 0:
            expiry_text = "expires soon"
        else:
            expiry_text = "expired"
    else:
        expiry_text = "no expiry"
    
    if is_new:
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{slug} — Forge Paste</title>{STYLE}</head><body>
<div class="header"><div class="left"><a href="/" style="display:flex;align-items:center;gap:12px">{ICON_SVG}<h1>Forge Paste</h1></a></div>
<div class="right"><a href="/" class="btn btn-primary">New</a></div></div>
<div class="result"><div class="result-box"><div class="success">✓ Paste created!</div>
<div class="url-box"><a href="/{slug}" class="url">{html.escape(url)}</a></div>
<div class="actions"><button class="btn btn-primary" onclick="navigator.clipboard.writeText('{html.escape(url)}');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy URL',2000)">Copy URL</button>
<a href="/{slug}" class="btn btn-secondary">View Paste</a><a href="/" class="btn btn-secondary">New Paste</a></div></div></div></body></html>"""
    
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{slug} — Forge Paste</title>{STYLE}</head><body>
<div class="header"><div class="left"><a href="/" style="display:flex;align-items:center;gap:12px">{ICON_SVG}<h1>Forge Paste</h1></a>
<span class="slug">/{slug}</span></div>
<div class="right"><span class="meta">{ta} · {v} view{'s' if v != 1 else ''} · {expiry_text}</span>
<button class="btn btn-secondary" onclick="navigator.clipboard.writeText(document.getElementById('raw').textContent);this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)">Copy</button>
<a href="/api/raw/{slug}" class="btn btn-secondary">Raw</a>
<a href="/" class="btn btn-primary">New</a></div></div>
<div class="viewer"><pre id="raw-wrap">{line_html}</pre><pre id="raw" style="display:none">{html.escape(paste["content"])}</pre></div></body></html>"""

def page_notfound():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not Found — Forge Paste</title>{STYLE}</head><body>
<div class="header"><div class="left"><a href="/" style="display:flex;align-items:center;gap:12px">{ICON_SVG}<h1>Forge Paste</h1></a></div></div>
<div class="notfound"><div><div class="emoji">🔍</div><h2>Paste not found</h2>
<p>This paste may have expired or never existed.</p>
<a href="/" class="btn btn-primary">Create New Paste</a></div></div></body></html>"""


class PasteHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quiet logging
        pass
    
    def _send(self, code, content, content_type="text/html"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.wfile.write(content)
    
    def _host(self):
        proto = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "localhost")
        return f"{proto}://{host}"
    
    def do_OPTIONS(self):
        self._send(204, "")
    
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parsed.query
        host = self._host()
        
        if path == "" or path == "/":
            return self._send(200, page_editor(host))
        
        # API routes
        if path.startswith("/api/raw/"):
            slug = path[9:]
            paste = get_paste(slug)
            if not paste:
                return self._send(404, "Not found", "text/plain")
            return self._send(200, paste["content"], "text/plain; charset=utf-8")
        
        if path.startswith("/api/documents/"):
            slug = path[15:]
            paste = get_paste(slug)
            if not paste:
                return self._send(404, json.dumps({"error": "not found"}), "application/json")
            return self._send(200, json.dumps({"key": paste["slug"], "data": paste["content"]}), "application/json")
        
        # Web viewer
        slug = path[1:]  # Remove leading /
        if slug and all(c in string.ascii_lowercase + string.digits for c in slug):
            paste = get_paste(slug)
            if paste:
                is_new = "new=1" in query
                return self._send(200, page_viewer(paste, host, is_new))
            return self._send(404, page_notfound())
        
        self._send(404, page_notfound())
    
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "Invalid Content-Length"}), "application/json")
        if length < 0:
            return self._send(400, json.dumps({"error": "Invalid Content-Length"}), "application/json")
        if length > MAX_SIZE:
            return self._send(413, json.dumps({"error": "Content too large (max 512KB)"}), "application/json")
        
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        
        if not body.strip():
            return self._send(400, json.dumps({"error": "Empty content"}), "application/json")
        
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        host = self._host()
        ip = self.headers.get("X-Real-IP") or self.client_address[0]
        
        slug = create_paste(body, ip=ip)
        
        if path == "/api/documents":
            # Hastebin-compatible response
            return self._send(200, json.dumps({"key": slug}), "application/json")
        
        # Default API response
        self._send(200, json.dumps({"key": slug, "url": f"{host}/{slug}"}), "application/json")


def main():
    global DB_PATH, HOST, PORT, MAX_AGE_DAYS
    
    parser = argparse.ArgumentParser(description="Forge Paste Server")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port (default: {PORT})")
    parser.add_argument("--host", default=HOST, help=f"Host (default: {HOST})")
    parser.add_argument("--db", default=DB_PATH, help=f"Database path (default: {DB_PATH})")
    parser.add_argument("--max-age", type=int, default=MAX_AGE_DAYS, help=f"Paste TTL in days, 0 = never expire (default: {MAX_AGE_DAYS})")
    args = parser.parse_args()
    
    DB_PATH = args.db
    HOST = args.host
    PORT = args.port
    MAX_AGE_DAYS = args.max_age
    
    init_db()
    
    server = HTTPServer((HOST, PORT), PasteHandler)
    print(f"🔥 Forge Paste running on http://{HOST}:{PORT}")
    print(f"📦 Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()

if __name__ == "__main__":
    main()
