"""
Readback API — free tier + Pro intent capture, wired to your real pipeline.

The transcribe / summarize / send_email functions below are adapted from your
Stockbee pipeline.py:
  - transcribe(): your exact AssemblyAI TranscriptionConfig, fed by generic audio
    resolution (direct URL -> AssemblyAI; page/YouTube -> yt-dlp) instead of the
    Stockbee/Bunny downloader, which doesn't apply to arbitrary podcast links.
  - summarize(): your Claude call structure + 180k-char truncation guard, with a
    general podcast prompt (still markdown out) instead of the Stockbee one.
  - send_email(): your markdown->HTML conversion, but sent to the USER's email
    (your original sent to a fixed TO_EMAIL) and rebranded from "Stockbee".

Install:
  pip install fastapi "uvicorn[standard]" "pydantic[email]" assemblyai anthropic sendgrid yt-dlp

Env (readback.env):
  ASSEMBLYAI_API_KEY=...
  ANTHROPIC_API_KEY=...
  CLAUDE_MODEL=claude-sonnet-4-6        # or whatever your config used; haiku-4-5 is cheaper
  SENDGRID_API_KEY=...
  FROM_EMAIL=hello@yourdomain.com       # SendGrid-verified sender

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from contextlib import closing

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

# ── config ────────────────────────────────────────────────────────────────
FREE_LIMIT = 3            # free summaries per email (lifetime)
IP_DAILY_LIMIT = 10       # max summaries per IP per day — blunt abuse/cost guard for launch spikes
DB = "readback.db"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ALLOWED_ORIGINS = [
    "https://earnote.app",
    "https://www.earnote.app",
    "http://localhost:8000",
]

app = FastAPI(title="Readback API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── tiny SQLite store ────────────────────────────────────────────────────────
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL, url TEXT NOT NULL,
            ip TEXT, status TEXT DEFAULT 'queued', created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS interest(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL, plan TEXT, created TEXT NOT NULL
        );
        """)
        con.commit()

init_db()

def _ensure_ip_column():
    """Safe migration: add jobs.ip if an older DB predates it."""
    with closing(db()) as con:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        if "ip" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN ip TEXT")
            con.commit()

_ensure_ip_column()

def now():
    return datetime.now(timezone.utc).isoformat()

def used_count(email: str) -> int:
    with closing(db()) as con:
        return con.execute("SELECT COUNT(*) c FROM jobs WHERE email=?", (email.lower(),)).fetchone()["c"]

def ip_count_today(ip: str) -> int:
    """How many summaries this IP has triggered since UTC midnight."""
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    with closing(db()) as con:
        return con.execute(
            "SELECT COUNT(*) c FROM jobs WHERE ip=? AND created>=?", (ip, start)
        ).fetchone()["c"]

# ── request models ──────────────────────────────────────────────────────────
class SubmitIn(BaseModel):
    url: str
    email: EmailStr

class InterestIn(BaseModel):
    email: EmailStr
    plan: str = "pro_9"

# ── endpoints ────────────────────────────────────────────────────────────────
@app.post("/submit")
def submit(body: SubmitIn, request: Request, bg: BackgroundTasks):
    email = body.email.lower()

    # client IP — behind nginx, the real IP is first in X-Forwarded-For
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")

    # per-IP daily cap: blunt guard so a launch spike can't run up the API bill
    if ip_count_today(ip) >= IP_DAILY_LIMIT:
        raise HTTPException(status_code=429, detail="Daily limit reached for this network. Try again tomorrow.")

    # per-email free cap
    used = used_count(email)
    if used >= FREE_LIMIT:
        raise HTTPException(status_code=429, detail="Free limit reached")

    with closing(db()) as con:
        cur = con.execute(
            "INSERT INTO jobs(email, url, ip, status, created) VALUES(?,?,?,?,?)",
            (email, body.url, ip, "queued", now()),
        )
        con.commit()
        job_id = cur.lastrowid

    bg.add_task(process_episode, job_id, body.url, email)
    return {"ok": True, "job_id": job_id, "remaining": FREE_LIMIT - (used + 1)}

@app.post("/interest")
def interest(body: InterestIn):
    with closing(db()) as con:
        con.execute("INSERT INTO interest(email, plan, created) VALUES(?,?,?)",
                    (body.email.lower(), body.plan, now()))
        con.commit()
    return {"ok": True}

@app.get("/stats")
def stats():
    with closing(db()) as con:
        summaries = con.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        capped = con.execute(
            "SELECT COUNT(*) c FROM (SELECT email FROM jobs GROUP BY email HAVING COUNT(*)>=?)",
            (FREE_LIMIT,)).fetchone()["c"]
        pro = con.execute("SELECT COUNT(*) c FROM interest").fetchone()["c"]
    return {"summaries_run": summaries, "emails_at_cap": capped, "pro_clicks": pro}

