# Earnote — deployment runbook

Goal: get the whole app live on **one EC2 box** behind HTTPS — `index.html` served as a
static site at `https://earnote.app`, and the FastAPI backend at `https://api.earnote.app` —
with the free-tier cap and `/interest` (the $9 Pro intent probe) working end to end.

Domain: **earnote.app** (Cloudflare registrar + DNS). `.app` forces HTTPS at the browser
level, so plain-HTTP access won't work once you're live — that's expected.

Architecture (single box):
- **Frontend** (`index.html`) → nginx serves static files on `earnote.app` + `www.earnote.app`
- **API** (`main.py`) → uvicorn on 127.0.0.1:8000, nginx reverse-proxies `api.earnote.app` to it
- Frontend calls the API cross-origin (`earnote.app` → `api.earnote.app`), so CORS must list the frontend origins exactly.

> Handing this to Claude Code: point it at this repo and say *"follow DEPLOY.md and execute
> the server setup over SSH to my EC2 box at <IP>; the pipeline functions in main.py are already
> wired."* It can run Phases 2–6. See CLAUDE.md for project context.

---

## Prerequisites
- **earnote.app** registered (done) with Cloudflare managing DNS.
- EC2 instance (Ubuntu) with an **Elastic IP** attached so the address survives reboots.
- Security group inbound: **22** (SSH, your IP only), **80**, **443**. Do *not* expose 8000 — nginx fronts it.
- API keys ready: AssemblyAI, Anthropic, SendGrid.

---

## Phase 1 — DNS (Cloudflare)
In Cloudflare → your domain → DNS → Records, add three A records, all pointing at your
**Elastic IP**, all **grey-cloud (DNS only)** for now (Proxied/orange can interfere with the
certbot HTTP challenge — flip to orange later if you want the CDN):

| Type | Name  | Content (IPv4)   | Proxy     |
|------|-------|------------------|-----------|
| A    | `@`   | <ELASTIC_IP>     | DNS only  |
| A    | `www` | <ELASTIC_IP>     | DNS only  |
| A    | `api` | <ELASTIC_IP>     | DNS only  |

Verify (allow a few minutes to propagate):
```bash
dig +short earnote.app
dig +short www.earnote.app
dig +short api.earnote.app
# all three should return your Elastic IP
```

---

