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

class ContactIn(BaseModel):
    name: str
    email: EmailStr
    message: str

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

@app.post("/contact")
def contact(body: ContactIn):
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Content
    owner = os.environ.get("OWNER_EMAIL")
    if not owner:
        raise HTTPException(status_code=500, detail="Contact not configured")
    message = Mail(
        from_email=os.environ["FROM_EMAIL"],
        to_emails=owner,
        subject=f"Earnote contact: {body.name}",
        html_content=Content("text/html", f"""
            <div style="font-family:Arial,sans-serif;max-width:600px;padding:20px">
                <h2 style="color:#FF3B00">New message via Earnote</h2>
                <p><b>Name:</b> {body.name}</p>
                <p><b>Email:</b> {body.email}</p>
                <p><b>Message:</b></p>
                <p style="background:#f5f5f5;padding:14px;border-radius:6px">{body.message}</p>
            </div>
        """),
    )
    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    sg.send(message)
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

_COOKIES_FILE = "/opt/earnote/youtube-cookies.txt"

def _download_audio(url: str) -> str:
    import yt_dlp
    tmpdir = tempfile.mkdtemp(prefix="rb_")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
        "remote_components": ["ejs:github"],
    }
    if os.path.exists(_COOKIES_FILE):
        opts["cookiefile"] = _COOKIES_FILE
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
    "Your goal is to give them a complete picture — they should know exactly what was discussed, "
    "what was argued, and what to take away.\n\n"
    "Be faithful, specific, and concrete. Use the speaker's actual words, numbers, and examples "
    "where possible. Write in markdown with this structure:\n\n"
    "## <a short descriptive title>\n"
    "A 4-6 sentence overview: the main topic, the guest's background (if any), "
    "and the core argument or narrative arc of the episode.\n\n"
    "### Main thesis\n"
    "1-2 sentences stating the central claim or key message of the episode.\n\n"
    "### Key points\n"
    "- Every substantive idea, insight, or argument — aim for 8-12 bullets\n"
    "- Be specific: name the frameworks, numbers, examples, and recommendations mentioned\n\n"
    "### Notable moments\n"
    "- Specific quotes, surprising claims, strong opinions, or memorable stories\n"
    "- Include any data, studies, or references cited\n\n"
    "### Action items\n"
    "- Concrete advice, tools, books, or next steps the speakers recommended\n"
    "- Omit this section entirely if the episode had none\n\n"
    "### Who should listen\n"
    "One sentence. Be specific about who gets the most value from this episode."
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

# ── 3. delivery — card-section HTML email ────────────────────────────────────
def _parse_summary(summary: str) -> dict:
    """Split Claude's markdown into title, overview paragraph, and named sections."""
    title_m = re.search(r'^## (.+)', summary, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "Your podcast summary"

    overview_m = re.search(r'^## .+\n+([\s\S]+?)(?=^###|\Z)', summary, re.MULTILINE)
    overview = overview_m.group(1).strip() if overview_m else ""

    sections = []
    for m in re.finditer(r'^### (.+)\n([\s\S]*?)(?=^###|\Z)', summary, re.MULTILINE):
        heading = m.group(1).strip()
        raw = m.group(2).strip()
        bullets = [re.sub(r'^\*\*(.+)\*\*$', r'\1', b.lstrip('-• ').strip())
                   for b in raw.splitlines() if b.strip().startswith(('-', '•', '*'))]
        sections.append({"heading": heading, "bullets": bullets, "text": raw if not bullets else ""})

    return {"title": title, "overview": overview, "sections": sections}

def _card(heading: str, bullets: list, text: str) -> str:
    AC = "#FF3B00"
    rows = "".join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid #efefef;font-size:15px;'
        f'color:#333;line-height:1.55;">'
        f'<span style="color:{AC};font-weight:700;margin-right:10px;">→</span>{b}</td></tr>'
        for b in bullets
    ) if bullets else (
        f'<tr><td style="padding:9px 0;font-size:15px;color:#444;line-height:1.65;">{text}</td></tr>'
    )
    return f"""
      <div style="margin:0 0 16px;border-radius:6px;overflow:hidden;border:1px solid #e8e8e8;">
        <div style="background:{AC};padding:9px 18px;">
          <span style="font-size:11px;font-weight:700;letter-spacing:.1em;
            text-transform:uppercase;color:#fff;">{heading}</span>
        </div>
        <div style="background:#fafafa;padding:6px 18px 4px;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>
        </div>
      </div>"""

def send_email(to_email: str, summary: str) -> None:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Content

    parsed = _parse_summary(summary)
    title_match = re.search(r'^## (.+)', summary, re.MULTILINE)
    subject = f"Earnote: {title_match.group(1).strip()}" if title_match else "Your podcast summary is ready"

    cards_html = "".join(_card(s["heading"], s["bullets"], s["text"]) for s in parsed["sections"])
    AC = "#FF3B00"

    html = f"""
    <div style="background:#f0f0f0;padding:32px 16px;font-family:Arial,Helvetica,sans-serif;">
    <div style="max-width:600px;margin:0 auto;">

      <div style="background:#0D0D0B;border-radius:8px 8px 0 0;padding:18px 28px;">
        <span style="font-size:20px;font-weight:900;letter-spacing:.06em;color:#fff;">
          EAR<span style="color:{AC};">NOTE</span>
        </span>
      </div>

      <div style="background:#fff;padding:28px 28px 12px;border:1px solid #e8e8e8;border-top:none;">
        <h1 style="font-size:22px;font-weight:700;color:#0D0D0B;line-height:1.3;margin:0 0 12px;">
          {parsed["title"]}
        </h1>
        <p style="font-size:15px;color:#555;line-height:1.65;margin:0 0 20px;
          padding-bottom:20px;border-bottom:3px solid {AC};">
          {parsed["overview"]}
        </p>
        {cards_html}
      </div>

      <div style="background:#f9f9f9;border:1px solid #e8e8e8;border-top:none;
        border-radius:0 0 8px 8px;padding:16px 28px;text-align:center;">
        <p style="font-size:13px;color:#888;margin:0 0 4px;">
          Made with <a href="https://earnote.app" style="color:{AC};text-decoration:none;
          font-weight:600;">Earnote</a>
          &nbsp;·&nbsp; Know someone who never finishes their queue? Forward this.
        </p>
        <p style="font-size:11px;color:#bbb;margin:6px 0 0;">
          {datetime.now().strftime('%d %b %Y, %H:%M UTC')}
        </p>
      </div>

    </div>
    </div>"""

    message = Mail(
        from_email=os.environ["FROM_EMAIL"],
        to_emails=to_email,
        subject=subject,
        html_content=Content("text/html", html),
    )
    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    sg.send(message)