# ── background job ───────────────────────────────────────────────────────────
def process_episode(job_id: int, url: str, email: str):
    try:
        transcript = transcribe(url)
        summary = summarize(transcript)
        send_email(email, summary)
        _set_status(job_id, "done")
    except Exception as e:
        _set_status(job_id, f"error: {e}")

def _set_status(job_id: int, status: str):
    with closing(db()) as con:
        con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        con.commit()

# ═════════════════════════════════════════════════════════════════════════════
# PIPELINE  (adapted from pipeline.py)
# ═════════════════════════════════════════════════════════════════════════════

# ── audio resolution (replaces the Stockbee/Bunny downloader) ────────────────
# AssemblyAI takes a public audio URL or a local file. A direct .mp3/.m4a link
# goes straight in; an Apple/YouTube/host page gets pulled with yt-dlp first.
# Spotify is DRM-locked and won't resolve.
_AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".mp4")

def _download_audio(url: str) -> str:
    import yt_dlp
    tmpdir = tempfile.mkdtemp(prefix="rb_")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

def resolve_audio(url: str):
    """Return (audio_ref_for_assemblyai, temp_dir_to_clean_or_None)."""
    clean = url.lower().split("?")[0]
    if clean.endswith(_AUDIO_EXT):
        return url, None
    path = _download_audio(url)
    return path, os.path.dirname(path)

# ── 1. transcription — your AssemblyAI config, verbatim ──────────────────────
def transcribe(url: str) -> str:
    import assemblyai as aai
    aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]

    audio_ref, tmp = resolve_audio(url)
    try:
        config = aai.TranscriptionConfig(
            speech_models=["universal-2"],
            language_code="en",
            punctuate=True,
            format_text=True,
            speaker_labels=True,
        )
        transcriber = aai.Transcriber(config=config)
        transcript = transcriber.transcribe(audio_ref)  # SDK polls until done

        if transcript.status == aai.TranscriptStatus.error:
            raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")
        if not transcript.text:
            raise RuntimeError("AssemblyAI returned an empty transcript")
        return transcript.text
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

# ── 2. summarization — your Claude call, general podcast prompt ──────────────
SUMMARY_SYSTEM_PROMPT = (
    "You summarize podcast episodes for someone who will NOT listen to the audio. "
    "Be faithful and concrete. Write in markdown with this structure:\n"
    "## <a short descriptive title>\n"
    "A 2-3 sentence overview.\n"
    "### Key points\n"
    "- the substantive takeaways, one per bullet\n"
    "### Notable moments\n"
    "- specific claims, examples, or quotes worth knowing\n"
    "### Who should listen\n"
    "One line. Keep the whole thing a 2-minute read."
)

def summarize(transcript: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    max_chars = 180_000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars]

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Please summarize this podcast episode.\n\n--- TRANSCRIPT ---\n{transcript}",
        }],
    )
    return message.content[0].text

# ── 3. delivery — your markdown->HTML conversion, sent to the USER ───────────
def send_email(to_email: str, summary: str) -> None:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Content

    # your conversion, kept as-is
    html_summary = summary.replace("\n", "<br>")
    html_summary = re.sub(r"## (.+)", r"<h2>\1</h2>", html_summary)
    html_summary = re.sub(r"### (.+)", r"<h3>\1</h3>", html_summary)
    html_summary = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_summary)
    html_summary = re.sub(r"^- (.+)", r"<li>\1</li>", html_summary, flags=re.MULTILINE)

    message = Mail(
        from_email=os.environ["FROM_EMAIL"],
        to_emails=to_email,                       # was a fixed TO_EMAIL in your pipeline
        subject="Your podcast summary is ready",
        html_content=Content("text/html", f"""
            <div style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px;">
                <h1 style="color: #be451e; border-bottom: 2px solid #be451e; padding-bottom: 10px;">
                    Earnote summary
                </h1>
                {html_summary}
                <hr style="margin-top: 30px; border: none; border-top: 1px solid #eee;">
                <p style="color: #6b6256; font-size: 13px; margin: 14px 0 4px;">
                    Made with <strong style="color:#be451e;">Earnote</strong> — paste a podcast link, get the summary.
                    <a href="https://earnote.app" style="color:#be451e;">earnote.app</a>
                </p>
                <p style="color: #9a9081; font-size: 13px; margin: 0 0 10px;">
                    Know someone who saves episodes they never finish? Forward this to them.
                </p>
                <p style="color: #b8afa0; font-size: 11px; margin: 0;">
                    Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}
                </p>
            </div>
        """),
    )

    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    sg.send(message)
