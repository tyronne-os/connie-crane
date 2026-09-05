"""
CRANE STUDIO — voice sample harvesting engine.

The UI is the control panel; this is the engine it commands. Runs extraction
jobs in a background thread pool so the request returns immediately and the
panel polls for progress.

Sources supported:
  * Any site yt-dlp has an extractor for (~1800, incl. YouTube, Vimeo, SoundCloud)
  * LibriVox        — public domain audiobooks, direct archive.org audio
  * Internet Archive — direct media URLs
  * A direct URL to an audio file you are entitled to fetch

Deliberately NOT supported: Audible / Amazon. See NOTES at the bottom.
"""

import os
import re
import json
import time
import uuid
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

VOICES_DIR = os.environ.get("CRANE_VOICE_VAULT",
                            os.path.join(os.path.dirname(__file__), "voice_vault"))
os.makedirs(VOICES_DIR, exist_ok=True)

_JOBS = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=2)

# Hosts this engine will not scrape. Their terms prohibit automated access,
# and getting past their bot protection means evading it, which is a
# different activity from fetching a file you're allowed to fetch.
BLOCKED_HOSTS = (
    "audible.com", "audible.co.uk", "audible.ca", "audible.de",
    "amazon.com", "amazon.co.uk", "amazon.ca", "amzn.to",
)


def _host(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower().lstrip("www.")
    except Exception:
        return ""


def is_blocked(url):
    h = _host(url)
    return any(h == b or h.endswith("." + b) for b in BLOCKED_HOSTS)


def safe_name(text, fallback="clip"):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_")
    return (cleaned or fallback)[:80]


def _set(job_id, **kw):
    with _LOCK:
        _JOBS.setdefault(job_id, {}).update(kw)


def get_job(job_id):
    with _LOCK:
        return dict(_JOBS.get(job_id, {}))


def all_jobs():
    with _LOCK:
        return [dict(v) for v in _JOBS.values()]


def _hhmmss(v):
    """Accept 90, '90', '1:30', '0:01:30' -> seconds (float). None if empty."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        if ":" in s:
            parts = [float(p) for p in s.split(":")]
            total = 0.0
            for p in parts:
                total = total * 60 + p
            return total
        return float(s)
    except ValueError:
        return None


def _run_ytdlp(job_id, url, title, start, end):
    import yt_dlp

    out_base = os.path.join(VOICES_DIR, safe_name(title or job_id))

    def hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip() or "…"
            _set(job_id, state="downloading",
                 detail=f"downloading {pct} {d.get('_speed_str','').strip()}")
        elif d.get("status") == "finished":
            _set(job_id, state="processing", detail="extracting audio")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_base + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
        }],
    }

    s, e = _hhmmss(start), _hhmmss(end)
    if s is not None or e is not None:
        a, b = (s or 0.0), e
        # download_ranges trims at the source rather than fetching the whole
        # file and cutting after — much faster for a short sample.
        opts["download_ranges"] = lambda *_: [{
            "start_time": a, "end_time": b if b is not None else float("inf")
        }]
        opts["force_keyframes_at_cuts"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    wav = out_base + ".wav"
    if not os.path.exists(wav):
        cand = [f for f in os.listdir(VOICES_DIR)
                if f.startswith(os.path.basename(out_base))]
        if not cand:
            raise RuntimeError("extraction produced no file")
        wav = os.path.join(VOICES_DIR, cand[0])

    return {
        "file": os.path.basename(wav),
        "path": wav,
        "size_kb": round(os.path.getsize(wav) / 1024, 1),
        "source_title": (info or {}).get("title"),
        "duration": (info or {}).get("duration"),
        "uploader": (info or {}).get("uploader"),
    }


def _run_direct(job_id, url, title):
    """Fetch a direct audio URL (archive.org, LibriVox, your own host)."""
    _set(job_id, state="downloading", detail="fetching direct URL")
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".mp3"
    out = os.path.join(VOICES_DIR, safe_name(title or job_id) + ext)
    req = urllib.request.Request(url, headers={"User-Agent": "CraneStudio/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(out, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
    return {
        "file": os.path.basename(out),
        "path": out,
        "size_kb": round(os.path.getsize(out) / 1024, 1),
        "source_title": title,
    }


def _worker(job_id, url, title, start, end, mode):
    _set(job_id, state="starting", detail="resolving source")
    try:
        if mode == "direct":
            result = _run_direct(job_id, url, title)
        else:
            result = _run_ytdlp(job_id, url, title, start, end)
        _set(job_id, state="done", detail="saved to vault",
             finished_at=time.time(), **result)
    except Exception as exc:
        msg = str(exc).splitlines()[0][:300] if str(exc) else exc.__class__.__name__
        _set(job_id, state="error", detail=msg, finished_at=time.time())


def submit(url, title=None, start=None, end=None):
    """Queue an extraction. Returns (job_id, error_or_None)."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None, "Give a full http(s) URL."
    if is_blocked(url):
        return None, (
            f"{_host(url)} is not supported. Its terms prohibit automated "
            "access, and there is no extractor for it. Use the publisher's "
            "own sample link, LibriVox, or a source you have rights to."
        )

    mode = "direct" if re.search(r"\.(mp3|wav|m4a|m4b|ogg|flac|aac)(\?|$)",
                                 url, re.I) else "ytdlp"
    job_id = uuid.uuid4().hex[:12]
    _set(job_id, id=job_id, url=url, title=title, state="queued",
         detail="waiting for a worker", mode=mode, started_at=time.time())
    _POOL.submit(_worker, job_id, url, title, start, end, mode)
    return job_id, None


def vault_files():
    out = []
    for f in sorted(os.listdir(VOICES_DIR)):
        p = os.path.join(VOICES_DIR, f)
        if os.path.isfile(p):
            out.append({"file": f, "size_kb": round(os.path.getsize(p) / 1024, 1),
                        "mtime": os.path.getmtime(p)})
    return sorted(out, key=lambda x: -x["mtime"])


# NOTES ---------------------------------------------------------------------
# Audible/Amazon are blocked above on purpose. Their terms prohibit automated
# access, and the "anti-bot protection" a managed scraper sells you past is
# the thing enforcing that. Separately: a narrator's recorded voice is their
# performance and, in several US states, their protected likeness — cloning an
# identifiable narrator from their commercial work is a rights problem no
# amount of infrastructure solves. LibriVox is public domain and clean.
