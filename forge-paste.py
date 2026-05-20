#!/usr/bin/env python3
"""
Forge Paste — The ultimate self-hosted pastebin.
Zero dependencies · Hastebin-compatible · Full REST API · Syntax highlighting

Usage:
    python3 forge-paste.py [--port PORT] [--host HOST] [--db PATH] [--max-age DAYS]

API:
    POST   /api/paste            — Create paste (JSON body or raw text)
    GET    /api/paste/<slug>     — Get paste (JSON)
    PUT    /api/paste/<slug>     — Update paste (requires edit_token)
    DELETE /api/paste/<slug>     — Delete paste (requires edit_token)
    GET    /api/raw/<slug>       — Raw text output
    GET    /api/download/<slug>  — Download as file
    GET    /api/pastes/recent    — List recent pastes
    GET    /api/stats            — Server stats
    GET    /api/languages        — Supported language list
    GET    /api/health           — Health check
    POST   /api/documents       — Hastebin-compat create
    GET    /api/documents/<slug> — Hastebin-compat get
"""

import sqlite3, string, random, os, json, argparse, time, hashlib, secrets, re, gzip
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from io import BytesIO
from threading import Thread
import html as html_mod

# ── Configuration ───────────────────────────────────────────
DB_PATH = os.environ.get("FORGE_PASTE_DB", "/var/lib/forge-paste/pastes.db")
HOST = os.environ.get("FORGE_PASTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("FORGE_PASTE_PORT", "7890"))
MAX_SIZE = 2 * 1024 * 1024  # 2MB
MAX_AGE_DAYS = int(os.environ.get("FORGE_PASTE_MAX_AGE", "30"))
_CLEANUP_INTERVAL = 3600
_last_cleanup = 0
VERSION = "2.0.0"

# ── Expiry presets (seconds) ────────────────────────────────
EXPIRY_PRESETS = {
    "10m": 600, "1h": 3600, "1d": 86400, "7d": 604800,
    "30d": 2592000, "90d": 7776000, "1y": 31536000, "never": 0,
}

# ── Languages ───────────────────────────────────────────────
LANGUAGES = [
    "plaintext","bash","c","cpp","csharp","css","dart","diff","dockerfile",
    "elixir","erlang","go","graphql","haskell","html","ini","java",
    "javascript","json","julia","kotlin","latex","lua","makefile","markdown",
    "nginx","objectivec","ocaml","perl","php","powershell","properties",
    "python","r","ruby","rust","scala","scss","shell","sql","swift",
    "toml","typescript","xml","yaml","zig",
]

# Extensions → language mapping for auto-detect
EXT_MAP = {
    "py":"python","js":"javascript","ts":"typescript","rb":"ruby","rs":"rust",
    "go":"go","java":"java","c":"c","cpp":"cpp","h":"c","hpp":"cpp",
    "cs":"csharp","php":"php","sh":"bash","bash":"bash","zsh":"bash",
    "sql":"sql","html":"html","htm":"html","css":"css","scss":"scss",
    "json":"json","xml":"xml","yaml":"yaml","yml":"yaml","toml":"toml",
    "md":"markdown","dockerfile":"dockerfile","makefile":"makefile",
    "lua":"lua","swift":"swift","kt":"kotlin","scala":"scala",
    "hs":"haskell","ex":"elixir","erl":"erlang","r":"r","jl":"julia",
    "dart":"dart","zig":"zig","ini":"ini","conf":"ini","cfg":"ini",
    "diff":"diff","patch":"diff","ps1":"powershell",
}

def detect_language(content, filename=None):
    """Auto-detect language from filename extension or content heuristics."""
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in EXT_MAP:
            return EXT_MAP[ext]
    # Content heuristics
    first = content[:200].strip()
    if first.startswith("#!/usr/bin/env python") or first.startswith("#!/usr/bin/python"):
        return "python"
    if first.startswith("#!/bin/bash") or first.startswith("#!/bin/sh"):
        return "bash"
    if first.startswith("#!/usr/bin/env node"):
        return "javascript"
    if first.startswith("<?php"):
        return "php"
    if first.startswith("<!DOCTYPE html") or first.startswith("<html"):
        return "html"
    if first.startswith("---") and "\n" in first:
        return "yaml"
    if first.startswith("{") and '"' in first[:50]:
        try:
            json.loads(content[:1000] if len(content) > 1000 else content)
            return "json"
        except Exception:
            pass
    if re.match(r"^(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s", first, re.I):
        return "sql"
    if re.match(r"^(FROM|RUN|CMD|COPY|ENV|EXPOSE|WORKDIR)\s", first, re.I):
        return "dockerfile"
    if "def " in first and ":" in first:
        return "python"
    if "function " in first or "const " in first or "let " in first:
        return "javascript"
    if re.match(r"^(diff|---|\+\+\+|@@)\s", first):
        return "diff"
    return "plaintext"

# ── Database ────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS pastes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            title TEXT,
            content TEXT NOT NULL,
            language TEXT,
            created_at REAL NOT NULL DEFAULT (unixepoch()),
            updated_at REAL,
            views INTEGER NOT NULL DEFAULT 0,
            ip TEXT,
            expires_at REAL,
            burn_after_read INTEGER NOT NULL DEFAULT 0,
            edit_token TEXT,
            parent_slug TEXT,
            size INTEGER NOT NULL DEFAULT 0,
            is_public INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Migrations for existing installs
    cols = [row[1] for row in db.execute("PRAGMA table_info(pastes)").fetchall()]
    migrations = {
        "expires_at": "ALTER TABLE pastes ADD COLUMN expires_at REAL",
        "burn_after_read": "ALTER TABLE pastes ADD COLUMN burn_after_read INTEGER NOT NULL DEFAULT 0",
        "edit_token": "ALTER TABLE pastes ADD COLUMN edit_token TEXT",
        "parent_slug": "ALTER TABLE pastes ADD COLUMN parent_slug TEXT",
        "size": "ALTER TABLE pastes ADD COLUMN size INTEGER NOT NULL DEFAULT 0",
        "is_public": "ALTER TABLE pastes ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1",
        "title": "ALTER TABLE pastes ADD COLUMN title TEXT",
        "updated_at": "ALTER TABLE pastes ADD COLUMN updated_at REAL",
    }
    for col, sql in migrations.items():
        if col not in cols:
            db.execute(sql)
    # Backfill size
    db.execute("UPDATE pastes SET size = LENGTH(content) WHERE size = 0 AND content IS NOT NULL")
    # Backfill expires_at
    if MAX_AGE_DAYS > 0:
        db.execute("UPDATE pastes SET expires_at = created_at + ? WHERE expires_at IS NULL", (MAX_AGE_DAYS * 86400,))
    db.commit()
    db.execute("CREATE INDEX IF NOT EXISTS idx_slug ON pastes(slug)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_created ON pastes(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_expires ON pastes(expires_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_public ON pastes(is_public)")
    db.commit()
    db.close()

def generate_slug(length=8):
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choices(chars, k=length))

