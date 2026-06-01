# CLAUDE.md — project context for Earnote

## What this is
Earnote turns a podcast episode into a clean, skimmable summary delivered by email.
A user pastes an episode URL + their email on the landing page; the backend transcribes
the audio, summarizes it with Claude, and emails the result. It's an MVP / willingness-to-pay
test, not a finished product.

Domain: **earnote.app** (Cloudflare registrar + DNS).
Hosting: a single **EC2** box (Ubuntu) serves both the static frontend and the API.

## Files
- `index.html` — the landing page. Static, no build step, vanilla HTML/CSS/JS.
  Two config constants at the top of the `<script>`: `BACKEND_URL` and `DEMO_MODE`.
  Free-tier form posts to `/submit`; the $9 "Pro" button posts to `/interest` (intent only — no charge).
- `main.py` — FastAPI backend. Endpoints: `POST /submit`, `POST /interest`, `GET /stats`.
  SQLite store (`readback.db`) with two tables: `jobs`, `interest`.
  Pipeline functions `transcribe()` / `summarize()` / `send_email()` are implemented
  (AssemblyAI → Claude → SendGrid), adapted from the owner's existing Stockbee pipeline.
- `DEPLOY.md` — the authoritative deployment runbook. Follow it phase by phase.

## How it behaves
- **Free tier:** 3 summaries per email address, enforced in `/submit` (returns HTTP 429 on the 4th).
- **Pro button:** logs the click to the `interest` table so we can measure demand. It does NOT take payment.
- **Delivery is async:** `/submit` returns immediately and the pipeline runs in a FastAPI
  BackgroundTask; the user gets the summary by email. Don't make `/submit` synchronous.
- **`/stats`** is the owner's dashboard: `{summaries_run, emails_at_cap, pro_clicks}`.

## Conventions / guardrails
- **Secrets:** never hardcode keys. They live in `/opt/earnote/earnote.env` (systemd `EnvironmentFile`)
  and are read via `os.environ`. Never commit that file or print key values.
- **Don't widen the free tier or disable the cap** without being asked — it's a deliberate cost control.
- **CORS:** `ALLOWED_ORIGINS` in `main.py` must list the exact frontend origins
  (`https://earnote.app`, `https://www.earnote.app`) — scheme + host, no trailing slash.
- **Keep it simple.** This is an MVP. Prefer the existing SQLite + BackgroundTasks design over
  introducing Redis/Celery/Postgres unless explicitly asked. Don't add a build system to the frontend.
- **Service name** is `earnote` (systemd). App dir `/opt/earnote`. Static files `/var/www/earnote`.

## Deploy target
- EC2 Ubuntu, Elastic IP, security group allows 22/80/443 only (8000 stays internal).
- nginx serves static files on the apex/www and reverse-proxies `api.earnote.app` → `127.0.0.1:8000`.
- HTTPS via certbot/Let's Encrypt for all three names in one cert.
- See DEPLOY.md for exact commands. When executing on the server, go phase by phase and
  confirm each (`dig`, `curl`, `systemctl status`) before moving on.

## Useful commands
- API logs: `journalctl -u earnote -f`
- Restart API: `sudo systemctl restart earnote`
- Inspect data: `sqlite3 /opt/earnote/readback.db` then `SELECT * FROM jobs;` / `SELECT * FROM interest;`
- nginx test/reload: `sudo nginx -t && sudo systemctl reload nginx`
