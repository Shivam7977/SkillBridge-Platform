# gunicorn.conf.py
# Place this file in your project root (same folder as app.py)
# Render will auto-detect it when running: gunicorn app:app

# ── Timeout ────────────────────────────────────────────────
# Default is 30s — way too short for:
#   - Mistral AI calls (up to 60s)
#   - Gmail SMTP email sends (can take 10-20s on cold connections)
#   - YouTube API calls (5-15s)
# 120s gives a safe buffer for all three in sequence.
timeout = 120

# ── Workers ────────────────────────────────────────────────
# Render free tier has 1 CPU. Keep at 1 to avoid OOM kills.
# (Render sets WEB_CONCURRENCY=1 anyway, this makes it explicit)
workers = 1

# ── Worker class ───────────────────────────────────────────
# 'sync' is fine for this app — no async needed
worker_class = "sync"

# ── Bind ───────────────────────────────────────────────────
# Render injects PORT automatically, but 10000 is the default
bind = "0.0.0.0:10000"

# ── Logging ────────────────────────────────────────────────
accesslog = "-"   # stdout
errorlog  = "-"   # stderr
loglevel  = "info"