def cleanup_expired():
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    db = get_db()
    # Delete expired
    db.execute("DELETE FROM pastes WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
    db.commit()
    db.close()

def create_paste(content, title=None, language=None, ip=None, expires_in=None, burn=False, is_public=True, parent_slug=None):
    cleanup_expired()
    db = get_db()
    slug = generate_slug()
    while db.execute("SELECT 1 FROM pastes WHERE slug=?", (slug,)).fetchone():
        slug = generate_slug()
    edit_token = secrets.token_urlsafe(24)
    now = time.time()
    if expires_in is not None:
        expires_at = now + expires_in if expires_in > 0 else None
    elif MAX_AGE_DAYS > 0:
        expires_at = now + (MAX_AGE_DAYS * 86400)
    else:
        expires_at = None
    if not language:
        language = detect_language(content)
    size = len(content.encode("utf-8"))
    db.execute(
        """INSERT INTO pastes (slug, title, content, language, created_at, views, ip,
           expires_at, burn_after_read, edit_token, parent_slug, size, is_public)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, title, content, language, now, ip, expires_at,
         1 if burn else 0, edit_token, parent_slug, size, 1 if is_public else 0)
    )
    db.commit()
    db.close()
    return slug, edit_token

def get_paste(slug, increment_view=True):
    db = get_db()
    row = db.execute(
        "SELECT * FROM pastes WHERE slug=? AND (expires_at IS NULL OR expires_at > ?)",
        (slug, time.time()),
    ).fetchone()
    if not row:
        db.close()
        return None
    paste = dict(row)
    if increment_view:
        db.execute("UPDATE pastes SET views = views + 1 WHERE slug=?", (slug,))
        db.commit()
    # Handle burn after read
    if paste.get("burn_after_read") and increment_view:
        db.execute("DELETE FROM pastes WHERE slug=?", (slug,))
        db.commit()
        paste["_burned"] = True
    db.close()
    return paste

def update_paste(slug, content, title=None, language=None):
    db = get_db()
    now = time.time()
    size = len(content.encode("utf-8"))
    if not language:
        language = detect_language(content)
    db.execute(
        "UPDATE pastes SET content=?, title=?, language=?, updated_at=?, size=? WHERE slug=?",
        (content, title, language, now, size, slug)
    )
    db.commit()
    db.close()

def delete_paste(slug):
    db = get_db()
    db.execute("DELETE FROM pastes WHERE slug=?", (slug,))
    db.commit()
    db.close()

def get_recent(limit=20, offset=0):
    db = get_db()
    rows = db.execute(
        """SELECT slug, title, language, created_at, views, size, burn_after_read, expires_at
           FROM pastes WHERE is_public=1 AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (time.time(), limit, offset)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

def get_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM pastes").fetchone()[0]
    total_views = db.execute("SELECT COALESCE(SUM(views),0) FROM pastes").fetchone()[0]
    total_size = db.execute("SELECT COALESCE(SUM(size),0) FROM pastes").fetchone()[0]
    active = db.execute(
        "SELECT COUNT(*) FROM pastes WHERE expires_at IS NULL OR expires_at > ?",
        (time.time(),)).fetchone()[0]
    db.close()
    return {"total_pastes": total, "active_pastes": active, "total_views": total_views,
            "total_size": total_size, "version": VERSION}

def time_ago(ts):
    seconds = int(time.time() - ts)
    if seconds < 60: return "just now"
    if seconds < 3600: return f"{seconds // 60}m ago"
    if seconds < 86400: return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"

def format_bytes(b):
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    return f"{b/1048576:.1f} MB"

# ── HTML Templates ──────────────────────────────────────────
ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="28" height="28"><rect width="64" height="64" rx="12" fill="#0a1628"/><path d="M17 35L47 35L50 33L48 31L16 31L14 33Z" fill="#b8d4e3"/><path d="M24 35L24 40L21 45L17 45L17 50L47 50L47 45L43 45L40 40L40 35Z" fill="#b8d4e3" opacity="0.9"/><path d="M32 12C36 19 35 23 34 26Q33 28 32 29Q31 28 30 26C29 23 28 19 32 12Z" fill="#2aa4dd"/><path d="M27 17C29 21 28.5 24 27.5 26Q27 28 27 29Q26.5 27.5 26 26C25 24 24.5 21 27 17Z" fill="#5bcaee" opacity="0.8"/><path d="M37 17C35 21 35.5 24 36.5 26Q37 28 37 29Q37.5 27.5 38 26C39 24 39.5 21 37 17Z" fill="#5bcaee" opacity="0.8"/></svg>'
FAVICON = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='12' fill='%230a1628'/><path d='M32 12C36 19 35 23 34 26Q33 28 32 29Q31 28 30 26C29 23 28 19 32 12Z' fill='%232aa4dd'/></svg>"

CSS = """
<style>
:root {
  --bg: #0a0f1a; --bg2: #0e1726; --bg3: #131f30; --border: #1a2d42;
  --accent: #2aa4dd; --accent2: #5bcaee; --accent-dim: rgba(42,164,221,0.15);
  --text: #e2e8f0; --text2: #94a3b8; --text3: #64748b; --text4: #475569;
  --green: #22c55e; --red: #ef4444; --orange: #f59e0b; --purple: #a78bfa;
  --font: -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif;
  --mono: 'JetBrains Mono','Fira Code','Cascadia Code',Menlo,monospace;
  --radius: 10px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;display:flex;flex-direction:column;font-size:14px}
a{color:var(--accent);text-decoration:none;transition:color .15s}
a:hover{color:var(--accent2)}
::selection{background:var(--accent-dim);color:var(--accent2)}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--text4)}

/* Header */
.hdr{border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;gap:12px;background:var(--bg2);position:sticky;top:0;z-index:100;backdrop-filter:blur(12px)}
.hdr .left{display:flex;align-items:center;gap:10px;min-width:0}
.hdr h1{font-size:18px;font-weight:700;white-space:nowrap}
.hdr .slug{font-size:11px;color:var(--text3);font-family:var(--mono);overflow:hidden;text-overflow:ellipsis}
.hdr nav{display:flex;gap:6px;align-items:center}
.hdr .meta{font-size:11px;color:var(--text3);white-space:nowrap}
.hdr .sep{color:var(--border);font-size:11px}

/* Buttons */
.btn{padding:6px 14px;border-radius:7px;border:1px solid transparent;cursor:pointer;font-size:13px;font-weight:500;transition:all .15s;color:var(--text);font-family:var(--font);display:inline-flex;align-items:center;gap:6px;white-space:nowrap;text-decoration:none;line-height:1.4}
.btn svg{width:14px;height:14px;flex-shrink:0}
.btn-p{background:var(--accent);border-color:var(--accent);color:#fff}
.btn-p:hover{background:var(--accent2);border-color:var(--accent2);color:#fff}
.btn-s{background:var(--bg3);border-color:var(--border)}
.btn-s:hover{background:var(--border);border-color:var(--text4)}
.btn-g{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.3);color:var(--green)}
.btn-g:hover{background:rgba(34,197,94,.2)}
.btn-r{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.3);color:var(--red)}
.btn-r:hover{background:rgba(239,68,68,.2)}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-icon{padding:6px 8px}

/* Tags */
.tag{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:500;font-family:var(--mono)}
.tag-lang{background:var(--accent-dim);color:var(--accent2)}
.tag-burn{background:rgba(239,68,68,.12);color:var(--red)}
.tag-exp{background:rgba(245,158,11,.12);color:var(--orange)}

/* Editor */
.editor{flex:1;display:flex;flex-direction:column;padding:16px;gap:12px}
.editor .top-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.editor .top-bar input[type=text]{flex:1;min-width:200px;background:var(--bg3);color:var(--text);font-family:var(--font);font-size:13px;padding:7px 12px;border-radius:7px;border:1px solid var(--border);outline:none;transition:border .15s}
.editor .top-bar input:focus{border-color:var(--accent)}
.editor .top-bar input::placeholder{color:var(--text4)}
.editor select{background:var(--bg3);color:var(--text2);font-family:var(--mono);font-size:12px;padding:7px 10px;border-radius:7px;border:1px solid var(--border);outline:none;cursor:pointer;appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%2364748b'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 8px center;padding-right:24px}
.editor textarea{flex:1;width:100%;background:var(--bg2);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.65;padding:20px;border-radius:var(--radius);border:1px solid var(--border);resize:none;outline:none;tab-size:4;-moz-tab-size:4;transition:border .15s}
.editor textarea:focus{border-color:rgba(42,164,221,.4)}
.editor textarea::placeholder{color:var(--text4)}
.editor .bottom-bar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.editor .info{font-size:12px;color:var(--text3);display:flex;gap:12px;align-items:center}
.editor .opts{display:flex;gap:8px;flex-wrap:wrap;align-items:center}

/* Checkbox */
.chk{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--text2);user-select:none}
.chk input{display:none}
.chk .box{width:16px;height:16px;border-radius:4px;border:1.5px solid var(--border);display:flex;align-items:center;justify-content:center;transition:all .15s;background:var(--bg3)}
.chk input:checked+.box{background:var(--accent);border-color:var(--accent)}
.chk input:checked+.box::after{content:'✓';color:#fff;font-size:10px;font-weight:700}

/* Viewer */
.viewer{flex:1;padding:16px;display:flex;flex-direction:column;gap:12px}
.viewer .toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.viewer .toolbar .tags{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.code-wrap{flex:1;background:var(--bg2);border-radius:var(--radius);border:1px solid var(--border);overflow:auto;position:relative}
.code-wrap pre{margin:0;padding:20px;padding-left:0}
.code-wrap code{font-family:var(--mono)!important;font-size:13px!important;line-height:1.65!important;background:transparent!important}
.code-wrap .lines{display:flex}
.code-wrap .line-nums{user-select:none;text-align:right;padding:20px 0;padding-right:16px;padding-left:20px;color:var(--text4);font-family:var(--mono);font-size:13px;line-height:1.65;border-right:1px solid var(--border);position:sticky;left:0;background:var(--bg2);z-index:1}
.code-wrap .line-nums a{color:var(--text4);display:block}
.code-wrap .line-nums a:hover{color:var(--accent)}
.code-wrap .line-nums a.active{color:var(--accent);background:var(--accent-dim)}
.code-wrap .code-body{padding:20px;padding-left:16px;flex:1;overflow-x:auto}
.code-wrap .code-body pre{padding:0}

/* Burn notice */
.burn-notice{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.25);border-radius:var(--radius);padding:12px 16px;display:flex;align-items:center;gap:10px;font-size:13px;color:var(--red)}
.burn-notice svg{flex-shrink:0}

/* Result page */
.result{display:flex;align-items:center;justify-content:center;flex:1;padding:24px}
.result-card{max-width:520px;width:100%;background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:40px;text-align:center}
.result-card .icon{font-size:48px;margin-bottom:16px}
.result-card h2{font-size:20px;font-weight:600;margin-bottom:8px}
.result-card .url-box{background:var(--bg);border:1px solid var(--accent-dim);border-radius:var(--radius);padding:16px;margin:20px 0;font-family:var(--mono);word-break:break-all;color:var(--accent);font-size:15px}
.result-card .actions{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.result-card .sub{font-size:12px;color:var(--text3);margin-top:16px}
.result-card .sub code{background:var(--bg3);padding:2px 6px;border-radius:4px;font-size:11px}

/* 404 */
.notfound{display:flex;align-items:center;justify-content:center;flex:1;text-align:center;padding:40px}
.notfound .icon{font-size:64px;margin-bottom:16px}
.notfound h2{font-size:22px;margin-bottom:8px}
.notfound p{color:var(--text3);margin-bottom:20px}

/* Recent list */
.recent-page{flex:1;padding:24px;max-width:960px;margin:0 auto;width:100%}
.recent-page h2{font-size:18px;margin-bottom:16px}
.paste-list{display:flex;flex-direction:column;gap:8px}
.paste-item{display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);transition:border .15s}
.paste-item:hover{border-color:var(--accent-dim)}
.paste-item .pi-slug{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--accent);min-width:80px}
.paste-item .pi-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text2);font-size:13px}
.paste-item .pi-meta{font-size:11px;color:var(--text4);display:flex;gap:10px;white-space:nowrap}

/* API docs */
.docs-page{flex:1;padding:24px;max-width:960px;margin:0 auto;width:100%}
.docs-page h2{font-size:22px;margin-bottom:8px}
.docs-page .intro{color:var(--text2);margin-bottom:24px;line-height:1.6}
.endpoint{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;overflow:hidden}
.endpoint summary{padding:14px 16px;cursor:pointer;display:flex;align-items:center;gap:10px;font-size:13px;font-weight:500;list-style:none}
.endpoint summary::-webkit-details-marker{display:none}
.endpoint summary::before{content:'▸';color:var(--text4);font-size:11px;transition:transform .15s}
.endpoint[open] summary::before{transform:rotate(90deg)}
.endpoint .method{font-family:var(--mono);font-size:11px;font-weight:700;padding:3px 8px;border-radius:5px;letter-spacing:.5px}
.method-get{background:rgba(34,197,94,.12);color:var(--green)}
.method-post{background:rgba(42,164,221,.12);color:var(--accent)}
.method-put{background:rgba(245,158,11,.12);color:var(--orange)}
.method-delete{background:rgba(239,68,68,.12);color:var(--red)}
.endpoint .path{font-family:var(--mono);color:var(--text2);font-size:13px}
.endpoint .desc{color:var(--text3);font-size:13px;margin-left:auto}
.endpoint .body{padding:0 16px 16px;font-size:13px;color:var(--text2);line-height:1.6}
.endpoint .body h4{font-size:12px;font-weight:600;color:var(--text);margin:12px 0 6px;text-transform:uppercase;letter-spacing:.5px}
.endpoint .body pre{background:var(--bg);border:1px solid var(--border);border-radius:7px;padding:12px;overflow-x:auto;font-family:var(--mono);font-size:12px;line-height:1.5;color:var(--accent2);margin:6px 0}
.endpoint .body code{font-family:var(--mono);font-size:12px;background:var(--bg3);padding:1px 5px;border-radius:3px}
.endpoint .body table{width:100%;border-collapse:collapse;margin:6px 0}
.endpoint .body th,.endpoint .body td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);font-size:12px}
.endpoint .body th{color:var(--text3);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px}

/* Footer */
.ftr{border-top:1px solid var(--border);padding:10px 24px;text-align:center;font-size:11px;color:var(--text4);background:var(--bg2)}
.ftr code{color:var(--text3)}

/* Toast */
.toast{position:fixed;bottom:24px;right:24px;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:10px 18px;border-radius:8px;font-size:13px;z-index:999;transform:translateY(20px);opacity:0;transition:all .2s;pointer-events:none}
.toast.show{transform:translateY(0);opacity:1}

/* Responsive */
@media(max-width:640px){
  .hdr{padding:10px 14px;flex-wrap:wrap}
  .hdr nav{gap:4px}
  .editor,.viewer{padding:10px}
  .editor .top-bar{flex-direction:column}
  .code-wrap .line-nums{padding-left:10px;padding-right:10px}
  .code-wrap .code-body{padding:12px 10px}
  .btn{padding:5px 10px;font-size:12px}
  .result-card{padding:24px 16px}
  .docs-page,.recent-page{padding:16px}
  .paste-item{flex-direction:column;align-items:flex-start;gap:6px}
}
</style>
"""

HLJS_HEAD = """
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark-dimmed.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
"""

TOAST_JS = """<div class="toast" id="toast"></div>
<script>function toast(m,d=2000){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),d)}</script>"""

def page_head(title, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(title)}</title>
<link rel="icon" href="{FAVICON}">
{CSS}{extra_head}</head><body>"""

def page_nav(slug=None, extra_right=""):
    left = f'<a href="/" style="display:flex;align-items:center;gap:10px">{ICON_SVG}<h1>Forge Paste</h1></a>'
    if slug:
        left += f'<span class="slug">/{slug}</span>'
    return f"""<div class="hdr"><div class="left">{left}</div>
<nav>{extra_right}
<a href="/recent" class="btn btn-s">Recent</a>
<a href="/api" class="btn btn-s">API</a>
<a href="/" class="btn btn-p">+ New</a>
</nav></div>"""

def page_footer(host):
    return f'<div class="ftr">Forge Paste v{VERSION} · <code>curl -X POST -d "text" {html_mod.escape(host)}/api/paste</code></div>'

def page_editor(host, fork_paste=None):
    stats = get_stats()
    title_val = ""
    content_val = ""
    lang_val = ""
    if fork_paste:
        title_val = html_mod.escape(fork_paste.get("title") or "")
        content_val = html_mod.escape(fork_paste["content"])
        lang_val = fork_paste.get("language") or ""

    lang_options = '<option value="">Auto-detect</option>'
    for l in LANGUAGES:
        sel = ' selected' if l == lang_val else ''
        lang_options += f'<option value="{l}"{sel}>{l}</option>'

    expiry_options = ""
    for label, val in [("10 minutes","10m"),("1 hour","1h"),("1 day","1d"),("7 days","7d"),("30 days","30d"),("90 days","90d"),("1 year","1y"),("Never","never")]:
        sel = ' selected' if val == "30d" else ''
        expiry_options += f'<option value="{val}"{sel}>{label}</option>'

    return f"""{page_head("Forge Paste")}
{page_nav(extra_right=f'<span class="meta">{stats["active_pastes"]:,} pastes · {format_bytes(stats["total_size"])}</span><span class="sep">·</span>')}
<form class="editor" id="form">
<div class="top-bar">
<input type="text" name="title" placeholder="Title (optional)" value="{title_val}" id="title" maxlength="120">
<select name="language" id="lang">{lang_options}</select>
<select name="expires" id="expires">{expiry_options}</select>
</div>
<textarea name="content" placeholder="Paste your code, logs, config, or text here..." autofocus id="ta">{content_val}</textarea>
<div class="bottom-bar">
<div class="info">
<span id="chars"></span>
<span id="lines"></span>
</div>
<div class="opts">
<label class="chk"><input type="checkbox" id="burn"><span class="box"></span> Burn after read</label>
<label class="chk"><input type="checkbox" id="unlisted"><span class="box"></span> Unlisted</label>
<button type="submit" class="btn btn-p" id="btn">💾 Save Paste</button>
</div>
</div>
</form>
{page_footer(host)}
{TOAST_JS}
<script>
const ta=document.getElementById('ta'),ch=document.getElementById('chars'),ln=document.getElementById('lines');
function upd(){{
  const v=ta.value;
  ch.textContent=v.length?v.length.toLocaleString()+' chars':'';
  ln.textContent=v.length?(v.split('\\n').length)+' lines':'';
}}
ta.addEventListener('input',upd);upd();
ta.addEventListener('keydown',e=>{{if(e.key==='Tab'){{e.preventDefault();const s=ta.selectionStart,en=ta.selectionEnd;ta.value=ta.value.substring(0,s)+'\\t'+ta.value.substring(en);ta.selectionStart=ta.selectionEnd=s+1;upd()}}}});
document.addEventListener('keydown',e=>{{if((e.ctrlKey||e.metaKey)&&e.key==='Enter')document.getElementById('form').dispatchEvent(new Event('submit'))}});
document.getElementById('form').addEventListener('submit',async e=>{{
  e.preventDefault();const btn=document.getElementById('btn');
  if(!ta.value.trim())return toast('Nothing to paste!');
  btn.disabled=true;btn.textContent='Saving...';
  try{{
    const body={{
      content:ta.value,
      title:document.getElementById('title').value||undefined,
      language:document.getElementById('lang').value||undefined,
      expires:document.getElementById('expires').value,
      burn:document.getElementById('burn').checked,
      unlisted:document.getElementById('unlisted').checked
    }};
    const r=await fetch('/api/paste',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
    const d=await r.json();
    if(d.key)window.location='/'+d.key+'?new=1&et='+encodeURIComponent(d.edit_token||'');
    else{{toast(d.error||'Error');btn.disabled=false;btn.textContent='💾 Save Paste';}}
  }}catch(err){{toast('Network error');btn.disabled=false;btn.textContent='💾 Save Paste';}}
}});
</script></body></html>"""

def page_viewer(paste, host, is_new=False, edit_token=None):
    slug = paste["slug"]
    lang = paste.get("language") or "plaintext"
    title = paste.get("title") or slug
    ta = time_ago(paste["created_at"])
    v = paste["views"]
    size = format_bytes(paste.get("size", len(paste["content"])))
    burn = paste.get("burn_after_read", 0)
    burned = paste.get("_burned", False)
    parent = paste.get("parent_slug")
    url = f"{host}/{slug}"
    lines = paste["content"].split("\n")
    lc = len(lines)
    gw = len(str(lc))

    expiry_html = ""
    ea = paste.get("expires_at")
    if ea:
        rem = ea - time.time()
        if rem > 86400:
            expiry_html = f'<span class="tag tag-exp">expires in {int(rem//86400)}d</span>'
        elif rem > 3600:
            expiry_html = f'<span class="tag tag-exp">expires in {int(rem//3600)}h</span>'
        elif rem > 0:
            expiry_html = '<span class="tag tag-exp">expires soon</span>'

    line_nums = "\n".join(f'<a href="#L{i}" id="L{i}">{i}</a>' for i in range(1, lc + 1))
    code_escaped = html_mod.escape(paste["content"])

    if is_new:
        et_param = f"&et={html_mod.escape(edit_token)}" if edit_token else ""
        et_display = f'<div class="sub">Edit token: <code>{html_mod.escape(edit_token or "")}</code> — save this to edit/delete later</div>' if edit_token else ""
        return f"""{page_head(f"{title} — Forge Paste")}
{page_nav(slug)}
<div class="result"><div class="result-card">
<div class="icon">🔥</div>
<h2>Paste created!</h2>
<div class="url-box"><a href="/{slug}">{html_mod.escape(url)}</a></div>
<div class="actions">
<button class="btn btn-p" onclick="navigator.clipboard.writeText('{html_mod.escape(url)}');toast('URL copied!')">📋 Copy URL</button>
<a href="/{slug}" class="btn btn-s">View</a>
<a href="/api/raw/{slug}" class="btn btn-s">Raw</a>
<a href="/" class="btn btn-s">+ New</a>
</div>
{et_display}
{"<div class='sub' style='color:var(--red)'>🔥 This paste will be destroyed after being read once</div>" if burn else ""}
</div></div>
{page_footer(host)}{TOAST_JS}</body></html>"""

    burn_html = ""
    if burned:
        burn_html = '<div class="burn-notice"><svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 9v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>🔥 This paste has been burned — this is the only time you can view it.</div>'
    elif burn:
        burn_html = '<div class="burn-notice"><svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 9v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>🔥 Burn after read — this paste will self-destruct after being viewed</div>'

    meta_parts = [ta, f"{v} view{'s' if v!=1 else ''}", size, f"{lc} lines"]
    meta_str = " · ".join(meta_parts)
    parent_html = f' · forked from <a href="/{parent}">{parent}</a>' if parent else ""

    right_btns = f"""
<span class="meta">{meta_str}{parent_html}</span><span class="sep">·</span>
<button class="btn btn-s" onclick="navigator.clipboard.writeText(document.getElementById('raw').textContent);toast('Copied!')">📋 Copy</button>
<a href="/api/raw/{slug}" class="btn btn-s">Raw</a>
<a href="/api/download/{slug}" class="btn btn-s">⬇ Download</a>
<a href="/{slug}/fork" class="btn btn-s">🍴 Fork</a>"""

    return f"""{page_head(f"{title} — Forge Paste", HLJS_HEAD)}
{page_nav(slug, extra_right=right_btns)}
<div class="viewer">
<div class="toolbar">
<div class="tags">
<span class="tag tag-lang">{html_mod.escape(lang)}</span>
{expiry_html}
{"<span class='tag tag-burn'>🔥 burn</span>" if burn and not burned else ""}
</div>
{f'<h3 style="font-size:15px;font-weight:600">{html_mod.escape(paste.get("title") or "")}</h3>' if paste.get("title") else ""}
</div>
{burn_html}
<div class="code-wrap">
<div class="lines">
<div class="line-nums">{line_nums}</div>
<div class="code-body"><pre><code class="language-{html_mod.escape(lang)}" id="hl">{code_escaped}</code></pre></div>
</div>
</div>
</div>
<pre id="raw" style="display:none">{code_escaped}</pre>
{page_footer(host)}{TOAST_JS}
<script>
hljs.highlightElement(document.getElementById('hl'));
// Line highlight from hash
function highlightLine(){{
  document.querySelectorAll('.line-nums a.active').forEach(a=>a.classList.remove('active'));
  const h=location.hash;if(h&&h.startsWith('#L')){{
    const el=document.getElementById(h.slice(1));if(el){{el.classList.add('active');el.scrollIntoView({{block:'center'}})}}
  }}
}}
window.addEventListener('hashchange',highlightLine);highlightLine();
</script></body></html>"""

def page_notfound():
    return f"""{page_head("Not Found — Forge Paste")}
{page_nav()}
<div class="notfound"><div>
<div class="icon">🔍</div>
<h2>Paste not found</h2>
<p>This paste may have expired, been burned, or never existed.</p>
<a href="/" class="btn btn-p">+ Create New Paste</a>
</div></div></body></html>"""

def page_recent(host, pastes):
    items = ""
    for p in pastes:
        t = html_mod.escape(p.get("title") or "")
        slug = p["slug"]
        lang = p.get("language") or "?"
        views = p.get("views", 0)
        ta_str = time_ago(p["created_at"])
        sz = format_bytes(p.get("size", 0))
        burn_tag = ' <span class="tag tag-burn" style="font-size:10px">🔥</span>' if p.get("burn_after_read") else ""
        items += f"""<a href="/{slug}" class="paste-item">
<span class="pi-slug">{slug}</span>
<span class="pi-title">{t or '<em style="color:var(--text4)">untitled</em>'}{burn_tag}</span>
<span class="pi-meta"><span class="tag tag-lang" style="font-size:10px">{html_mod.escape(lang)}</span><span>{sz}</span><span>{views} views</span><span>{ta_str}</span></span>
</a>"""
    return f"""{page_head("Recent — Forge Paste")}
{page_nav()}
<div class="recent-page">
<h2>📋 Recent Pastes</h2>
<div class="paste-list">{items if items else '<p style="color:var(--text3);padding:40px;text-align:center">No pastes yet. <a href="/">Create one!</a></p>'}</div>
</div>{page_footer(host)}</body></html>"""

def page_api_docs(host):
    h = html_mod.escape(host)
    return f"""{page_head("API — Forge Paste")}
{page_nav()}
<div class="docs-page">
<h2>🔧 API Documentation</h2>
<p class="intro">Forge Paste provides a full REST API with hastebin compatibility. All endpoints accept and return JSON. Base URL: <code>{h}</code></p>

<details class="endpoint" open>
<summary><span class="method method-post">POST</span><span class="path">/api/paste</span><span class="desc">Create a new paste</span></summary>
<div class="body">
<h4>Request (JSON)</h4>
<table><tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr>
<tr><td><code>content</code></td><td>string</td><td>✅</td><td>Paste content (max 2MB)</td></tr>
<tr><td><code>title</code></td><td>string</td><td></td><td>Optional title (max 120 chars)</td></tr>
<tr><td><code>language</code></td><td>string</td><td></td><td>Syntax language (auto-detected if omitted)</td></tr>
<tr><td><code>expires</code></td><td>string</td><td></td><td>Expiry: <code>10m</code> <code>1h</code> <code>1d</code> <code>7d</code> <code>30d</code> <code>90d</code> <code>1y</code> <code>never</code></td></tr>
<tr><td><code>burn</code></td><td>bool</td><td></td><td>Destroy after first read</td></tr>
<tr><td><code>unlisted</code></td><td>bool</td><td></td><td>Hide from recent list</td></tr>
</table>
<h4>Alternative: Raw Body</h4>
<p>Send raw text as the body with any content type other than <code>application/json</code>:</p>
<pre>curl -X POST -d "Hello World" {h}/api/paste</pre>
<h4>Response</h4>
<pre>{{
  "key": "a1b2c3d4",
  "url": "{h}/a1b2c3d4",
  "raw_url": "{h}/api/raw/a1b2c3d4",
  "edit_token": "xyz...",
  "expires_at": 1716300000.0
}}</pre>
<h4>Examples</h4>
<pre># Simple paste
curl -X POST -d "your text here" {h}/api/paste

# JSON with options
curl -X POST -H "Content-Type: application/json" \\
  -d '{{"content":"print(42)","language":"python","expires":"1h","burn":true}}' \\
  {h}/api/paste

# Pipe a file
cat script.py | curl -X POST -d @- {h}/api/paste

# With title
curl -X POST -H "Content-Type: application/json" \\
  -d '{{"content":"...","title":"My Config"}}' {h}/api/paste</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/paste/&lt;slug&gt;</span><span class="desc">Get paste metadata & content</span></summary>
<div class="body">
<h4>Response</h4>
<pre>{{
  "key": "a1b2c3d4",
  "title": "My Paste",
  "content": "...",
  "language": "python",
  "created_at": 1716200000.0,
  "views": 42,
  "size": 1234,
  "expires_at": 1716300000.0
}}</pre>
<pre>curl {h}/api/paste/a1b2c3d4</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/raw/&lt;slug&gt;</span><span class="desc">Get raw text content</span></summary>
<div class="body">
<p>Returns the paste content as <code>text/plain</code>.</p>
<pre>curl {h}/api/raw/a1b2c3d4</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/download/&lt;slug&gt;</span><span class="desc">Download as file</span></summary>
<div class="body">
<p>Returns the paste with <code>Content-Disposition: attachment</code> header. Optional <code>?filename=name.ext</code> query param.</p>
<pre>curl -OJ {h}/api/download/a1b2c3d4</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-put">PUT</span><span class="path">/api/paste/&lt;slug&gt;</span><span class="desc">Update a paste</span></summary>
<div class="body">
<h4>Request</h4>
<table><tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr>
<tr><td><code>edit_token</code></td><td>string</td><td>✅</td><td>Token from create response</td></tr>
<tr><td><code>content</code></td><td>string</td><td>✅</td><td>New content</td></tr>
<tr><td><code>title</code></td><td>string</td><td></td><td>New title</td></tr>
<tr><td><code>language</code></td><td>string</td><td></td><td>New language</td></tr>
</table>
<pre>curl -X PUT -H "Content-Type: application/json" \\
  -d '{{"edit_token":"xyz...","content":"updated"}}' \\
  {h}/api/paste/a1b2c3d4</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-delete">DELETE</span><span class="path">/api/paste/&lt;slug&gt;</span><span class="desc">Delete a paste</span></summary>
<div class="body">
<h4>Request</h4>
<pre>curl -X DELETE -H "Content-Type: application/json" \\
  -d '{{"edit_token":"xyz..."}}' \\
  {h}/api/paste/a1b2c3d4</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/pastes/recent</span><span class="desc">List recent public pastes</span></summary>
<div class="body">
<p>Query: <code>?limit=20&amp;offset=0</code></p>
<pre>curl {h}/api/pastes/recent?limit=10</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/stats</span><span class="desc">Server statistics</span></summary>
<div class="body">
<pre>curl {h}/api/stats</pre>
<pre>{{
  "total_pastes": 420,
  "active_pastes": 380,
  "total_views": 12500,
  "total_size": 2048000,
  "version": "{VERSION}"
}}</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/languages</span><span class="desc">List supported languages</span></summary>
<div class="body">
<pre>curl {h}/api/languages</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/health</span><span class="desc">Health check</span></summary>
<div class="body">
<pre>curl {h}/api/health</pre>
<pre>{{"status":"ok","version":"{VERSION}"}}</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-post">POST</span><span class="path">/api/documents</span><span class="desc">Hastebin-compatible create</span></summary>
<div class="body">
<p>Send raw text body. Returns <code>{{"key":"..."}}</code></p>
<pre>curl -X POST -d "hello" {h}/api/documents</pre>
</div></details>

<details class="endpoint">
<summary><span class="method method-get">GET</span><span class="path">/api/documents/&lt;slug&gt;</span><span class="desc">Hastebin-compatible get</span></summary>
<div class="body">
<pre>curl {h}/api/documents/a1b2c3d4</pre>
<pre>{{"key":"a1b2c3d4","data":"..."}}</pre>
</div></details>

<h3 style="margin-top:32px;font-size:16px">🖥️ CLI Aliases</h3>
<p class="intro">Add these to your <code>.bashrc</code> or <code>.zshrc</code>:</p>
<pre style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px;font-family:var(--mono);font-size:12px;line-height:1.6;color:var(--accent2);overflow-x:auto"># Paste from clipboard
alias fpaste='xclip -selection clipboard -o | curl -s -X POST -d @- {h}/api/paste | jq -r .url'

# Paste a file
fp() {{ curl -s -X POST -d @"$1" {h}/api/paste | jq -r .url; }}

# Paste from stdin
alias fp-='curl -s -X POST -d @- {h}/api/paste | jq -r .url'

# Get a paste
fpget() {{ curl -s {h}/api/raw/"$1"; }}
</pre>

</div>{page_footer(host)}</body></html>"""

# ── HTTP Handler ────────────────────────────────────────────
class PasteHandler(BaseHTTPRequestHandler):
    server_version = f"ForgePaste/{VERSION}"

    def log_message(self, format, *args):
        pass  # Quiet

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, content):
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, content, filename=None):
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(code)
        if filename:
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        else:
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _host(self):
        proto = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "localhost")
        return f"{proto}://{host}"

    def _ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            return None, "Invalid Content-Length"
        if length > MAX_SIZE:
            return None, f"Content too large (max {format_bytes(MAX_SIZE)})"
        return self.rfile.read(length), None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        host = self._host()

        # Static pages
        if path == "/":
            return self._html(200, page_editor(host))
        if path == "/recent":
            pastes = get_recent(limit=50)
            return self._html(200, page_recent(host, pastes))
        if path == "/api" or path == "/api/docs":
            return self._html(200, page_api_docs(host))

        # API: health
        if path == "/api/health":
            return self._json(200, {"status": "ok", "version": VERSION})

        # API: stats
        if path == "/api/stats":
            return self._json(200, get_stats())

        # API: languages
        if path == "/api/languages":
            return self._json(200, {"languages": LANGUAGES})

        # API: recent
        if path == "/api/pastes/recent":
            limit = min(int(query.get("limit", [20])[0]), 100)
            offset = max(int(query.get("offset", [0])[0]), 0)
            return self._json(200, {"pastes": get_recent(limit, offset)})

        # API: raw
        if path.startswith("/api/raw/"):
            slug = path[9:]
            paste = get_paste(slug)
            if not paste:
                return self._text(404, "Not found")
            return self._text(200, paste["content"])

        # API: download
        if path.startswith("/api/download/"):
            slug = path[14:]
            paste = get_paste(slug)
            if not paste:
                return self._text(404, "Not found")
            lang = paste.get("language") or "txt"
            ext_map = {"python":"py","javascript":"js","typescript":"ts","bash":"sh","ruby":"rb",
                       "rust":"rs","csharp":"cs","plaintext":"txt","markdown":"md","yaml":"yml"}
            ext = ext_map.get(lang, lang)
            fname = query.get("filename", [f"{slug}.{ext}"])[0]
            return self._text(200, paste["content"], filename=fname)

        # API: paste detail
        if path.startswith("/api/paste/"):
            slug = path[11:]
            paste = get_paste(slug)
            if not paste:
                return self._json(404, {"error": "not found"})
            return self._json(200, {
                "key": paste["slug"], "title": paste.get("title"),
                "content": paste["content"], "language": paste.get("language"),
                "created_at": paste["created_at"], "updated_at": paste.get("updated_at"),
                "views": paste["views"], "size": paste.get("size"),
                "expires_at": paste.get("expires_at"),
                "burn_after_read": bool(paste.get("burn_after_read")),
            })

        # API: hastebin compat
        if path.startswith("/api/documents/"):
            slug = path[15:]
            paste = get_paste(slug)
            if not paste:
                return self._json(404, {"error": "not found"})
            return self._json(200, {"key": paste["slug"], "data": paste["content"]})

        # Web: fork
        if path.endswith("/fork"):
            slug = path[1:-5]
            paste = get_paste(slug, increment_view=False)
            if not paste:
                return self._html(404, page_notfound())
            paste["parent_slug"] = slug
            return self._html(200, page_editor(host, fork_paste=paste))

        # Web: view paste
        slug = path[1:]
        if slug and re.match(r'^[a-z0-9]+$', slug):
            paste = get_paste(slug)
            if paste:
                is_new = "new" in query
                et = query.get("et", [None])[0]
                return self._html(200, page_viewer(paste, host, is_new, edit_token=et))
            return self._html(404, page_notfound())

        self._html(404, page_notfound())

    def do_POST(self):
        body, err = self._read_body()
        if err:
            return self._json(400, {"error": err})
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        host = self._host()
        ip = self._ip()

        # Hastebin compat
        if path == "/api/documents":
            text = body.decode("utf-8", errors="replace")
            if not text.strip():
                return self._json(400, {"error": "Empty content"})
            slug, _ = create_paste(text, ip=ip)
            return self._json(200, {"key": slug})

        # Main create
        if path == "/api/paste":
            ct = self.headers.get("Content-Type", "")
            if "application/json" in ct:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return self._json(400, {"error": "Invalid JSON"})
                content = data.get("content", "")
                title = data.get("title")
                language = data.get("language")
                expires_key = data.get("expires", "30d")
                burn = data.get("burn", False)
                unlisted = data.get("unlisted", False)
                parent = data.get("parent_slug")
            else:
                content = body.decode("utf-8", errors="replace")
                title = None
                language = None
                expires_key = "30d"
                burn = False
                unlisted = False
                parent = None

            if not content or not content.strip():
                return self._json(400, {"error": "Empty content"})
            if len(content.encode()) > MAX_SIZE:
                return self._json(413, {"error": f"Content too large (max {format_bytes(MAX_SIZE)})"})

            expires_in = EXPIRY_PRESETS.get(expires_key, EXPIRY_PRESETS["30d"])

            slug, edit_token = create_paste(
                content, title=title, language=language, ip=ip,
                expires_in=expires_in, burn=burn,
                is_public=not unlisted, parent_slug=parent
            )
            result = {
                "key": slug, "url": f"{host}/{slug}",
                "raw_url": f"{host}/api/raw/{slug}",
                "edit_token": edit_token,
            }
            if expires_in > 0:
                result["expires_at"] = time.time() + expires_in
            return self._json(200, result)

        self._json(404, {"error": "Not found"})

    def do_PUT(self):
        body, err = self._read_body()
        if err:
            return self._json(400, {"error": err})
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/paste/"):
            slug = path[11:]
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return self._json(400, {"error": "Invalid JSON"})

            token = data.get("edit_token", "")
            if not token:
                return self._json(401, {"error": "edit_token required"})

            paste = get_paste(slug, increment_view=False)
            if not paste:
                return self._json(404, {"error": "not found"})
            if paste.get("edit_token") != token:
                return self._json(403, {"error": "Invalid edit_token"})

            content = data.get("content")
            if not content or not content.strip():
                return self._json(400, {"error": "Empty content"})

            update_paste(slug, content, title=data.get("title"), language=data.get("language"))
            return self._json(200, {"key": slug, "updated": True})

        self._json(404, {"error": "Not found"})

    def do_DELETE(self):
        body, err = self._read_body()
        if err:
            return self._json(400, {"error": err})
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/paste/"):
            slug = path[11:]
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                return self._json(400, {"error": "Invalid JSON"})

            token = data.get("edit_token", "")
            if not token:
                return self._json(401, {"error": "edit_token required"})

            paste = get_paste(slug, increment_view=False)
            if not paste:
                return self._json(404, {"error": "not found"})
            if paste.get("edit_token") != token:
                return self._json(403, {"error": "Invalid edit_token"})

            delete_paste(slug)
            return self._json(200, {"key": slug, "deleted": True})

        self._json(404, {"error": "Not found"})


# ── Threaded server ─────────────────────────────────────────
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def main():
    global DB_PATH, HOST, PORT, MAX_AGE_DAYS

    parser = argparse.ArgumentParser(description="Forge Paste Server")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--max-age", type=int, default=MAX_AGE_DAYS)
    args = parser.parse_args()

    DB_PATH = args.db
    HOST = args.host
    PORT = args.port
    MAX_AGE_DAYS = args.max_age

    init_db()

    server = ThreadedHTTPServer((HOST, PORT), PasteHandler)
    print(f"🔥 Forge Paste v{VERSION} running on http://{HOST}:{PORT}")
    print(f"📦 Database: {DB_PATH}")
    print(f"⏱  Max age: {MAX_AGE_DAYS} days (0=never)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()

if __name__ == "__main__":
    main()
