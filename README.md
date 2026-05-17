# 🔥 Forge Paste

A lightweight, self-hosted paste server with a clean UI. Hastebin-compatible API.

**Zero dependencies** — runs on Python 3.10+ with the standard library only. Uses SQLite for storage.

## Quick Install

```bash
# Clone
git clone https://github.com/everestmcarthur/forge-paste.git /opt/forge-paste

# Create data directory
sudo mkdir -p /var/lib/forge-paste
sudo chown www-data:www-data /var/lib/forge-paste

# Install systemd service
sudo cp /opt/forge-paste/forge-paste.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forge-paste
```

## API

```bash
# Create a paste
curl -X POST -d "your text here" https://logs.yourdomain.com/api/paste
# → {"key": "abc123", "url": "https://logs.yourdomain.com/abc123"}

# Hastebin-compatible
curl -X POST -d "your text" https://logs.yourdomain.com/api/documents
# → {"key": "abc123"}

# Get raw text
curl https://logs.yourdomain.com/api/raw/abc123

# Get JSON
curl https://logs.yourdomain.com/api/documents/abc123
# → {"key": "abc123", "data": "your text"}
```

## Wings/Spark Integration

Upload diagnostics directly:
```bash
sudo spark diagnostics --hastebin-url=https://logs.yourdomain.com
```

Upload logs via curl:
```bash
tail -n 300 /var/www/forge/storage/logs/laravel-$(date +%F).log | curl -X POST --data-binary @- https://logs.yourdomain.com/api/paste
```

## Caddy Reverse Proxy

```
logs.yourdomain.com {
    reverse_proxy 127.0.0.1:7890
}
```

## Configuration

| Env / Flag | Default | Description |
|---|---|---|
| `FORGE_PASTE_PORT` / `--port` | 7890 | Listen port |
| `FORGE_PASTE_HOST` / `--host` | 127.0.0.1 | Listen address |
| `FORGE_PASTE_DB` / `--db` | /var/lib/forge-paste/pastes.db | SQLite database path |

## License

MIT
