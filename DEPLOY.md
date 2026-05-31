# Readback — deployment runbook

Goal: get `index.html` live on a CDN and `main.py` running on your EC2 box behind HTTPS, with the free-tier cap and `/interest` logging working end to end.

Architecture:
- **Frontend** (`index.html`) → static host (Cloudflare Pages / Netlify / Vercel), e.g. `https://yourdomain.com`
- **API** (`main.py`) → uvicorn on EC2, behind nginx + Let's Encrypt, at `https://api.yourdomain.com`
- Frontend talks to API over HTTPS; CORS is locked to your frontend origin.

> Handing this to Claude Code: point it at the repo containing `main.py` and your existing pipeline, then say *"follow DEPLOY.md; wire my existing transcribe/summarize/send_email functions into the stubs in main.py, then do the server setup."* It can execute Phases 2–5 over SSH.

---

## Prerequisites
- A domain you control (DNS managed somewhere you can add records).
- EC2 instance (Ubuntu) with an **Elastic IP** attached so the address doesn't change on reboot.
- Security group inbound rules: **22** (SSH, your IP only), **80**, **443**. Do *not* expose 8000 publicly — nginx fronts it.
- Your existing API keys: AssemblyAI, Anthropic, SendGrid.

---

## Phase 1 — DNS
Add an **A record**: `api.yourdomain.com` → your EC2 Elastic IP.
Point the apex/`www` at your static host per its instructions (Phase 6).
Verify: `dig +short api.yourdomain.com` returns your Elastic IP.

---

## Phase 2 — Server prep
SSH in, then:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip nginx
sudo mkdir -p /opt/readback && sudo chown $USER:$USER /opt/readback
cd /opt/readback
# copy main.py here (scp, git clone, or Claude Code)
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install "fastapi" "uvicorn[standard]" "pydantic[email]"
# pydantic[email] is required — EmailStr needs the email-validator package
# add whatever your pipeline needs too: assemblyai, anthropic, sendgrid, etc.
```

The SQLite file `readback.db` will be created in `/opt/readback` on first run. That directory is your persistence — back it up if the Pro-intent and usage data matter to you.

---

## Phase 3 — Run the API as a service
Keep secrets out of code. Create an env file:

```bash
sudo tee /opt/readback/readback.env >/dev/null <<'EOF'
ASSEMBLYAI_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
SENDGRID_API_KEY=your_key
EOF
sudo chmod 600 /opt/readback/readback.env
```

(Read them in `main.py` with `os.environ[...]` inside your pipeline functions.)

Create the systemd unit:

```bash
sudo tee /etc/systemd/system/readback.service >/dev/null <<'EOF'
[Unit]
Description=Readback API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/readback
EnvironmentFile=/opt/readback/readback.env
ExecStart=/opt/readback/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now readback
sudo systemctl status readback --no-pager
```

Bound to `127.0.0.1` on purpose — only nginx (same box) can reach it. `--workers 2` is plenty for an MVP; the background jobs run in-process per worker, which is fine here. Swap to gunicorn with uvicorn workers later if you outgrow it.

Quick local check: `curl -s localhost:8000/stats` should return JSON.

---

## Phase 4 — nginx reverse proxy
```bash
sudo tee /etc/nginx/sites-available/readback >/dev/null <<'EOF'
server {
    listen 80;
    server_name api.yourdomain.com;

    # uploads to /submit are tiny (just a URL + email), but give headroom
    client_max_body_size 2m;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # transcription/summarization can take a while; don't cut long requests off
        # (only relevant if you later make /submit synchronous — the current
        #  design returns immediately and emails the result, so this is just safety)
        proxy_read_timeout 300s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/readback /etc/nginx/sites-enabled/readback
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Test over plain HTTP before TLS: `curl -s http://api.yourdomain.com/stats`.

---

## Phase 5 — HTTPS (Let's Encrypt)
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.yourdomain.com --redirect --agree-tos -m you@email.com --no-eff-email
```

Certbot rewrites the nginx config to add the 443 server block, the cert paths, and an HTTP→HTTPS redirect. Renewal is automatic via the certbot systemd timer; confirm with:

```bash
sudo certbot renew --dry-run
```

Now `https://api.yourdomain.com/stats` should work.

---

## Phase 6 — Frontend
Recommended: **Cloudflare Pages** or **Netlify** — drag-drop `index.html`, free, global CDN, instant HTTPS. (Or serve it from this same nginx box on the apex domain if you'd rather keep it all in one place.)

Before (or right after) deploying, edit `index.html`:
- `BACKEND_URL = "https://api.yourdomain.com"`
- `DEMO_MODE = false`

And in `main.py`, set `ALLOWED_ORIGINS` to your real frontend origin(s), e.g.:
```python
ALLOWED_ORIGINS = ["https://yourdomain.com", "https://www.yourdomain.com"]
```
Restart after editing: `sudo systemctl restart readback`.

> CORS gotcha: the origin must match exactly — scheme + host, no trailing slash. `https://yourdomain.com` ≠ `https://www.yourdomain.com`. Include every variant your page is served from.

---

## Phase 7 — Smoke test
From your laptop:

```bash
# 1. free submit works and counts down
curl -s -X POST https://api.yourdomain.com/submit \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/ep.mp3","email":"test@you.com"}'
# expect: {"ok":true,"job_id":1,"remaining":2}

# 2. run it 3 more times with the same email — the 4th should 429
# 3. Pro intent logs
curl -s -X POST https://api.yourdomain.com/interest \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@you.com","plan":"pro_9"}'

# 4. your dashboard numbers
curl -s https://api.yourdomain.com/stats
# expect: {"summaries_run":4,"emails_at_cap":1,"pro_clicks":1}
```

Then load the real page in a browser, submit a genuine episode, and confirm the email arrives. Hit the $9 button and confirm `pro_clicks` ticks up in `/stats`.

You're live. `/stats` is now your whole dashboard: how many summaries people pull, how many hit the free wall, and how many click the $9 button — your conversion signal, measured on real traffic.

---

## Notes for when it grows
- **Reset / inspect data:** it's just SQLite — `sqlite3 /opt/readback/readback.db` and query `jobs` / `interest` directly.
- **Abuse:** the cap is per-email, so burner addresses bypass it. If that bites, add a per-IP daily cap in `/submit` or require email verification before the first summary.
- **Logs:** `journalctl -u readback -f` for the API, `/var/log/nginx/` for the proxy.
- **Scaling the workers:** if jobs pile up, move transcription to a real queue (RQ/Celery + Redis) instead of in-process BackgroundTasks — but not before you need it.