## Phase 2 — Server prep
SSH in, then:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip nginx
sudo mkdir -p /opt/earnote && sudo chown $USER:$USER /opt/earnote
cd /opt/earnote
# copy main.py here (scp, git clone, or Claude Code)
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install "fastapi" "uvicorn[standard]" "pydantic[email]" assemblyai anthropic sendgrid yt-dlp
# pydantic[email] is required — EmailStr needs the email-validator package
# yt-dlp resolves non-direct audio (YouTube / podcast host pages) for the pipeline
```

The SQLite file `readback.db` is created in `/opt/earnote` on first run. That file is your
persistence (usage counts + Pro-intent log) — back it up if the data matters.

---

## Phase 3 — Run the API as a service
Keep secrets out of code. Create an env file:

```bash
sudo tee /opt/earnote/earnote.env >/dev/null <<'EOF'
ASSEMBLYAI_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
SENDGRID_API_KEY=your_key
CLAUDE_MODEL=claude-sonnet-4-6
FROM_EMAIL=hello@earnote.app
EOF
sudo chmod 600 /opt/earnote/earnote.env
```

(`main.py` reads these with `os.environ[...]`. `FROM_EMAIL` must be on the SendGrid-authenticated domain — see Phase 7.)

Create the systemd unit:

```bash
sudo tee /etc/systemd/system/earnote.service >/dev/null <<'EOF'
[Unit]
Description=Earnote API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/earnote
EnvironmentFile=/opt/earnote/earnote.env
ExecStart=/opt/earnote/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now earnote
sudo systemctl status earnote --no-pager
```

Bound to `127.0.0.1` on purpose — only nginx (same box) reaches it. Quick check:
`curl -s localhost:8000/stats` should return JSON.

---

## Phase 4 — Frontend files (served from this box)
```bash
sudo mkdir -p /var/www/earnote
sudo cp /opt/earnote/index.html /var/www/earnote/index.html   # adjust source path as needed
sudo chown -R www-data:www-data /var/www/earnote
```
Redeploying the frontend later = overwrite this one file.

---

## Phase 5 — nginx (two server blocks: static site + API proxy)

**Frontend** (apex + www → static files):
```bash
sudo tee /etc/nginx/sites-available/earnote-frontend >/dev/null <<'EOF'
server {
    listen 80;
    server_name earnote.app www.earnote.app;

    root /var/www/earnote;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/earnote-frontend /etc/nginx/sites-enabled/earnote-frontend
```

**API** (api subdomain → uvicorn):
```bash
sudo tee /etc/nginx/sites-available/earnote-api >/dev/null <<'EOF'
server {
    listen 80;
    server_name api.earnote.app;

    client_max_body_size 2m;   # /submit payloads are tiny (URL + email)

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # safety; /submit returns immediately & emails the result
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/earnote-api /etc/nginx/sites-enabled/earnote-api

sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

nginx routes by `server_name`, so one box answers both. Test over plain HTTP before TLS:
```bash
curl -s http://earnote.app | head        # landing page HTML
curl -s http://api.earnote.app/stats      # JSON
```

---

## Phase 6 — HTTPS for all three names (Let's Encrypt)
One certbot run covers the static site and the API:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx \
  -d earnote.app -d www.earnote.app -d api.earnote.app \
  --redirect --agree-tos -m you@earnote.app --no-eff-email
```

Certbot edits both server blocks to add 443 listeners, cert paths, and HTTP→HTTPS redirects.
Renewal is automatic; confirm with `sudo certbot renew --dry-run`.

Now wire the two sides together:
- In `/var/www/earnote/index.html`: set `BACKEND_URL = "https://api.earnote.app"` and `DEMO_MODE = false`.
- In `main.py`: `ALLOWED_ORIGINS = ["https://earnote.app", "https://www.earnote.app"]`, then `sudo systemctl restart earnote`.

> CORS gotcha: origins must match exactly — scheme + host, no trailing slash, and `earnote.app`
> ≠ `www.earnote.app`. The page is served from the apex but calls `api.` — that's cross-origin,
> so this config is doing real work.

---

## Phase 7 — Email deliverability (SendGrid)
Without this, summary emails land in spam.
1. SendGrid → Settings → Sender Authentication → **Authenticate Your Domain** → enter `earnote.app`.
2. It generates ~3 CNAME records (DKIM + tracking). Add each in Cloudflare DNS exactly as given, **grey-cloud (DNS only)** — proxying mail-auth records breaks them.
3. Click Verify in SendGrid. Add the SPF TXT record too if prompted.
4. Confirm `FROM_EMAIL=hello@earnote.app` (Phase 3) is on this authenticated domain.

---

## Phase 8 — Smoke test
From your laptop:

```bash
# 1. free submit works and counts down
curl -s -X POST https://api.earnote.app/submit \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/ep.mp3","email":"test@you.com"}'
# expect: {"ok":true,"job_id":1,"remaining":2}

# 2. run it 3 more times with the same email — the 4th should 429
# 3. Pro intent logs
curl -s -X POST https://api.earnote.app/interest \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@you.com","plan":"pro_9"}'

# 4. your dashboard numbers
curl -s https://api.earnote.app/stats
# expect: {"summaries_run":4,"emails_at_cap":1,"pro_clicks":1}
```

Then load `https://earnote.app` in a browser, submit a real episode, confirm the email arrives,
and click the $9 button — `pro_clicks` should tick up in `/stats`.

You're live. `/stats` is your whole dashboard: summaries pulled, how many hit the free wall,
and how many clicked the $9 button — your conversion signal on real traffic.

---

## Notes for when it grows
- **Inspect data:** `sqlite3 /opt/earnote/readback.db`, then query `jobs` / `interest`.
- **Audio resolution:** direct .mp3/.m4a links and yt-dlp-supported sites (YouTube, most hosts) work; Spotify is DRM-locked and won't resolve; a bare Apple Podcasts *page* may need the RSS enclosure. Watch the `status` column for failures.
- **Abuse:** the free cap is per-email, so burners bypass it. Add a per-IP daily cap in `/submit` or require email verification if it bites.
- **Logs:** `journalctl -u earnote -f` (API), `/var/log/nginx/` (proxy/static).
- **CDN later:** flip the apex A-record to orange-cloud so Cloudflare caches `index.html`, offloading the box.
- **Scaling workers:** if jobs pile up, move transcription to a real queue (RQ/Celery + Redis) — not before you need it.
