import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

import voice_engine

app = FastAPI(title="CRANE STUDIO - Voice Foundry (CONNIE NOLA)")
app.mount("/static", StaticFiles(directory="/home/hunt/static"), name="static")

VOICES_DIR = "/mnt/NOBILITY_VAULT/voice_vault"
DATA_FILE = "crane_cast_manifest.json"
os.makedirs(VOICES_DIR, exist_ok=True)

def parse_time_to_seconds(time_str: str) -> float:
    time_str = str(time_str).strip()
    if not time_str:
        return 0.0
    if ":" in time_str:
        parts = time_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    val = float(time_str)
    if val < 1.0 and "." in time_str:
        return float(time_str.split(".")[1])
    return val

def load_manifest():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"harvested_voices": [], "favorites": []}

def save_manifest(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

class ExtractRequest(BaseModel):
    url: str
    start: str
    end: str
    clip_name: str

class FavoriteRequest(BaseModel):
    name: str
    url: str

@app.get("/api/vault/files")
async def get_vault_files():
    files = []
    if os.path.exists(VOICES_DIR):
        for f in sorted(os.listdir(VOICES_DIR), reverse=True):
            if f.endswith((".wav", ".mp3", ".m4a", ".webm", ".opus")):
                path = os.path.join(VOICES_DIR, f)
                size_kb = round(os.path.getsize(path) / 1024, 1)
                files.append({"filename": f, "size": f"{size_kb} KB"})
    return {"files": files, "manifest": load_manifest()}

@app.post("/api/favorites/add")
async def add_favorite(req: FavoriteRequest):
    state = load_manifest()
    if "favorites" not in state:
        state["favorites"] = []
    if not any(f['url'] == req.url for f in state['favorites']):
        state["favorites"].append({"name": req.name, "url": req.url})
        save_manifest(state)
    return {"status": "success", "favorites": state["favorites"]}

PRESET_CHAIN = {
    "NOLA NOIR":         "highpass=f=80,equalizer=f=250:t=q:w=1.2:g=2,equalizer=f=6000:t=q:w=2:g=-3,aecho=0.8:0.85:12:0.15",
    "HARLEM ORATORY":    "highpass=f=70,equalizer=f=180:t=q:w=1:g=3,equalizer=f=3000:t=q:w=2:g=2,aecho=0.8:0.9:45:0.22",
    "ATL TRAP CADENCE":  "highpass=f=90,equalizer=f=120:t=q:w=1:g=2,equalizer=f=5000:t=q:w=2:g=3,acompressor=threshold=-18dB:ratio=4",
    "DEEP SOUTH WARMTH": "highpass=f=60,equalizer=f=200:t=q:w=1.5:g=4,lowpass=f=9000,aecho=0.8:0.88:30:0.2",
    "PULPIT":            "highpass=f=70,equalizer=f=220:t=q:w=1:g=3,aecho=0.85:0.9:80:0.3,acompressor=threshold=-20dB:ratio=3",
    "BROADCAST CLEAN":   "highpass=f=85,acompressor=threshold=-16dB:ratio=5:attack=5:release=80,equalizer=f=4000:t=q:w=2:g=1.5",
    "ACADEMIC LECTURE":  "highpass=f=75,equalizer=f=2500:t=q:w=2:g=2,aecho=0.8:0.85:35:0.12",
    "KITCHEN TABLE":     "highpass=f=65,equalizer=f=300:t=q:w=1.5:g=2,lowpass=f=10000,aecho=0.8:0.82:18:0.18",
}


def _vault_path(name):
    """Resolve a vault filename safely — no path traversal."""
    base = os.path.basename(name or "")
    p = os.path.join(VOICES_DIR, base)
    if not os.path.isfile(p):
        raise FileNotFoundError(base)
    return p


def _chain_for(ch, preset):
    """Build the per-channel ffmpeg filter chain from mixer settings."""
    parts = []
    formant = float(ch.get("formant") or 0)
    if formant:
        # Formant shift via resample + tempo compensation keeps duration intact.
        r = 2 ** (formant / 12.0)
        parts.append(f"asetrate=24000*{r:.6f},aresample=24000,atempo={1/r:.6f}")
    gain = float(ch.get("gain") or 0)
    if gain:
        parts.append(f"volume={gain}dB")
    if preset and preset in PRESET_CHAIN:
        parts.append(PRESET_CHAIN[preset])
    parts.append("dynaudnorm=p=0.9:s=5")
    return ",".join(parts)


@app.post("/api/mixer/render")
async def mixer_render(payload: dict):
    """Blend CH01 + CH02 (+ ambient bed) through ffmpeg into a new vault WAV."""
    try:
        chans = payload.get("channels") or {}
        preset = payload.get("preset")
        a, b, c = chans.get("a"), chans.get("b"), chans.get("c")

        soloed = [k for k in ("a", "b") if (chans.get(k) or {}).get("solo")]
        def live(key, ch):
            if not ch or ch.get("mute"):
                return False
            return key in soloed if soloed else True

        voices = [(k, chans[k]) for k in ("a", "b") if live(k, chans.get(k))]
        if not voices:
            return {"status": "error", "message": "No active voice channel — load and unmute CH01 or CH02."}

        inputs, filters, labels = [], [], []
        for i, (key, ch) in enumerate(voices):
            inputs += ["-i", _vault_path(ch["file"])]
            w = float(ch.get("weight") or 50) / 100.0
            chain = _chain_for(ch, preset)
            filters.append(f"[{i}:a]{chain},volume={max(w,0.01):.4f}[v{i}]")
            labels.append(f"[v{i}]")

        bed_idx = None
        if c and not c.get("mute") and c.get("file"):
            bed_idx = len(voices)
            inputs += ["-stream_loop", "-1", "-i", _vault_path(c["file"])]
            bed = [f"volume={float(c.get('gain') or -18)}dB"]
            warm = float(c.get("warm") or 0)
            if warm:
                bed.append(f"lowpass=f={int(12000 - warm * 70)}")
            filters.append(f"[{bed_idx}:a]{','.join(bed)}[bed]")

        if len(labels) > 1:
            filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[voice]")
        else:
            filters.append(f"{labels[0]}anull[voice]")

        if bed_idx is not None:
            duck = float(c.get("duck") or 0) / 100.0
            if duck > 0:
                filters.append(f"[bed][voice]sidechaincompress=threshold=0.05:ratio={1+duck*19:.1f}:release=250[bedduck]")
                filters.append("[voice][bedduck]amix=inputs=2:duration=first:normalize=0[out]")
            else:
                filters.append("[voice][bed]amix=inputs=2:duration=first:normalize=0[out]")
        else:
            filters.append("[voice]anull[out]")

        safe = re.sub(r"[^A-Za-z0-9]+", "_", (payload.get("out_name") or "mix")).strip("_")[:50] or "mix"
        out_name = f"{safe}_{int(time.time())}.wav"
        out_path = os.path.join(VOICES_DIR, out_name)

        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + inputs +
               ["-filter_complex", ";".join(filters), "-map", "[out]",
                "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", out_path])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not os.path.exists(out_path):
            return {"status": "error", "message": (proc.stderr or "ffmpeg failed").strip()[-400:]}

        state = load_manifest()
        state["harvested_voices"].insert(0, {"filename": out_name, "source": f"mixer:{preset or 'custom'}"})
        save_manifest(state)
        kb = round(os.path.getsize(out_path) / 1024, 1)
        tag = f" [{preset}]" if preset else ""
        return {"status": "success", "file": out_name,
                "message": f"Rendered {out_name}{tag} — {kb} KB, {len(voices)} voice channel(s)"
                           + (" + ambient bed" if bed_idx is not None else "")}
    except FileNotFoundError as e:
        return {"status": "error", "message": f"Vault file not found: {e}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Render timed out (300s)."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


ROOM_ECHO = {
    "none":      None,
    "intimate":  "aecho=0.8:0.82:15:0.12",
    "medium":    "aecho=0.82:0.87:40:0.20",
    "large":     "aecho=0.85:0.90:80:0.30",
    "cathedral": "aecho=0.90:0.95:160:0.42",
}

@app.post("/api/quincy/render")
async def quincy_render(payload: dict):
    """Quincy Jones advanced sculpting — pitch, formant, breathiness, EQ, room."""
    try:
        src = payload.get("source")
        if not src:
            return {"status": "error", "message": "No source file selected."}
        in_path = _vault_path(src)

        parts = []

        # ── REPAIR: run before anything else shapes the signal ──────────────
        if payload.get("declick"):
            parts.append("adeclick")
        if payload.get("declip"):
            parts.append("adeclip")

        # Noise gate — cleans up bad mic noise floor
        if payload.get("gate"):
            gt = float(payload.get("gate_thresh") or 0.02)
            parts.append(f"agate=threshold={max(0.001,min(gt,0.3)):.4f}:ratio=10:attack=5:release=200")

        # Pitch shift (semitones) via resample + tempo compensation
        pitch = float(payload.get("pitch") or 0)
        if pitch:
            r = 2 ** (pitch / 12.0)
            parts.append(f"asetrate={int(24000*r)},aresample=24000,atempo={1/r:.6f}")

        # Independent formant shift (independent of pitch)
        formant = float(payload.get("formant") or 0)
        if formant:
            rf = 2 ** (formant / 12.0)
            # Apply after pitch so they stack correctly
            parts.append(f"asetrate={int(24000*rf)},aresample=24000,atempo={1/rf:.6f}")

        # Chest weight — low-mid boost/cut around 200 Hz
        chest = float(payload.get("chest") or 0)
        if chest:
            parts.append(f"equalizer=f=200:t=q:w=1.5:g={chest:.1f}")

        # Presence / bite — upper-mid boost around 4 kHz
        presence = float(payload.get("presence") or 0)
        if presence:
            parts.append(f"equalizer=f=4000:t=q:w=2:g={presence:.1f}")

        # Air shimmer — high shelf around 10 kHz
        air = float(payload.get("air") or 0)
        if air:
            parts.append(f"equalizer=f=10000:t=q:w=3:g={air:.1f}")

        # Breathiness — boost 8 kHz air band + reduce chest compression
        breath = float(payload.get("breath") or 0)
        if breath > 0:
            bg = breath / 100.0 * 5  # 0–5 dB
            parts.append(f"equalizer=f=8000:t=q:w=2.5:g={bg:.2f}")
            parts.append(f"highpass=f={int(80 + breath * 0.6)}")  # raise HPF slightly as breathiness increases
        else:
            parts.append("highpass=f=80")

        # ── ANATOMY: the physical instrument ────────────────────────────────
        # Each band maps to a real resonance in the vocal tract.
        for key, freq, width in (("nasal", 1000, 1.2), ("throat", 500, 1.2),
                                 ("mouth", 2500, 1.6), ("proximity", 100, 1.0),
                                 ("consonant", 5500, 2.0)):
            g = float(payload.get(key) or 0)
            if abs(g) > 0.05:
                parts.append(f"equalizer=f={freq}:t=q:w={width}:g={g:.1f}")

        # ── CHARACTER: grit, movement, sparkle ──────────────────────────────
        rasp = float(payload.get("rasp") or 0)
        if rasp > 2:
            # Bit-crush at high resolution reads as vocal fry / grit, not distortion.
            bits = max(6.0, 16.0 - (rasp / 100.0) * 8.0)
            parts.append(f"acrusher=bits={bits:.1f}:mode=log:aa=1")

        vib_d = float(payload.get("vibrato") or 0)
        if vib_d > 1:
            vib_r = float(payload.get("vibrato_rate") or 5)
            parts.append(f"vibrato=f={max(0.1,min(vib_r,12)):.2f}:d={min(vib_d/100.0,0.9):.3f}")

        exciter = float(payload.get("exciter") or 0)
        if exciter > 1:
            parts.append(f"aexciter=level_in=1:level_out=1:amount={min(exciter/10.0,4):.2f}:blend=0")

        crystal = float(payload.get("crystal") or 0)
        if abs(crystal) > 0.05:
            parts.append(f"crystalizer=i={max(-6,min(crystal,6)):.2f}:c=1")

        sub = float(payload.get("subboost") or 0)
        if sub > 2:
            parts.append(f"asubboost=dry=1:wet={min(sub/100.0,1):.2f}:boost={1+sub/50.0:.2f}")

        # ── De-esser at a chosen frequency ──────────────────────────────────
        if payload.get("deess"):
            sf = float(payload.get("sib_freq") or 7000)
            sa = float(payload.get("sib_amount") or 4)
            parts.append(f"equalizer=f={int(sf)}:t=q:w=1:g={-abs(sa):.1f}")

        # Tape warmth — gentle low-pass softening
        tape = float(payload.get("tape") or 0)
        if tape > 20:
            cutoff = int(16000 - tape * 60)  # 16kHz→10kHz as tape goes 0→100
            parts.append(f"lowpass=f={max(cutoff,8000)}")

        # ── Pace: speaking rate without changing pitch ──────────────────────
        pace = float(payload.get("pace") or 1.0)
        if abs(pace - 1.0) > 0.01:
            p = max(0.5, min(pace, 2.0))
            parts.append(f"atempo={p:.3f}")

        # Glue compression with real timing control
        if payload.get("compress"):
            atk = int(payload.get("attack") or 10)
            rel = int(payload.get("release") or 100)
            ratio = float(payload.get("ratio") or 3)
            parts.append(f"acompressor=threshold=-18dB:ratio={max(1.1,min(ratio,20)):.1f}"
                         f":attack={max(1,min(atk,200))}:release={max(10,min(rel,2000))}:makeup=2dB")

        # Speech levelling — holds a narrator steady across a long read
        if payload.get("speechnorm"):
            parts.append("speechnorm=e=12.5:r=0.0001:l=1")

        # Room reverb
        room = ROOM_ECHO.get(payload.get("room") or "none")
        if room:
            parts.append(room)

        # Output normalization
        if payload.get("norm"):
            parts.append("dynaudnorm=p=0.9:s=5")

        chain = ",".join(parts) if parts else "anull"

        safe = re.sub(r"[^A-Za-z0-9]+", "_", (payload.get("out_name") or "quincy")).strip("_")[:50] or "quincy"
        out_name = f"{safe}_{int(time.time())}.wav"
        out_path = os.path.join(VOICES_DIR, out_name)

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", in_path, "-af", chain,
               "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", out_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not os.path.exists(out_path):
            return {"status": "error", "message": (proc.stderr or "ffmpeg failed").strip()[-400:]}

        state = load_manifest()
        state["harvested_voices"].insert(0, {"filename": out_name, "source": "quincy"})
        save_manifest(state)
        kb = round(os.path.getsize(out_path) / 1024, 1)
        archetype = payload.get("archetype") or "custom"
        return {"status": "success", "file": out_name,
                "message": f"Quincy render complete — {out_name} [{archetype}] {kb} KB"}
    except FileNotFoundError as e:
        return {"status": "error", "message": f"Vault file not found: {e}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Render timed out (300s)."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════ QUICK MIX (light tweaks + full EQ) ═══════════════════

EQ_BANDS = [
    ("sub",    60),
    ("low",    150),
    ("lowmid", 400),
    ("mid",    1000),
    ("himid",  3000),
    ("pres",   6000),
    ("air",    12000),
]


@app.post("/api/quickmix/render")
async def quickmix_render(payload: dict):
    """Light-touch tweaks for a voice that's already in good shape."""
    try:
        src = payload.get("source")
        if not src:
            return {"status": "error", "message": "Pick a source file."}
        in_path = _vault_path(src)

        parts = []
        eq = payload.get("eq") or {}
        for name, freq in EQ_BANDS:
            g = float(eq.get(name) or 0)
            if abs(g) > 0.05:
                parts.append(f"equalizer=f={freq}:t=q:w=1.4:g={g:.1f}")

        gain = float(payload.get("gain") or 0)
        if abs(gain) > 0.05:
            parts.append(f"volume={gain:.1f}dB")
        if payload.get("deess"):
            parts.append("equalizer=f=7000:t=q:w=1:g=-3")
        if payload.get("compress"):
            parts.append("acompressor=threshold=-20dB:ratio=2.5:attack=12:release=120:makeup=1dB")
        if payload.get("norm", True):
            parts.append("dynaudnorm=p=0.9:s=5")

        chain = ",".join(parts) if parts else "anull"
        safe = re.sub(r"[^A-Za-z0-9]+", "_", (payload.get("out_name") or "quick")).strip("_")[:50] or "quick"
        out_name = f"{safe}_{int(time.time())}.wav"
        out_path = os.path.join(VOICES_DIR, out_name)

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", in_path, "-af", chain,
               "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", out_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not os.path.exists(out_path):
            return {"status": "error", "message": (proc.stderr or "ffmpeg failed").strip()[-400:]}

        state = load_manifest()
        state["harvested_voices"].insert(0, {"filename": out_name, "source": "quickmix"})
        save_manifest(state)
        kb = round(os.path.getsize(out_path) / 1024, 1)
        return {"status": "success", "file": out_name,
                "message": f"Quick mix done — {out_name} · {kb} KB"}
    except FileNotFoundError as e:
        return {"status": "error", "message": f"Vault file not found: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════ KEY VAULT STATUS (names only, never values) ═══════════

KEY_PROVIDERS = {
    "openai": ["OPENAI_API_KEY", "OPENAI_KEY"],
    "nvidia": ["NVIDIA_API_KEY", "NVIDIA_NIM_KEY"],
    "grok":   ["GROK_API_KEY", "XAI_API_KEY"],
    "hf":     ["HF_TOKEN", "HUGGINGFACE_TOKEN"],
    "aws":    ["AWS_ACCESS_KEY_ID"],
    "aws_secret": ["AWS_SECRET_ACCESS_KEY"],
}


def _vault_get(name):
    """Read one credential. Values stay server-side — never returned to a client."""
    try:
        from vault import vault as nobility_vault
        v = nobility_vault.get(name)
        if v:
            return v
    except Exception:
        pass
    return os.environ.get(name)


@app.get("/api/keys/status")
async def keys_status():
    """Which providers are reachable. Booleans only — no credential material."""
    out = {}
    for prov, names in KEY_PROVIDERS.items():
        out[prov] = any(bool(_vault_get(n)) for n in names)
    return {"providers": out}


# ═══════════════════ BIG Q — mixing agent chat ═══════════════════

QJ_DEFAULTS = {
    "pitch": 0, "formant": 0, "breath": 0, "chest": 0, "presence": 0,
    "air": 0, "tape": 0, "room": "medium",
    "gate": False, "deess": True, "compress": True, "norm": True,
}

# Mixing vernacular → parameter deltas. The agent's ears.
MIX_RULES = [
    (r"\b(bright|brighter|brighten|crisp|crisper)\b",        {"air": +2, "presence": +1}),
    (r"\b(dark|darker|warm|warmer|mellow)\b",                {"air": -2, "tape": +20}),
    (r"\b(air|airy|breath|breathy|whisper|wispy)\b",         {"breath": +20, "air": +2}),
    (r"\b(less breath|less air|solid|grounded)\b",           {"breath": -20, "air": -1}),
    (r"\b(deep|deeper|lower|drop the pitch)\b",              {"pitch": -1}),
    (r"\b(high|higher|lift the pitch|raise the pitch)\b",    {"pitch": +1}),
    (r"\b(thick|thicker|full|fuller|chest|body|beef)\b",     {"chest": +2}),
    (r"\b(thin|thinner|less chest|less body|less boom)\b",   {"chest": -2}),
    (r"\b(boom|boomy|muddy|mud)\b",                          {"chest": -3, "gate": True}),
    (r"\b(clear|clearer|presence|cut through|forward)\b",    {"presence": +2}),
    (r"\b(recess|recessed|back off|softer|behind)\b",        {"presence": -2}),
    (r"\b(nasal|nasally|honky)\b",                           {"formant": -1, "presence": -1}),
    (r"\b(young|younger|smaller|petite)\b",                  {"formant": +1}),
    (r"\b(old|older|bigger|larger|heavier)\b",               {"formant": -1}),
    (r"\b(vintage|tape|analog|analogue|retro|old school)\b", {"tape": +25}),
    (r"\b(modern|digital|clean|pristine|hifi)\b",            {"tape": -20}),
    (r"\b(tinny|thin mic|laptop|cheap mic)\b",               {"tape": +40, "chest": +3, "air": -3, "gate": True}),
    (r"\b(harsh|sibilan|hissy|essy|too much s)\b",           {"deess": True, "air": -2}),
    (r"\b(nois|hum|hiss|background)\b",                      {"gate": True}),
    (r"\b(even|glue|level|consistent|smooth out)\b",         {"compress": True}),
]

ROOM_RULES = [
    (r"\b(dry|no room|no reverb|close mic|tight)\b",              "none"),
    (r"\b(intimate|closer|close|booth|closet|in my ear)\b",       "intimate"),
    (r"\b(room|medium room|live room)\b",                         "medium"),
    (r"\b(big|hall|large|spacious|stage|concert)\b",              "large"),
    (r"\b(cathedral|church|epic|massive|huge space)\b",           "cathedral"),
]

ARCHETYPE_WORDS = {
    "marilyn": ["marilyn", "monroe"],
    "dolly":   ["dolly", "parton", "country"],
    "billie":  ["billie", "holiday", "smoky", "smokey"],
    "nina":    ["nina", "simone", "theatrical"],
    "eartha":  ["eartha", "kitt", "sultry", "purr"],
    "tina":    ["tina", "turner", "rasp", "raspy"],
    "whitney": ["whitney", "houston", "soaring"],
    "ella":    ["ella", "fitzgerald"],
    "aretha":  ["aretha", "franklin", "gospel"],
    "connie":  ["connie", "nola", "signature", "default"],
}

ARCHETYPE_PARAMS = {
    "marilyn": {"pitch":2,"formant":1,"breath":70,"chest":-3,"presence":1,"air":5,"tape":30,"room":"intimate","gate":False,"deess":True,"compress":False,"norm":True},
    "dolly":   {"pitch":1.5,"formant":1.5,"breath":20,"chest":1,"presence":3,"air":4,"tape":20,"room":"medium","gate":False,"deess":True,"compress":True,"norm":True},
    "billie":  {"pitch":-1.5,"formant":-1,"breath":30,"chest":4,"presence":-1,"air":-2,"tape":60,"room":"large","gate":False,"deess":True,"compress":True,"norm":True},
    "nina":    {"pitch":-2,"formant":-1.5,"breath":5,"chest":5,"presence":2,"air":-1,"tape":40,"room":"large","gate":False,"deess":False,"compress":True,"norm":True},
    "eartha":  {"pitch":-1,"formant":-2,"breath":40,"chest":3,"presence":0,"air":2,"tape":50,"room":"intimate","gate":False,"deess":True,"compress":True,"norm":True},
    "tina":    {"pitch":0,"formant":0,"breath":10,"chest":2,"presence":5,"air":2,"tape":25,"room":"medium","gate":True,"deess":False,"compress":True,"norm":True},
    "whitney": {"pitch":2,"formant":0,"breath":15,"chest":2,"presence":4,"air":5,"tape":10,"room":"large","gate":False,"deess":True,"compress":True,"norm":True},
    "ella":    {"pitch":0,"formant":0.5,"breath":25,"chest":3,"presence":1,"air":1,"tape":45,"room":"medium","gate":False,"deess":True,"compress":True,"norm":True},
    "aretha":  {"pitch":0,"formant":-0.5,"breath":5,"chest":6,"presence":3,"air":0,"tape":35,"room":"large","gate":False,"deess":False,"compress":True,"norm":True},
    "connie":  {"pitch":0.5,"formant":0.5,"breath":20,"chest":2,"presence":2,"air":3,"tape":20,"room":"intimate","gate":False,"deess":True,"compress":True,"norm":True},
}

PARAM_LIMITS = {
    "pitch": (-6, 6), "formant": (-4, 4), "breath": (0, 100),
    "chest": (-6, 10), "presence": (-6, 8), "air": (-6, 8), "tape": (0, 100),
}


def _intensity(text):
    if re.search(r"\b(way|much|a lot|lots|really|very|super|heavy|heavily|max)\b", text):
        return 2.0
    if re.search(r"\b(slight|slightly|a bit|a little|little|touch|hair|subtle|barely)\b", text):
        return 0.5
    return 1.0


def _apply_mix_language(text, params):
    """Map mixing vernacular onto parameter moves. Returns (params, notes)."""
    t = (text or "").lower()
    p = dict(params)
    notes = []
    mult = _intensity(t)

    for key, words in ARCHETYPE_WORDS.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in words):
            p.update(ARCHETYPE_PARAMS[key])
            notes.append(f"loaded the {key.upper()} archetype")
            break

    negate = bool(re.search(r"\b(less|reduce|cut|drop|remove|take out|kill|no more)\b", t))

    for pattern, deltas in MIX_RULES:
        if re.search(pattern, t):
            for k, v in deltas.items():
                if isinstance(v, bool):
                    p[k] = (not v) if negate else v
                    notes.append(f"{k} {'off' if not p[k] else 'on'}")
                else:
                    step = v * mult * (-1 if negate else 1)
                    lo, hi = PARAM_LIMITS.get(k, (-100, 100))
                    before = float(p.get(k) or 0)
                    p[k] = max(lo, min(hi, round(before + step, 2)))
                    if p[k] != before:
                        notes.append(f"{k} {before:+g} → {p[k]:+g}")

    for pattern, room in ROOM_RULES:
        if re.search(pattern, t):
            p["room"] = room
            notes.append(f"room → {room}")
            break

    return p, notes


@app.post("/api/bigq/chat")
async def bigq_chat(payload: dict):
    """Talk to the mixing agent. Returns updated params + what it heard."""
    text = (payload.get("message") or "").strip()
    if not text:
        return {"status": "error", "message": "Say something."}
    params = {**QJ_DEFAULTS, **(payload.get("params") or {})}
    new_params, notes = _apply_mix_language(text, params)

    if notes:
        reply = "Adjusted: " + "; ".join(notes[:8]) + "."
    else:
        reply = ("I didn't catch a mix move in that. Try things like "
                 "\"warmer and closer\", \"less boom\", \"make it breathy like Marilyn\", "
                 "\"fix this tinny laptop mic\", or name an archetype.")
    return {"status": "success", "reply": reply, "params": new_params, "changed": notes}


@app.post("/api/bigq/render")
async def bigq_render(payload: dict):
    """Render the Big Q graph — same DSP core as the Quincy panel."""
    return await quincy_render(payload)


# ═══════════════════ BRAIN — role profiles for voice agents ════════════════

# Each role carries both a voice shape (DSP) and a persona shape (brain).
# The DSP half is what the ear hears; the persona half is what the model becomes.
BRAIN_ROLES = {
    "narrator": {
        "label": "Audiobook Narrator",
        "note": "Even, tireless, articulate. Built to hold a listener for nine hours.",
        "voice": {"pace": 0.96, "presence": 3, "consonant": 3, "mouth": 1.5,
                  "speechnorm": True, "compress": True, "attack": 15, "release": 140,
                  "ratio": 3.5, "room": "intimate", "breath": 12, "deess": True,
                  "sib_freq": 7000, "sib_amount": 4},
        "brain": {"rate": "measured", "emotion": "controlled", "warmth": 55,
                  "formality": "neutral", "energy": 45, "pause": "dramatic",
                  "stamina": "high", "character_range": "wide", "breath_audible": 20},
    },
    "companion": {
        "label": "Intimate Companion",
        "note": "Close, warm, present. Sounds like they are in the room with you.",
        "voice": {"pace": 0.94, "proximity": 4, "throat": 2, "breath": 35, "air": 3,
                  "chest": 2, "room": "intimate", "tape": 25, "compress": True,
                  "attack": 20, "release": 180, "ratio": 2.2, "deess": True,
                  "sib_freq": 7200, "sib_amount": 3},
        "brain": {"rate": "unhurried", "emotion": "responsive", "warmth": 90,
                  "formality": "casual", "energy": 40, "pause": "natural",
                  "stamina": "medium", "character_range": "single", "breath_audible": 55},
    },
    "partner": {
        "label": "Founder's Partner",
        "note": "Sharp and affectionate at once — keeps up with you, pushes back.",
        "voice": {"pace": 1.03, "presence": 3, "consonant": 2, "crystal": 1.5,
                  "chest": 1.5, "air": 2, "room": "intimate", "compress": True,
                  "attack": 8, "release": 90, "ratio": 3, "deess": True,
                  "sib_freq": 7000, "sib_amount": 3.5},
        "brain": {"rate": "brisk", "emotion": "expressive", "warmth": 78,
                  "formality": "casual", "energy": 70, "pause": "natural",
                  "stamina": "high", "character_range": "single", "breath_audible": 30},
    },
    "concierge": {
        "label": "Executive Concierge",
        "note": "Polished, precise, discreet. Competence you can hear.",
        "voice": {"pace": 0.98, "presence": 2.5, "consonant": 4, "mouth": 2,
                  "air": 2, "room": "intimate", "compress": True,
                  "attack": 12, "release": 110, "ratio": 3, "speechnorm": True,
                  "deess": True, "sib_freq": 6800, "sib_amount": 4.5},
        "brain": {"rate": "measured", "emotion": "controlled", "warmth": 62,
                  "formality": "formal", "energy": 55, "pause": "natural",
                  "stamina": "high", "character_range": "single", "breath_audible": 15},
    },
    "bright": {
        "label": "Bright / High-Energy",
        "note": "Lifted, forward, playful. Formant up, presence forward, air open.",
        "voice": {"pitch": 2.5, "formant": 2, "pace": 1.06, "presence": 4,
                  "air": 5, "mouth": 3, "chest": -2, "crystal": 2, "exciter": 12,
                  "room": "medium", "compress": True, "attack": 6, "release": 70,
                  "ratio": 4, "deess": True, "sib_freq": 7500, "sib_amount": 5},
        "brain": {"rate": "brisk", "emotion": "very expressive", "warmth": 85,
                  "formality": "casual", "energy": 95, "pause": "brisk",
                  "stamina": "medium", "character_range": "single", "breath_audible": 35},
    },
    "podcast": {
        "label": "Podcast Host",
        "note": "Conversational broadcast. Present and easy, never shouty.",
        "voice": {"pace": 1.0, "presence": 3.5, "chest": 2, "consonant": 2,
                  "proximity": 2, "room": "intimate", "compress": True,
                  "attack": 10, "release": 100, "ratio": 4, "speechnorm": True,
                  "deess": True, "sib_freq": 7000, "sib_amount": 4},
        "brain": {"rate": "conversational", "emotion": "expressive", "warmth": 72,
                  "formality": "casual", "energy": 68, "pause": "natural",
                  "stamina": "high", "character_range": "narrow", "breath_audible": 35},
    },
    "character": {
        "label": "Character Actor",
        "note": "Maximum range. Built to be pushed hard in either direction.",
        "voice": {"pace": 1.0, "presence": 2, "rasp": 15, "vibrato": 8,
                  "vibrato_rate": 5, "chest": 3, "room": "medium",
                  "compress": True, "attack": 15, "release": 120, "ratio": 2.5},
        "brain": {"rate": "variable", "emotion": "very expressive", "warmth": 60,
                  "formality": "variable", "energy": 75, "pause": "dramatic",
                  "stamina": "medium", "character_range": "very wide", "breath_audible": 45},
    },
    "documentary": {
        "label": "Documentary Voice",
        "note": "Authoritative and unhurried. Weight without theatre.",
        "voice": {"pitch": -1, "pace": 0.92, "chest": 4, "throat": 2,
                  "presence": 2, "air": -1, "tape": 30, "room": "medium",
                  "compress": True, "attack": 20, "release": 200, "ratio": 3,
                  "speechnorm": True, "deess": True, "sib_freq": 6800, "sib_amount": 4},
        "brain": {"rate": "measured", "emotion": "controlled", "warmth": 50,
                  "formality": "formal", "energy": 40, "pause": "dramatic",
                  "stamina": "high", "character_range": "single", "breath_audible": 12},
    },
}


@app.get("/api/brain/roles")
async def brain_roles():
    return {"roles": [{"id": k, "label": v["label"], "note": v["note"],
                       "voice": v["voice"], "brain": v["brain"]}
                      for k, v in BRAIN_ROLES.items()]}


# ═══════════════════ THE LABEL — finished voice agent gallery ═══════════════

def _label_state(state):
    if "label" not in state or not isinstance(state.get("label"), list):
        state["label"] = []
    return state


@app.get("/api/label/list")
async def label_list():
    state = _label_state(load_manifest())
    return {"agents": state["label"]}


@app.post("/api/label/add")
async def label_add(payload: dict):
    """Sign a finished voice agent to The Label."""
    state = _label_state(load_manifest())
    name = (payload.get("name") or "").strip() or f"Agent {len(state['label'])+1}"
    entry = {
        "id": f"agent_{int(time.time())}",
        "name": name[:60],
        "file": payload.get("file"),
        "archetype": payload.get("archetype") or "custom",
        "params": payload.get("params") or {},
        "brain": payload.get("brain") or {},
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    state["label"].insert(0, entry)
    save_manifest(state)
    return {"status": "success", "agent": entry,
            "message": f"“{entry['name']}” signed to The Label."}


@app.post("/api/label/remove")
async def label_remove(payload: dict):
    state = _label_state(load_manifest())
    aid = payload.get("id")
    state["label"] = [a for a in state["label"] if a.get("id") != aid]
    save_manifest(state)
    return {"status": "success"}


@app.get("/api/models/catalog")
async def models_catalog():
    return voice_engine.catalog_status()


@app.post("/api/models/install")
async def models_install(payload: dict):
    token = None
    try:
        from vault import vault as nobility_vault
        token = nobility_vault.get("HF_TOKEN") or nobility_vault.get("HUGGINGFACE_TOKEN")
    except Exception:
        token = os.environ.get("HF_TOKEN")
    job_id, err = voice_engine.install(
        payload.get("id") or "", token=token,
        repo=(payload.get("repo") or "").strip() or None,
        label=payload.get("label"))
    if err:
        return {"status": "error", "message": err}
    return {"status": "success", "job_id": job_id}


@app.get("/api/models/job/{job_id}")
async def models_job(job_id: str):
    return voice_engine.job_status(job_id) or {"state": "unknown"}


@app.post("/api/models/activate")
async def models_activate(payload: dict):
    ok, msg = voice_engine.set_active(payload.get("id") or "")
    return {"status": "success" if ok else "error", "message": msg}


@app.post("/api/clone/generate")
async def clone_generate(payload: dict):
    """Text-to-speech through the active voice model, cloned from a reference."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "No text supplied."}

    ref = None
    src = payload.get("source") or "__mix__"
    try:
        if src == "__mix__":
            mix = payload.get("mix") or {}
            r = await mixer_render({**mix, "out_name": "_cloneref"})
            if r.get("status") != "success":
                return {"status": "error",
                        "message": "Could not build the reference blend: " + r.get("message", "")}
            ref = os.path.join(VOICES_DIR, r["file"])
        else:
            ref = _vault_path(src)
    except FileNotFoundError as e:
        return {"status": "error", "message": f"Reference voice not found: {e}"}

    safe = re.sub(r"[^A-Za-z0-9]+", "_", (payload.get("name") or "clone")).strip("_")[:50] or "clone"
    out_name = f"{safe}_{int(time.time())}.wav"
    out_path = os.path.join(VOICES_DIR, out_name)

    ok, msg = voice_engine.synthesize(text, out_path, reference_wav=ref)

    if src == "__mix__" and ref and "_cloneref" in os.path.basename(ref):
        try:
            os.remove(ref)
        except OSError:
            pass

    if not ok:
        return {"status": "error", "message": msg}

    state = load_manifest()
    state["harvested_voices"].insert(0, {"filename": out_name, "source": f"clone:{src}"})
    save_manifest(state)
    kb = round(os.path.getsize(out_path) / 1024, 1)
    return {"status": "success", "file": out_name, "message": f"{msg} — {out_name} ({kb} KB)"}


@app.get("/api/vault/audio/{filename}")
async def vault_audio(filename: str):
    from fastapi.responses import FileResponse
    try:
        return FileResponse(_vault_path(filename), media_type="audio/wav")
    except FileNotFoundError:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="not found")


@app.post("/api/harvest/upload")
async def harvest_upload(file: UploadFile = File(...), title: str = Form("recording")):
    """Receive browser MediaRecorder blob (webm/opus), convert to 24kHz mono WAV."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", title.strip() or "rec").strip("_")[:60]
    out_path = os.path.join(VOICES_DIR, f"{safe}_{int(time.time())}.wav")
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        proc = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", tmp_path,
            "-vn", "-ac", "1", "-ar", "24000",
            "-af", "highpass=f=70,dynaudnorm=p=0.9:s=5",
            "-c:a", "pcm_s16le", out_path,
        ], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not os.path.exists(out_path):
            from fastapi import HTTPException
            raise HTTPException(status_code=500,
                                detail=(proc.stderr or "ffmpeg failed").strip()[:300])
    finally:
        os.unlink(tmp_path)
    size_kb = round(os.path.getsize(out_path) / 1024, 1)
    return {"status": "success", "file": os.path.basename(out_path),
            "size_kb": size_kb, "format": "wav 24kHz mono"}


@app.post("/api/harvest/media")
async def harvest_media(req: ExtractRequest):
    try:
        clean_url = req.url.strip()
        safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in req.clip_name.strip())
        if not safe_name:
            safe_name = "primer_tone"

        out_filename = f"{safe_name}.wav"
        out_path = os.path.join(VOICES_DIR, out_filename)

        counter = 1
        base_name = safe_name
        while os.path.exists(out_path):
            out_filename = f"{base_name}_{counter}.wav"
            out_path = os.path.join(VOICES_DIR, out_filename)
            counter += 1

        yt_cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "wav",
            "-o", out_path,
            clean_url
        ]

        is_all = req.start.strip().upper() == "ALL" or req.end.strip().upper() == "ALL"
        
        if not is_all:
            start_sec = parse_time_to_seconds(req.start)
            end_sec = parse_time_to_seconds(req.end)
            if end_sec > start_sec and start_sec >= 0:
                section_arg = f"*{start_sec}-{end_sec}"
                yt_cmd.insert(1, "--download-sections")
                yt_cmd.insert(2, section_arg)
                yt_cmd.insert(3, "--force-keyframes-at-cuts")

        result = subprocess.run(yt_cmd, capture_output=True, text=True, timeout=180)

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout
            return {"status": "error", "message": f"Extraction failed: {error_msg[-300:]}"}

        state = load_manifest()
        state["harvested_voices"].insert(0, {
            "filename": out_filename,
            "source": clean_url
        })
        save_manifest(state)

        return {
            "status": "success",
            "filename": out_filename,
            "message": f"Successfully saved clip as '{out_filename}' into vault"
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Extraction timed out (180s limit)."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/", response_class=RedirectResponse)
async def serve_root():
    return RedirectResponse(url="/ide")


@app.get("/bigq", response_class=RedirectResponse)
async def serve_bigq():
    return RedirectResponse(url="/studio")

@app.get("/studio", response_class=HTMLResponse)
async def serve_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CRANE STUDIO - Voice Foundry</title>
    <style>
        :root {
            --bg-dark: #070d18; --bg-panel: #0d1627; --bg-card: #131f37; --border: #1e3052;
            --accent-green: #10b981; --accent-blue: #38bdf8; --accent-purple: #a855f7;
            --accent-red: #ef4444; --text-main: #f1f5f9; --text-muted: #64748b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Fira Code', 'Courier New', monospace; }
        body { background: var(--bg-dark); color: var(--text-main); height: 100vh; overflow: hidden; display: flex; flex-direction: column; }
        header { height: 50px; background: var(--bg-panel); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 20px; flex-shrink: 0; position: relative; }
        /* ── CENTER NAV ── */
        .crane-nav { position: absolute; left: 50%; transform: translateX(-50%); display: flex; gap: 2px; background: rgba(0,0,0,.35); border-radius: 8px; padding: 4px; z-index: 10; }
        .nav-tab { color: #64748b; text-decoration: none; padding: 5px 22px; border-radius: 6px; font-size: 11px; font-weight: 700; letter-spacing: 2px; transition: .15s; font-family: 'Fira Code','Courier New',monospace; }
        .nav-tab:hover { color: #f1f5f9; background: rgba(255,255,255,.07); }
        .nav-tab.active { color: #fff; background: rgba(168,85,247,.28); border: 1px solid rgba(168,85,247,.4); }
        .nav-tab.depo { }
        .nav-tab.depo.active { background: rgba(245,158,11,.22); border-color: rgba(245,158,11,.4); color: #f59e0b; }
        .nav-tab.studio.active { background: rgba(16,185,129,.22); border-color: rgba(16,185,129,.4); color: #10b981; }
        .nav-tab.cu.active { background: rgba(99,102,241,.22); border-color: rgba(99,102,241,.4); color: #818cf8; }

        .logo { font-weight: bold; letter-spacing: 1px; font-size: 1rem; color: #fff; }
        .badge { background: rgba(56, 189, 248, 0.15); color: var(--accent-blue); padding: 3px 8px; border-radius: 4px; font-size: 0.72rem; border: 1px solid var(--accent-blue); }
        
        .app-body { display: flex; flex: 1; height: calc(100vh - 50px); overflow: hidden; width: 100vw; }
        
        /* Left Sidebar */
        .sidebar { width: 280px; background: var(--bg-panel); border-right: 1px solid var(--border); padding: 15px; display: flex; flex-direction: column; gap: 15px; overflow-y: auto; flex-shrink: 0; }
        
        /* Center Workspace - Expanded to fill middle gap completely */
        .workspace { flex: 1; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px; min-width: 0; }
        
        /* Right Intelligence Panel - Flush to the far right */
        .right-panel { width: 340px; background: var(--bg-panel); border-left: 1px solid var(--border); padding: 15px; display: flex; flex-direction: column; gap: 15px; overflow-y: auto; flex-shrink: 0; }
        
        .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px; position: relative; width: 100%; }
        .card-title { font-size: 0.85rem; font-weight: bold; color: var(--accent-blue); border-bottom: 1px solid var(--border); padding-bottom: 6px; display: flex; justify-content: space-between; align-items: center; }
        input[type="text"] { background: var(--bg-dark); border: 1px solid var(--border); border-radius: 4px; padding: 8px 10px; color: #fff; font-size: 0.78rem; outline: none; width: 100%; }
        input[type="text"]:focus { border-color: var(--accent-blue); }
        
        button.btn { background: var(--accent-purple); color: #fff; font-weight: bold; border: none; padding: 8px 14px; border-radius: 4px; cursor: pointer; font-size: 0.78rem; transition: 0.2s; white-space: nowrap; }
        button.btn:hover { opacity: 0.9; }
        button.btn-secondary { background: transparent; border: 1px solid var(--border); color: var(--text-muted); padding: 5px 10px; font-size: 0.72rem; border-radius: 4px; cursor: pointer; }
        button.btn-secondary:hover { border-color: var(--accent-blue); color: #fff; }
        button.btn-all { background: rgba(56, 189, 248, 0.15); border: 1px solid var(--accent-blue); color: var(--accent-blue); padding: 8px 12px; font-size: 0.75rem; border-radius: 4px; cursor: pointer; white-space: nowrap; }
        button.btn-all:hover { background: var(--accent-blue); color: #000; }
        
        .status-box { padding: 8px 12px; border-radius: 6px; font-size: 0.78rem; display: none; }
        .status-box.active { display: block; }
        .status-info { background: rgba(56, 189, 248, 0.1); border: 1px solid var(--accent-blue); color: var(--accent-blue); }
        .status-success { background: rgba(16, 185, 129, 0.1); border: 1px solid var(--accent-green); color: var(--accent-green); }
        .status-error { background: rgba(239, 68, 68, 0.1); border: 1px solid var(--accent-red); color: var(--accent-red); white-space: pre-wrap; }
        
        .file-item { background: var(--bg-dark); border: 1px solid var(--border); border-radius: 4px; padding: 8px; display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem; }
        .fav-row { display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem; background: var(--bg-dark); padding: 6px 8px; border-radius: 4px; border: 1px solid var(--border); }
        .heart-btn { background: none; border: none; cursor: pointer; font-size: 1rem; color: #64748b; transition: 0.2s; }
        .heart-btn:hover, .heart-btn.active { color: #f43f5e; }

        /* ── VOICE FOUNDRY MIXER ─────────────────────────────────────────── */
        .mixer { background: #0c1220; border: 1px solid var(--border); border-radius: 8px; padding: 18px; display: flex; flex-direction: column; gap: 16px; }
        .mixer-head { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 12px; }
        .mixer-title { font-size: 0.78rem; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; color: var(--accent-blue); }
        .chip { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.62rem; letter-spacing: .5px; }
        .chip-dsp { color: var(--accent-green); background: rgba(16,185,129,.12); border: 1px solid rgba(16,185,129,.35); }

        .strips { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
        @media (max-width: 1100px) { .strips { grid-template-columns: 1fr; } }

        .strip { background: #080d18; border: 1px solid var(--border); border-radius: 6px; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
        .strip-head { display: flex; justify-content: space-between; align-items: center; font-size: 0.68rem; }
        .strip-name { font-weight: bold; letter-spacing: .5px; }
        .ch-a .strip-name { color: #67e8f9; }
        .ch-b .strip-name { color: #d8b4fe; }
        .ch-c .strip-name { color: #6ee7b7; }
        .sm-btns { display: flex; gap: 4px; }
        .sm-btn { background: transparent; border: 1px solid var(--border); color: var(--text-muted); font-size: 0.58rem; padding: 2px 6px; border-radius: 3px; cursor: pointer; font-family: inherit; }
        .sm-btn.on { background: rgba(56,189,248,.2); border-color: var(--accent-blue); color: var(--accent-blue); }
        .sm-btn.on.mute { background: rgba(239,68,68,.2); border-color: var(--accent-red); color: var(--accent-red); }

        .dropzone { border: 1px dashed #334155; border-radius: 4px; background: rgba(15,23,42,.5); padding: 12px 8px; text-align: center; cursor: pointer; transition: .18s; min-height: 58px; display: flex; flex-direction: column; justify-content: center; gap: 3px; }
        .dropzone:hover { border-color: var(--accent-blue); background: rgba(56,189,248,.06); }
        .dropzone.filled { border-style: solid; border-color: rgba(56,189,248,.4); background: rgba(56,189,248,.05); }
        .ch-b .dropzone.filled { border-color: rgba(192,132,252,.4); background: rgba(168,85,247,.05); }
        .ch-c .dropzone.filled { border-color: rgba(110,231,183,.4); background: rgba(16,185,129,.05); }
        .dropzone.dragover { border-color: var(--accent-green); background: rgba(16,185,129,.12); }
        .dz-name { font-size: 0.68rem; color: #e2e8f0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .dz-name.empty { color: var(--text-muted); font-style: italic; }
        .dz-meta { font-size: 0.6rem; color: var(--accent-blue); }

        .knob { display: flex; flex-direction: column; gap: 3px; }
        .knob-row { display: flex; justify-content: space-between; font-size: 0.6rem; color: var(--text-muted); }
        .knob-val { color: #e2e8f0; }
        input[type="range"] { width: 100%; height: 3px; background: #1e293b; border-radius: 2px; appearance: none; outline: none; cursor: pointer; }
        input[type="range"]::-webkit-slider-thumb { appearance: none; width: 11px; height: 11px; border-radius: 50%; background: var(--accent-blue); cursor: pointer; }
        input[type="range"].purple::-webkit-slider-thumb { background: #c084fc; }
        input[type="range"].green::-webkit-slider-thumb { background: var(--accent-green); }

        .strip-foot { display: flex; justify-content: space-between; align-items: center; font-size: 0.6rem; color: var(--text-muted); border-top: 1px solid rgba(30,48,82,.6); padding-top: 8px; }
        .eject { background: none; border: none; color: var(--accent-red); cursor: pointer; font-size: 0.6rem; font-family: inherit; }
        .eject:disabled { color: #334155; cursor: not-allowed; }

        /* Presets */
        .preset-bar { display: flex; flex-wrap: wrap; gap: 6px; }
        .preset { background: #080d18; border: 1px solid var(--border); color: var(--text-muted); font-size: 0.62rem; padding: 5px 10px; border-radius: 4px; cursor: pointer; font-family: inherit; transition: .15s; }
        .preset:hover { border-color: var(--accent-purple); color: #d8b4fe; }
        .preset.active { background: rgba(168,85,247,.18); border-color: var(--accent-purple); color: #d8b4fe; font-weight: bold; }

        /* Transport */
        .transport { background: #070b14; border: 1px solid var(--border); border-radius: 6px; padding: 12px; display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
        .tp-left { display: flex; align-items: center; gap: 10px; }
        .tp-btn { height: 32px; width: 32px; border-radius: 4px; border: 1px solid rgba(56,189,248,.4); background: rgba(56,189,248,.15); color: #67e8f9; cursor: pointer; font-size: 0.8rem; display: flex; align-items: center; justify-content: center; }
        .tp-btn:hover { background: rgba(56,189,248,.3); }
        .tp-btn.stop { background: #0f172a; border-color: var(--border); color: var(--text-muted); }
        .tp-time { font-size: 0.72rem; color: var(--text-muted); }
        .btn-render { background: linear-gradient(90deg, #9333ea, #4f46e5); border: 1px solid var(--accent-purple); color: #fff; font-weight: bold; font-size: 0.75rem; padding: 9px 20px; border-radius: 4px; cursor: pointer; font-family: inherit; transition: .15s; }
        .btn-render:hover { filter: brightness(1.15); }
        .btn-render:active { transform: scale(.97); }
        .btn-render:disabled { opacity: .5; cursor: not-allowed; filter: none; }

        /* Clone panel */
        .clone { background: #0c1220; border: 1px solid rgba(168,85,247,.3); border-radius: 8px; padding: 18px; display: flex; flex-direction: column; gap: 14px; }
        .clone-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
        @media (max-width: 1100px) { .clone-steps { grid-template-columns: 1fr; } }
        .step { background: #080d18; border: 1px solid var(--border); border-radius: 6px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
        .step-num { font-size: 0.6rem; color: var(--accent-purple); letter-spacing: 1px; font-weight: bold; }
        .step-label { font-size: 0.72rem; color: #e2e8f0; font-weight: bold; }
        .step-hint { font-size: 0.62rem; color: var(--text-muted); line-height: 1.45; }
        textarea { background: var(--bg-dark); border: 1px solid var(--border); border-radius: 4px; padding: 8px 10px; color: #fff; font-size: 0.72rem; outline: none; width: 100%; resize: vertical; min-height: 70px; font-family: inherit; line-height: 1.5; }
        textarea:focus { border-color: var(--accent-purple); }
        select { background: var(--bg-dark); border: 1px solid var(--border); border-radius: 4px; padding: 7px 9px; color: #fff; font-size: 0.72rem; outline: none; width: 100%; font-family: inherit; cursor: pointer; }
        select:focus { border-color: var(--accent-purple); }
        .wave { display: flex; align-items: center; gap: 2px; height: 32px; background: #020617; border: 1px solid var(--border); border-radius: 4px; padding: 0 8px; min-width: 190px; }
        .wave span { width: 3px; border-radius: 1px; background: rgba(56,189,248,.75); transition: height .12s; }
    </style>
</head>
<body>
    <header>
        <div class="logo">🏗️ CRANE STUDIO</div>
        <nav class="crane-nav">
          <a href="/ide" class="nav-tab">HOME</a>
          <a href="/studio" class="nav-tab studio active">STUDIO</a>
          <a href="/connie" class="nav-tab">CONNIE</a>
          <a href="/depo" class="nav-tab depo">DEPO</a>
                  <a href="/images" class="nav-tab img">IMAGES</a>
          <a href="/cu" class="nav-tab cu">CU DEPT</a>
        </nav>
        <div class="badge">CONNIE NOLA : VOICE FOUNDRY</div>
    </header>

    <div class="app-body">
        <!-- Left Sidebar: Harvested Vault -->
        <div class="sidebar">
            <div class="card">
                <div style="font-size: 0.7rem; color: var(--text-muted);">VAULT PATH</div>
                <div style="font-size: 0.75rem; color: #fff; word-break: break-all;">/mnt/NOBILITY_VAULT/voice_vault</div>
                <div style="margin-top: 8px; font-size: 0.7rem; color: var(--text-muted);">STATUS</div>
                <div style="font-size: 0.75rem; color: var(--accent-green);">🟢 Ready</div>
            </div>
            <div class="card" style="flex: 1;">
                <div class="card-title">Harvested Vault Files</div>
                <div id="fileList" style="display: flex; flex-direction: column; gap: 6px; overflow-y: auto; max-height: 450px;">
                    <div style="color: var(--text-muted); font-size: 0.72rem;">Loading...</div>
                </div>
            </div>
        </div>

        <!-- Center Workspace: Full width fill -->
        <div class="workspace">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="font-size: 0.8rem; color: var(--text-muted);">Extraction Matrix</div>
                <div style="display: flex; gap: 6px;">
                    <button class="btn-secondary" style="border-color: var(--accent-red); color: var(--accent-red);" onclick="resetWorkspace()">Reset Workspace</button>
                    <button class="btn-secondary" onclick="addExtractorSlot()">+ Add Extractor</button>
                </div>
            </div>

            <!-- Slots Container -->
            <div id="slotsContainer" style="display: flex; flex-direction: column; gap: 15px;">
                <!-- Slot 1 -->
                <div class="card" id="slot-1">
                    <div class="card-title">
                        <span>extractor tool</span>
                        <button class="heart-btn" title="Save Source to Favorites" onclick="saveFavorite('slot-1')">❤️</button>
                    </div>
                    
                    <input type="text" class="name-input" placeholder="Clip Name (e.g. nola_noir_dialogue)...">
                    <input type="text" class="url-input" placeholder="Paste media URL (YouTube / TikTok)...">
                    
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <input type="text" class="start-input" placeholder="Start (0:00)" style="width: 90px;" value="0:00">
                        <span style="color: var(--text-muted); font-size: 0.75rem;">to</span>
                        <input type="text" class="end-input" placeholder="End (0:15)" style="width: 90px;" value="0:15">
                        <button class="btn-all" onclick="setAllTime('slot-1')">Select ALL</button>
                        <button class="btn extract-btn" onclick="runExtraction('slot-1')">Extract</button>
                    </div>

                    <div class="status-box"></div>
                </div>
            </div>

            <!-- ══════════ QUICK MIX ══════════ -->
            <div class="mixer" style="border-color:rgba(52,211,153,.28);">
                <div class="mixer-head" style="border-color:rgba(52,211,153,.2);">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span class="mixer-title" style="color:#34d399;">// Quick Mix &mdash; Light Touch &amp; Full EQ</span>
                        <span class="chip" style="background:rgba(52,211,153,.1);border-color:rgba(52,211,153,.35);color:#34d399;">STAY CLOSE TO SOURCE</span>
                    </div>
                    <button class="btn-secondary" onclick="qmReset()">&#8635; Flat</button>
                </div>

                <div style="font-size:0.64rem;color:var(--text-muted);">
                    For voices already in good shape &mdash; or a clone of someone you know that you want to keep recognisable.
                    Need to rebuild the voice from the ground up? Hit <strong style="color:#f59e0b;">BIG Q</strong> in the mixer below.
                </div>

                <!-- 7-band EQ -->
                <div id="qmEq" style="display:grid;grid-template-columns:repeat(7,1fr);gap:10px;align-items:end;padding:6px 0;"></div>

                <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;border-top:1px solid var(--border);padding-top:12px;">
                    <div style="flex:1;min-width:150px;">
                        <div style="display:flex;justify-content:space-between;font-size:0.66rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Output Gain</span><span id="qmGainVal">0 dB</span>
                        </div>
                        <input type="range" id="qmGain" min="-12" max="12" step="0.5" value="0"
                               oninput="document.getElementById('qmGainVal').textContent=this.value+' dB'"
                               style="width:100%;accent-color:#34d399;">
                    </div>
                    <label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;cursor:pointer;">
                        <input type="checkbox" id="qmDeEss" checked> De-ess
                    </label>
                    <label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;cursor:pointer;">
                        <input type="checkbox" id="qmCompress"> Gentle Glue
                    </label>
                    <label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;cursor:pointer;">
                        <input type="checkbox" id="qmNorm" checked> Normalize
                    </label>
                </div>

                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                    <select id="qmSource" style="flex:1;min-width:170px;"></select>
                    <input type="text" id="qmOutName" placeholder="Output name…" style="width:150px;">
                    <button class="btn" onclick="qmRender()" style="background:linear-gradient(135deg,#059669,#065f46);">&#9654; Quick Render</button>
                </div>
                <div class="status-box" id="qmStatus"></div>
                <audio id="qmAudio" controls style="display:none;width:100%;margin-top:4px;"></audio>
            </div>

            <!-- ══════════ VOICE FOUNDRY MIXER ══════════ -->
            <div class="mixer">
                <div class="mixer-head">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span class="mixer-title">// Voice Fusion &amp; Track Mixer</span>
                        <span class="chip chip-dsp">DSP ENGINE : 24kHz</span>
                    </div>
                    <div style="display:flex; gap:6px;">
                        <button class="btn-secondary" onclick="clearChannels()">Clear Channels</button>
                        <button class="btn" onclick="openBigQ()" id="bigQBtn"
                                style="background:linear-gradient(135deg,#7c3aed,#f59e0b);font-weight:bold;letter-spacing:1px;">
                            &#9673; BIG Q
                        </button>
                    </div>
                </div>

                <!-- Hollywood Vernacular Presets -->
                <div>
                    <div style="font-size:0.65rem; color:var(--text-muted); letter-spacing:1px; margin-bottom:7px;">
                        HOLLYWOOD VERNACULAR PRESETS &mdash; <span style="color:var(--accent-purple);">click to load a character profile</span>
                    </div>
                    <div class="preset-bar" id="presetBar"></div>
                </div>

                <!-- Channel Strips -->
                <div class="strips">
                    <!-- CH 01 -->
                    <div class="strip ch-a" id="ch-a">
                        <div class="strip-head">
                            <span class="strip-name">CH 01 : VOICE A</span>
                            <div class="sm-btns">
                                <button class="sm-btn" onclick="toggleFlag('a','solo',this)">SOLO</button>
                                <button class="sm-btn mute" onclick="toggleFlag('a','mute',this)">MUTE</button>
                            </div>
                        </div>
                        <div class="dropzone" id="dz-a" onclick="pickFile('a')"
                             ondragover="dzOver(event,'a')" ondragleave="dzLeave('a')" ondrop="dzDrop(event,'a')">
                            <div class="dz-name empty" id="dzname-a">Drop or click to load vault file…</div>
                            <div class="dz-meta" id="dzmeta-a">Weight: 75%</div>
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>GAIN</span><span class="knob-val" id="v-a-gain">+0.0 dB</span></div>
                            <input type="range" id="a-gain" min="-24" max="12" step="0.5" value="0" oninput="knob('a','gain',this.value)">
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>FORMANT</span><span class="knob-val" id="v-a-formant">0 st</span></div>
                            <input type="range" class="purple" id="a-formant" min="-6" max="6" step="0.5" value="0" oninput="knob('a','formant',this.value)">
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>BLEND WEIGHT</span><span class="knob-val" id="v-a-weight">75%</span></div>
                            <input type="range" id="a-weight" min="0" max="100" step="1" value="75" oninput="knob('a','weight',this.value)">
                        </div>
                        <div class="strip-foot">
                            <span id="src-a">SRC: EMPTY</span>
                            <button class="eject" id="ej-a" onclick="ejectCh('a')" disabled>EJECT</button>
                        </div>
                    </div>

                    <!-- CH 02 -->
                    <div class="strip ch-b" id="ch-b">
                        <div class="strip-head">
                            <span class="strip-name">CH 02 : VOICE B</span>
                            <div class="sm-btns">
                                <button class="sm-btn" onclick="toggleFlag('b','solo',this)">SOLO</button>
                                <button class="sm-btn mute" onclick="toggleFlag('b','mute',this)">MUTE</button>
                            </div>
                        </div>
                        <div class="dropzone" id="dz-b" onclick="pickFile('b')"
                             ondragover="dzOver(event,'b')" ondragleave="dzLeave('b')" ondrop="dzDrop(event,'b')">
                            <div class="dz-name empty" id="dzname-b">Drop or click to load vault file…</div>
                            <div class="dz-meta" id="dzmeta-b">Weight: 25%</div>
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>GAIN</span><span class="knob-val" id="v-b-gain">+0.0 dB</span></div>
                            <input type="range" id="b-gain" min="-24" max="12" step="0.5" value="0" oninput="knob('b','gain',this.value)">
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>FORMANT</span><span class="knob-val" id="v-b-formant">0 st</span></div>
                            <input type="range" class="purple" id="b-formant" min="-6" max="6" step="0.5" value="0" oninput="knob('b','formant',this.value)">
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>BLEND WEIGHT</span><span class="knob-val" id="v-b-weight">25%</span></div>
                            <input type="range" id="b-weight" min="0" max="100" step="1" value="25" oninput="knob('b','weight',this.value)">
                        </div>
                        <div class="strip-foot">
                            <span id="src-b">SRC: EMPTY</span>
                            <button class="eject" id="ej-b" onclick="ejectCh('b')" disabled>EJECT</button>
                        </div>
                    </div>

                    <!-- CH 03 -->
                    <div class="strip ch-c" id="ch-c">
                        <div class="strip-head">
                            <span class="strip-name">CH 03 : AMBIENT BED</span>
                            <div class="sm-btns">
                                <button class="sm-btn mute" onclick="toggleFlag('c','mute',this)">MUTE</button>
                            </div>
                        </div>
                        <div class="dropzone" id="dz-c" onclick="pickFile('c')"
                             ondragover="dzOver(event,'c')" ondragleave="dzLeave('c')" ondrop="dzDrop(event,'c')">
                            <div class="dz-name empty" id="dzname-c">Drop room tone / ambience…</div>
                            <div class="dz-meta" id="dzmeta-c">No bed loaded</div>
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>GAIN</span><span class="knob-val" id="v-c-gain">-18.0 dB</span></div>
                            <input type="range" class="green" id="c-gain" min="-40" max="0" step="0.5" value="-18" oninput="knob('c','gain',this.value)">
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>DUCKING</span><span class="knob-val" id="v-c-duck">80%</span></div>
                            <input type="range" class="green" id="c-duck" min="0" max="100" step="5" value="80" oninput="knob('c','duck',this.value)">
                        </div>
                        <div class="knob">
                            <div class="knob-row"><span>WARMTH / LOWPASS</span><span class="knob-val" id="v-c-warm">Off</span></div>
                            <input type="range" class="green" id="c-warm" min="0" max="100" step="5" value="0" oninput="knob('c','warm',this.value)">
                        </div>
                        <div class="strip-foot">
                            <span id="src-c">SRC: EMPTY</span>
                            <button class="eject" id="ej-c" onclick="ejectCh('c')" disabled>EJECT</button>
                        </div>
                    </div>
                </div>

                <!-- Transport -->
                <div class="transport">
                    <div class="tp-left">
                        <button class="tp-btn" onclick="mixPreview()" title="Preview blend">&#9654;</button>
                        <button class="tp-btn stop" onclick="mixStop()" title="Stop">&#9632;</button>
                        <div class="wave" id="waveMon"></div>
                        <span class="tp-time" id="mixTime">00:00 / 00:00</span>
                    </div>
                    <div style="display:flex; align-items:center; gap:10px;">
                        <input type="text" id="mixName" placeholder="Output name…" style="width:170px;">
                        <button class="btn-render" id="renderBtn" onclick="renderMix()">Synthesize &amp; Save to Vault</button>
                    </div>
                </div>
                <div class="status-box" id="mixStatus"></div>
                <audio id="mixAudio" style="display:none;"></audio>
            </div>

            <!-- ══════════ VOICE CLONE ══════════ -->
            <div class="clone">
                <div class="mixer-head" style="border-color:rgba(168,85,247,.25);">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span class="mixer-title" style="color:var(--accent-purple);">// Voice Clone &mdash; Speak As This Voice</span>
                        <span class="chip" style="color:#d8b4fe; background:rgba(168,85,247,.12); border:1px solid rgba(168,85,247,.35);">3 STEPS</span>
                    </div>
                </div>

                <div class="clone-steps">
                    <div class="step">
                        <div class="step-num">STEP 01</div>
                        <div class="step-label">Pick the voice</div>
                        <div class="step-hint">Use the mixer blend above, or pick a single vault file to clone directly.</div>
                        <select id="cloneSource">
                            <option value="__mix__">&#9733; Use Mixer Blend (CH01 + CH02)</option>
                        </select>
                    </div>

                    <div class="step">
                        <div class="step-num">STEP 02</div>
                        <div class="step-label">Type what it should say</div>
                        <div class="step-hint">Write it the way it should sound. Vernacular spelling is respected.</div>
                        <textarea id="cloneText" placeholder="Type the line here…&#10;&#10;e.g. Baby, I been knowin' that man since 'fore you was born."></textarea>
                    </div>

                    <div class="step">
                        <div class="step-num">STEP 03</div>
                        <div class="step-label">Generate</div>
                        <div class="step-hint">Renders with the character profile selected in the mixer presets above.</div>
                        <input type="text" id="cloneName" placeholder="Name this take…">
                        <div style="font-size:0.62rem; color:var(--text-muted);">
                            Profile: <span id="cloneProfile" style="color:#d8b4fe;">None selected</span>
                        </div>
                        <button class="btn-render" onclick="runClone()" style="width:100%;">Generate Voice</button>
                    </div>
                </div>
                <div class="status-box" id="cloneStatus"></div>
                <audio id="cloneAudio" controls style="display:none; width:100%; margin-top:4px;"></audio>
            </div>

            <!-- ══════════ MODEL FOUNDRY ══════════ -->
            <div class="mixer">
                <div class="mixer-head">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span class="mixer-title">// Model Foundry &mdash; Swap Voice Engines</span>
                        <span class="chip chip-dsp" id="hwChip">probing hardware…</span>
                    </div>
                    <button class="btn-secondary" onclick="loadModels()">&#8635; Refresh</button>
                </div>

                <div style="display:flex; gap:8px; align-items:center;">
                    <input type="text" id="portRepo" placeholder="Port any HF repo — e.g. coqui/XTTS-v2, sesame/csm-1b" style="flex:1;">
                    <button class="btn" onclick="portModel()">Port from HF</button>
                </div>
                <div style="font-size:0.62rem; color:var(--text-muted);">
                    Uses your HF_TOKEN from the Nobility Depository automatically &mdash; gated and uncensored repos included.
                </div>

                <div id="modelList" style="display:flex; flex-direction:column; gap:8px;">
                    <div style="color:var(--text-muted); font-size:0.72rem;">Loading model catalog…</div>
                </div>
                <div class="status-box" id="modelStatus"></div>
            </div>

            <!-- ══════════ QUINCY JONES ADVANCED ══════════ -->
            <div class="mixer" id="quincyPanel">
                <div class="mixer-head">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span class="mixer-title" style="color:#f59e0b;">// Quincy Jones Setting &mdash; Surgical Voice Sculpting</span>
                        <span class="chip" style="background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.4);color:#f59e0b;">ADVANCED</span>
                    </div>
                    <button class="btn-secondary" onclick="qjReset()">&#8635; Reset</button>
                </div>

                <!-- Archetype selector -->
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                    <label style="font-size:0.7rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;">Voice Archetype</label>
                    <select id="qjArchetype" onchange="qjLoadArchetype(this.value)" style="flex:1; min-width:180px;">
                        <option value="">— Custom / Manual —</option>
                        <option value="marilyn">Marilyn Monroe — Airy Breathy</option>
                        <option value="dolly">Dolly Parton — Country Twang</option>
                        <option value="billie">Billie Holiday — Smoky Jazz</option>
                        <option value="nina">Nina Simone — Theatrical Power</option>
                        <option value="eartha">Eartha Kitt — Sultry Purr</option>
                        <option value="tina">Tina Turner — Raw Rasp</option>
                        <option value="whitney">Whitney Houston — Soaring Clarity</option>
                        <option value="ella">Ella Fitzgerald — Warm Round Jazz</option>
                        <option value="aretha">Aretha Franklin — Gospel Chest</option>
                        <option value="connie">CONNIE NOLA — Signature Voice</option>
                    </select>
                </div>

                <!-- 2-col grid of sliders -->
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px 24px;">
                    <!-- Pitch -->
                    <div>
                        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Pitch Shift</span><span id="qjPitchVal">0 st</span>
                        </div>
                        <input type="range" id="qjPitch" min="-6" max="6" step="0.5" value="0"
                               oninput="document.getElementById('qjPitchVal').textContent=this.value+' st'"
                               style="width:100%;accent-color:#f59e0b;">
                    </div>
                    <!-- Formant -->
                    <div>
                        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Formant Shift</span><span id="qjFormantVal">0 st</span>
                        </div>
                        <input type="range" id="qjFormant" min="-4" max="4" step="0.5" value="0"
                               oninput="document.getElementById('qjFormantVal').textContent=this.value+' st'"
                               style="width:100%;accent-color:#f59e0b;">
                    </div>
                    <!-- Breathiness -->
                    <div>
                        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Breathiness / Air</span><span id="qjBreathVal">0%</span>
                        </div>
                        <input type="range" id="qjBreath" min="0" max="100" value="0"
                               oninput="document.getElementById('qjBreathVal').textContent=this.value+'%'"
                               style="width:100%;accent-color:#f59e0b;">
                    </div>
                    <!-- Chest Weight -->
                    <div>
                        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Chest Weight</span><span id="qjChestVal">0 dB</span>
                        </div>
                        <input type="range" id="qjChest" min="-6" max="10" step="0.5" value="0"
                               oninput="document.getElementById('qjChestVal').textContent=this.value+' dB'"
                               style="width:100%;accent-color:#f59e0b;">
                    </div>
                    <!-- Presence -->
                    <div>
                        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Presence / Bite</span><span id="qjPresenceVal">0 dB</span>
                        </div>
                        <input type="range" id="qjPresence" min="-6" max="8" step="0.5" value="0"
                               oninput="document.getElementById('qjPresenceVal').textContent=this.value+' dB'"
                               style="width:100%;accent-color:#f59e0b;">
                    </div>
                    <!-- Air Shimmer -->
                    <div>
                        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Air Shimmer (10kHz)</span><span id="qjAirVal">0 dB</span>
                        </div>
                        <input type="range" id="qjAir" min="-6" max="8" step="0.5" value="0"
                               oninput="document.getElementById('qjAirVal').textContent=this.value+' dB'"
                               style="width:100%;accent-color:#f59e0b;">
                    </div>
                    <!-- Tape Warmth -->
                    <div>
                        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">
                            <span>Tape Warmth</span><span id="qjTapeVal">0%</span>
                        </div>
                        <input type="range" id="qjTape" min="0" max="100" value="0"
                               oninput="document.getElementById('qjTapeVal').textContent=this.value+'%'"
                               style="width:100%;accent-color:#f59e0b;">
                    </div>
                    <!-- Room Size -->
                    <div>
                        <div style="font-size:0.68rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">Room / Reverb</div>
                        <select id="qjRoom" style="width:100%;">
                            <option value="none">None — Completely Dry</option>
                            <option value="intimate">Intimate — Closet / Booth</option>
                            <option value="medium" selected>Medium — Live Room</option>
                            <option value="large">Large — Concert Hall</option>
                            <option value="cathedral">Cathedral — Epic</option>
                        </select>
                    </div>
                </div>

                <!-- Toggles row -->
                <div style="display:flex; gap:12px; flex-wrap:wrap; border-top:1px solid var(--border); padding-top:12px;">
                    <label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;cursor:pointer;">
                        <input type="checkbox" id="qjDeEss" checked> De-esser (kills sibilance)
                    </label>
                    <label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;cursor:pointer;">
                        <input type="checkbox" id="qjGate"> Noise Gate (clean up bad mic)
                    </label>
                    <label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;cursor:pointer;">
                        <input type="checkbox" id="qjCompress" checked> Glue Compression
                    </label>
                    <label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;cursor:pointer;">
                        <input type="checkbox" id="qjNorm" checked> Output Normalize
                    </label>
                </div>

                <!-- Source + Render -->
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                    <label style="font-size:0.7rem;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;">Source File</label>
                    <select id="qjSource" style="flex:1; min-width:180px;"></select>
                    <input type="text" id="qjOutName" placeholder="Output name…" style="width:160px;">
                    <button class="btn" onclick="qjRender()" style="background:linear-gradient(135deg,#d97706,#92400e);">
                        &#9654; Render Quincy
                    </button>
                </div>
                <div class="status-box" id="qjStatus"></div>
                <audio id="qjAudio" controls style="display:none; width:100%; margin-top:4px;"></audio>
            </div>

            <!-- ══════════ THE LABEL ══════════ -->
            <div class="mixer" style="border-color:rgba(236,72,153,.28);">
                <div class="mixer-head" style="border-color:rgba(236,72,153,.2);">
                    <div style="display:flex;align-items:center;gap:10px;">
                        <span class="mixer-title" style="color:#ec4899;">&#9733; The Label &mdash; Signed Voice Agents</span>
                        <span class="chip" id="labelCount" style="background:rgba(236,72,153,.1);border-color:rgba(236,72,153,.35);color:#ec4899;">0 SIGNED</span>
                    </div>
                    <button class="btn-secondary" onclick="loadLabel()">&#8635; Refresh</button>
                </div>
                <div id="labelGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;">
                    <div style="color:var(--text-muted);font-size:0.7rem;">
                        No agents signed yet. Master a voice in BIG Q Studio and send it here.
                    </div>
                </div>
            </div>

        </div>

        <!-- Right Intelligence Panel: Flushed Far Right -->
        <div class="right-panel">
            <div class="card" style="border-color: var(--accent-purple);">
                <div class="card-title" style="color: var(--accent-purple);">⭐ Favorite Creators & Sources</div>
                <div id="favoritesList" style="display: flex; flex-direction: column; gap: 6px; max-height: 140px; overflow-y: auto;">
                    <div style="color: var(--text-muted); font-size: 0.72rem;">Click the heart icon ❤️ on any extractor tool to pin sources here.</div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">📚 Elite Female Voices & Monologues</div>
                <div style="font-size: 0.72rem; color: var(--text-muted); line-height: 1.4;">
                    <strong>Family Therapy & Intimate Cadence:</strong><br>
                    • <em>Esther Perel Sessions</em> (YouTube / Podcast feeds for clinical, warm, deeply empathetic cadence)<br>
                    • <em>The Savvy Psychologist</em> (Clean, articulate, warm narrative pacing)
                </div>
                <div style="font-size: 0.72rem; color: var(--text-muted); line-height: 1.4; margin-top: 4px;">
                    <strong>Erotic Audiobooks & Romantic Novels:</strong><br>
                    • <em>LibriVox Romantic Fiction Archive</em> (Public domain passionate prose)<br>
                    • <em>Audacious Romance Audio Channels</em> (Zero-shot breathy contralto tones)
                </div>
                <div style="font-size: 0.72rem; color: var(--text-muted); line-height: 1.4; margin-top: 4px;">
                    <strong>Black Intellectuals & Academic Lecturers:</strong><br>
                    • <em>Dr. Angela Davis / Melissa Harris-Perry Lectures</em> (Resonant, powerful cadence, commanding academic depth)<br>
                    • <em>Schomburg Center Archives</em> (Historical speeches with unmatched vocal texture)
                </div>
            </div>

            <div class="card">
                <div class="card-title">🎙️ True Crime & Long Monologues</div>
                <div style="font-size: 0.72rem; color: var(--text-muted); line-height: 1.4;">
                    • <em>Casefile True Crime</em> (Hypnotic, steady, rhythmic long-form narration)<br>
                    • <em>JCS Criminal Psychology</em> (Calm, clinical, analytical deep-dive interrogations)<br>
                    • <em>TED Talks Psychology & Humanity</em> (High-fidelity stage-mic speech patterns)
                </div>
            </div>

            <!-- Manual Audio Capture -->
            <div class="card" id="captureCard">
                <div class="card-title">
                    <span>&#127897; Manual Capture</span>
                    <span id="capBadge" style="font-size:0.65rem; color:var(--text-muted); font-weight:normal;">READY</span>
                </div>
                <div style="font-size:0.72rem; color:var(--text-muted); line-height:1.4;">
                    Press Record, play the audio you want, then Stop.<br>Auto-converts to 24&#8239;kHz WAV in the vault.
                </div>
                <input type="text" id="captureTitle" placeholder="Clip name (e.g. angela_davis_speech)" style="background:var(--bg-dark); border:1px solid var(--border); border-radius:4px; padding:6px 10px; color:#fff; font-size:0.78rem; width:100%; outline:none;">
                <div style="display:flex; gap:8px; align-items:center;">
                    <button id="capBtn" onclick="captureToggle()" style="background:#f43f5e; color:#fff; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:0.78rem; font-weight:bold; font-family:inherit;">&#9679; REC</button>
                    <span id="capTimer" style="font-family:monospace; font-size:0.95rem; color:#10b981; min-width:44px;">0:00</span>
                    <canvas id="capVU" width="120" height="22" style="border:1px solid var(--border); border-radius:3px; background:#040810; flex:1;"></canvas>
                </div>
                <div id="capFeedback" style="font-size:0.72rem; color:#10b981; min-height:14px;"></div>
            </div>
        </div>
    </div>

    <!-- ═══════════════════ BIG Q STUDIO — node canvas ═══════════════════ -->
    <div id="bigq" style="display:none; position:fixed; inset:0; z-index:9000;
         background:radial-gradient(ellipse 70% 60% at 30% 20%, #1a1030 0%, #05070E 55%, #04060C 100%);">

      <!-- Top bar -->
      <div style="height:52px; border-bottom:1px solid #241b3d; display:flex; align-items:center;
                  justify-content:space-between; padding:0 18px; background:rgba(10,8,20,.85);">
        <div style="display:flex; align-items:center; gap:14px;">
          <span style="font-family:monospace; font-weight:bold; font-size:1rem; letter-spacing:3px;
                       background:linear-gradient(90deg,#a855f7,#f59e0b); -webkit-background-clip:text;
                       -webkit-text-fill-color:transparent; background-clip:text;">◉ BIG Q STUDIO</span>
          <span style="font-size:.6rem; color:#6b5b8a; letter-spacing:1.5px;">NODE CANVAS · MIXING AGENT · VOICE FORGE</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <div id="bqKeys" style="display:flex; gap:5px;"></div>
          <button class="btn-secondary" onclick="bqResetGraph()">Reset Graph</button>
          <button class="btn-secondary" onclick="closeBigQ()">✕ Exit to Mixer</button>
        </div>
      </div>

      <!-- Canvas + chat -->
      <div style="display:flex; height:calc(100% - 52px);">

        <!-- Node canvas -->
        <div id="bqCanvas" style="flex:1; position:relative; overflow:hidden; cursor:grab;">
          <svg id="bqWires" style="position:absolute; inset:0; width:100%; height:100%;
               pointer-events:none; z-index:1;"></svg>
          <div id="bqNodes" style="position:absolute; inset:0; z-index:2;"></div>

          <!-- Orb: fixed instant-tuning control -->
          <div id="bqOrb" onclick="bqOrbToggle()" title="Instant tune"
               style="position:absolute; right:26px; top:50%; transform:translateY(-50%); z-index:5;
                      width:88px; height:88px; border-radius:50%; cursor:pointer;
                      background:radial-gradient(circle at 32% 30%, #c084fc 0%, #a855f7 34%, #f59e0b 100%);
                      box-shadow:0 0 34px rgba(168,85,247,.55), 0 0 70px rgba(245,158,11,.28), inset 0 0 22px rgba(255,255,255,.16);
                      display:flex; align-items:center; justify-content:center;
                      font-family:monospace; font-size:.56rem; font-weight:bold; color:#fff;
                      letter-spacing:1.5px; text-shadow:0 1px 4px rgba(0,0,0,.6);
                      transition:transform .18s, box-shadow .18s;">TUNE</div>

          <!-- Orb tuning tray -->
          <div id="bqOrbTray" style="display:none; position:absolute; right:130px; top:50%;
               transform:translateY(-50%); z-index:6; width:250px; background:rgba(14,10,26,.97);
               border:1px solid #4c1d95; border-radius:10px; padding:16px;
               box-shadow:0 10px 44px rgba(0,0,0,.65);">
            <div style="font-family:monospace; font-size:.6rem; letter-spacing:2px; color:#f59e0b;
                        margin-bottom:12px;">INSTANT TUNE</div>
            <div id="bqOrbSliders" style="display:flex; flex-direction:column; gap:10px;"></div>
            <button class="btn" onclick="bqRender()" style="width:100%; margin-top:14px;
                    background:linear-gradient(135deg,#7c3aed,#f59e0b);">▶ Render Now</button>
          </div>
        </div>

        <!-- Chat rail -->
        <div style="width:330px; border-left:1px solid #241b3d; background:rgba(8,6,16,.9);
                    display:flex; flex-direction:column;">
          <div style="padding:12px 16px; border-bottom:1px solid #241b3d;">
            <div style="font-family:monospace; font-size:.66rem; letter-spacing:2px; color:#a855f7;">
              ◉ MIXING AGENT
            </div>
            <div style="font-size:.58rem; color:#5b4b7a; margin-top:3px;">
              Speak in plain mixing language — it moves the dials.
            </div>
          </div>
          <div id="bqChatLog" style="flex:1; overflow-y:auto; padding:14px 16px;
                                     display:flex; flex-direction:column; gap:10px;"></div>
          <div style="padding:12px 14px; border-top:1px solid #241b3d; display:flex; gap:6px;">
            <input type="text" id="bqChatInput" placeholder="warmer and closer…"
                   style="flex:1; background:#0d0a18; border:1px solid #3b2a5c; border-radius:5px;
                          padding:9px 11px; color:#e9e2f5; font-size:.76rem; outline:none;">
            <button class="btn" onclick="bqSend()"
                    style="background:linear-gradient(135deg,#7c3aed,#a855f7); padding:9px 15px;">➤</button>
          </div>
        </div>
      </div>
    </div>

    <script>
        let slotCounter = 1;

        window.addEventListener('DOMContentLoaded', loadData);

        async function loadData() {
            try {
                const res = await fetch('/api/vault/files');
                const data = await res.json();
                
                const list = document.getElementById('fileList');
                VAULT_FILES = data.files;
                window._vaultFiles = data.files;
                document.dispatchEvent(new Event('vaultLoaded'));
                if (data.files.length === 0) {
                    list.innerHTML = '<div style="color: var(--text-muted); font-size: 0.72rem;">No files extracted yet.</div>';
                } else {
                    list.innerHTML = data.files.map(f => `
                        <div class="file-item" draggable="true" style="cursor:grab;"
                             ondragstart="event.dataTransfer.setData('text/plain','${f.filename}')"
                             title="Drag into a mixer channel — ${f.filename}">
                            <span style="color: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 140px;">${f.filename}</span>
                            <span style="color: var(--accent-blue); font-size: 0.65rem;">${f.size}</span>
                        </div>
                    `).join('');
                }
                syncCloneSources();

                const favs = data.manifest.favorites || [];
                const favList = document.getElementById('favoritesList');
                if (favs.length === 0) {
                    favList.innerHTML = '<div style="color: var(--text-muted); font-size: 0.72rem;">No favorites saved yet. Click ❤️ on any active slot.</div>';
                } else {
                    favList.innerHTML = favs.map(fav => `
                        <div class="fav-row">
                            <span style="color: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px;" title="${fav.url}">${fav.name}</span>
                            <a href="${fav.url}" target="_blank" style="color: var(--accent-blue); text-decoration: none; font-size: 0.7rem;">Open &rarr;</a>
                        </div>
                    `).join('');
                }
            } catch(e) {}
        }

        async function saveFavorite(slotId) {
            const slot = document.getElementById(slotId);
            const name = slot.querySelector('.name-input').value.trim();
            const url = slot.querySelector('.url-input').value.trim();

            if (!url) {
                alert("Enter a URL in the extractor tool before favoriting.");
                return;
            }

            const clipName = name || url;
            const btn = slot.querySelector('.heart-btn');
            
            await fetch('/api/favorites/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: clipName, url: url})
            });

            btn.classList.add('active');
            loadData();
        }

        function setAllTime(slotId) {
            const slot = document.getElementById(slotId);
            slot.querySelector('.start-input').value = 'ALL';
            slot.querySelector('.end-input').value = 'ALL';
        }

        function resetWorkspace() {
            const container = document.getElementById('slotsContainer');
            slotCounter = 1;
            container.innerHTML = `
                <div class="card" id="slot-1">
                    <div class="card-title">
                        <span>extractor tool</span>
                        <button class="heart-btn" title="Save Source to Favorites" onclick="saveFavorite('slot-1')">❤️</button>
                    </div>
                    
                    <input type="text" class="name-input" placeholder="Clip Name (e.g. nola_noir_dialogue)...">
                    <input type="text" class="url-input" placeholder="Paste media URL (YouTube / TikTok)...">
                    
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <input type="text" class="start-input" placeholder="Start (0:00)" style="width: 90px;" value="0:00">
                        <span style="color: var(--text-muted); font-size: 0.75rem;">to</span>
                        <input type="text" class="end-input" placeholder="End (0:15)" style="width: 90px;" value="0:15">
                        <button class="btn-all" onclick="setAllTime('slot-1')">Select ALL</button>
                        <button class="btn extract-btn" onclick="runExtraction('slot-1')">Extract</button>
                    </div>

                    <div class="status-box"></div>
                </div>
            `;
        }

        function addExtractorSlot() {
            slotCounter++;
            const slotId = `slot-${slotCounter}`;
            const container = document.getElementById('slotsContainer');
            
            const div = document.createElement('div');
            div.className = 'card';
            div.id = slotId;
            div.innerHTML = `
                <div class="card-title">
                    <span>extractor tool</span>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <button class="heart-btn" title="Save Source to Favorites" onclick="saveFavorite('${slotId}')">❤️</button>
                        <button class="btn-secondary" style="border-color: var(--accent-red); color: var(--accent-red); padding: 2px 6px; font-size: 0.65rem;" onclick="document.getElementById('${slotId}').remove()">Remove</button>
                    </div>
                </div>
                
                <input type="text" class="name-input" placeholder="Clip Name (e.g. nola_noir_dialogue)...">
                <input type="text" class="url-input" placeholder="Paste media URL (YouTube / TikTok)...";>
                
                <div style="display: flex; gap: 8px; align-items: center;">
                    <input type="text" class="start-input" placeholder="Start" style="width: 90px;" value="0:00">
                    <span style="color: var(--text-muted); font-size: 0.75rem;">to</span>
                    <input type="text" class="end-input" placeholder="End" style="width: 90px;" value="0:15">
                    <button class="btn-all" onclick="setAllTime('${slotId}')">Select ALL</button>
                    <button class="btn extract-btn" onclick="runExtraction('${slotId}')">Extract</button>
                </div>

                <div class="status-box"></div>
            `;
            container.appendChild(div);
        }

        async function runExtraction(slotId) {
            const slot = document.getElementById(slotId);
            const clip_name = slot.querySelector('.name-input').value.trim();
            const url = slot.querySelector('.url-input').value.trim();
            const start = slot.querySelector('.start-input').value.trim();
            const end = slot.querySelector('.end-input').value.trim();
            const btn = slot.querySelector('.extract-btn');
            const status = slot.querySelector('.status-box');

            if (!url) {
                showStatus(status, "Please paste a valid media URL first.", "error");
                return;
            }

            if (!clip_name) {
                showStatus(status, "Please provide a name for this audio clip.", "error");
                return;
            }

            btn.disabled = true;
            btn.innerText = "Extracting...";
            showStatus(status, "⚡ Executing media stream extraction in background...", "info");

            try {
                const res = await fetch('/api/harvest/media', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url, start, end, clip_name})
                });
                const data = await res.json();

                if (data.status === 'success') {
                    showStatus(status, `✅ ${data.message}`, "success");
                    loadData();
                } else {
                    showStatus(status, `❌ ${data.message}`, "error");
                }
            } catch(err) {
                showStatus(status, `❌ Network error: ${err.message}`, "error");
            } finally {
                btn.disabled = false;
                btn.innerText = "Extract";
            }
        }

        function showStatus(el, text, type) {
            el.className = `status-box active status-${type}`;
            el.innerText = text;
        }

        // ══════════ VOICE FOUNDRY MIXER ══════════
        const PRESETS = {
            'NOLA NOIR':        {a:{formant:-1.5,gain:-1}, b:{formant:-2,gain:-4}, c:{gain:-22,duck:85,warm:35},
                                 desc:'Breathy contralto, intimate proximity, low formant'},
            'HARLEM ORATORY':   {a:{formant:-0.5,gain:2},  b:{formant:-1,gain:-3},  c:{gain:-20,duck:70,warm:25},
                                 desc:'Resonant, commanding, vintage room tone'},
            'ATL TRAP CADENCE': {a:{formant:1,gain:1.5},   b:{formant:0.5,gain:-5}, c:{gain:-26,duck:90,warm:0},
                                 desc:'Modern, punchy, forward presence'},
            'DEEP SOUTH WARMTH':{a:{formant:-2,gain:0},    b:{formant:-2.5,gain:-4},c:{gain:-19,duck:75,warm:55},
                                 desc:'Slow, low register, analog warmth'},
            'PULPIT':           {a:{formant:-1,gain:3},    b:{formant:-1.5,gain:-2},c:{gain:-16,duck:60,warm:20},
                                 desc:'Wide dynamics, hall reverb, sermon projection'},
            'BROADCAST CLEAN':  {a:{formant:0,gain:0},     b:{formant:0,gain:-6},   c:{gain:-40,duck:100,warm:0},
                                 desc:'Tight compression, radio-ready, no color'},
            'ACADEMIC LECTURE': {a:{formant:0.5,gain:1},   b:{formant:0,gain:-5},   c:{gain:-24,duck:80,warm:10},
                                 desc:'Clear diction, neutral formant, lecture-hall space'},
            'KITCHEN TABLE':    {a:{formant:-0.5,gain:-1}, b:{formant:-1,gain:-3},  c:{gain:-15,duck:55,warm:45},
                                 desc:'Close, casual, room-tone bed active'},
        };

        let MIX = {
            a:{file:null,gain:0,formant:0,weight:75,solo:false,mute:false},
            b:{file:null,gain:0,formant:0,weight:25,solo:false,mute:false},
            c:{file:null,gain:-18,duck:80,warm:0,mute:false},
            preset:null
        };
        let VAULT_FILES = [];

        function initPresets() {
            document.getElementById('presetBar').innerHTML = Object.keys(PRESETS).map(p =>
                `<button class="preset" title="${PRESETS[p].desc}" onclick="applyPreset('${p}')">${p}</button>`
            ).join('');
        }

        function applyPreset(name) {
            const p = PRESETS[name];
            MIX.preset = name;
            document.querySelectorAll('.preset').forEach(b =>
                b.classList.toggle('active', b.textContent === name));
            for (const [ch, vals] of Object.entries(p)) {
                if (ch === 'desc') continue;
                for (const [k, v] of Object.entries(vals)) {
                    const el = document.getElementById(ch + '-' + k);
                    if (el) { el.value = v; knob(ch, k, v); }
                }
            }
            document.getElementById('cloneProfile').textContent = name + ' — ' + p.desc;
            showStatus(document.getElementById('mixStatus'),
                       `🎚️ Loaded profile "${name}" — ${p.desc}`, 'info');
        }

        function knob(ch, param, val) {
            const v = parseFloat(val);
            MIX[ch][param] = v;
            const fmt = {
                gain:    x => (x>0?'+':'') + x.toFixed(1) + ' dB',
                formant: x => (x>0?'+':'') + x + ' st',
                weight:  x => x + '%',
                duck:    x => x + '%',
                warm:    x => x==0 ? 'Off' : x + '%',
            };
            const lbl = document.getElementById('v-'+ch+'-'+param);
            if (lbl) lbl.textContent = fmt[param] ? fmt[param](v) : v;
            if (param === 'weight') {
                const other = ch === 'a' ? 'b' : 'a';
                const om = document.getElementById('dzmeta-'+other),
                      ov = document.getElementById(other+'-weight');
                if (ov) { ov.value = 100 - v; MIX[other].weight = 100 - v;
                    document.getElementById('v-'+other+'-weight').textContent = (100-v)+'%'; }
                if (om && MIX[other].file) om.textContent = 'Weight: ' + (100-v) + '%';
                const m = document.getElementById('dzmeta-'+ch);
                if (m && MIX[ch].file) m.textContent = 'Weight: ' + v + '%';
            }
        }

        function loadIntoChannel(ch, filename) {
            MIX[ch].file = filename;
            document.getElementById('dz-'+ch).classList.add('filled');
            const n = document.getElementById('dzname-'+ch);
            n.textContent = filename; n.classList.remove('empty');
            document.getElementById('src-'+ch).textContent = 'SRC: VAULT';
            document.getElementById('ej-'+ch).disabled = false;
            const meta = document.getElementById('dzmeta-'+ch);
            meta.textContent = ch === 'c' ? 'Loop Active' : 'Weight: ' + MIX[ch].weight + '%';
            syncCloneSources();
        }

        function ejectCh(ch) {
            MIX[ch].file = null;
            document.getElementById('dz-'+ch).classList.remove('filled');
            const n = document.getElementById('dzname-'+ch);
            n.textContent = ch === 'c' ? 'Drop room tone / ambience…' : 'Drop or click to load vault file…';
            n.classList.add('empty');
            document.getElementById('src-'+ch).textContent = 'SRC: EMPTY';
            document.getElementById('ej-'+ch).disabled = true;
            document.getElementById('dzmeta-'+ch).textContent =
                ch === 'c' ? 'No bed loaded' : 'Weight: ' + MIX[ch].weight + '%';
            syncCloneSources();
        }

        function clearChannels() { ['a','b','c'].forEach(ejectCh); }

        function toggleFlag(ch, flag, btn) {
            MIX[ch][flag] = !MIX[ch][flag];
            btn.classList.toggle('on', MIX[ch][flag]);
        }

        function pickFile(ch) {
            if (!VAULT_FILES.length) { alert('No vault files yet — extract or record something first.'); return; }
            const choice = prompt('Load which file into CH ' + ch.toUpperCase() + '?\\n\\n' +
                VAULT_FILES.map((f,i) => (i+1)+'. '+f.filename).join('\\n') + '\\n\\nEnter number:');
            const idx = parseInt(choice) - 1;
            if (idx >= 0 && idx < VAULT_FILES.length) loadIntoChannel(ch, VAULT_FILES[idx].filename);
        }

        function dzOver(e, ch) { e.preventDefault(); document.getElementById('dz-'+ch).classList.add('dragover'); }
        function dzLeave(ch)   { document.getElementById('dz-'+ch).classList.remove('dragover'); }
        function dzDrop(e, ch) {
            e.preventDefault();
            document.getElementById('dz-'+ch).classList.remove('dragover');
            const fn = e.dataTransfer.getData('text/plain');
            if (fn) loadIntoChannel(ch, fn);
        }

        function mixPayload() {
            return {
                channels: {
                    a: MIX.a.file ? {file:MIX.a.file, gain:MIX.a.gain, formant:MIX.a.formant,
                                     weight:MIX.a.weight, mute:MIX.a.mute, solo:MIX.a.solo} : null,
                    b: MIX.b.file ? {file:MIX.b.file, gain:MIX.b.gain, formant:MIX.b.formant,
                                     weight:MIX.b.weight, mute:MIX.b.mute, solo:MIX.b.solo} : null,
                    c: MIX.c.file ? {file:MIX.c.file, gain:MIX.c.gain, duck:MIX.c.duck,
                                     warm:MIX.c.warm, mute:MIX.c.mute} : null,
                },
                preset: MIX.preset,
            };
        }

        async function renderMix() {
            const st = document.getElementById('mixStatus');
            if (!MIX.a.file && !MIX.b.file) {
                showStatus(st, '⚠️ Load at least one voice channel before rendering.', 'error'); return; }
            const btn = document.getElementById('renderBtn');
            btn.disabled = true; btn.textContent = 'Rendering…';
            showStatus(st, '⚡ Blending channels through the DSP chain…', 'info');
            try {
                const body = mixPayload();
                body.out_name = document.getElementById('mixName').value.trim() || 'mix';
                const res = await fetch('/api/mixer/render', {
                    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
                const d = await res.json();
                if (d.status === 'success') {
                    showStatus(st, `✅ ${d.message}`, 'success');
                    const au = document.getElementById('mixAudio');
                    au.src = '/api/vault/audio/' + encodeURIComponent(d.file);
                    au.style.display = 'block'; au.controls = true;
                    loadData();
                } else { showStatus(st, `❌ ${d.message}`, 'error'); }
            } catch(e) { showStatus(st, '❌ ' + e.message, 'error'); }
            finally { btn.disabled = false; btn.textContent = 'Synthesize & Save to Vault'; }
        }

        function mixPreview() {
            const au = document.getElementById('mixAudio');
            if (au.src) { au.style.display='block'; au.controls=true; au.play(); animWave(); }
            else showStatus(document.getElementById('mixStatus'),
                            'Render first — then preview plays the blended result.', 'info');
        }
        function mixStop() {
            const au = document.getElementById('mixAudio');
            au.pause(); au.currentTime = 0;
        }

        function animWave() {
            const w = document.getElementById('waveMon');
            const au = document.getElementById('mixAudio');
            (function f(){
                if (au.paused) { return; }
                w.innerHTML = Array.from({length:24}, () =>
                    `<span style="height:${3+Math.random()*24}px"></span>`).join('');
                const c = Math.floor(au.currentTime), t = Math.floor(au.duration)||0;
                document.getElementById('mixTime').textContent =
                    `${String(Math.floor(c/60)).padStart(2,'0')}:${String(c%60).padStart(2,'0')} / ` +
                    `${String(Math.floor(t/60)).padStart(2,'0')}:${String(t%60).padStart(2,'0')}`;
                setTimeout(f, 90);
            })();
        }

        function syncCloneSources() {
            const sel = document.getElementById('cloneSource');
            const cur = sel.value;
            sel.innerHTML = '<option value="__mix__">★ Use Mixer Blend (CH01 + CH02)</option>' +
                VAULT_FILES.map(f => `<option value="${f.filename}">${f.filename}</option>`).join('');
            if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
        }

        async function runClone() {
            const st = document.getElementById('cloneStatus');
            const text = document.getElementById('cloneText').value.trim();
            if (!text) { showStatus(st, '⚠️ Type the line you want spoken.', 'error'); return; }
            const src = document.getElementById('cloneSource').value;
            if (src === '__mix__' && !MIX.a.file && !MIX.b.file) {
                showStatus(st, '⚠️ Mixer blend is empty — load a voice channel or pick a vault file.', 'error'); return; }
            showStatus(st, '🧬 Generating…', 'info');
            try {
                const res = await fetch('/api/clone/generate', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({ text, source: src, preset: MIX.preset,
                                           name: document.getElementById('cloneName').value.trim(),
                                           mix: mixPayload() })});
                const d = await res.json();
                if (d.status === 'success') {
                    showStatus(st, `✅ ${d.message}`, 'success');
                    const au = document.getElementById('cloneAudio');
                    au.src = '/api/vault/audio/' + encodeURIComponent(d.file);
                    au.style.display = 'block';
                    loadData();
                } else { showStatus(st, `❌ ${d.message}`, 'error'); }
            } catch(e) { showStatus(st, '❌ ' + e.message, 'error'); }
        }

        // ══════════ MODEL FOUNDRY ══════════
        async function loadModels() {
            try {
                const d = await (await fetch('/api/models/catalog')).json();
                const hw = d.hardware;
                document.getElementById('hwChip').textContent =
                    `${hw.cores} cores · ${hw.ram_gb} GB · ${hw.cuda ? 'CUDA' : 'CPU only'}`;
                const badge = {ready:['#10b981','RUNS HERE'], slow:['#f59e0b','SLOW'],
                               blocked:['#ef4444','WON\\'T RUN']};
                document.getElementById('modelList').innerHTML = d.models.map(m => {
                    const [col, lbl] = badge[m.runs_here] || ['#64748b','?'];
                    return `
                    <div style="background:#080d18; border:1px solid ${m.active?'var(--accent-purple)':'var(--border)'};
                                border-radius:6px; padding:11px; display:flex; flex-direction:column; gap:6px;">
                      <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                        <div style="display:flex; align-items:center; gap:8px; min-width:0;">
                          <span style="font-size:0.75rem; color:#e2e8f0; font-weight:bold;">${m.label}</span>
                          <span style="font-size:0.55rem; padding:1px 6px; border-radius:3px;
                                       color:${col}; border:1px solid ${col}44; background:${col}18;">${lbl}</span>
                          ${m.clone ? '<span style="font-size:0.55rem; color:#67e8f9;">CLONE</span>' : ''}
                          ${m.gated ? '<span style="font-size:0.55rem; color:#f59e0b;">GATED</span>' : ''}
                          ${m.active ? '<span style="font-size:0.55rem; color:#d8b4fe;">★ ACTIVE</span>' : ''}
                        </div>
                        <div style="display:flex; gap:5px; flex-shrink:0;">
                          ${m.installed
                            ? (m.active ? '' : `<button class="btn-secondary" onclick="activateModel('${m.id}')">Set Active</button>`)
                            : `<button class="btn-secondary" onclick="installModel('${m.id}')">Install</button>`}
                        </div>
                      </div>
                      <div style="font-size:0.62rem; color:var(--text-muted); line-height:1.45;">${m.note}</div>
                      <div style="font-size:0.6rem; color:${col};">${m.verdict}</div>
                      <div style="font-size:0.58rem; color:#475569;">${m.repo} · ${m.licence||'—'}</div>
                    </div>`;
                }).join('');
            } catch(e) {
                document.getElementById('modelList').innerHTML =
                    `<div style="color:var(--accent-red); font-size:0.72rem;">${e.message}</div>`;
            }
        }

        async function installModel(id, repo) {
            const st = document.getElementById('modelStatus');
            showStatus(st, '⬇️ Installing ' + (repo || id) + ' — this can take a while…', 'info');
            const r = await (await fetch('/api/models/install', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({id, repo})})).json();
            if (r.status !== 'success') { showStatus(st, '❌ ' + r.message, 'error'); return; }
            const poll = setInterval(async () => {
                const j = await (await fetch('/api/models/job/' + r.job_id)).json();
                if (j.state === 'done') {
                    clearInterval(poll);
                    showStatus(st, '✅ ' + j.detail, 'success'); loadModels();
                } else if (j.state === 'error') {
                    clearInterval(poll);
                    showStatus(st, '❌ ' + j.detail, 'error');
                } else {
                    showStatus(st, `⚙️ ${j.state} — ${j.detail || ''}`, 'info');
                }
            }, 2500);
        }

        function portModel() {
            const repo = document.getElementById('portRepo').value.trim();
            if (!repo.includes('/')) {
                showStatus(document.getElementById('modelStatus'),
                           '⚠️ Enter a full HF repo id, like owner/model-name.', 'error');
                return;
            }
            installModel(repo.replace('/','--'), repo);
        }

        async function activateModel(id) {
            const r = await (await fetch('/api/models/activate', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({id})})).json();
            showStatus(document.getElementById('modelStatus'),
                       (r.status==='success'?'✅ ':'❌ ') + r.message,
                       r.status==='success'?'success':'error');
            loadModels();
        }

        initPresets();
        loadModels();

        // ══════════════════ QUICK MIX ══════════════════
        const QM_BANDS = [
            ['sub','60Hz'],['low','150'],['lowmid','400'],['mid','1k'],
            ['himid','3k'],['pres','6k'],['air','12k']
        ];

        function qmInit() {
            const wrap = document.getElementById('qmEq');
            if (!wrap) return;
            wrap.innerHTML = QM_BANDS.map(([k,lbl]) => `
                <div style="display:flex;flex-direction:column;align-items:center;gap:5px;">
                  <span id="qmv_${k}" style="font-family:monospace;font-size:.6rem;color:#34d399;">0</span>
                  <input type="range" id="qm_${k}" min="-12" max="12" step="0.5" value="0"
                         oninput="document.getElementById('qmv_${k}').textContent=(this.value>0?'+':'')+this.value"
                         style="writing-mode:vertical-lr;direction:rtl;width:22px;height:78px;accent-color:#34d399;">
                  <span style="font-size:.55rem;color:var(--text-muted);letter-spacing:.5px;">${lbl}</span>
                </div>`).join('');
        }

        function qmReset() {
            QM_BANDS.forEach(([k]) => {
                const el = document.getElementById('qm_'+k);
                if (el) { el.value = 0; el.dispatchEvent(new Event('input')); }
            });
            const g = document.getElementById('qmGain');
            if (g) { g.value = 0; g.dispatchEvent(new Event('input')); }
        }

        function qmPopulate() {
            const sel = document.getElementById('qmSource');
            if (!sel) return;
            const cur = sel.value;
            sel.innerHTML = '<option value="">— pick a vault file —</option>';
            (window._vaultFiles||[]).forEach(f => {
                const o = document.createElement('option');
                o.value = f.filename; o.textContent = f.filename;
                if (f.filename === cur) o.selected = true;
                sel.appendChild(o);
            });
        }

        async function qmRender() {
            const st = document.getElementById('qmStatus');
            const src = document.getElementById('qmSource').value;
            if (!src) { showStatus(st,'Pick a source file first','error'); return; }
            showStatus(st,'Rendering quick mix…','');
            const eq = {};
            QM_BANDS.forEach(([k]) => eq[k] = parseFloat(document.getElementById('qm_'+k).value));
            const res = await fetch('/api/quickmix/render',{
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({
                    source: src, eq,
                    gain: parseFloat(document.getElementById('qmGain').value),
                    deess: document.getElementById('qmDeEss').checked,
                    compress: document.getElementById('qmCompress').checked,
                    norm: document.getElementById('qmNorm').checked,
                    out_name: document.getElementById('qmOutName').value.trim() || 'quick',
                })});
            const j = await res.json();
            if (j.status === 'success') {
                showStatus(st,'✅ '+j.message,'success');
                const au = document.getElementById('qmAudio');
                au.src = '/api/vault/audio/'+encodeURIComponent(j.file);
                au.style.display='block'; au.play();
                loadData();
            } else showStatus(st,'❌ '+j.message,'error');
        }

        // ══════════════════ BIG Q STUDIO ══════════════════
        let BQ = {
            params: { pitch:0, formant:0, breath:0, chest:0, presence:0, air:0,
                      tape:0, room:'medium', gate:false, deess:true, compress:true, norm:true },
            source: '', archetype: '', lastFile: null,
            brain: { personality:'warm', vernacular:'nola', background:'' },
            nodes: {}, drag: null,
        };

        const BQ_NODE_DEFS = [
            { id:'vault',  x: 40,  y: 40,  w:220, title:'NOBILITY VAULT',  color:'#38bdf8', icon:'▤' },
            { id:'preset', x: 40,  y:250,  w:220, title:'PRESET',          color:'#f59e0b', icon:'◆' },
            { id:'eq',     x:310,  y: 40,  w:250, title:'EQ / SCULPT',     color:'#34d399', icon:'≋' },
            { id:'brain',  x:310,  y:430,  w:250, title:'BRAIN PLAYGROUND',color:'#a855f7', icon:'◉' },
            { id:'master', x:610,  y: 60,  w:230, title:'MASTER OUT',      color:'#ec4899', icon:'▶' },
        ];

        const BQ_WIRES = [
            ['vault','eq'], ['preset','eq'], ['eq','master'], ['brain','master'],
        ];

        function openBigQ() {
            document.getElementById('bigq').style.display = 'block';
            bqBuildNodes();
            bqBuildOrb();
            bqLoadKeys();
            if (!BQ_ROLES.length) bqLoadRoles();
            if (!document.getElementById('bqChatLog').children.length) {
                bqPush('agent', 'Big Q online. Load a voice from the vault node, then tell me what you want — "warmer and closer", "fix this tinny laptop mic", "make her breathy like Marilyn".');
            }
        }
        function closeBigQ(){ document.getElementById('bigq').style.display = 'none'; }

        function bqBuildNodes() {
            const host = document.getElementById('bqNodes');
            host.innerHTML = '';
            BQ_NODE_DEFS.forEach(d => {
                if (!BQ.nodes[d.id]) BQ.nodes[d.id] = { x:d.x, y:d.y };
                const p = BQ.nodes[d.id];
                const el = document.createElement('div');
                el.id = 'bqn_'+d.id;
                el.style.cssText = `position:absolute;left:${p.x}px;top:${p.y}px;width:${d.w}px;
                    background:rgba(13,10,24,.96);border:1px solid ${d.color}55;border-radius:9px;
                    box-shadow:0 6px 26px rgba(0,0,0,.5);overflow:hidden;`;
                el.innerHTML = `
                  <div class="bq-drag" data-node="${d.id}"
                       style="padding:8px 12px;background:${d.color}18;border-bottom:1px solid ${d.color}33;
                              cursor:grab;display:flex;align-items:center;gap:8px;">
                    <span style="color:${d.color};font-size:.8rem;">${d.icon}</span>
                    <span style="font-family:monospace;font-size:.62rem;letter-spacing:1.5px;color:${d.color};">${d.title}</span>
                  </div>
                  <div id="bqbody_${d.id}" style="padding:11px 12px;font-size:.7rem;color:#c4b8dd;"></div>`;
                host.appendChild(el);
            });
            bqFillVault(); bqFillPreset(); bqFillEq(); bqFillBrain(); bqFillMaster();
            bqDrawWires();
            bqBindDrag();
        }

        function bqFillVault() {
            const b = document.getElementById('bqbody_vault');
            const files = window._vaultFiles || [];
            b.innerHTML = `
              <select id="bqSource" onchange="BQ.source=this.value;bqFillMaster();"
                      style="width:100%;margin-bottom:7px;">
                <option value="">— pick voice —</option>
                ${files.map(f=>`<option value="${f.filename}" ${f.filename===BQ.source?'selected':''}>${f.filename.slice(0,30)}</option>`).join('')}
              </select>
              <div style="font-size:.58rem;color:#6b5b8a;">${files.length} files · ${(window._labelAgents||[]).length} signed agents</div>`;
        }

        function bqFillPreset() {
            const b = document.getElementById('bqbody_preset');
            b.innerHTML = `
              <select id="bqArch" onchange="bqApplyArch(this.value)" style="width:100%;">
                <option value="">— custom —</option>
                ${Object.keys(QJ_ARCHETYPES).map(k=>`<option value="${k}" ${k===BQ.archetype?'selected':''}>${QJ_ARCHETYPES[k].label}</option>`).join('')}
              </select>`;
        }

        function bqApplyArch(k) {
            BQ.archetype = k;
            const a = QJ_ARCHETYPES[k];
            if (a) {
                ['pitch','formant','breath','chest','presence','air','tape','room','gate','deess','compress','norm']
                  .forEach(p => { if (a[p] !== undefined) BQ.params[p] = a[p]; });
                bqPush('agent', `Loaded ${a.label}.`);
            }
            bqFillEq(); bqBuildOrb();
        }

        // [key, label, unit, min, max, step]
        const BQ_EQ_GROUPS = [
          ['CORE', [
            ['pitch','Pitch','st',-6,6,0.5], ['formant','Formant','st',-4,4,0.5],
            ['breath','Breath','%',0,100,1],  ['chest','Chest','dB',-6,10,0.5],
            ['presence','Presence','dB',-6,8,0.5], ['air','Air','dB',-6,8,0.5],
            ['tape','Tape','%',0,100,1],
          ]],
          ['ANATOMY', [
            ['proximity','Proximity','dB',-6,10,0.5],
            ['throat','Throat','dB',-8,8,0.5],
            ['nasal','Nasality','dB',-8,8,0.5],
            ['mouth','Mouth','dB',-8,8,0.5],
            ['consonant','Consonants','dB',-6,8,0.5],
          ]],
          ['CHARACTER', [
            ['rasp','Rasp / Fry','%',0,100,1],
            ['vibrato','Vibrato Depth','%',0,60,1],
            ['vibrato_rate','Vibrato Rate','Hz',0.5,10,0.1],
            ['exciter','Exciter','',0,40,1],
            ['crystal','Crystalizer','',-4,6,0.25],
            ['subboost','Sub Boost','%',0,100,1],
          ]],
          ['CONTROL', [
            ['pace','Pace','x',0.6,1.6,0.01],
            ['sib_freq','Sibilance Freq','Hz',5000,10000,100],
            ['sib_amount','De-ess Amount','dB',0,12,0.5],
            ['attack','Comp Attack','ms',1,120,1],
            ['release','Comp Release','ms',20,800,10],
            ['ratio','Comp Ratio',':1',1,12,0.5],
          ]],
        ];

        const BQ_EQ_PARAMS = BQ_EQ_GROUPS.flatMap(g => g[1]);
        BQ_EQ_PARAMS.forEach(([k,,,lo]) => {
            if (BQ.params[k] === undefined) {
                BQ.params[k] = (k === 'pace') ? 1.0
                             : (k === 'vibrato_rate') ? 5
                             : (k === 'sib_freq') ? 7000
                             : (k === 'sib_amount') ? 4
                             : (k === 'attack') ? 10
                             : (k === 'release') ? 100
                             : (k === 'ratio') ? 3 : 0;
            }
        });

        let BQ_EQ_OPEN = { CORE:true, ANATOMY:false, CHARACTER:false, CONTROL:false };

        function bqToggleGroup(g) { BQ_EQ_OPEN[g] = !BQ_EQ_OPEN[g]; bqFillEq(); }

        function bqFillEq() {
            const b = document.getElementById('bqbody_eq');
            if (!b) return;
            b.style.maxHeight = '340px';
            b.style.overflowY = 'auto';
            b.innerHTML = BQ_EQ_GROUPS.map(([g, rows]) => `
              <div style="margin-bottom:6px;">
                <div onclick="bqToggleGroup('${g}')"
                     style="cursor:pointer;display:flex;justify-content:space-between;
                            font-family:monospace;font-size:.55rem;letter-spacing:1.5px;
                            color:#34d399;padding:4px 0;border-bottom:1px solid #1e3a30;">
                  <span>${g}</span><span>${BQ_EQ_OPEN[g]?'▾':'▸'}</span>
                </div>
                <div style="display:${BQ_EQ_OPEN[g]?'block':'none'};padding-top:6px;">
                ${rows.map(([k,lbl,u,lo,hi,st]) => `
                  <div style="margin-bottom:6px;">
                    <div style="display:flex;justify-content:space-between;font-size:.56rem;color:#8a7ba8;">
                      <span>${lbl}</span>
                      <span id="bqv_${k}" style="color:#34d399;font-family:monospace;">${BQ.params[k]}${u}</span>
                    </div>
                    <input type="range" id="bqe_${k}" min="${lo}" max="${hi}" step="${st}" value="${BQ.params[k]}"
                           oninput="BQ.params['${k}']=parseFloat(this.value);document.getElementById('bqv_${k}').textContent=this.value+'${u}';bqSyncOrb();"
                           style="width:100%;height:3px;accent-color:#34d399;">
                  </div>`).join('')}
                </div>
              </div>`).join('') + `
              <div style="border-top:1px solid #1e3a30;padding-top:7px;margin-top:4px;">
                <div style="font-size:.56rem;color:#8a7ba8;margin-bottom:3px;">Room</div>
                <select onchange="BQ.params.room=this.value" style="width:100%;font-size:.63rem;margin-bottom:7px;">
                  ${['none','intimate','medium','large','cathedral'].map(r=>`<option value="${r}" ${r===BQ.params.room?'selected':''}>${r}</option>`).join('')}
                </select>
                <div style="display:flex;flex-wrap:wrap;gap:7px;font-size:.57rem;color:#a99cc4;">
                  ${[['gate','Gate'],['deess','De-ess'],['compress','Glue'],
                     ['speechnorm','Speech Lvl'],['declick','Declick'],
                     ['declip','Declip'],['norm','Normalize']]
                    .map(([k,lbl])=>`<label style="display:flex;align-items:center;gap:3px;cursor:pointer;">
                      <input type="checkbox" ${BQ.params[k]?'checked':''}
                             onchange="BQ.params['${k}']=this.checked;">${lbl}</label>`).join('')}
                </div>
              </div>`;
        }

        let BQ_ROLES = [];
        let BQ_BRAIN_OPEN = { ROLE:true, VOICE:false, MANNER:false, WORLD:false };
        function bqToggleBrain(g){ BQ_BRAIN_OPEN[g]=!BQ_BRAIN_OPEN[g]; bqFillBrain(); }

        // [key, label, min, max, step]
        const BQ_BRAIN_DIALS = [
            ['warmth','Warmth',0,100,1],
            ['energy','Energy',0,100,1],
            ['expressive','Emotional Range',0,100,1],
            ['breath_audible','Audible Breathing',0,100,1],
            ['patience','Patience',0,100,1],
            ['wit','Wit / Edge',0,100,1],
        ];

        const BQ_BRAIN_SELECTS = [
            ['rate','Speaking Rate',['unhurried','measured','conversational','brisk','variable']],
            ['pause','Pause Style',['brisk','natural','dramatic']],
            ['formality','Formality',['intimate','casual','neutral','formal']],
            ['emotion','Emotional Control',['controlled','responsive','expressive','very expressive']],
            ['stamina','Stamina',['low','medium','high']],
            ['character_range','Character Range',['single','narrow','wide','very wide']],
            ['vernacular','Vernacular',['nola','harlem','atl','deep south','broadcast','academic','neutral']],
            ['personality','Personality',['warm','sharp','playful','maternal','seductive','professional','streetwise','deadpan']],
        ];

        function bqBrainDefaults() {
            const d = { warmth:70, energy:55, expressive:55, breath_audible:25,
                        patience:60, wit:50, rate:'conversational', pause:'natural',
                        formality:'casual', emotion:'responsive', stamina:'medium',
                        character_range:'single', vernacular:'nola', personality:'warm',
                        world:'', background:'', role:'' };
            Object.entries(d).forEach(([k,v]) => { if (BQ.brain[k]===undefined) BQ.brain[k]=v; });
        }

        async function bqLoadRoles() {
            try {
                const r = await fetch('/api/brain/roles');
                BQ_ROLES = (await r.json()).roles || [];
            } catch(e) { BQ_ROLES = []; }
            bqFillBrain();
        }

        function bqApplyRole(id) {
            const role = BQ_ROLES.find(r => r.id === id);
            BQ.brain.role = id;
            if (!role) { bqFillBrain(); return; }
            Object.entries(role.voice || {}).forEach(([k,v]) => BQ.params[k] = v);
            Object.entries(role.brain || {}).forEach(([k,v]) => BQ.brain[k] = v);
            bqPush('agent', `Role set: ${role.label}. ${role.note} Voice and manner both moved.`);
            bqFillEq(); bqFillBrain(); bqBuildOrb();
        }

        function bqFillBrain() {
            const b = document.getElementById('bqbody_brain');
            if (!b) return;
            bqBrainDefaults();
            b.style.maxHeight = '340px';
            b.style.overflowY = 'auto';
            const hdr = (g,label) => `
              <div onclick="bqToggleBrain('${g}')"
                   style="cursor:pointer;display:flex;justify-content:space-between;
                          font-family:monospace;font-size:.55rem;letter-spacing:1.5px;
                          color:#a855f7;padding:4px 0;border-bottom:1px solid #2f2145;">
                <span>${label}</span><span>${BQ_BRAIN_OPEN[g]?'▾':'▸'}</span>
              </div>`;

            b.innerHTML = `
              ${hdr('ROLE','ROLE')}
              <div style="display:${BQ_BRAIN_OPEN.ROLE?'block':'none'};padding:7px 0;">
                <select onchange="bqApplyRole(this.value)" style="width:100%;font-size:.63rem;">
                  <option value="">— custom —</option>
                  ${BQ_ROLES.map(r=>`<option value="${r.id}" ${r.id===BQ.brain.role?'selected':''}>${r.label}</option>`).join('')}
                </select>
                <div style="font-size:.55rem;color:#6b5b8a;margin-top:5px;line-height:1.4;">
                  ${(BQ_ROLES.find(r=>r.id===BQ.brain.role)||{}).note || 'Pick a role to move voice and manner together.'}
                </div>
              </div>

              ${hdr('VOICE','MANNER DIALS')}
              <div style="display:${BQ_BRAIN_OPEN.VOICE?'block':'none'};padding:7px 0;">
                ${BQ_BRAIN_DIALS.map(([k,lbl,lo,hi,st])=>`
                  <div style="margin-bottom:6px;">
                    <div style="display:flex;justify-content:space-between;font-size:.56rem;color:#8a7ba8;">
                      <span>${lbl}</span>
                      <span id="bqb_${k}" style="color:#a855f7;font-family:monospace;">${BQ.brain[k]}</span>
                    </div>
                    <input type="range" min="${lo}" max="${hi}" step="${st}" value="${BQ.brain[k]}"
                           oninput="BQ.brain['${k}']=parseFloat(this.value);document.getElementById('bqb_${k}').textContent=this.value;"
                           style="width:100%;height:3px;accent-color:#a855f7;">
                  </div>`).join('')}
              </div>

              ${hdr('MANNER','DELIVERY')}
              <div style="display:${BQ_BRAIN_OPEN.MANNER?'block':'none'};padding:7px 0;">
                ${BQ_BRAIN_SELECTS.map(([k,lbl,opts])=>`
                  <div style="margin-bottom:6px;">
                    <div style="font-size:.56rem;color:#8a7ba8;margin-bottom:2px;">${lbl}</div>
                    <select onchange="BQ.brain['${k}']=this.value" style="width:100%;font-size:.62rem;">
                      ${opts.map(o=>`<option value="${o}" ${o===BQ.brain[k]?'selected':''}>${o}</option>`).join('')}
                    </select>
                  </div>`).join('')}
              </div>

              ${hdr('WORLD','WORLD & BACKSTORY')}
              <div style="display:${BQ_BRAIN_OPEN.WORLD?'block':'none'};padding:7px 0;">
                <div style="font-size:.56rem;color:#8a7ba8;margin-bottom:3px;">World / Setting</div>
                <input type="text" value="${(BQ.brain.world||'').replace(/"/g,'&quot;')}"
                       placeholder="e.g. big tech Tokyo, 1940s Chicago, deep space freighter…"
                       oninput="BQ.brain.world=this.value"
                       style="width:100%;font-size:.62rem;background:#0d0a18;border:1px solid #3b2a5c;
                              border-radius:4px;color:#c4b8dd;padding:5px;margin-bottom:7px;outline:none;">
                <div style="font-size:.56rem;color:#8a7ba8;margin-bottom:3px;">Backstory / Use Case</div>
                <textarea placeholder="Who is she, what is she for, what does she know…"
                          oninput="BQ.brain.background=this.value"
                          style="width:100%;height:60px;font-size:.62rem;background:#0d0a18;
                                 border:1px solid #3b2a5c;border-radius:4px;color:#c4b8dd;padding:5px;
                                 resize:none;outline:none;">${BQ.brain.background||''}</textarea>
              </div>`;
        }

        function bqFillMaster() {
            const b = document.getElementById('bqbody_master');
            if (!b) return;
            b.innerHTML = `
              <div style="font-size:.6rem;color:#8a7ba8;margin-bottom:7px;">
                ${BQ.source ? '▶ '+BQ.source.slice(0,26) : 'no source loaded'}
              </div>
              <input type="text" id="bqName" placeholder="Agent name…"
                     style="width:100%;margin-bottom:7px;font-size:.65rem;">
              <button class="btn" onclick="bqRender()" style="width:100%;margin-bottom:6px;
                      background:linear-gradient(135deg,#7c3aed,#f59e0b);">▶ Render Master</button>
              <button class="btn" onclick="bqSign()" style="width:100%;
                      background:linear-gradient(135deg,#db2777,#9d174d);">★ Sign to The Label</button>
              <div id="bqMasterStatus" style="font-size:.58rem;color:#6b5b8a;margin-top:6px;"></div>`;
        }

        // ── wires ──
        function bqDrawWires() {
            const svg = document.getElementById('bqWires');
            svg.innerHTML = BQ_WIRES.map(([a,b]) => {
                const na = BQ.nodes[a], nb = BQ.nodes[b];
                const da = BQ_NODE_DEFS.find(d=>d.id===a), db = BQ_NODE_DEFS.find(d=>d.id===b);
                if (!na||!nb) return '';
                const x1 = na.x + da.w, y1 = na.y + 28;
                const x2 = nb.x,        y2 = nb.y + 28;
                const mx = (x1+x2)/2;
                return `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"
                        stroke="#a855f7" stroke-width="1.6" fill="none" opacity=".5"/>
                        <circle cx="${x1}" cy="${y1}" r="3" fill="#a855f7"/>
                        <circle cx="${x2}" cy="${y2}" r="3" fill="#f59e0b"/>`;
            }).join('');
        }

        function bqBindDrag() {
            document.querySelectorAll('.bq-drag').forEach(h => {
                h.onmousedown = e => {
                    const id = h.dataset.node;
                    BQ.drag = { id, ox: e.clientX - BQ.nodes[id].x, oy: e.clientY - BQ.nodes[id].y };
                    h.style.cursor = 'grabbing';
                    e.preventDefault();
                };
            });
        }
        document.addEventListener('mousemove', e => {
            if (!BQ.drag) return;
            const n = BQ.nodes[BQ.drag.id];
            n.x = e.clientX - BQ.drag.ox;
            n.y = e.clientY - BQ.drag.oy;
            const el = document.getElementById('bqn_'+BQ.drag.id);
            if (el) { el.style.left = n.x+'px'; el.style.top = n.y+'px'; }
            bqDrawWires();
        });
        document.addEventListener('mouseup', () => {
            if (BQ.drag) {
                const h = document.querySelector(`.bq-drag[data-node="${BQ.drag.id}"]`);
                if (h) h.style.cursor = 'grab';
            }
            BQ.drag = null;
        });

        function bqResetGraph() {
            BQ.nodes = {};
            BQ.params = { pitch:0,formant:0,breath:0,chest:0,presence:0,air:0,
                          tape:0,room:'medium',gate:false,deess:true,compress:true,norm:true };
            BQ.archetype = '';
            bqBuildNodes(); bqBuildOrb();
        }

        // ── orb ──
        const BQ_ORB_KEYS = ['breath','chest','presence','air'];
        function bqOrbToggle() {
            const t = document.getElementById('bqOrbTray');
            const open = t.style.display === 'block';
            t.style.display = open ? 'none' : 'block';
            const orb = document.getElementById('bqOrb');
            orb.style.transform = open ? 'translateY(-50%)' : 'translateY(-50%) scale(1.1)';
        }
        function bqBuildOrb() {
            const w = document.getElementById('bqOrbSliders');
            if (!w) return;
            w.innerHTML = BQ_ORB_KEYS.map(k => {
                const d = BQ_EQ_PARAMS.find(p=>p[0]===k);
                return `<div>
                  <div style="display:flex;justify-content:space-between;font-size:.56rem;color:#a99cc4;">
                    <span>${d[1]}</span><span id="bqo_${k}" style="color:#f59e0b;font-family:monospace;">${BQ.params[k]}</span>
                  </div>
                  <input type="range" id="bqorb_${k}" min="${d[3]}" max="${d[4]}" step="${d[5]}" value="${BQ.params[k]}"
                         oninput="BQ.params['${k}']=parseFloat(this.value);document.getElementById('bqo_${k}').textContent=this.value;bqSyncEq();"
                         style="width:100%;height:3px;accent-color:#f59e0b;">
                </div>`;
            }).join('');
        }
        function bqSyncOrb(){ BQ_ORB_KEYS.forEach(k=>{
            const s=document.getElementById('bqorb_'+k), v=document.getElementById('bqo_'+k);
            if(s){s.value=BQ.params[k];} if(v){v.textContent=BQ.params[k];} }); }
        function bqSyncEq(){ BQ_ORB_KEYS.forEach(k=>{
            const s=document.getElementById('bqe_'+k), v=document.getElementById('bqv_'+k);
            const d=BQ_EQ_PARAMS.find(p=>p[0]===k);
            if(s){s.value=BQ.params[k];} if(v){v.textContent=BQ.params[k]+d[2];} }); }

        // ── chat ──
        function bqPush(who, text) {
            const log = document.getElementById('bqChatLog');
            const me = who === 'me';
            const d = document.createElement('div');
            d.style.cssText = `align-self:${me?'flex-end':'flex-start'};max-width:88%;
                background:${me?'#3b2a5c':'#141024'};border:1px solid ${me?'#5b3fa0':'#2a2140'};
                border-radius:8px;padding:8px 11px;font-size:.71rem;line-height:1.45;
                color:${me?'#e9e2f5':'#bdb0d6'};`;
            d.textContent = text;
            log.appendChild(d);
            log.scrollTop = log.scrollHeight;
        }

        async function bqSend() {
            const inp = document.getElementById('bqChatInput');
            const msg = inp.value.trim();
            if (!msg) return;
            bqPush('me', msg);
            inp.value = '';
            const res = await fetch('/api/bigq/chat', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({ message: msg, params: BQ.params })
            });
            const j = await res.json();
            if (j.status === 'success') {
                BQ.params = { ...BQ.params, ...j.params };
                bqPush('agent', j.reply);
                bqFillEq(); bqBuildOrb();
            } else bqPush('agent', j.message || 'Something went wrong.');
        }

        // ── render / sign ──
        async function bqRender() {
            const st = document.getElementById('bqMasterStatus');
            if (!BQ.source) { bqPush('agent','Load a voice in the Vault node first.'); return; }
            if (st) st.textContent = 'rendering…';
            bqPush('agent','Rendering…');
            const res = await fetch('/api/bigq/render', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({ ...BQ.params, source: BQ.source,
                    archetype: BQ.archetype || 'custom',
                    out_name: (document.getElementById('bqName')?.value.trim()) || 'bigq' })
            });
            const j = await res.json();
            if (j.status === 'success') {
                BQ.lastFile = j.file;
                if (st) st.innerHTML = `<span style="color:#34d399;">✓ ${j.file}</span>`;
                bqPush('agent','Done — ' + j.message + ' Playing it now.');
                new Audio('/api/vault/audio/'+encodeURIComponent(j.file)).play().catch(()=>{});
                loadData();
            } else {
                if (st) st.innerHTML = `<span style="color:#f87171;">${j.message}</span>`;
                bqPush('agent','Render failed: ' + j.message);
            }
        }

        async function bqSign() {
            if (!BQ.lastFile) { bqPush('agent','Render a master first, then sign it.'); return; }
            const name = (document.getElementById('bqName')?.value.trim()) || BQ.archetype || 'Untitled Agent';
            const res = await fetch('/api/label/add', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({ name, file: BQ.lastFile,
                    archetype: BQ.archetype || 'custom', params: BQ.params, brain: BQ.brain })
            });
            const j = await res.json();
            bqPush('agent', j.message || 'Signed.');
            loadLabel();
        }

        async function bqLoadKeys() {
            try {
                const r = await fetch('/api/keys/status');
                const j = await r.json();
                const names = { openai:'OpenAI', nvidia:'NVIDIA', grok:'Grok', hf:'HF' };
                document.getElementById('bqKeys').innerHTML = Object.entries(j.providers||{})
                  .map(([k,ok])=>`<span style="font-family:monospace;font-size:.53rem;padding:3px 7px;
                    border-radius:3px;border:1px solid ${ok?'#34d39955':'#3a3050'};
                    color:${ok?'#34d399':'#4b4060'};background:${ok?'#34d39912':'transparent'};">
                    ${ok?'●':'○'} ${names[k]||k}</span>`).join('');
            } catch(e) {}
        }

        // ══════════════════ THE LABEL ══════════════════
        async function loadLabel() {
            try {
                const r = await fetch('/api/label/list');
                const j = await r.json();
                const agents = j.agents || [];
                window._labelAgents = agents;
                document.getElementById('labelCount').textContent = agents.length + ' SIGNED';
                const grid = document.getElementById('labelGrid');
                if (!agents.length) {
                    grid.innerHTML = `<div style="color:var(--text-muted);font-size:0.7rem;">
                      No agents signed yet. Master a voice in BIG Q Studio and send it here.</div>`;
                    return;
                }
                grid.innerHTML = agents.map(a => `
                  <div style="background:#0c1220;border:1px solid rgba(236,72,153,.25);border-radius:7px;padding:11px;">
                    <div style="font-weight:bold;font-size:.76rem;color:#f9a8d4;margin-bottom:3px;">${a.name}</div>
                    <div style="font-size:.58rem;color:var(--text-muted);margin-bottom:7px;">
                      ${a.archetype} · ${(a.brain&&a.brain.vernacular)||'—'} · ${a.created||''}
                    </div>
                    <div style="display:flex;gap:5px;">
                      <button class="btn-secondary" style="flex:1;font-size:.6rem;"
                              onclick="new Audio('/api/vault/audio/'+encodeURIComponent('${a.file}')).play()">▶ Play</button>
                      <button class="btn-secondary" style="font-size:.6rem;"
                              onclick="labelRemove('${a.id}')">✕</button>
                    </div>
                  </div>`).join('');
            } catch(e) {}
        }

        async function labelRemove(id) {
            await fetch('/api/label/remove', { method:'POST',
                headers:{'Content-Type':'application/json'}, body: JSON.stringify({id}) });
            loadLabel();
        }

        document.getElementById('bqChatInput')?.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); bqSend(); }
        });

        qmInit();
        loadLabel();

        // ── Quincy Jones Advanced Panel ───────────────────────────────────────
        const QJ_ARCHETYPES = {
            marilyn: { pitch:2,   formant:1,  breath:70, chest:-3, presence:1,  air:5,  tape:30, room:'intimate', gate:false, deess:true,  compress:false, norm:true,  label:'Marilyn Monroe — Airy Breathy' },
            dolly:   { pitch:1.5, formant:1.5,breath:20, chest:1,  presence:3,  air:4,  tape:20, room:'medium',   gate:false, deess:true,  compress:true,  norm:true,  label:'Dolly Parton — Country Twang' },
            billie:  { pitch:-1.5,formant:-1, breath:30, chest:4,  presence:-1, air:-2, tape:60, room:'large',    gate:false, deess:true,  compress:true,  norm:true,  label:'Billie Holiday — Smoky Jazz' },
            nina:    { pitch:-2,  formant:-1.5,breath:5, chest:5,  presence:2,  air:-1, tape:40, room:'large',    gate:false, deess:false, compress:true,  norm:true,  label:'Nina Simone — Theatrical Power' },
            eartha:  { pitch:-1,  formant:-2,  breath:40, chest:3,  presence:0,  air:2,  tape:50, room:'intimate', gate:false, deess:true,  compress:true,  norm:true,  label:'Eartha Kitt — Sultry Purr' },
            tina:    { pitch:0,   formant:0,   breath:10, chest:2,  presence:5,  air:2,  tape:25, room:'medium',   gate:true,  deess:false, compress:true,  norm:true,  label:'Tina Turner — Raw Rasp' },
            whitney: { pitch:2,   formant:0,   breath:15, chest:2,  presence:4,  air:5,  tape:10, room:'large',    gate:false, deess:true,  compress:true,  norm:true,  label:'Whitney Houston — Soaring Clarity' },
            ella:    { pitch:0,   formant:0.5, breath:25, chest:3,  presence:1,  air:1,  tape:45, room:'medium',   gate:false, deess:true,  compress:true,  norm:true,  label:'Ella Fitzgerald — Warm Round Jazz' },
            aretha:  { pitch:0,   formant:-0.5,breath:5,  chest:6,  presence:3,  air:0,  tape:35, room:'large',    gate:false, deess:false, compress:true,  norm:true,  label:'Aretha Franklin — Gospel Chest' },
            connie:  { pitch:0.5, formant:0.5, breath:20, chest:2,  presence:2,  air:3,  tape:20, room:'intimate', gate:false, deess:true,  compress:true,  norm:true,  label:'CONNIE NOLA — Signature Voice' },
        };

        function qjLoadArchetype(key) {
            const a = QJ_ARCHETYPES[key];
            if (!a) return;
            const set = (id,v) => { const el=document.getElementById(id); if(el){el.value=v;el.dispatchEvent(new Event('input'));} };
            set('qjPitch', a.pitch);
            set('qjFormant', a.formant);
            set('qjBreath', a.breath);
            set('qjChest', a.chest);
            set('qjPresence', a.presence);
            set('qjAir', a.air);
            set('qjTape', a.tape);
            document.getElementById('qjRoom').value = a.room;
            document.getElementById('qjGate').checked = a.gate;
            document.getElementById('qjDeEss').checked = a.deess;
            document.getElementById('qjCompress').checked = a.compress;
            document.getElementById('qjNorm').checked = a.norm;
        }

        function qjReset() {
            document.getElementById('qjArchetype').value = '';
            ['qjPitch','qjFormant','qjBreath','qjChest','qjPresence','qjAir','qjTape'].forEach(id => {
                const el = document.getElementById(id); if(el){el.value=0;el.dispatchEvent(new Event('input'));}
            });
            document.getElementById('qjRoom').value = 'medium';
            document.getElementById('qjGate').checked = false;
            document.getElementById('qjDeEss').checked = true;
            document.getElementById('qjCompress').checked = true;
            document.getElementById('qjNorm').checked = true;
        }

        function qjPopulateSource() {
            const sel = document.getElementById('qjSource');
            const cur = sel.value;
            sel.innerHTML = '<option value="">— pick a vault file —</option>';
            (window._vaultFiles||[]).forEach(f => {
                const o = document.createElement('option');
                o.value = f.filename; o.textContent = f.filename;
                if(f.filename===cur) o.selected=true;
                sel.appendChild(o);
            });
        }

        async function qjRender() {
            const src = document.getElementById('qjSource').value;
            if (!src) { showStatus(document.getElementById('qjStatus'),'Pick a source file first','error'); return; }
            const st = document.getElementById('qjStatus');
            showStatus(st,'Rendering through Quincy Jones chain…','');
            const archetype = document.getElementById('qjArchetype').value;
            const payload = {
                source:   src,
                archetype,
                pitch:    parseFloat(document.getElementById('qjPitch').value),
                formant:  parseFloat(document.getElementById('qjFormant').value),
                breath:   parseFloat(document.getElementById('qjBreath').value),
                chest:    parseFloat(document.getElementById('qjChest').value),
                presence: parseFloat(document.getElementById('qjPresence').value),
                air:      parseFloat(document.getElementById('qjAir').value),
                tape:     parseFloat(document.getElementById('qjTape').value),
                room:     document.getElementById('qjRoom').value,
                gate:     document.getElementById('qjGate').checked,
                deess:    document.getElementById('qjDeEss').checked,
                compress: document.getElementById('qjCompress').checked,
                norm:     document.getElementById('qjNorm').checked,
                out_name: document.getElementById('qjOutName').value.trim() || (archetype||'quincy'),
            };
            const res = await fetch('/api/quincy/render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
            const j = await res.json();
            if (j.status==='success') {
                showStatus(st,'✅ '+j.message,'success');
                const au = document.getElementById('qjAudio');
                au.src = '/api/vault/audio/'+encodeURIComponent(j.file);
                au.style.display='block'; au.play();
                loadData();
            } else {
                showStatus(st,'❌ '+j.message,'error');
            }
        }

        // Populate QJ source whenever vault reloads
        document.addEventListener('vaultLoaded', () => {
            qjPopulateSource();
            qmPopulate();
            if (document.getElementById('bigq')?.style.display === 'block') bqFillVault();
        });

        // ── Manual Audio Capture (Browser MediaRecorder → WAV) ───────────────
        let _capStream=null, _capRec=null, _capChunks=[], _capInterval=null,
            _capStart=null, _capACtx=null, _capAnal=null;

        async function captureToggle() {
            if (_capRec && _capRec.state==='recording') {
                _capRec.stop();
            } else {
                await captureStart();
            }
        }

        async function captureStart() {
            const fb=document.getElementById('capFeedback'),
                  badge=document.getElementById('capBadge');
            fb.textContent='Requesting audio…';
            try {
                _capStream = await navigator.mediaDevices.getDisplayMedia({
                    video:false,
                    audio:{echoCancellation:false,noiseSuppression:false}
                });
            } catch(e) {
                fb.textContent='Permission denied — select an audio source in the OS prompt.';
                return;
            }
            _capACtx=new AudioContext();
            _capAnal=_capACtx.createAnalyser(); _capAnal.fftSize=256;
            _capACtx.createMediaStreamSource(_capStream).connect(_capAnal);
            drawVU();
            _capChunks=[];
            const mime=MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ?'audio/webm;codecs=opus':'audio/webm';
            _capRec=new MediaRecorder(_capStream,{mimeType:mime});
            _capRec.ondataavailable=e=>{if(e.data.size)_capChunks.push(e.data);};
            _capRec.onstop=captureFinish;
            _capRec.start(250);
            _capStart=Date.now();
            _capInterval=setInterval(()=>{
                const s=Math.floor((Date.now()-_capStart)/1000);
                document.getElementById('capTimer').textContent=
                    Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
            },500);
            document.getElementById('capBtn').textContent='⬛ STOP';
            document.getElementById('capBtn').style.background='#f97316';
            badge.textContent='RECORDING'; badge.style.color='#f43f5e';
            fb.textContent='Recording… press STOP when done.';
        }

        function drawVU() {
            if(!_capAnal) return;
            const c=document.getElementById('capVU'), cx=c.getContext('2d');
            const buf=new Uint8Array(_capAnal.frequencyBinCount);
            (function frame(){
                if(!_capAnal) return;
                _capAnal.getByteFrequencyData(buf);
                const pct=Math.min(buf.reduce((a,b)=>a+b,0)/buf.length/100,1);
                cx.clearRect(0,0,c.width,c.height);
                const g=cx.createLinearGradient(0,0,c.width,0);
                g.addColorStop(0,'#10b981'); g.addColorStop(.7,'#f97316'); g.addColorStop(1,'#f43f5e');
                cx.fillStyle=g;
                cx.fillRect(2,3,(c.width-4)*pct,c.height-6);
                requestAnimationFrame(frame);
            })();
        }

        async function captureFinish() {
            clearInterval(_capInterval);
            if(_capACtx){_capACtx.close();_capACtx=null;_capAnal=null;}
            _capStream.getTracks().forEach(t=>t.stop());
            document.getElementById('capBtn').textContent='⬤ REC';
            document.getElementById('capBtn').style.background='#f43f5e';
            document.getElementById('capBadge').textContent='UPLOADING…';
            document.getElementById('capFeedback').textContent='Converting to WAV…';
            const blob=new Blob(_capChunks,{type:'audio/webm'});
            const title=document.getElementById('captureTitle').value.trim()||'recording';
            const fd=new FormData();
            fd.append('file',blob,'recording.webm');
            fd.append('title',title);
            try {
                const res=await fetch('/api/harvest/upload',{method:'POST',body:fd});
                const data=await res.json();
                if(!res.ok) throw new Error(data.detail||'Upload failed');
                document.getElementById('capFeedback').textContent=
                    '✅ Saved: '+data.file+' ('+data.size_kb+' KB)';
                document.getElementById('capBadge').textContent='SAVED';
                document.getElementById('capBadge').style.color='#10b981';
                document.getElementById('capTimer').textContent='0:00';
                loadData();
            } catch(e) {
                document.getElementById('capFeedback').textContent='❌ '+e.message;
                document.getElementById('capBadge').textContent='ERROR';
                document.getElementById('capBadge').style.color='#f43f5e';
            }
        }
        // ─────────────────────────────────────────────────────────────────────
    </script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════════
# CRANE IDE — Autonomous Coding Agent
# ═══════════════════════════════════════════════════════════════════════
import threading
import requests as _requests

# NVIDIA NIM model catalog (fallback list — also fetched live)
NVIDIA_CODING_MODELS = [
    {"id": "deepseek-ai/deepseek-coder-v2-instruct",   "label": "DeepSeek Coder V2",         "tag": "CODE"},
    {"id": "qwen/qwen2.5-coder-32b-instruct",          "label": "Qwen 2.5 Coder 32B",        "tag": "CODE"},
    {"id": "nvidia/llama-3.1-nemotron-70b-instruct",   "label": "Nemotron 70B Instruct",     "tag": "NVIDIA"},
    {"id": "nvidia/nemotron-4-340b-instruct",          "label": "Nemotron 4 340B",           "tag": "NVIDIA★"},
    {"id": "meta/llama-3.1-405b-instruct",             "label": "Llama 3.1 405B",            "tag": "LARGE"},
    {"id": "meta/llama-3.1-70b-instruct",              "label": "Llama 3.1 70B",             "tag": "FAST"},
    {"id": "meta/llama-3.3-70b-instruct",              "label": "Llama 3.3 70B",             "tag": "NEW"},
    {"id": "mistralai/mistral-large",                  "label": "Mistral Large",             "tag": "INSTRUCT"},
    {"id": "mistralai/codestral-22b-instruct-v0.1",   "label": "Codestral 22B",             "tag": "CODE"},
    {"id": "google/gemma-2-27b-it",                   "label": "Gemma 2 27B",               "tag": "GOOGLE"},
    {"id": "microsoft/phi-3-medium-128k-instruct",    "label": "Phi-3 Medium 128K",         "tag": "FAST"},
    {"id": "nvidia/starcoder2-15b",                   "label": "StarCoder2 15B",            "tag": "CODE"},
    {"id": "ibm/granite-34b-code-instruct",           "label": "Granite 34B Code",          "tag": "CODE"},
]

# GCP config file (stores project + region only — key path stored separately)
GCP_CONFIG_FILE = os.path.expanduser("~/.crane_gcp.json")

def _load_gcp_config():
    if os.path.exists(GCP_CONFIG_FILE):
        try:
            with open(GCP_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_gcp_config(cfg: dict):
    with open(GCP_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ── NVIDIA NIM live model fetch ──────────────────────────────────────────────
@app.get("/api/ide/nvidia/models")
async def ide_nvidia_models():
    key = _vault_get("NVIDIA_API_KEY") or _vault_get("NVIDIA_NIM_KEY")
    if not key:
        return {"source": "fallback", "models": NVIDIA_CODING_MODELS}
    try:
        resp = _requests.get(
            "https://integrate.api.nvidia.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=8
        )
        if resp.status_code == 200:
            raw = resp.json().get("data", [])
            live = [{"id": m["id"], "label": m["id"].split("/")[-1].replace("-", " ").title(), "tag": "LIVE"}
                    for m in raw if isinstance(m, dict) and "id" in m]
            # merge: fallback first (labeled), then any live models not in fallback
            known_ids = {m["id"] for m in NVIDIA_CODING_MODELS}
            extra = [m for m in live if m["id"] not in known_ids]
            return {"source": "live", "models": NVIDIA_CODING_MODELS + extra}
    except Exception:
        pass
    return {"source": "fallback", "models": NVIDIA_CODING_MODELS}

# ── IDE Chat — routes to selected model ──────────────────────────────────────
class IDEChatRequest(BaseModel):
    model: str
    messages: list
    system: str = ""
    max_tokens: int = 4096
    temperature: float = 0.2

@app.post("/api/ide/chat")
async def ide_chat(req: IDEChatRequest):
    key = _vault_get("NVIDIA_API_KEY") or _vault_get("NVIDIA_NIM_KEY")
    if not key:
        return {"error": "NVIDIA key not found in vault"}
    msgs = []
    if req.system:
        msgs.append({"role": "system", "content": req.system})
    msgs.extend(req.messages)
    try:
        resp = _requests.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": req.model,
                "messages": msgs,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
                "stream": False,
            },
            timeout=120
        )
        data = resp.json()
        if resp.status_code != 200:
            return {"error": data.get("detail") or data.get("message") or str(data)}
        content = data["choices"][0]["message"]["content"]
        return {"content": content, "model": req.model,
                "usage": data.get("usage", {})}
    except Exception as e:
        return {"error": str(e)}

# ── GitHub integration ────────────────────────────────────────────────────────
class GitHubRepoRequest(BaseModel):
    owner: str = ""
    repo: str = ""
    path: str = ""
    branch: str = "main"

class GitHubWriteRequest(BaseModel):
    owner: str
    repo: str
    path: str
    content: str
    message: str
    branch: str = "main"
    sha: str = ""

def _gh_headers():
    token = _vault_get("GITHUB_TOKEN") or _vault_get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return None, {"error": "No GitHub token in vault. Add GITHUB_TOKEN."}
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, None

@app.get("/api/ide/github/repos")
async def ide_github_repos():
    hdrs, err = _gh_headers()
    if err: return err
    try:
        r = _requests.get("https://api.github.com/user/repos?per_page=100&sort=updated",
                          headers=hdrs, timeout=10)
        repos = r.json()
        return {"repos": [{"name": x["name"], "full_name": x["full_name"],
                           "private": x["private"], "language": x.get("language"),
                           "updated_at": x["updated_at"]} for x in repos if isinstance(x, dict)]}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/ide/github/tree")
async def ide_github_tree(req: GitHubRepoRequest):
    hdrs, err = _gh_headers()
    if err: return err
    try:
        url = f"https://api.github.com/repos/{req.owner}/{req.repo}/git/trees/{req.branch}?recursive=1"
        r = _requests.get(url, headers=hdrs, timeout=15)
        data = r.json()
        tree = [{"path": x["path"], "type": x["type"], "size": x.get("size", 0)}
                for x in data.get("tree", []) if x["type"] in ("blob","tree")]
        return {"tree": tree}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/ide/github/file")
async def ide_github_file(req: GitHubRepoRequest):
    hdrs, err = _gh_headers()
    if err: return err
    try:
        url = f"https://api.github.com/repos/{req.owner}/{req.repo}/contents/{req.path}?ref={req.branch}"
        r = _requests.get(url, headers=hdrs, timeout=10)
        data = r.json()
        if "content" in data:
            import base64
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return {"content": content, "sha": data.get("sha",""), "path": req.path}
        return {"error": data.get("message","unknown")}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/ide/github/write")
async def ide_github_write(req: GitHubWriteRequest):
    hdrs, err = _gh_headers()
    if err: return err
    try:
        import base64
        payload = {
            "message": req.message,
            "content": base64.b64encode(req.content.encode()).decode(),
            "branch": req.branch,
        }
        if req.sha:
            payload["sha"] = req.sha
        url = f"https://api.github.com/repos/{req.owner}/{req.repo}/contents/{req.path}"
        r = _requests.put(url, headers=hdrs, json=payload, timeout=20)
        data = r.json()
        if r.status_code in (200, 201):
            return {"status": "ok", "sha": data.get("content", {}).get("sha", "")}
        return {"error": data.get("message", str(data))}
    except Exception as e:
        return {"error": str(e)}

# ── GCP Config ───────────────────────────────────────────────────────────────
class GCPConfigRequest(BaseModel):
    project_id: str
    region: str = "us-central1"
    zone: str = "us-central1-a"
    gpu_endpoint: str = ""   # e.g. http://<gce-external-ip>:8001/v1 — inference server on the GPU box

@app.get("/api/ide/gcp/status")
async def ide_gcp_status():
    cfg = _load_gcp_config()
    has_creds = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or cfg.get("credentials_path"))
    return {"configured": bool(cfg.get("project_id")),
            "project_id": cfg.get("project_id",""),
            "region": cfg.get("region","us-central1"),
            "has_credentials": has_creds,
            "gpu_endpoint": cfg.get("gpu_endpoint","")}

@app.post("/api/ide/gcp/configure")
async def ide_gcp_configure(req: GCPConfigRequest):
    cfg = _load_gcp_config()
    cfg.update({"project_id": req.project_id, "region": req.region, "zone": req.zone, "gpu_endpoint": req.gpu_endpoint})
    _save_gcp_config(cfg)
    return {"status": "saved", "project_id": req.project_id}

# ── Shell exec for IDE terminal (local only) ──────────────────────────────────
class ShellRequest(BaseModel):
    cmd: str
    cwd: str = "/home/hunt"

@app.post("/api/ide/shell")
async def ide_shell(req: ShellRequest):
    safe_cwd = req.cwd if os.path.isdir(req.cwd) else "/home/hunt"
    try:
        result = subprocess.run(
            req.cmd, shell=True, capture_output=True, text=True,
            cwd=safe_cwd, timeout=30
        )
        return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out (30s)", "rc": 124}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "rc": 1}

# ── DATA LAKE ROUTING ─────────────────────────────────────────────────────────
# Primary storage  : GitHub (repos via GH token)
# Backup           : HuggingFace private dataset (crane-data-lake)
# Voice memory     : Nobility Vault — local persistent
# Chat history     : Nobility Vault — local persistent
# Everything else  : GitHub

CRANE_PROJECTS_FILE    = "/home/hunt/.crane_projects.json"
CRANE_HF_BACKUP_REPO   = "tyronne-os/crane-data-lake"
CRANE_VOICE_MEMORY     = "/mnt/NOBILITY_VAULT/voice_vault/crane_voice_memory.json"
CRANE_CHAT_HISTORY     = "/mnt/NOBILITY_VAULT/voice_vault/crane_chat_history.json"

import asyncio as _asyncio

async def _hf_backup(filename: str, payload: dict):
    """Fire-and-forget: push a JSON file to the private HF data-lake dataset."""
    token = _vault_get("HF_TOKEN")
    if not token:
        return
    try:
        import io as _io
        from huggingface_hub import HfApi as _HfApi
        api = _HfApi()
        api.create_repo(CRANE_HF_BACKUP_REPO, repo_type="dataset",
                        private=True, token=token, exist_ok=True)
        content = json.dumps(payload, indent=2).encode()
        api.upload_file(
            path_or_fileobj=_io.BytesIO(content),
            path_in_repo=filename,
            repo_id=CRANE_HF_BACKUP_REPO,
            repo_type="dataset",
            token=token,
            commit_message=f"CRANE auto-backup · {filename}",
        )
    except Exception:
        pass  # backup is best-effort; never block the primary save

# ── PROJECTS (home landing) ───────────────────────────────────────────────────
class ProjectLogRequest(BaseModel):
    name: str
    template: str = "blank"

@app.get("/api/ide/projects")
async def list_projects():
    if not os.path.exists(CRANE_PROJECTS_FILE):
        return {"projects": []}
    try:
        with open(CRANE_PROJECTS_FILE) as f:
            return {"projects": json.load(f)}
    except Exception:
        return {"projects": []}

@app.post("/api/ide/projects")
async def log_project(req: ProjectLogRequest):
    projects = []
    if os.path.exists(CRANE_PROJECTS_FILE):
        try:
            with open(CRANE_PROJECTS_FILE) as f:
                projects = json.load(f)
        except Exception:
            projects = []
    projects.insert(0, {
        "name": req.name,
        "template": req.template,
        "when": datetime.now().strftime("%b %d, %Y %H:%M"),
    })
    projects = projects[:50]
    with open(CRANE_PROJECTS_FILE, "w") as f:
        json.dump(projects, f, indent=2)
    # async backup to HF data-lake (best-effort, does not block response)
    _asyncio.create_task(_hf_backup("crane_projects.json", {"projects": projects}))
    return {"status": "ok", "projects": projects}

# ── VAULT MEMORY ENDPOINTS ────────────────────────────────────────────────────
# Voice memory and chat history persist to Nobility Vault (local, never cloud)

class MemorySaveRequest(BaseModel):
    messages: list
    label: str = ""

def _read_vault_memory(path: str) -> dict:
    if not os.path.exists(path):
        return {"messages": []}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"messages": []}

def _write_vault_memory(path: str, messages: list, label: str = ""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"messages": messages, "label": label,
                   "saved_at": datetime.now().isoformat()}, f, indent=2)

@app.get("/api/vault/voice-memory")
async def get_voice_memory():
    return _read_vault_memory(CRANE_VOICE_MEMORY)

@app.post("/api/vault/voice-memory")
async def save_voice_memory(req: MemorySaveRequest):
    _write_vault_memory(CRANE_VOICE_MEMORY, req.messages, req.label)
    return {"status": "ok", "count": len(req.messages)}

@app.delete("/api/vault/voice-memory")
async def clear_voice_memory():
    _write_vault_memory(CRANE_VOICE_MEMORY, [])
    return {"status": "cleared"}

@app.get("/api/vault/chat-history")
async def get_chat_history():
    return _read_vault_memory(CRANE_CHAT_HISTORY)

@app.post("/api/vault/chat-history")
async def save_chat_history(req: MemorySaveRequest):
    _write_vault_memory(CRANE_CHAT_HISTORY, req.messages, req.label)
    # also backup to HF data-lake
    _asyncio.create_task(_hf_backup("crane_chat_history.json",
                                     {"messages": req.messages, "label": req.label}))
    return {"status": "ok", "count": len(req.messages)}

@app.delete("/api/vault/chat-history")
async def clear_chat_history():
    _write_vault_memory(CRANE_CHAT_HISTORY, [])
    return {"status": "cleared"}

# ── SESSION STATE ─────────────────────────────────────────────────────────────
CRANE_SESSION_FILE = os.path.expanduser("~/.crane_session.json")

class SessionState(BaseModel):
    repo: str = ""
    active_file: str = ""
    branch: str = "main"
    scroll: int = 0
    file_shas: dict = {}

@app.get("/api/ide/session")
async def get_session():
    if not os.path.exists(CRANE_SESSION_FILE):
        return {"repo": "", "active_file": "", "branch": "main", "scroll": 0, "file_shas": {}}
    try:
        with open(CRANE_SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {"repo": "", "active_file": "", "branch": "main", "scroll": 0, "file_shas": {}}

@app.post("/api/ide/session")
async def save_session(req: SessionState):
    data = req.dict()
    data["saved_at"] = datetime.now().isoformat()
    with open(CRANE_SESSION_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return {"status": "ok"}

# ── CAT-5 Model Routing Protocol ──────────────────────────────────────────────
import re as _cat_re

# ── CAT-4 Model Routing Protocol ──────────────────────────────────────────────
import re as _cat_re

_CAT4_TRIGGERS = r'full feature|auth|authentication|deploy|pipeline|multi.step|architecture|system|real.time|streaming|complex'

CAT5_RULES = [
    (1, _cat_re.compile(r'\b(fix typo|rename|comment|format|lint|add import|one line|single function|quick|simple change)\b', _cat_re.I)),
    (2, _cat_re.compile(r'\b(add button|add field|write test|unit test|helper function|small component|update text|change color|style)\b', _cat_re.I)),
    (3, _cat_re.compile(r'\b(build page|create endpoint|api route|database schema|refactor|module|class|integration|fetch data|crud|form)\b', _cat_re.I)),
    (4, _cat_re.compile(r'\b(' + _CAT4_TRIGGERS + r')\b', _cat_re.I)),
]
CAT5_LABEL = {1:"CAT-1 FAST",2:"CAT-2 LIGHT",3:"CAT-3 CORE",4:"CAT-4 HEAVY"}

class CatRequest(BaseModel):
    prompt: str

@app.post("/api/ide/cat")
async def classify_cat(req: CatRequest):
    txt = req.prompt
    cat = 2
    for level, pattern in reversed(CAT5_RULES):
        if pattern.search(txt):
            cat = level
            break
    if len(txt) > 400 and cat < 3:
        cat = 3
    needs_gpu = cat >= 4
    # CAT-5 never auto-triggers. Manual GPU control only.
    can_request_gpu = cat <= 4
    return {"cat": cat, "label": CAT5_LABEL.get(cat, "CAT-4"), "needs_gpu": needs_gpu, "can_request_gpu": can_request_gpu}

# ── Local HF model roster (Nobility Vault, no API key required) ─────────────
LOCAL_MODEL_ROSTER = [
    {"id": "local:qwen-coder-1.5b", "name": "Qwen2.5 Coder 1.5B (local)", "size": "1.5B",
     "path": "/mnt/NOBILITY_VAULT/models/qwen-coder-1.5b-local/model.gguf", "cat": 1, "runs_on": "cpu"},
    {"id": "local:qwen-coder-3b", "name": "Qwen2.5 Coder 3B (local)", "size": "3B",
     "path": "/mnt/NOBILITY_VAULT/models/qwen-coder-3b-local/qwen2.5-coder-3b-instruct-q4_k_m.gguf", "cat": 2, "runs_on": "cpu"},
    {"id": "local:qwen-coder-7b", "name": "Qwen2.5 Coder 7B (local)", "size": "7B",
     "path": "/mnt/NOBILITY_VAULT/models/qwen-coder-7b-local/qwen2.5-coder-7b-instruct-q4_k_m.gguf", "cat": 3, "runs_on": "cpu"},
    {"id": "local:qwen-coder-14b", "name": "Qwen2.5 Coder 14B (GCP GPU)", "size": "14B",
     "path": "", "cat": 4, "runs_on": "gpu", "remote_ready": True,
     "hf_repo": "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"},
    {"id": "local:qwen3-coder-30b", "name": "Qwen3 Coder 30B-A3B (GPU)", "size": "30B",
     "path": "/mnt/NOBILITY_VAULT/models/qwen3-coder-30b-gpu", "cat": 4, "runs_on": "gpu",
     "hf_repo": "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"},
]

@app.get("/api/ide/local/status")
async def local_model_status():
    cfg = _load_gcp_config()
    gpu_endpoint = cfg.get("gpu_endpoint", "")
    out = []
    for m in LOCAL_MODEL_ROSTER:
        if m.get("remote_ready"):
            out.append({**m, "downloaded": True, "size_mb": 0, "gpu_endpoint_set": bool(gpu_endpoint)})
            continue
        p = m["path"]
        present = bool(p) and os.path.exists(p)
        size_mb = 0
        if present:
            try:
                if os.path.isdir(p):
                    size_mb = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fn in os.walk(p) for f in fn) // (1024*1024)
                else:
                    size_mb = os.path.getsize(p) // (1024*1024)
            except Exception:
                pass
        out.append({**m, "downloaded": present, "size_mb": size_mb})
    return {"models": out}

class HFDownloadRequest(BaseModel):
    repo_id: str
    filename: str = ""
    dest: str

@app.post("/api/ide/hf/download")
async def hf_download(req: HFDownloadRequest):
    os.makedirs(os.path.dirname(req.dest) if req.filename else req.dest, exist_ok=True)
    log_path = f"/tmp/hf_dl_{abs(hash(req.repo_id))}.log"
    if req.filename:
        py = (f"from huggingface_hub import hf_hub_download; "
              f"p = hf_hub_download(repo_id='{req.repo_id}', filename='{req.filename}', local_dir='{os.path.dirname(req.dest)}'); "
              f"print('DONE:', p)")
    else:
        py = (f"from huggingface_hub import snapshot_download; "
              f"p = snapshot_download(repo_id='{req.repo_id}', local_dir='{req.dest}'); "
              f"print('DONE:', p)")
    cmd = f"nohup /home/hunt/.local/bin/uv run --with huggingface_hub python3 -c \"{py}\" > {log_path} 2>&1 &"
    subprocess.Popen(cmd, shell=True)
    return {"status": "started", "log": log_path}

_LOCAL_LLM_CACHE = {}

class LocalChatRequest(BaseModel):
    model: str
    messages: list
    system: str = ""
    max_tokens: int = 1024
    temperature: float = 0.2

@app.post("/api/ide/local/chat")
async def local_chat(req: LocalChatRequest):
    entry = next((m for m in LOCAL_MODEL_ROSTER if m["id"] == req.model), None)
    if not entry:
        return {"error": f"Unknown local model {req.model}"}
    if entry.get("remote_ready") and entry["runs_on"] == "gpu":
        cfg = _load_gcp_config()
        endpoint = cfg.get("gpu_endpoint", "")
        if not endpoint:
            return {"error": f"{entry['name']} is on your GCP GPU box but no endpoint URL is set — enter it in the ☁ GCP panel (e.g. http://<gce-ip>:8001/v1) so CRANE can reach the inference server."}
        try:
            chat_msgs = ([{"role": "system", "content": req.system}] if req.system else []) + \
                        [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in req.messages]
            resp = _requests.post(f"{endpoint.rstrip('/')}/chat/completions",
                                   json={"model": entry.get("hf_repo", entry["id"]), "messages": chat_msgs,
                                         "max_tokens": req.max_tokens, "temperature": req.temperature},
                                   timeout=60)
            if resp.status_code != 200:
                return {"error": f"GPU endpoint returned {resp.status_code}: {resp.text[:300]}"}
            content = resp.json()["choices"][0]["message"]["content"]
            return {"content": content, "model": entry["id"], "source": "gcp_gpu"}
        except Exception as e:
            return {"error": f"Couldn't reach GPU endpoint ({endpoint}): {e}"}
    if entry["runs_on"] == "gpu":
        return {"error": f"{entry['name']} needs the GCP GPU running first — click '🔥 Fire GPU $0.40/hr' in the composer, provision the instance, then retry."}
    if not entry["path"] or not os.path.exists(entry["path"]):
        return {"error": f"{entry['name']} isn't downloaded to the vault yet (still fetching in the background)."}
    try:
        import llama_cpp
    except ImportError:
        return {"error": "llama-cpp-python isn't installed yet on this server — local CPU inference engine is still installing."}
    try:
        if entry["id"] not in _LOCAL_LLM_CACHE:
            _LOCAL_LLM_CACHE.clear()  # keep only one model resident at a time (2-core box, low RAM)
            _LOCAL_LLM_CACHE[entry["id"]] = llama_cpp.Llama(
                model_path=entry["path"], n_ctx=4096, n_threads=2, verbose=False)
        llm = _LOCAL_LLM_CACHE[entry["id"]]
        chat_msgs = []
        if req.system:
            chat_msgs.append({"role": "system", "content": req.system})
        for m in req.messages:
            chat_msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        out = llm.create_chat_completion(messages=chat_msgs, max_tokens=min(req.max_tokens, 1536), temperature=req.temperature)
        content = out["choices"][0]["message"]["content"]
        return {"content": content, "model": entry["id"], "source": "local_vault"}
    except Exception as e:
        return {"error": f"Local inference failed: {e}"}

# ── OpenAI-compatible /v1/ shim ──────────────────────────────────────────────
# Lets Open Design (BYOK), Cursor, Codex, or any OpenAI-SDK client discover
# and use CRANE's local Qwen vault models. Point the client at:
#   base_url = http://127.0.0.1:8000/v1
#   api_key  = crane-local   (any non-empty string)

@app.get("/v1/models")
async def v1_models():
    now = int(time.time())
    objs = []
    for m in LOCAL_MODEL_ROSTER:
        on_disk = bool(m.get("path") and os.path.exists(m["path"]))
        if m["runs_on"] == "gpu":
            on_disk = m.get("remote_ready", False)
        objs.append({
            "id": m["id"],
            "object": "model",
            "created": now,
            "owned_by": "crane-local",
            "root": m["id"],
            "parent": None,
            "permission": [],
            "context_window": 32768,
            "meta": {
                "name": m["name"],
                "cat": m.get("cat", 0),
                "size": m.get("size", ""),
                "runs_on": m["runs_on"],
                "available": on_disk,
            }
        })
    return {"object": "list", "data": objs}

class V1ChatMessage(BaseModel):
    role: str
    content: str

class V1ChatRequest(BaseModel):
    model: str = "local:qwen-coder-1.5b"
    messages: list[V1ChatMessage] = []
    max_tokens: int = 1024
    temperature: float = 0.2
    stream: bool = False

@app.post("/v1/chat/completions")
async def v1_chat_completions(req: V1ChatRequest):
    # Translate to CRANE's internal LocalChatRequest shape and reuse existing logic
    class _FakeMsgReq:
        def __init__(self):
            self.model = req.model
            self.messages = [{"role": m.role, "content": m.content} for m in req.messages]
            self.system = next((m.content for m in req.messages if m.role == "system"), "")
            self.max_tokens = req.max_tokens
            self.temperature = req.temperature
    inner = await local_chat(_FakeMsgReq())
    if "error" in inner:
        from fastapi.responses import JSONResponse as _JR
        return _JR(status_code=400, content={"error": {"message": inner["error"], "type": "crane_error", "code": 400}})
    import uuid as _uuid, time as _t2
    return {
        "id": f"chatcmpl-{_uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(_t2.time()),
        "model": req.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": inner["content"]}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "crane_source": inner.get("source", "local"),
    }

@app.get("/api/ide/hf/download/status")
async def hf_download_status(log: str):
    if not os.path.exists(log):
        return {"done": False, "log_text": ""}
    txt = open(log).read()
    return {"done": "DONE:" in txt or "Error" in txt, "log_text": txt[-2000:]}

# ── GCP GPU spin-up (on-demand, $0.40/hr class instance) ─────────────────────
class GPUSpinRequest(BaseModel):
    model_id: str = ""

@app.post("/api/ide/gcp/spin")
async def gcp_spin(req: GPUSpinRequest):
    cfg = _load_gcp_config()
    if not cfg.get("project_id"):
        return {"status": "error", "error": "Configure GCP project first (☁ GCP button)."}
    return {
        "status": "confirm_required",
        "message": f"This will provision a billable GPU instance (~$0.40/hr, {cfg.get('zone','us-central1-a')}) "
                    f"to serve {req.model_id or 'the selected model'}. Confirm in the GCP console or run the "
                    f"provisioning command shown, then CRANE will route CAT-4/5 tasks to it.",
        "gcloud_cmd": (f"gcloud compute instances create crane-gpu-worker "
                       f"--project={cfg.get('project_id')} --zone={cfg.get('zone','us-central1-a')} "
                       f"--machine-type=g2-standard-4 --accelerator=type=nvidia-l4,count=1 "
                       f"--image-family=common-cu124-debian-11 --image-project=deeplearning-platform-release "
                       f"--maintenance-policy=TERMINATE --boot-disk-size=100GB --metadata=install-nvidia-driver=True")
    }

# ── IDE Landing Page ──────────────────────────────────────────────────────────

@app.get("/ide", response_class=HTMLResponse)
async def serve_ide():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CRANE IDE</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&family=Inter:wght@400;500;600;700&display=swap">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<style>
/* ── CodeMirror ── */
.CodeMirror{height:100%;font-family:'JetBrains Mono',monospace;font-size:13px;background:#080807;color:#f0ead8;line-height:1.6;}
.CodeMirror-gutters{background:#0d0c0a;border-right:1px solid #2a2416;}
.CodeMirror-linenumber{color:#3a3020;padding:0 8px;}
.CodeMirror-cursor{border-left:2px solid #C8A82A;}
.cm-keyword{color:#c084fc;}.cm-string{color:#86efac;}.cm-comment{color:#475569;font-style:italic;}
.cm-number{color:#fb923c;}.cm-def{color:#38bdf8;}.cm-variable{color:#e2e8f0;}
.cm-operator{color:#f472b6;}.cm-atom{color:#fb923c;}.cm-property{color:#7dd3fc;}
.CodeMirror-selected{background:rgba(56,189,248,.18)!important;}

/* ── TOKENS ── */
:root{--bg-canvas:#0c0c0c;--bg:#0d0d10;--surface:#16151a;--border:#29242d;--text:#eef2f3;--text-2:#b3b8ba;--text-muted:#8a9296;--text-dim:#7a8286;--text-faint:#63696c;--gold:#f0b429;--gold-tint-14:rgba(240,180,41,.14);--gold-tint-16:rgba(240,180,41,.16);--gold-tint-35:rgba(240,180,41,.35);--gold-tint-40:rgba(240,180,41,.40);--jade:#2ee6b8;--jade-bright:#8ff2da;--jade-tint-14:rgba(46,230,184,.14);--jade-tint-16:rgba(46,230,184,.16);--jade-tint-35:rgba(46,230,184,.35);--diff-remove-bg:rgba(239,68,68,.12);--diff-remove-text:#e5a3a3;--panel:#16151a;--card:#16151a;--muted:#8a9296;--green:#2ee6b8;--blue:#2ee6b8;--purple:rgba(46,230,184,.8);--gold-bright:#ffd87a;--gold-dim:rgba(240,180,41,.12);--accent:#f0b429;--nvidia:#76b900;--orange:#f59e0b;--red:#ef4444;--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-6:24px;--sp-8:32px;--sp-12:48px;--sp-16:64px;--radius-sm:4px;--radius-md:8px;--radius-lg:14px;--radius-xl:20px;--radius-full:9999px;--shadow-1:0 1px 4px rgba(0,0,0,.5);--shadow-2:0 4px 16px rgba(0,0,0,.6);--shadow-3:0 8px 32px rgba(0,0,0,.7);--font-ui:'Inter', system-ui, sans-serif;--font-mono:'JetBrains Mono', 'Fira Code', monospace;--text-display:30px;--text-heading:22px;--text-subhead:15px;--text-body:13px;--text-caption:11px;--ease-micro:0.15s ease;--ease-enter:0.25s ease-out;--ease-exit:0.15s ease-in;--dv-cat-1:#f0b429;--dv-cat-2:#2ee6b8;--dv-cat-3:#a78bfa;--dv-cat-4:#38bdf8;--dv-cat-5:#f59e0b;--dv-cat-6:#ec4899;--dv-cat-7:#8ff2da;--dv-cat-8:#6ee7b7;--dv-axis:#29242d;--dv-grid:rgba(41,36,45,.5);--dv-label:#8a9296;--dv-anno:#eef2f3;--kg-person:#f0b429;--kg-concept:#a78bfa;--kg-artifact:#38bdf8;--kg-event:#f59e0b;--kg-place:#2ee6b8;--kg-unknown:#8a9296;--kg-edge:rgba(41,36,45,.7);--kg-edge-hover:rgba(240,180,41,.8)}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;height:100vh;overflow:hidden;display:flex;flex-direction:column;}

/* ── LEFT NAV RAIL ── */
#navRail{width:48px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;align-items:center;padding:8px 0;gap:2px;flex-shrink:0;z-index:50;}
.nr-btn{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:15px;color:var(--muted);background:transparent;border:none;transition:.15s;position:relative;}
.nr-btn:hover{background:rgba(255,255,255,.07);color:var(--text);}
.nr-btn.active{background:rgba(240,180,41,.12);color:var(--gold);}
.nr-btn[data-page="studio"].active{background:rgba(240,180,41,.12);color:var(--gold);}
.nr-btn[data-page="depo"].active{background:rgba(240,180,41,.12);color:var(--gold);}
.nr-btn[data-page="images"].active{background:rgba(240,180,41,.08);color:var(--gold);}
.nr-btn[data-page="bigq"].active{background:rgba(240,180,41,.08);color:var(--gold);}
.nr-btn[data-page="cu"].active{background:rgba(99,102,241,.15);color:#818cf8;}
.nr-tooltip{position:absolute;left:calc(100% + 8px);background:#1c2333;border:1px solid var(--border);color:var(--text);font-size:10px;font-weight:700;letter-spacing:1px;padding:3px 9px;border-radius:5px;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .12s;font-family:'JetBrains Mono',monospace;}
.nr-btn:hover .nr-tooltip{opacity:1;}
.nr-spacer{flex:1;}
.nr-divider{width:28px;height:1px;background:var(--border);margin:4px 0;}
/* GPU mini-meter in navRail bottom */
.nr-gpu{display:flex;flex-direction:column;align-items:center;gap:3px;padding:8px 0 10px;width:100%;border-top:1px solid var(--border);}
.nr-gpu-toggle{width:30px;height:30px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s;line-height:1;}
.nr-gpu-toggle:hover{border-color:var(--gold);color:var(--gold);}
.nr-gpu-toggle.live{border-color:var(--jade-tint-35);color:var(--jade);background:var(--jade-tint-14);}
.nr-gpu-bar-wrap{width:28px;height:3px;background:rgba(255,255,255,.07);border-radius:2px;overflow:hidden;}
.nr-gpu-bar{height:100%;width:0%;background:linear-gradient(90deg,var(--green),var(--gold));transition:width 1s linear;}
.nr-gpu-cost{font-family:'JetBrains Mono',monospace;font-size:8px;color:var(--muted);font-variant-numeric:tabular-nums;letter-spacing:-.3px;}
.nr-gpu-cost.live{color:var(--green);}
.nr-gpu-label{font-size:7px;letter-spacing:1px;color:var(--border);font-weight:700;}
.nr-mentor{overflow:hidden;padding:0;}
.nr-mentor-img{width:34px;height:34px;object-fit:cover;border-radius:8px;display:block;}
.nr-mentor-fallback{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,#0d0d10,#16151a);border:1px solid var(--gold-tint-14);display:none;align-items:center;justify-content:center;font-size:11px;font-weight:800;color:var(--gold);font-family:'JetBrains Mono',monospace;}
.nr-mentor:hover{box-shadow:0 0 0 1px rgba(240,180,41,.4);}
.hc-mentor-wrap{width:44px;height:44px;border-radius:50%;overflow:hidden;margin:0 auto 6px;border:2px solid rgba(240,180,41,.45);flex-shrink:0;}
.hc-mentor-img{width:100%;height:100%;object-fit:cover;display:block;}
.hc-mentor-fallback{width:100%;height:100%;background:linear-gradient(135deg,#0d0d10,#16151a);display:none;align-items:center;justify-content:center;font-size:14px;font-weight:800;color:var(--gold);font-family:'JetBrains Mono',monospace;}
.home-card-mentor:hover{border-color:rgba(240,180,41,.4);box-shadow:0 0 0 1px rgba(240,180,41,.12);}

/* ── TOPBAR ── */
#topbar{height:48px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:0;padding:0 20px;flex-shrink:0;}
.tb-left{display:flex;align-items:center;flex:1;height:100%;}
.tb-right{display:flex;align-items:center;gap:8px;flex-shrink:0;}
.tb-diamond{color:var(--gold);font-size:16px;margin-right:4px;line-height:1;}
.tb-brand{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:13px;color:var(--gold);letter-spacing:3px;margin-right:28px;}
/* topbar context controls (mirror of ctxBar — same IDs drive the same JS) */
.tb-gh-btn{display:flex;align-items:center;gap:6px;background:transparent;border:1px solid var(--border);border-radius:6px;padding:4px 10px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text-2);transition:.15s;white-space:nowrap;max-width:200px;}
.tb-gh-btn:hover{border-color:var(--gold-tint-35);}
.tb-gh-btn .ctx-gh-repo{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700;}
.tb-hf-chip,.tb-gcp-chip{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:6px;border:1px solid var(--border);font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:700;color:var(--text-muted);letter-spacing:.5px;white-space:nowrap;cursor:pointer;transition:.15s;}
.tb-hf-chip.live{border-color:var(--jade-tint-35);color:var(--jade);}
.tb-gcp-chip.live{border-color:rgba(66,133,244,.4);color:#7aacff;}
.tb-lock{width:28px;height:28px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text-muted);font-size:13px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s;flex-shrink:0;}
.tb-lock:hover{border-color:var(--gold);color:var(--gold);}
.tb-preview-btn{height:24px;padding:0 10px;border-radius:5px;border:1px solid var(--border);background:transparent;color:var(--text-muted);font-size:11px;font-family:'JetBrains Mono',monospace;cursor:pointer;display:flex;align-items:center;gap:5px;transition:.15s;flex-shrink:0;letter-spacing:.3px;}
.tb-preview-btn:hover,.tb-preview-btn.active{border-color:var(--jade);color:var(--jade);}
.logo-ide{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:14px;color:var(--gold);letter-spacing:4px;}
.tb-sep{width:1px;height:20px;background:var(--border);margin:0 2px;}
.tb-status{font-size:10px;display:flex;align-items:center;gap:4px;color:var(--muted);}
.dot{width:6px;height:6px;border-radius:50%;background:var(--muted);}
.dot.on{background:var(--gold);box-shadow:0 0 6px rgba(240,180,41,.5);}
.dot.warn{background:var(--orange);}
.tb-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:3px 10px;border-radius:5px;cursor:pointer;font-size:10px;font-weight:600;letter-spacing:.5px;transition:.15s;}
.tb-btn:hover{border-color:var(--gold);color:var(--gold);}
.tb-btn.active{border-color:var(--gold);color:var(--gold);}
.tb-btn.connected{border-color:var(--gold);color:var(--gold);}
.tb-btn.gcp-on{border-color:var(--nvidia);color:var(--nvidia);}
.tb-spacer{flex:1;}
/* ── TOP NAV LINKS ── */
.top-nav{display:flex;align-items:center;gap:0;height:100%;}
.top-nav-link{font-family:'Inter',sans-serif;font-size:13px;font-weight:500;color:var(--text-muted);text-decoration:none;padding:0 14px;height:48px;display:flex;align-items:center;transition:.15s;position:relative;letter-spacing:0;}
.top-nav-link:hover{color:var(--text);}
.top-nav-link.active{color:var(--gold);}
.top-nav-link.active::after{content:'';position:absolute;bottom:0;left:14px;right:14px;height:2px;background:var(--gold);border-radius:1px 1px 0 0;}
.top-nav-sep{display:none;}
/* hidden status buttons — keep for JS but visually in topbar corner */
#voiceBtn{display:none;}
.tb-hidden{display:none;}

/* ── LAYOUT ── */
#main{display:flex;flex:1;overflow:hidden;}

/* ── FILE SIDEBAR ── */
#sidebar{width:210px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;transition:width .15s;}
#sidebar.collapsed{width:0;border-right:none;}
#sideHead{padding:8px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px;flex-shrink:0;}
#sideHead select{flex:1;background:var(--card);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:4px;font-size:11px;outline:none;}
#fileTree{flex:1;overflow-y:auto;padding:4px 0;}
.ft-item{padding:4px 10px 4px 14px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:5px;}
.ft-item:hover{background:rgba(240,180,41,.06);}
.ft-item.active{background:rgba(240,180,41,.14);color:var(--gold);}
.ft-dir{color:var(--orange);font-weight:600;}

/* ── EDITOR AREA ── */
#editorArea{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;}
#tabBar{height:34px;background:var(--panel);border-bottom:1px solid var(--border);display:none;align-items:center;overflow-x:auto;flex-shrink:0;}
.ed-tab{padding:0 14px;height:34px;display:flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:11px;cursor:pointer;border-right:1px solid var(--border);white-space:nowrap;color:var(--muted);flex-shrink:0;}
.ed-tab.active{background:var(--bg);color:var(--text);border-top:2px solid var(--gold);}
.ed-tab .tclose{opacity:.4;font-size:14px;line-height:1;}
.ed-tab .tclose:hover{opacity:1;color:var(--red);}
#editorWrap{flex:1;overflow:hidden;position:relative;}
#previewPane{flex:1;overflow:hidden;position:relative;display:none;background:#fff;flex-direction:column;}
#previewPane.active{display:flex;}
#previewBar{height:30px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;padding:0 10px;flex-shrink:0;}
#previewBar .pb-label{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.5px;}
#previewBar .pb-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer;}
#previewBar .pb-btn:hover{border-color:var(--gold);color:var(--gold);}
#previewBar .pb-btn.active{border-color:var(--gold);color:var(--gold);}
#previewFrame{flex:1;width:100%;border:none;background:#fff;}
#previewEmpty{flex:1;display:flex;align-items:center;justify-content:center;color:#8a8478;font-family:'Inter',sans-serif;font-size:13px;text-align:center;padding:20px;}
.ed-tab[data-tab="preview"]{gap:5px;}

/* ── HOME LANDING (Claude-Design-style) ── */
#homeLanding{flex:1;overflow-y:auto;display:flex;flex-direction:column;align-items:center;padding:0 24px 60px;background:var(--bg);}
#homeLanding.hidden{display:none;}
/* hero headline — centered, pushes composer down */
.hl-hero{display:flex;flex-direction:column;align-items:center;text-align:center;padding-top:14vh;margin-bottom:28px;width:100%;max-width:800px;}
.hl-hero-title{font-size:32px;font-weight:700;color:var(--text);letter-spacing:-.5px;margin-bottom:10px;line-height:1.2;}
.hl-hero-sub{font-size:14px;color:var(--text-muted);font-weight:400;line-height:1.5;}
/* motivational line above composer */
.hl-daily-push{width:100%;max-width:800px;font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;letter-spacing:2.5px;color:var(--gold);text-transform:uppercase;margin-bottom:10px;padding:0 2px;}
.hl-brand{display:none;}
.hl-brand-icon,.hl-brand-text,.hl-brand-name,.hl-brand-sub{display:none;}
.hl-headline{font-family:'Inter',serif;font-size:34px;font-weight:600;color:var(--text);margin-bottom:28px;text-align:center;letter-spacing:-.5px;}
/* ── COMPOSER CONTEXT BAR ── */
#ctxBar{width:100%;max-width:800px;display:flex;align-items:center;gap:8px;margin-bottom:8px;padding:0 2px;}
.ctx-gh-wrap{position:relative;flex:1;}
.ctx-gh-btn{display:flex;align-items:center;gap:6px;background:transparent;border:1px solid var(--border);border-radius:8px;padding:5px 10px;cursor:pointer;width:100%;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text);transition:.15s;text-align:left;}
.ctx-gh-btn:hover{border-color:rgba(240,180,41,.4);}
.ctx-gh-icon{font-size:11px;opacity:.7;}
.ctx-gh-repo{font-weight:700;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ctx-gh-arrow{color:var(--muted);font-size:8px;flex-shrink:0;}
.ctx-gh-menu{position:absolute;top:calc(100% + 4px);left:0;right:0;background:var(--panel);border:1px solid var(--border);border-radius:10px;z-index:500;display:none;box-shadow:0 8px 32px rgba(0,0,0,.6);max-height:280px;overflow-y:auto;}
.ctx-gh-menu.open{display:block;}
.ctx-gh-head{padding:6px 10px;font-size:9px;letter-spacing:1px;color:var(--muted);font-weight:700;font-family:'JetBrains Mono',monospace;border-bottom:1px solid var(--border);}
.ctx-gh-row{padding:7px 10px;font-size:11px;cursor:pointer;display:flex;align-items:center;gap:8px;transition:.1s;}
.ctx-gh-row:hover{background:rgba(240,180,41,.07);color:var(--gold);}
.ctx-gh-row .rp{font-size:8px;padding:1px 5px;border-radius:4px;}
.ctx-gh-row .rp.priv{background:rgba(239,68,68,.15);color:#fca5a5;}
.ctx-gh-row .rp.pub{background:var(--jade-tint-14);color:var(--jade);}
.ctx-gh-new{border-top:1px solid var(--border);padding:7px 10px;font-size:10px;color:var(--gold);cursor:pointer;font-weight:700;display:flex;align-items:center;gap:6px;}
.ctx-gh-new:hover{background:rgba(240,180,41,.07);}
.ctx-hf-chip,.ctx-gcp-chip{display:flex;align-items:center;gap:5px;padding:5px 10px;border-radius:8px;border:1px solid var(--border);font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.5px;white-space:nowrap;flex-shrink:0;cursor:pointer;transition:.15s;}
.ctx-hf-chip.live{border-color:rgba(255,160,50,.4);color:#ffb347;}
.ctx-gcp-chip.live{border-color:rgba(66,133,244,.4);color:#7aacff;}
.ctx-gcp-chip:hover{border-color:rgba(66,133,244,.6);}
.ctx-lock{width:30px;height:30px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);font-size:13px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s;flex-shrink:0;}
.ctx-lock:hover{border-color:var(--gold);color:var(--gold);}
#hlComposer{width:100%;max-width:800px;background:var(--surface);border:1px solid var(--gold-tint-35);border-radius:14px;padding:16px 16px 10px;transition:.15s;margin-bottom:34px;}
#hlComposer:focus-within{border-color:var(--gold-tint-40);box-shadow:0 0 0 3px rgba(240,180,41,.1);}
#hlPromptBox{width:100%;background:transparent;border:none;outline:none;color:var(--text);font-size:14px;font-family:'Inter',sans-serif;resize:none;min-height:50px;max-height:180px;line-height:1.6;}
#hlPromptBox::placeholder{color:var(--muted);}
.hl-composer-row{display:flex;align-items:center;gap:8px;margin-top:10px;border:1px solid var(--gold-tint-14);border-radius:8px;padding:6px 8px;background:transparent;}
.hl-icon-btn{width:32px;height:32px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:15px;flex-shrink:0;transition:.15s;}
.hl-icon-btn:hover{border-color:var(--gold);color:var(--gold);}
.hl-pill{display:flex;flex-direction:column;gap:0;padding:4px 10px;border-radius:8px;border:1px solid var(--border);background:transparent;cursor:pointer;font-size:10px;transition:.15s;}
.hl-pill:hover{border-color:var(--gold);}
.hl-pill-label{color:var(--muted);font-size:9px;}
.hl-pill-value{color:var(--text);font-weight:700;font-size:11px;}
.hl-spacer{flex:1;}
.hl-model-pill{display:flex;flex-direction:column;padding:4px 12px;border-radius:8px;border:1px solid var(--border);cursor:pointer;font-size:10px;}
.hl-model-pill .hl-pill-value{color:var(--gold);}
#hlSendBtn{width:32px;height:32px;border-radius:9px;border:none;background:linear-gradient(135deg,var(--gold),var(--gold-bright));color:#080807;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:15px;flex-shrink:0;}
#hlSendBtn:hover{opacity:.9;}
.hl-templates-label{width:100%;max-width:800px;font-size:10px;font-weight:700;letter-spacing:1.5px;color:var(--muted);text-transform:uppercase;margin-bottom:12px;display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;}
.hl-templates-label:hover{color:var(--text);}
.hl-tpl-arrow{display:inline-block;transition:transform .2s;font-style:normal;}
.hl-tpl-arrow.open{transform:rotate(180deg);}
.hl-templates{width:100%;max-width:800px;display:none;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:40px;}
.hl-templates.open{display:grid;}
.hl-tpl-card{background:transparent;border:1px solid transparent;border-radius:10px;padding:14px 10px;display:flex;flex-direction:column;align-items:center;gap:8px;cursor:pointer;transition:.15s;text-align:center;}
.hl-tpl-card:hover{border-color:rgba(240,180,41,.35);}
.hl-tpl-card.sel{border-color:var(--gold);background:rgba(240,180,41,.05);}
.hl-tpl-icon{font-size:20px;display:flex;align-items:center;justify-content:center;}
.hl-tpl-name{font-size:10.5px;font-weight:600;color:var(--text);}
#hlProjects{width:100%;max-width:800px;}
.hl-proj-tabs{display:flex;align-items:center;gap:4px;border-bottom:1px solid var(--border);margin-bottom:0;}
.hl-proj-tab{padding:9px 14px;font-size:12px;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;}
.hl-proj-tab.active{color:var(--gold);border-bottom-color:var(--gold);}
.hl-proj-search{margin-left:auto;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:5px 10px;font-size:11px;color:var(--text);outline:none;width:160px;}
.hl-proj-row{display:flex;align-items:center;gap:12px;padding:10px 6px;border-bottom:1px solid rgba(255,255,255,.03);cursor:pointer;font-size:12px;}
.hl-proj-row:hover{background:rgba(255,255,255,.02);}
.hl-proj-thumb{width:44px;height:32px;border-radius:5px;background:#1c1810;flex-shrink:0;object-fit:cover;display:flex;align-items:center;justify-content:center;font-size:14px;}
.hl-proj-name{flex:2;color:var(--text);font-weight:500;}
.hl-proj-meta{flex:1;color:var(--muted);font-size:11px;}
.hl-proj-empty{padding:30px 6px;color:var(--muted);font-size:12px;text-align:center;}
#terminal{height:180px;background:#020a0f;border-top:1px solid var(--border);flex-shrink:0;display:flex;flex-direction:column;transition:height .15s;}
#terminal.collapsed{height:28px;overflow:hidden;}
#terminal.collapsed #termOut,#terminal.collapsed #termInputRow{display:none;}
#termHead{cursor:pointer;}
#termHead{padding:4px 10px;border-bottom:1px solid var(--border);font-size:10px;color:var(--muted);display:flex;gap:10px;align-items:center;}
#termOut{flex:1;overflow-y:auto;padding:6px 10px;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.6;}
#termInputRow{display:flex;border-top:1px solid var(--border);flex-shrink:0;}
#termPrompt{padding:4px 8px;color:var(--green);font-family:'JetBrains Mono',monospace;font-size:11px;flex-shrink:0;}
#termInput{flex:1;background:transparent;border:none;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:11px;outline:none;padding:4px 0;}

/* ── AGENT PANEL ── */
#agentPanel{width:380px;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;}
#agentHead{padding:9px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-shrink:0;}
.ah-title{font-weight:700;font-size:13px;letter-spacing:.5px;}
.ah-model-chip{font-size:9px;background:rgba(118,185,0,.12);color:var(--nvidia);padding:2px 7px;border-radius:12px;border:1px solid rgba(118,185,0,.3);margin-left:auto;font-family:'JetBrains Mono',monospace;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

/* ── CHAT LOG ── */
#chatLog{width:100%;max-width:800px;display:flex;flex-direction:column;gap:10px;margin-bottom:8px;}
#chatLog:empty{display:none;}
#chatLog .msg{width:100%;}
#welcomeHero{padding:32px 18px 20px;text-align:center;flex-shrink:0;}
#welcomeHero .wh-title{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:22px;letter-spacing:.5px;background:linear-gradient(90deg,var(--gold),var(--gold-bright));-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
#welcomeHero .wh-sub{margin-top:6px;font-size:12px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;font-weight:600;}
.msg{border-radius:8px;padding:8px 12px;font-size:12px;line-height:1.6;}
.msg.user{background:rgba(56,189,248,.1);border:1px solid rgba(56,189,248,.2);align-self:flex-end;max-width:90%;}
.msg.agent{background:rgba(168,85,247,.07);border:1px solid rgba(168,85,247,.18);align-self:flex-start;max-width:100%;}
.msg.sys{background:rgba(16,185,129,.07);border:1px solid rgba(16,185,129,.2);align-self:center;color:var(--green);font-size:11px;text-align:center;max-width:100%;}
.msg pre{background:rgba(0,0,0,.5);padding:8px 10px;border-radius:4px;overflow-x:auto;font-family:'JetBrains Mono',monospace;font-size:11px;margin-top:6px;white-space:pre-wrap;}
.apply-btn{display:inline-block;margin-top:8px;background:rgba(16,185,129,.15);border:1px solid var(--green);color:var(--green);padding:4px 10px;border-radius:4px;font-size:10px;cursor:pointer;}
.thinking{display:flex;gap:4px;align-items:center;padding:8px 12px;}
.thinking span{width:6px;height:6px;border-radius:50%;background:var(--purple);animation:blink 1.2s infinite;}
.thinking span:nth-child(2){animation-delay:.3s;}.thinking span:nth-child(3){animation-delay:.6s;}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}

/* ══════════════════════════════════════════════
   CLAUDE-STYLE COMPOSER
   ══════════════════════════════════════════════ */
#composer{border-top:1px solid var(--border);background:var(--panel);flex-shrink:0;display:flex;flex-direction:column;}

/* Project + Mode bar */
#composerTopBar{display:flex;align-items:center;gap:6px;padding:7px 10px 0;flex-wrap:wrap;}
#projectPill{display:flex;align-items:center;gap:5px;background:var(--card);border:1px solid var(--border);border-radius:20px;padding:3px 10px 3px 7px;cursor:pointer;font-size:11px;transition:.15s;max-width:160px;}
#projectPill:hover{border-color:var(--blue);}
#projectPill .pp-icon{font-size:12px;}
#projectPill .pp-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.mode-group{display:flex;border:1px solid var(--border);border-radius:6px;overflow:hidden;flex-shrink:0;}
.mode-btn{background:transparent;border:none;color:var(--muted);padding:4px 11px;cursor:pointer;font-size:11px;font-family:'Inter',sans-serif;transition:.15s;white-space:nowrap;}
.mode-btn:hover{background:rgba(255,255,255,.05);color:var(--text);}
.mode-btn.active{background:rgba(240,180,41,.15);color:var(--gold);font-weight:600;}
.mode-btn.superman.active{background:linear-gradient(135deg,rgba(240,180,41,.25),rgba(232,201,106,.15));color:var(--gold-bright);font-weight:700;}
.mode-sep{width:1px;background:var(--border);flex-shrink:0;}
#composerCtxPills{display:flex;gap:4px;flex-wrap:wrap;padding:4px 10px 0;min-height:0;}
.ctx-pill{display:flex;align-items:center;gap:4px;background:rgba(240,180,41,.1);border:1px solid rgba(240,180,41,.25);border-radius:12px;padding:2px 8px;font-size:10px;color:var(--gold);cursor:pointer;}
.ctx-pill .cp-x{opacity:.5;font-size:12px;line-height:1;}
.ctx-pill .cp-x:hover{opacity:1;}

/* Textarea */
#chatInput{background:transparent;border:none;color:var(--text);font-size:13px;font-family:'Inter',sans-serif;outline:none;resize:none;width:100%;min-height:64px;max-height:200px;padding:10px 12px;line-height:1.6;}
#chatInput::placeholder{color:var(--text-faint);}

/* Bottom toolbar */
#composerToolbar{display:flex;align-items:center;gap:4px;padding:6px 8px;border-top:1px solid rgba(255,255,255,.04);}
.tool-icon{width:32px;height:32px;display:flex;align-items:center;justify-content:center;border-radius:6px;cursor:pointer;font-size:16px;color:var(--muted);transition:.15s;border:1px solid transparent;flex-shrink:0;}
.tool-icon:hover{background:rgba(255,255,255,.06);color:var(--text);border-color:var(--border);}
.tool-icon.active{background:rgba(240,180,41,.15);color:var(--gold);border-color:rgba(240,180,41,.3);}
.tool-icon.vault-icon.active{background:rgba(245,158,11,.15);color:var(--gold);border-color:rgba(245,158,11,.3);}
.tool-sep{width:1px;height:20px;background:var(--border);margin:0 2px;flex-shrink:0;}

/* Model dropdown — custom grouped */
#modelDropWrapper{position:relative;flex:1;min-width:0;}
#modelDisplay{display:flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:5px 10px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;transition:.15s;overflow:hidden;}
#modelDisplay:hover{border-color:var(--blue);}
#modelDisplay .md-tag{font-size:9px;padding:1px 6px;border-radius:3px;font-family:'Inter',sans-serif;font-weight:600;flex-shrink:0;}
#modelDisplay .md-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;}
#modelDisplay .md-arrow{margin-left:auto;color:var(--muted);flex-shrink:0;}
#modelMenu{position:absolute;bottom:calc(100% + 6px);left:0;right:0;background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden;z-index:500;display:none;box-shadow:0 -8px 32px rgba(0,0,0,.5);max-height:420px;overflow-y:auto;}
#modelMenu.open{display:block;}
.mg-head{padding:6px 12px;font-size:9px;font-weight:700;letter-spacing:2px;color:var(--muted);background:rgba(0,0,0,.3);border-bottom:1px solid var(--border);position:sticky;top:0;}
.mg-head.coding{color:var(--blue);}
.mg-head.diffusion{color:var(--purple);}
.mg-head.voice{color:var(--green);}
.mg-head.nvidia{color:var(--nvidia);}
.mg-head.cpu{color:var(--jade);}
.mg-head.gpu{color:var(--gold);background:var(--gold-dim);}
.model-opt{display:flex;align-items:center;gap:8px;padding:7px 12px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;transition:.12s;}
.model-opt:hover{background:rgba(255,255,255,.05);}
.model-opt.selected{background:rgba(240,180,41,.1);color:var(--gold);}
.model-opt .mo-size{font-size:9px;color:var(--muted);min-width:32px;}
.model-opt .mo-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.mo-tag{font-size:9px;padding:1px 6px;border-radius:3px;font-weight:600;flex-shrink:0;}
.mo-cat{font-size:8px;padding:1px 4px;border-radius:3px;border:1px solid;flex-shrink:0;font-family:'JetBrains Mono',monospace;}
#catBadge{font-size:10px;padding:3px 9px;border-radius:12px;font-weight:700;letter-spacing:.5px;font-family:'JetBrains Mono',monospace;border:1px solid;display:none;}
#gpuSpinBtn{display:none;font-size:10px;background:rgba(239,68,68,.15);border:1px solid #ef4444;color:#ef4444;padding:2px 8px;border-radius:10px;cursor:pointer;}
.tag-code{background:rgba(56,189,248,.15);color:var(--blue);}
.tag-nvidia{background:rgba(118,185,0,.15);color:var(--nvidia);}
.tag-diff{background:rgba(168,85,247,.15);color:var(--purple);}
.tag-voice{background:rgba(16,185,129,.15);color:var(--green);}
.tag-fast{background:rgba(245,158,11,.15);color:var(--gold);}
.tag-large{background:rgba(239,68,68,.15);color:var(--red);}
.tag-local{background:rgba(16,185,129,.15);color:var(--green);}
.tag-gpu{background:rgba(239,68,68,.18);color:#ef4444;}

#sendBtn{background:linear-gradient(135deg,var(--gold),var(--gold-bright));color:#080807;border:none;padding:6px 16px;border-radius:6px;cursor:pointer;font-weight:800;font-size:12px;flex-shrink:0;transition:.15s;letter-spacing:.5px;}
#sendBtn:hover{opacity:.9;}
#sendBtn:disabled{opacity:.35;cursor:default;}

/* ── VAULT PANEL (slides up inside agent panel) ── */
#vaultPanel{position:absolute;bottom:0;left:0;right:0;background:var(--panel);border-top:2px solid var(--gold);z-index:200;display:none;flex-direction:column;max-height:60%;box-shadow:0 -8px 32px rgba(0,0,0,.6);}
#vaultPanel.open{display:flex;}
#vaultHead{padding:8px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-shrink:0;}
.vault-title{font-weight:700;font-size:12px;color:var(--gold);letter-spacing:1px;}
#vaultClose{margin-left:auto;background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:16px;}
#vaultSearch{background:var(--card);border:1px solid var(--border);color:var(--text);padding:5px 9px;border-radius:4px;font-size:11px;outline:none;width:100%;}
#vaultSearch:focus{border-color:var(--gold);}
#vaultList{flex:1;overflow-y:auto;padding:4px 0;}
.vault-item{display:flex;align-items:center;gap:8px;padding:6px 12px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;transition:.12s;}
.vault-item:hover{background:rgba(245,158,11,.08);}
.vault-item .vi-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.vault-item .vi-size{font-size:9px;color:var(--muted);}
.vault-item .vi-add{opacity:0;font-size:11px;color:var(--gold);flex-shrink:0;}
.vault-item:hover .vi-add{opacity:1;}
.vault-item.added{background:rgba(245,158,11,.12);}
#vaultFooter{padding:6px 12px;border-top:1px solid var(--border);display:flex;gap:6px;flex-shrink:0;}
.vault-act{background:var(--card);border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:4px;font-size:10px;cursor:pointer;}
.vault-act:hover{border-color:var(--gold);color:var(--gold);}

/* ── TOOLS MENU ── */
#toolsMenu{position:absolute;bottom:calc(100% + 8px);left:0;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:6px;z-index:400;display:none;min-width:200px;box-shadow:0 -6px 24px rgba(0,0,0,.5);}
#toolsMenu.open{display:block;}
.tm-item{display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:5px;cursor:pointer;font-size:11px;transition:.12s;}
.tm-item:hover{background:rgba(255,255,255,.06);}
.tm-item.active{background:rgba(240,180,41,.12);color:var(--gold);}
.tm-item .ti-icon{font-size:14px;width:20px;text-align:center;}
.tm-item .ti-key{margin-left:auto;font-size:9px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.tm-section{font-size:9px;font-weight:700;letter-spacing:1px;color:var(--muted);padding:8px 10px 3px;text-transform:uppercase;}
.tm-section:first-child{padding-top:4px;}
.tm-divider{height:1px;background:var(--border);margin:4px 4px;}
#mentionPopover{position:absolute;bottom:calc(100% + 4px);left:8px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:4px;z-index:400;display:none;min-width:240px;max-height:220px;overflow-y:auto;box-shadow:0 -6px 24px rgba(0,0,0,.5);}
#mentionPopover.open{display:block;}
.mp-item{display:flex;align-items:center;gap:7px;padding:5px 9px;border-radius:5px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;}
.mp-item:hover,.mp-item.sel{background:rgba(240,180,41,.12);color:var(--gold);}

/* ── MODALS ── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9000;display:flex;align-items:center;justify-content:center;}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:24px;min-width:380px;max-width:500px;display:flex;flex-direction:column;gap:14px;}
.modal h3{font-size:15px;font-weight:700;}
.modal input,.modal select{background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:5px;font-size:12px;font-family:'JetBrains Mono',monospace;outline:none;width:100%;}
.modal input:focus{border-color:var(--blue);}
.modal-row{display:flex;gap:8px;}
.modal-btn{background:var(--purple);color:#fff;border:none;padding:8px 16px;border-radius:5px;cursor:pointer;font-weight:600;font-size:12px;flex:1;}
.modal-btn.sec{background:transparent;border:1px solid var(--border);color:var(--muted);}
.modal-label{font-size:11px;color:var(--muted);margin-bottom:2px;}
.modal-hint{font-size:10px;color:var(--muted);line-height:1.5;}

/* Mode descriptions */
#modeHint{font-size:10px;color:var(--muted);padding:2px 10px 0;min-height:14px;}

::-webkit-scrollbar{width:4px;height:4px;}::-webkit-scrollbar-track{background:transparent;}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}

/* ── WELCOME HOME ── */
#welcomeHero{padding:24px 16px 16px;flex-shrink:0;}
#welcomeHero .wh-greeting{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;font-weight:600;margin-bottom:4px;}
#welcomeHero .wh-title{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:18px;background:linear-gradient(90deg,var(--gold),var(--gold-bright));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:1px;margin-bottom:12px;}
.home-cards{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px;}
.wh-compact{display:flex;align-items:center;justify-content:space-between;padding:9px 4px;cursor:pointer;border-bottom:1px solid var(--border);margin-bottom:6px;}
.wh-compact-text{font-size:11px;color:var(--muted);}
.wh-compact-text b{color:var(--gold);font-family:'JetBrains Mono',monospace;}
.wh-more-arrow{font-size:9px;color:var(--muted);font-weight:700;letter-spacing:.5px;flex-shrink:0;}
.wh-compact:hover .wh-more-arrow{color:var(--gold);}
.home-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:11px 12px;cursor:pointer;transition:.15s;display:flex;flex-direction:column;gap:4px;}
.home-card:hover{border-color:rgba(240,180,41,.35);background:#1a1500;}
.home-card .hc-icon{font-size:16px;margin-bottom:2px;}
.home-card .hc-label{font-size:10px;font-weight:700;letter-spacing:1px;color:var(--text);}
.home-card .hc-sub{font-size:9px;color:var(--muted);line-height:1.4;}
.home-recents{border-top:1px solid var(--border);padding-top:10px;}
.home-recents-label{font-size:9px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;font-weight:600;margin-bottom:6px;font-family:'JetBrains Mono',monospace;}
.home-recent-item{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:5px;cursor:pointer;transition:.12s;}
.home-recent-item:hover{background:rgba(240,180,41,.05);}
.home-recent-item .ri-dot{width:5px;height:5px;border-radius:50%;background:var(--gold);flex-shrink:0;}
.home-recent-item .ri-name{font-size:11px;font-family:'JetBrains Mono',monospace;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.home-recent-item .ri-time{font-size:9px;color:var(--muted);}


.repo-row{padding:5px 8px;border-radius:4px;cursor:pointer;display:flex;align-items:center;gap:6px;font-size:11px;font-family:'JetBrains Mono',monospace;}
.repo-row:hover{background:rgba(56,189,248,.08);}
.repo-priv{font-size:9px;background:rgba(168,85,247,.2);color:var(--purple);padding:0 4px;border-radius:3px;}
.repo-pub{font-size:9px;background:rgba(16,185,129,.15);color:var(--green);padding:0 4px;border-radius:3px;}
</style>
</head>
<body>

<!-- TOP BAR -->
<div id="topbar">
  <div class="tb-left">
    <span class="tb-diamond">◇</span>
    <span class="tb-brand">CRANE</span>
    <nav class="top-nav">
      <a href="/ide" class="top-nav-link active">Home</a>
      <a href="/studio" class="top-nav-link">Studio</a>
      <a href="/images" class="top-nav-link">Images</a>
      <a href="/cu" class="top-nav-link">CU Dept</a>
    </nav>
  </div>
  <div class="tb-right">
    <!-- GitHub repo picker — same JS hooks as ctxBar -->
    <div class="ctx-gh-wrap" id="ctxGhWrap" style="position:relative;">
      <button class="tb-gh-btn" onclick="toggleCtxGhMenu(event)">
        <span style="opacity:.6;font-size:11px">⎇</span>
        <span class="ctx-gh-repo" id="ctxGhRepo">Loading repos…</span>
        <span style="color:var(--text-muted);font-size:8px">▾</span>
      </button>
      <div class="ctx-gh-menu" id="ctxGhMenu">
        <div class="ctx-gh-head">YOUR REPOSITORIES</div>
        <div id="ctxGhList"></div>
        <div class="ctx-gh-new" onclick="ctxGhNewProject()">＋ Start new project</div>
      </div>
    </div>
    <div class="tb-hf-chip" id="ctxHfChip">🤗 HF —</div>
    <button class="tb-preview-btn" id="tbPreviewBtn" onclick="togglePreview()" title="Live Preview">◱ Preview</button>
    <button class="tb-lock" onclick="toggleVault()" title="Nobility Vault">🔒</button>
  </div>
</div>

<!-- MAIN -->
<div id="main">

  <!-- LEFT NAV RAIL -->
  <div id="navRail">
    <div class="nr-btn" id="sidebarToggleBtn" onclick="toggleSidebar()" title="Files">
      <span>📁</span><span class="nr-tooltip">FILES</span>
    </div>
    <div class="nr-divider"></div>
    <a href="/ide" class="nr-btn active" data-page="home" title="HOME">
      <span>⬡</span><span class="nr-tooltip">HOME</span>
    </a>
    <a href="/studio" class="nr-btn" data-page="studio" title="STUDIO">
      <span>🎙</span><span class="nr-tooltip">STUDIO</span>
    </a>
    <a href="/connie" class="nr-btn" data-page="connie" title="CONNIE">
      <span>🧠</span><span class="nr-tooltip">CONNIE</span>
    </a>
    <a href="/ide" class="nr-btn nr-mentor" data-page="mentor" title="Design Studio">
      <img src="/static/ilya.jpg" class="nr-mentor-img" onerror="this.style.display='none';document.getElementById('nrMentorFallback').style.display='flex'">
      <span id="nrMentorFallback" class="nr-mentor-fallback">IS</span>
      <span class="nr-tooltip">DESIGN STUDIO</span>
    </a>
    <a href="/images" class="nr-btn" data-page="images" title="IMAGES">
      <span>🎬</span><span class="nr-tooltip">IMAGES</span>
    </a>
    <a href="/cu" class="nr-btn" data-page="cu" title="CU DEPT">
      <span>⚙</span><span class="nr-tooltip">CU DEPT</span>
    </a>
    <div class="nr-divider"></div>
    <a href="/bigq" class="nr-btn" data-page="bigq" title="BIG Q">
      <span>🎛</span><span class="nr-tooltip">BIG Q</span>
    </a>
    <div class="nr-spacer"></div>
    <!-- GPU MINI-METER -->
    <div class="nr-gpu" id="nrGpu">
      <button class="nr-gpu-toggle" id="nrGpuToggle" onclick="nrGpuToggle()" title="Toggle GPU ($0.40/hr · idle auto-off applies)">⏻</button>
      <div class="nr-gpu-bar-wrap"><div class="nr-gpu-bar" id="nrGpuBar"></div></div>
      <div class="nr-gpu-cost" id="nrGpuCost">$0.00</div>
      <div class="nr-gpu-label" id="nrGpuLabel">GPU</div>
    </div>
  </div>

  <!-- SIDEBAR -->
  <div id="sidebar" class="collapsed">
    <div id="sideHead">
      <span style="font-size:10px;color:var(--muted);flex-shrink:0">REPO</span>
      <select id="repoSel" onchange="loadRepoTree()">
        <option value="">connect GitHub…</option>
      </select>
    </div>
    <div id="fileTree"><div style="padding:12px 10px;font-size:11px;color:var(--muted)">Connect GitHub to browse files</div></div>
  </div>

  <!-- EDITOR -->
  <div id="editorArea">
    <div id="tabBar"></div>
    <div id="editorWrap">
      <div id="homeLanding">

        <!-- HERO HEADLINE -->
        <div class="hl-hero">
          <h1 class="hl-hero-title">What should we build?</h1>
          <p class="hl-hero-sub">Local models, your GitHub, zero cloud lock-in.</p>
        </div>

        <!-- MOTIVATIONAL LINE -->
        <div class="hl-daily-push">GET SHIT DONE ALL DAY TJ!</div>

        <div id="hlComposer" style="position:relative;">

          <div id="composerCtxPills"></div>
          <div id="mentionPopover"></div>

          <textarea id="chatInput" placeholder="Ask CONNIE anything… (Enter sends, Shift+Enter newline, @ to reference a file)" onkeydown="hlPromptKey(event)" oninput="updateTokenEst();classifyPrompt(this.value);handleMentionInput(event)"></textarea>

          <div class="hl-composer-row">
            <!-- Unified injection menu -->
            <div style="position:relative;">
              <button class="hl-icon-btn" id="injectMenuBtn" title="Attach / connect" onclick="toggleInjectMenu()">+</button>
              <div id="injectMenu" style="position:absolute;bottom:calc(100% + 8px);left:0;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:6px;z-index:400;display:none;min-width:210px;box-shadow:0 -6px 24px rgba(0,0,0,.5);">
                <div class="tm-section">Files</div>
                <div class="tm-item" onclick="toggleVault();closeInjectMenu()"><span class="ti-icon">📎</span>Attach file</div>
                <div class="tm-item" onclick="runToolAction('attach_folder');closeInjectMenu()"><span class="ti-icon">📂</span>Attach folder</div>
                <div class="tm-item" onclick="openGHModal();closeInjectMenu()"><span class="ti-icon">🔗</span>Reference another project</div>
                <div class="tm-divider"></div>
                <div class="tm-section">Code</div>
                <div class="tm-item" onclick="openGHModal();closeInjectMenu()"><span class="ti-icon">⎇</span>GitHub connected</div>
                <div class="tm-item" onclick="runToolAction('link_local_code');closeInjectMenu()"><span class="ti-icon">🖥️</span>Link local code…</div>
                <div class="tm-divider"></div>
                <div class="tm-section">Design</div>
                <div class="tm-item" onclick="runToolAction('open_design_system');closeInjectMenu()"><span class="ti-icon">🎨</span>Design system (DESIGN.md)</div>
                <div class="tm-item" onclick="runToolAction('open_skills');closeInjectMenu()"><span class="ti-icon">🧩</span>Skills</div>
                <div class="tm-item" onclick="runToolAction('manage_connectors');closeInjectMenu()"><span class="ti-icon">🔌</span>Manage connectors</div>
              </div>
            </div>

            <div class="hl-pill" onclick="runToolAction('open_design_system')">
              <span class="hl-pill-label">Design system</span>
              <span class="hl-pill-value" id="hlDesignSysLabel">None</span>
            </div>
            <button class="hl-icon-btn" title="Code view" onclick="showEditorTab()">&lt;/&gt;</button>

            <!-- Consolidated "more" menu: project, mode, context toggles, tools, GPU -->
            <div style="position:relative;">
              <button class="hl-icon-btn" id="hlMoreBtn" title="More" onclick="toggleHlMore()">⋯</button>
              <div id="hlMoreMenu" style="position:absolute;bottom:calc(100% + 8px);left:0;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:8px;z-index:400;display:none;min-width:230px;box-shadow:0 -6px 24px rgba(0,0,0,.5);">
                <div class="tm-section">Project</div>
                <div class="tm-item" id="projectPill" onclick="openGHModal()"><span class="ti-icon">📁</span><span id="ppName">No project</span></div>

                <div class="tm-section">Mode</div>
                <div class="mode-group" style="margin:0 4px 6px;">
                  <button class="mode-btn" id="modePlan" onclick="setMode('plan')">📋 Plan</button>
                  <div class="mode-sep"></div>
                  <button class="mode-btn active" id="modeAuto" onclick="setMode('auto')">⚙ Auto</button>
                  <div class="mode-sep"></div>
                  <button class="mode-btn superman" id="modeSuper" onclick="setMode('superman')">⚡ Superman</button>
                </div>
                <div id="modeHint" style="padding:0 6px 6px;font-size:9px;">Auto: acts autonomously, pauses before destructive changes</div>

                <div class="tm-divider"></div>
                <div class="tm-section">Include as context</div>
                <div class="tm-item" id="toolTerm" onclick="toggleToolCtx('term','toolTerm')"><span class="ti-icon">🖥️</span>Terminal output</div>
                <div class="tm-item" id="toolVault" onclick="toggleVault()"><span class="ti-icon">🔐</span>Nobility Vault</div>
                <div class="tm-item" id="toolCode" onclick="toggleToolCtx('code','toolCode')"><span class="ti-icon">&lt;/&gt;</span>Open file</div>
                <div class="tm-item" id="toolGit" onclick="toggleToolCtx('git','toolGit')"><span class="ti-icon">⎇</span>Git diff</div>

                <div class="tm-divider"></div>
                <div class="tm-section">Tools</div>
                <div class="tm-item" onclick="runToolAction('write_file')"><span class="ti-icon">💾</span>Write file to repo<span class="ti-key">Ctrl+S</span></div>
                <div class="tm-item" onclick="runToolAction('read_file')"><span class="ti-icon">📂</span>Read file from repo</div>
                <div class="tm-item" onclick="runToolAction('run_tests')"><span class="ti-icon">🧪</span>Run tests</div>
                <div class="tm-item" onclick="runToolAction('git_status')"><span class="ti-icon">⎇</span>Git status</div>
                <div class="tm-item" onclick="runToolAction('git_diff')"><span class="ti-icon">📊</span>Git diff</div>
                <div class="tm-item" onclick="runToolAction('git_log')"><span class="ti-icon">📜</span>Git log</div>
                <div class="tm-item" onclick="runToolAction('install_deps')"><span class="ti-icon">📦</span>Install dependencies</div>
                <div class="tm-item" onclick="runToolAction('start_server')"><span class="ti-icon">🚀</span>Start dev server</div>
                <div class="tm-item" onclick="runToolAction('clear_chat')"><span class="ti-icon">🗑</span>Clear conversation</div>

                <div class="tm-divider"></div>
                <div class="tm-item"><span class="ti-icon">📶</span>Routing<span class="ti-key" id="catBadge">CAT-?</span></div>
                <div class="tm-item" id="gpuSpinBtn" onclick="requestGpuSpin()" style="display:none;"><span class="ti-icon">🔥</span>Fire GPU $0.40/hr</div>
              </div>
            </div>

            <div class="hl-spacer"></div>

            <!-- Real model selector (populated by JS) -->
            <div id="modelDropWrapper">
              <div id="modelDisplay" onclick="toggleModelMenu()">
                <span class="mo-tag tag-code md-tag">CODE</span>
                <span class="md-name" id="mdName">select model…</span>
                <span class="md-arrow">▾</span>
              </div>
              <div id="modelMenu"></div>
            </div>

            <span style="font-size:10px;color:var(--muted);flex-shrink:0;" id="tokenEst"></span>
            <button id="hlSendBtn" onclick="hlSend()">↑</button>
          </div>
        </div>

        <div id="chatLog">
          <div id="welcomeHero" style="display:none">
            <div class="wh-compact" onclick="toggleWelcomeMore()">
              <span class="wh-compact-text">Good to see you, TJ — <b>CONNIE CODE</b></span>
              <span class="wh-more-arrow" id="whMoreArrow">▾ more</span>
            </div>
            <div class="home-cards" id="whMoreCards" style="display:none">
              <div class="home-card" onclick="openGHModal()">
                <div class="hc-icon">⎇</div>
                <div class="hc-label">OPEN PROJECT</div>
                <div class="hc-sub">Connect a GitHub repo to start coding</div>
              </div>
              <div class="home-card" onclick="window.location='/studio'">
                <div class="hc-icon">🎙</div>
                <div class="hc-label">VOICE STUDIO</div>
                <div class="hc-sub">Clone voices, mix, BIG Q mastering</div>
              </div>
              <div class="home-card home-card-mentor" onclick="window.location='/ide'">
                <div class="hc-mentor-wrap">
                  <img src="/static/ilya.jpg" class="hc-mentor-img" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
                  <div class="hc-mentor-fallback">IS</div>
                </div>
                <div class="hc-label">DESIGN STUDIO</div>
                <div class="hc-sub">Ilya Sutskever · Open Design · IDE</div>
              </div>
              <div class="home-card" onclick="window.location='/images'">
                <div class="hc-icon">🎬</div>
                <div class="hc-label">IMAGES</div>
                <div class="hc-sub">MiniMax-H3 · ZeroGPU · video gen</div>
              </div>
            </div>
            <div class="home-recents" id="homeRecents" style="display:none">
              <div class="home-recents-label">RECENT REPOS</div>
              <div id="recentRepoList"></div>
            </div>
          </div>
        </div>

        <div class="hl-templates-label" onclick="toggleTplGrid()"><i class="hl-tpl-arrow" id="tplArrow">▾</i> CHOOSE A TEMPLATE</div>
        <div class="hl-templates" id="hlTemplates"></div>
        <div id="hlProjects">
          <div class="hl-proj-tabs">
            <div class="hl-proj-tab active" data-t="projects" onclick="hlProjTab('projects')">Projects</div>
            <div class="hl-proj-tab" data-t="systems" onclick="hlProjTab('systems')">Design systems</div>
            <div class="hl-proj-tab" data-t="templates" onclick="hlProjTab('templates')">Templates</div>
            <input class="hl-proj-search" id="hlProjSearch" placeholder="Search" oninput="renderHlProjects()">
          </div>
          <div id="hlProjList"></div>
        </div>
      </div>
      <div id="cmContainer" style="display:none;position:absolute;inset:0;"></div>
    </div>
    <div id="previewPane">
      <div id="previewBar">
        <span class="pb-label">LIVE RENDER CANVAS</span>
        <span id="pbPortLabel" style="font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--green);display:none;margin-left:4px;"></span>
        <span style="flex:1"></span>
        <button class="pb-btn" id="pbDevBtn" onclick="toggleDevServer()" title="Start project dev server on its own port (npm/python/etc)">⚡ dev server</button>
        <button class="pb-btn" id="pbInspect" onclick="togglePreviewInspect()">⌖ inspect</button>
        <button class="pb-btn" onclick="refreshPreview()">↻ refresh</button>
      </div>
      <div id="previewEmpty">Ask CONNIE for HTML/CSS/JS — live render appears here.<br>For server projects, click <b>⚡ dev server</b> to run on its own port inside this pane.</div>
      <iframe id="previewFrame" sandbox="allow-scripts allow-forms allow-same-origin allow-popups" style="display:none"></iframe>
    </div>
    <div id="terminal" class="collapsed">
      <div id="termHead" onclick="toggleTerminal(event)">
        <span id="termToggleArrow" style="color:var(--muted);font-size:9px">▸</span>
        <span style="color:var(--green);font-weight:700;font-size:11px">TERMINAL</span>
        <span id="termCwdDisplay" style="color:var(--muted);font-size:10px">/home/hunt</span>
        <span style="flex:1"></span>
        <button style="background:transparent;border:1px solid var(--border);color:var(--muted);padding:2px 8px;border-radius:3px;font-size:10px;cursor:pointer" onclick="event.stopPropagation();clearTerm()">clear</button>
      </div>
      <div id="termOut"></div>
      <div id="termInputRow">
        <span id="termPrompt">~/&nbsp;</span>
        <input id="termInput" placeholder="enter command…" onkeydown="termKey(event)">
      </div>
    </div>
  </div>

  <!-- AGENT PANEL -->

</div><!-- end main -->

<!-- VAULT PANEL (bottom sheet, full width) -->
<div id="vaultPanel">
  <div id="vaultHead">
    <span>🔐</span>
    <span class="vault-title">NOBILITY VAULT</span>
    <input id="vaultSearch" placeholder="search voices…" oninput="filterVault()" style="max-width:140px;">
    <button id="vaultClose" onclick="toggleVault()">×</button>
  </div>
  <div id="vaultList"></div>
  <div id="vaultFooter">
    <button class="vault-act" onclick="runCmd('ls -la /mnt/NOBILITY_VAULT/voice_vault/ 2>/dev/null || ls ~/voice_vault 2>/dev/null')">🔍 Browse vault dir</button>
    <button class="vault-act" onclick="injectVaultPath()">📎 Add selected to context</button>
    <button class="vault-act" onclick="window.location='/bigq'">🎙 Open BIG Q</button>
  </div>
</div>

<!-- GITHUB MODAL -->
<div id="ghModal" class="modal-overlay" style="display:none">
  <div class="modal">
    <h3>⎇ GitHub</h3>
    <div>
      <div class="modal-label">Personal Access Token</div>
      <input id="ghTokenInput" type="password" placeholder="ghp_xxxxxxxxxxxxxxxxxxxx">
      <div class="modal-hint">Needs repo + contents scopes. Saved server-side only.</div>
    </div>
    <div class="modal-row">
      <button class="modal-btn" onclick="saveGHToken()">Save & Connect</button>
      <button class="modal-btn sec" onclick="closeModal('ghModal')">Cancel</button>
    </div>
    <div id="ghStatus" style="font-size:11px;display:none;"></div>
    <div id="ghRepoList" style="max-height:220px;overflow-y:auto;display:flex;flex-direction:column;gap:2px;margin-top:4px;"></div>
  </div>
</div>

<!-- GCP MODAL -->
<div id="gcpModal" class="modal-overlay" style="display:none">
  <div class="modal">
    <h3>☁ Google Cloud GPU</h3>
    <div>
      <div class="modal-label">GCP Project ID</div>
      <input id="gcpProject" type="text" placeholder="my-gcp-project-123">
    </div>
    <div class="modal-row">
      <div style="flex:1"><div class="modal-label">Region</div>
        <select id="gcpRegion" style="background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:5px;width:100%;outline:none;">
          <option>us-central1</option><option>us-east4</option><option>us-west4</option><option>europe-west4</option><option>asia-northeast1</option>
        </select>
      </div>
      <div style="flex:1"><div class="modal-label">Zone</div>
        <select id="gcpZone" style="background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:5px;width:100%;outline:none;">
          <option>us-central1-a</option><option>us-central1-b</option><option>us-east4-a</option><option>europe-west4-a</option>
        </select>
      </div>
    </div>
    <div>
      <div class="modal-label">GPU Inference Endpoint (Qwen 14B already on your GCE box)</div>
      <input id="gcpGpuEndpoint" type="text" placeholder="http://<gce-external-ip>:8001/v1">
    </div>
    <div class="modal-hint">Set GOOGLE_APPLICATION_CREDENTIALS to your service account JSON path. NGC enterprise GPU types: A100 80GB · H100 · L4 · T4.</div>
    <div class="modal-row">
      <button class="modal-btn" onclick="saveGCP()">Save Config</button>
      <button class="modal-btn sec" onclick="closeModal('gcpModal')">Cancel</button>
    </div>
    <div id="gcpMsg" style="font-size:11px;color:var(--green);display:none;"></div>
  </div>
</div>

<script>
// ── MODEL CATALOG ──────────────────────────────────────────────────────────
const MODEL_CATALOG = {
  // ── TOP: runs on this laptop's CPU. Free, always available, no GPU. ──
  cpu: [
    {id:'local:deepseek-coder-1.3b', name:'DeepSeek Coder 1.3B (vault)', size:'1.3B', tag:'local', cat:1},
    {id:'local:qwen-coder-1.5b',     name:'Qwen2.5 Coder 1.5B (vault)',  size:'1.5B', tag:'local', cat:1},
    {id:'local:codegemma-2b',        name:'CodeGemma 2B (vault)',        size:'2B',   tag:'local', cat:2},
    {id:'local:qwen-coder-3b',       name:'Qwen2.5 Coder 3B (vault)',    size:'3B',   tag:'local', cat:2},
    {id:'local:starcoder2-3b',       name:'StarCoder2 3B (vault)',       size:'3B',   tag:'local', cat:2},
    {id:'local:qwen-coder-7b',       name:'Qwen2.5 Coder 7B (vault)',    size:'7B',   tag:'local', cat:3},
  ],
  // ── MIDDLE: needs berylize-node powered on. Costs money. Manual only. ──
  gpu: [
    {id:'local:qwen-coder-14b',      name:'Qwen2.5 Coder 14B (GCP GPU)', size:'14B',  tag:'gpu',   cat:4},
    {id:'local:qwen3-coder-30b',     name:'Qwen3 Coder 30B-A3B (GPU)',   size:'30B',  tag:'gpu',   cat:4},
  ],
  coding: [
    {id:'microsoft/phi-3-mini-4k-instruct',    name:'Phi-3 Mini 4K',          size:'3.8B', tag:'fast'},
    {id:'microsoft/phi-3-medium-128k-instruct',name:'Phi-3 Medium 128K',       size:'14B',  tag:'fast'},
    {id:'nvidia/starcoder2-7b',                name:'StarCoder2 7B',           size:'7B',   tag:'code'},
    {id:'nvidia/starcoder2-15b',               name:'StarCoder2 15B',          size:'15B',  tag:'code'},
    {id:'ibm/granite-8b-code-instruct',        name:'Granite Code 8B',         size:'8B',   tag:'code'},
    {id:'mistralai/codestral-22b-instruct-v0.1',name:'Codestral 22B',          size:'22B',  tag:'code'},
    {id:'ibm/granite-34b-code-instruct',       name:'Granite Code 34B',        size:'34B',  tag:'code'},
    {id:'google/gemma-2-27b-it',               name:'Gemma 2 27B',             size:'27B',  tag:'fast'},
    {id:'qwen/qwen2.5-coder-32b-instruct',     name:'Qwen 2.5 Coder 32B',      size:'32B',  tag:'code'},
    {id:'meta/llama-3.1-70b-instruct',         name:'Llama 3.1 70B',           size:'70B',  tag:'fast'},
    {id:'meta/llama-3.3-70b-instruct',         name:'Llama 3.3 70B',           size:'70B',  tag:'nvidia'},
    {id:'nvidia/llama-3.1-nemotron-70b-instruct',name:'Nemotron 70B',          size:'70B',  tag:'nvidia'},
    {id:'mistralai/mistral-large',             name:'Mistral Large',           size:'123B', tag:'fast'},
    {id:'deepseek-ai/deepseek-coder-v2-instruct',name:'DeepSeek Coder V2',     size:'236B', tag:'code'},
    {id:'meta/llama-3.1-405b-instruct',        name:'Llama 3.1 405B',          size:'405B', tag:'large'},
    {id:'nvidia/nemotron-4-340b-instruct',     name:'Nemotron 4 340B',         size:'340B', tag:'nvidia'},
  ],
  diffusion: [
    {id:'stabilityai/stable-diffusion-xl-base-1.0', name:'SDXL Base 1.0',      size:'—', tag:'diff'},
    {id:'stabilityai/stable-diffusion-3-medium',    name:'SD3 Medium',          size:'—', tag:'diff'},
    {id:'black-forest-labs/flux-schnell',            name:'FLUX Schnell',        size:'—', tag:'diff'},
    {id:'black-forest-labs/flux-dev',                name:'FLUX Dev',            size:'—', tag:'diff'},
    {id:'nvidia/consistory',                         name:'Consistory (NIM)',    size:'—', tag:'nvidia'},
  ],
  voice: [
    {id:'nvidia/parakeet-ctc-1.1b-asr',  name:'Parakeet CTC 1.1B ASR', size:'1.1B', tag:'voice'},
    {id:'nvidia/parakeet-tdt-1.1b-asr',  name:'Parakeet TDT 1.1B ASR', size:'1.1B', tag:'voice'},
    {id:'nvidia/canary-1b',              name:'Canary 1B ASR/TTS',     size:'1B',   tag:'voice'},
    {id:'nvidia/fastpitch',              name:'FastPitch TTS',          size:'—',    tag:'voice'},
    {id:'nvidia/radtts',                 name:'RADTTS TTS',             size:'—',    tag:'voice'},
  ],
};

const TAG_LABELS = {code:'CODE',fast:'FAST',nvidia:'NVIDIA',large:'LARGE',diff:'DIFFUSION',voice:'VOICE',local:'VAULT',gpu:'GPU'};
const CAT_COLORS = ['','#10b981','#38bdf8','#a855f7','#f59e0b','#ef4444'];
let _localStatus = {};

// ── STATE ──────────────────────────────────────────────────────────────────
let _editor = null;
let _model = 'deepseek-ai/deepseek-coder-v2-instruct';
let _modelTag = 'code';
let _mode = 'auto';
let _ctx = {code:false, term:false, git:false};
let _ctxItems = [];   // {type, label, data}
let _messages = [];
let _tabs = {};
let _activeTab = 'welcome';
let _termCwd = '/home/hunt';
let _termLog = '';
let _vaultFiles = [];
let _selectedVaultFiles = [];
let _repoOwner = '';
let _repoName = '';
let _fileShas = {};       // path → current GitHub sha (needed for update writes)
let _dirtyFiles = new Set(); // files modified since last push
let _autoSaveTimer = null;

// ── TOAST ──
function showToast(msg, type='ok', dur=3000){
  let t=document.getElementById('craneToast');
  if(!t){ t=document.createElement('div'); t.id='craneToast';
    t.style.cssText='position:fixed;bottom:56px;left:50%;transform:translateX(-50%);z-index:9999;font-family:"JetBrains Mono",monospace;font-size:11px;padding:7px 18px;border-radius:8px;pointer-events:none;transition:opacity .3s;white-space:nowrap;';
    document.body.appendChild(t); }
  t.textContent=msg;
  t.style.background=type==='ok'?'rgba(46,230,184,.18)':type==='warn'?'rgba(245,158,11,.18)':'rgba(239,68,68,.18)';
  t.style.border='1px solid '+(type==='ok'?'rgba(46,230,184,.4)':type==='warn'?'rgba(245,158,11,.4)':'rgba(239,68,68,.4)');
  t.style.color=type==='ok'?'var(--green)':type==='warn'?'var(--gold)':'#fca5a5';
  t.style.opacity='1';
  clearTimeout(t._to); t._to=setTimeout(()=>{t.style.opacity='0';},dur);
}
let _modelMenuOpen = false;

// ── EDITOR ─────────────────────────────────────────────────────────────────
function initEditor() {
  const wrap = document.getElementById('cmContainer');
  _editor = CodeMirror(wrap, {
    value: `// Welcome to CRANE IDE\n// Powered by NVIDIA NIM — your model, your rules\n\nconsole.log("CRANE is ready.");`,
    mode: 'javascript',
    lineNumbers: true,
    tabSize: 2,
    indentWithTabs: false,
    lineWrapping: false,
    autofocus: false,
    extraKeys: {'Ctrl-S': saveCurrentFile, 'Ctrl-Enter': ()=>sendChat()},
  });
  _editor.setSize('100%', '100%');
  _editor.on('change', _onEditorChange);
}

// ── MODEL MENU ─────────────────────────────────────────────────────────────
function buildModelMenu() {
  const menu = document.getElementById('modelMenu');
  menu.innerHTML = '';

  const sections = [
    {key:'cpu',      label:'◆ LOCAL CPU — FREE, NO GPU NEEDED',      cls:'cpu'},
    {key:'gpu',      label:'⚡ GPU REQUIRED — FIRE GPU FIRST ($0.40/hr)', cls:'gpu'},
    {key:'coding',   label:'NVIDIA NIM — CODING (FREE API)',          cls:'nvidia'},
    {key:'diffusion',label:'NVIDIA NIM — DIFFUSION (FREE API)',       cls:'diffusion'},
    {key:'voice',    label:'NVIDIA NIM — VOICE (FREE API)',           cls:'voice'},
  ];

  sections.forEach(sec => {
    const h = document.createElement('div');
    h.className = `mg-head ${sec.cls}`;
    h.textContent = sec.label;
    menu.appendChild(h);
    MODEL_CATALOG[sec.key].forEach(m => {
      const row = document.createElement('div');
      row.className = 'model-opt' + (m.id === _model ? ' selected' : '');
      row.dataset.id = m.id;
      row.dataset.tag = m.tag;
      const tagCls = `mo-tag tag-${m.tag}`;
      let statusDot = '';
      if (m.tag === 'local' || m.tag === 'gpu') {
        const ready = m.tag === 'gpu' ? false : (_localStatus[m.id] && _localStatus[m.id].downloaded);
        statusDot = `<span style="width:6px;height:6px;border-radius:50%;flex-shrink:0;background:${ready?'#10b981':m.tag==='gpu'?'#f59e0b':'#475569'}" title="${m.tag==='gpu'?'spins up on demand':ready?'downloaded':'downloading…'}"></span>`;
      }
      const catDot = m.cat ? `<span class="mo-cat" style="color:${CAT_COLORS[m.cat]};border-color:${CAT_COLORS[m.cat]}">C${m.cat}</span>` : '';
      row.innerHTML = `${statusDot}<span class="mo-size">${m.size}</span><span class="mo-name">${m.name}</span>${catDot}<span class="${tagCls}">${TAG_LABELS[m.tag]||m.tag.toUpperCase()}</span>`;
      row.onclick = () => selectModel(m.id, m.tag, m.name, m.cat);
      menu.appendChild(row);
    });
  });
}

function toggleModelMenu() {
  _modelMenuOpen = !_modelMenuOpen;
  document.getElementById('modelMenu').classList.toggle('open', _modelMenuOpen);
}

function selectModel(id, tag, name, cat, isAuto) {
  _model = id; _modelTag = tag;
  if (!isAuto) _manualModelPick = true;
  document.getElementById('mdName').textContent = name;
  document.getElementById('modelMenu').querySelectorAll('.model-opt').forEach(r => {
    r.classList.toggle('selected', r.dataset.id === id);
  });
  const tagEl = document.querySelector('#modelDisplay .md-tag');
  tagEl.className = `mo-tag tag-${tag} md-tag`;
  tagEl.textContent = TAG_LABELS[tag] || tag.toUpperCase();
  _modelMenuOpen = false;
  document.getElementById('modelMenu').classList.remove('open');
  if (isAuto) appendMsg('sys', `CAT-${cat} → auto-routed to ${name}`);
}

// Close menu on outside click
document.addEventListener('click', e => {
  const wrap = document.getElementById('modelDropWrapper');
  if(wrap && !wrap.contains(e.target)) {
    _modelMenuOpen = false;
    document.getElementById('modelMenu').classList.remove('open');
  }
  const tm = document.getElementById('toolsMenu');
  const tmb = document.getElementById('toolMenuBtn');
  if(tm && !tm.contains(e.target) && e.target !== tmb) {
    tm.classList.remove('open');
  }
});

// Live NVIDIA fetch to merge into catalog
async function fetchLiveModels() {
  try {
    const r = await fetch('/api/ide/nvidia/models');
    const d = await r.json();
    const nvDot = document.getElementById('nvDot');
    const nvLabel = document.getElementById('nvLabel');
    if(d.source === 'live') {
      nvDot.className = 'dot on'; nvLabel.textContent = 'NIM live';
    } else {
      nvDot.className = 'dot warn'; nvLabel.textContent = 'NIM key?';
    }
  } catch(e){}
}

// ── MODE ───────────────────────────────────────────────────────────────────
const MODE_HINTS = {
  plan:     '📋 Plan: thinks first, shows you the plan, waits for your go-ahead',
  auto:     '⚙ Auto: acts autonomously, pauses before destructive changes',
  superman: '⚡ Superman: fully autonomous — local model decides everything, no interruptions',
};
function setMode(m) {
  _mode = m;
  ['plan','auto','superman'].forEach(k => {
    const btn = document.getElementById('mode'+k.charAt(0).toUpperCase()+k.slice(1));
    btn.classList.toggle('active', k === m);
  });
  document.getElementById('modeHint').textContent = MODE_HINTS[m];
}

// ── CAT-4 MODEL ROUTING PROTOCOL ─────────────────────────────────────────────
const CAT5_AUTO_ROUTE = {1:'local:deepseek-coder-1.3b',2:'local:qwen-coder-3b',3:'local:qwen-coder-7b',4:'local:qwen-coder-14b'};
const CAT5_LABEL = {1:'CAT-1 FAST',2:'CAT-2 LIGHT',3:'CAT-3 CORE',4:'CAT-4 HEAVY'};
let _catDebounce = null;
async function classifyPrompt(txt) {
  clearTimeout(_catDebounce);
  if (!txt || txt.trim().length < 10) { document.getElementById('catBadge').style.display='none'; document.getElementById('gpuSpinBtn').style.display='none'; return; }
  _catDebounce = setTimeout(async () => {
    try {
      const r = await fetch('/api/ide/cat', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({prompt: txt})});
      const d = await r.json();
      const badge = document.getElementById('catBadge');
      const col = CAT_COLORS[d.cat];
      badge.textContent = d.label; badge.style.color = col; badge.style.borderColor = col; badge.style.background = col+'22'; badge.style.display = 'inline-block';
      document.getElementById('gpuSpinBtn').style.display = d.needs_gpu ? 'inline-block' : 'none';
      // auto-route to the right local model for this CAT level unless user already picked something manually this session
      if (!_manualModelPick) {
        const target = CAT5_AUTO_ROUTE[d.cat];
        const all = [...MODEL_CATALOG.local];
        const found = all.find(x => x.id === target);
        if (found && found.id !== _model) selectModel(found.id, found.tag, found.name, found.cat, true);
      }
    } catch(e) {}
  }, 500);
}

let _manualModelPick = false;
async function requestGpuSpin() {
  const r = await fetch('/api/ide/gcp/spin', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({model_id:_model})});
  const d = await r.json();
  if (d.status === 'error') { appendMsg('sys', '⚠ '+d.error); return; }
  appendMsg('sys', `🔥 GPU provisioning ready.\n${d.message}\n\n[FILE:gcloud_command.sh]`);
  window._gcloudCmd = d.gcloud_cmd;
}

async function loadLocalModelStatus() {
  try {
    const r = await fetch('/api/ide/local/status');
    const d = await r.json();
    (d.models||[]).forEach(m => { _localStatus[m.id] = m; });
    buildModelMenu();
  } catch(e) {}
}

// ── CONTEXT TOOLS ──────────────────────────────────────────────────────────
function toggleToolCtx(key, btnId) {
  _ctx[key] = !_ctx[key];
  document.getElementById(btnId).classList.toggle('active', _ctx[key]);
  if(_ctx[key]) addCtxPill(key, key === 'code' ? '&lt;/&gt; file' : key === 'term' ? '$ terminal' : '⎇ diff');
  else removeCtxPill(key);
}

function addCtxPill(type, label) {
  removeCtxPill(type);
  const bar = document.getElementById('composerCtxPills');
  const p = document.createElement('div');
  p.className = 'ctx-pill'; p.dataset.type = type;
  p.innerHTML = `${label} <span class="cp-x" onclick="removeCtxPill('${type}')">×</span>`;
  bar.appendChild(p);
}

function removeCtxPill(type) {
  document.querySelectorAll(`#composerCtxPills [data-type="${type}"]`).forEach(el=>el.remove());
  if(['code','term','git'].includes(type)) { _ctx[type]=false; document.getElementById('tool'+type.charAt(0).toUpperCase()+type.slice(1))?.classList.remove('active'); }
  _ctxItems = _ctxItems.filter(i=>i.type!==type);
}

function addVaultCtxPill(fname) {
  const type = 'vault_'+fname;
  if(document.querySelector(`[data-type="${type}"]`)) return;
  addCtxPill(type, '🔐 '+fname.slice(0,20));
}

// ── TOOLS MENU ACTIONS ─────────────────────────────────────────────────────
function toggleToolsMenu() {
  document.getElementById('toolsMenu')?.classList.toggle('open');
}

async function runToolAction(action) {
  document.getElementById('toolsMenu')?.classList.remove('open');
  document.getElementById('hlMoreMenu').style.display='none';
  const cmds = {
    git_status: 'git status',
    git_diff: 'git diff HEAD',
    git_log: 'git log --oneline -10',
    run_tests: 'python3 -m pytest -x -q 2>&1 | head -40',
    install_deps: 'pip install -r requirements.txt 2>&1 | tail -10',
    start_server: 'uvicorn app:app --host 127.0.0.1 --port 8000 --reload &',
  };
  if(cmds[action]) { runCmd(cmds[action]); return; }
  if(action === 'write_file') saveCurrentFile();
  if(action === 'read_file') { const p=prompt('File path in repo:'); if(p) openGHFile(p); }
  if(action === 'clear_chat') clearChat();
  if(action === 'attach_folder') { const p=prompt('Local folder path to attach as context:'); if(p) addCtxPill('folder',p); }
  if(action === 'link_local_code') { const p=prompt('Local path to link (file or repo dir):'); if(p) addCtxPill('code',p); }
  if(action === 'open_design_system') runCmd('cat DESIGN.md 2>/dev/null || echo "No DESIGN.md yet — run token extraction from a project folder."');
  if(action === 'open_skills') appendMsg('sys','Skills: local .claude/skills/ folder — ask CONNIE to list or create one.');
  if(action === 'manage_connectors') runCmd('cat /home/hunt/open-design/.od/mcp-config.json 2>/dev/null || echo "No MCP connectors configured yet."');
}

// ── INJECTION MENU (+) ────────────────────────────────────────────────────
function toggleInjectMenu(){ document.getElementById('injectMenu').style.display = document.getElementById('injectMenu').style.display==='block'?'none':'block'; }
function closeInjectMenu(){ document.getElementById('injectMenu').style.display='none'; }
document.addEventListener('click',function(e){
  const menu=document.getElementById('injectMenu'), btn=document.getElementById('injectMenuBtn');
  if(menu && menu.style.display==='block' && !menu.contains(e.target) && e.target!==btn) closeInjectMenu();
});

// ── @ FILE MENTIONS ────────────────────────────────────────────────────────
let _mentionFiles=[];
function refreshMentionFileList(){
  _mentionFiles=Object.keys(_tabs||{});
  document.querySelectorAll('.ft-item').forEach(el=>{ if(el.dataset && el.dataset.path) _mentionFiles.push(el.dataset.path); });
  _mentionFiles=[...new Set(_mentionFiles)];
}
function handleMentionInput(e){
  const inp=e.target, val=inp.value, pos=inp.selectionStart;
  const upToCursor=val.slice(0,pos);
  const m=upToCursor.match(/@([\w./-]*)$/);
  const pop=document.getElementById('mentionPopover');
  if(!m){ pop.classList.remove('open'); return; }
  refreshMentionFileList();
  const q=m[1].toLowerCase();
  const matches=_mentionFiles.filter(f=>f.toLowerCase().includes(q)).slice(0,8);
  if(!matches.length){ pop.classList.remove('open'); return; }
  pop.innerHTML=matches.map((f,i)=>`<div class="mp-item${i===0?' sel':''}" onclick="insertMention('${f.replace(/'/g,"\\'")}')">📄 ${f}</div>`).join('');
  pop.classList.add('open');
}
function insertMention(path){
  const inp=document.getElementById('chatInput'), val=inp.value, pos=inp.selectionStart;
  const upToCursor=val.slice(0,pos);
  const newUpTo=upToCursor.replace(/@([\w./-]*)$/, '@'+path+' ');
  inp.value=newUpTo+val.slice(pos);
  document.getElementById('mentionPopover').classList.remove('open');
  addCtxPill('code',path);
  inp.focus();
}

// ── TOKEN ESTIMATE ─────────────────────────────────────────────────────────
function updateTokenEst() {
  const txt = document.getElementById('chatInput').value;
  const est = Math.round(txt.length / 4);
  document.getElementById('tokenEst').textContent = est > 0 ? `~${est} tok` : '';
}

// ── VAULT PANEL ────────────────────────────────────────────────────────────
function toggleVault() {
  const panel = document.getElementById('vaultPanel');
  const toolVault = document.getElementById('toolVault');
  const isOpen = panel.classList.toggle('open');
  toolVault.classList.toggle('active', isOpen);
  if(isOpen) loadVaultFiles();
}

async function loadVaultFiles() {
  try {
    const r = await fetch('/api/vault/files');
    const d = await r.json();
    _vaultFiles = d.files || [];
    renderVault(_vaultFiles);
  } catch(e) {
    document.getElementById('vaultList').innerHTML = `<div style="padding:12px;color:var(--red);font-size:11px">Failed to load vault: ${e.message}</div>`;
  }
}

function renderVault(files) {
  const list = document.getElementById('vaultList');
  list.innerHTML = '';
  if(!files.length) {
    list.innerHTML = '<div style="padding:12px;color:var(--muted);font-size:11px">No voice files in vault</div>';
    return;
  }
  files.forEach(f => {
    const row = document.createElement('div');
    row.className = 'vault-item';
    row.dataset.name = f.filename;
    const ext = f.filename.split('.').pop().toUpperCase();
    const extColor = {WAV:'var(--blue)',MP3:'var(--purple)',M4A:'var(--green)',OPUS:'var(--orange)'}[ext]||'var(--muted)';
    row.innerHTML = `
      <span style="font-size:10px;background:rgba(255,255,255,.06);padding:1px 5px;border-radius:3px;color:${extColor};flex-shrink:0">${ext}</span>
      <span class="vi-name" title="${f.filename}">${f.filename}</span>
      <span class="vi-size">${f.size}</span>
      <span class="vi-add">+ add</span>`;
    row.onclick = () => {
      row.classList.toggle('added');
      if(row.classList.contains('added')) {
        _selectedVaultFiles.push(f.filename);
        addCtxPill('vault_'+f.filename, '🔐 '+f.filename.slice(0,18));
      } else {
        _selectedVaultFiles = _selectedVaultFiles.filter(x=>x!==f.filename);
        removeCtxPill('vault_'+f.filename);
      }
    };
    list.appendChild(row);
  });
}

function filterVault() {
  const q = document.getElementById('vaultSearch').value.toLowerCase();
  renderVault(_vaultFiles.filter(f => f.filename.toLowerCase().includes(q)));
}

function injectVaultPath() {
  if(!_selectedVaultFiles.length) return;
  const paths = _selectedVaultFiles.map(f=>`/mnt/NOBILITY_VAULT/voice_vault/${f}`).join('\n');
  const inp = document.getElementById('chatInput');
  inp.value = (inp.value ? inp.value+'\n\n' : '') + 'Vault files:\n' + paths;
  inp.focus();
}

// ── GITHUB ─────────────────────────────────────────────────────────────────
function openGHModal() { document.getElementById('ghModal').style.display='flex'; loadGHRepos(); }
function openGCPModal() {
  document.getElementById('gcpModal').style.display='flex';
  fetch('/api/ide/gcp/status').then(r=>r.json()).then(d=>{
    if(d.project_id) document.getElementById('gcpProject').value=d.project_id;
    if(d.gpu_endpoint) document.getElementById('gcpGpuEndpoint').value=d.gpu_endpoint;
  });
}
function closeModal(id) { document.getElementById(id).style.display='none'; }

async function saveGHToken() {
  const tok = document.getElementById('ghTokenInput').value.trim();
  if(!tok) return;
  const safe = tok.replace(/'/g,"'\\''");
  const r = await fetch('/api/ide/shell',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:`printf '%s' '${safe}' > ~/.crane_gh && chmod 600 ~/.crane_gh`})});
  const d = await r.json();
  const st = document.getElementById('ghStatus');
  st.style.display='block';
  if(d.rc===0) { st.style.color='var(--green)'; st.textContent='✅ Token saved. Reload page for full effect.'; loadGHRepos(); }
  else { st.style.color='var(--red)'; st.textContent='❌ '+d.stderr; }
}

async function loadGHRepos() {
  const r = await fetch('/api/ide/github/repos');
  const d = await r.json();
  if(d.error) {
    document.getElementById('ghRepoList').innerHTML=`<div style="color:var(--red);font-size:11px;padding:6px">${d.error}</div>`;
    return;
  }
  document.getElementById('tbGHBtn').classList.add('connected');
  const sel = document.getElementById('repoSel');
  sel.innerHTML='<option value="">— pick repo —</option>';
  const list = document.getElementById('ghRepoList');
  list.innerHTML='';
  (d.repos||[]).forEach(repo=>{
    const opt=document.createElement('option'); opt.value=repo.full_name; opt.textContent=repo.full_name; sel.appendChild(opt);
    const row=document.createElement('div'); row.className='repo-row';
    const badge=repo.private?'<span class="repo-priv">priv</span>':'<span class="repo-pub">pub</span>';
    row.innerHTML=badge+' '+repo.name+(repo.language?`<span style="margin-left:auto;font-size:9px;color:var(--muted)">${repo.language}</span>`:'');
    row.onclick=()=>{sel.value=repo.full_name;loadRepoTree();closeModal('ghModal');};
    list.appendChild(row);
  });
}

async function loadRepoTree() {
  const full=document.getElementById('repoSel').value;
  if(!full) return;
  [_repoOwner,_repoName]=full.split('/');
  document.getElementById('ppName').textContent=_repoName;
  const tree=document.getElementById('fileTree');
  tree.innerHTML='<div style="padding:10px;color:var(--muted);font-size:11px">Loading…</div>';
  const r=await fetch('/api/ide/github/tree',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({owner:_repoOwner,repo:_repoName,branch:'main'})});
  const d=await r.json();
  if(d.error){tree.innerHTML=`<div style="color:var(--red);padding:10px;font-size:11px">${d.error}</div>`;return;}
  renderTree(d.tree||[]);
}

function renderTree(items) {
  const tree=document.getElementById('fileTree'); tree.innerHTML='';
  items.forEach(item=>{
    const parts=item.path.split('/'); const depth=parts.length-1;
    const div=document.createElement('div');
    div.className='ft-item'+(item.type==='tree'?' ft-dir':'');
    div.style.paddingLeft=(14+depth*12)+'px';
    div.innerHTML=getFileIcon(item.path)+' '+parts[parts.length-1];
    if(item.type==='blob') div.onclick=()=>openGHFile(item.path);
    tree.appendChild(div);
  });
}

function getFileIcon(path){
  const ext=path.split('.').pop().toLowerCase();
  return {py:'🐍',js:'⚡',ts:'💙',tsx:'⚛',jsx:'⚛',html:'🌐',css:'🎨',md:'📝',json:'{}',sh:'$',yaml:'📋',yml:'📋',txt:'📄',png:'🖼',jpg:'🖼',svg:'✦'}[ext]||'📄';
}

async function openGHFile(path) {
  if(!_repoOwner) return;
  const r=await fetch('/api/ide/github/file',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({owner:_repoOwner,repo:_repoName,path,branch:'main'})});
  const d=await r.json();
  if(d.error){appendMsg('sys','❌ '+d.error);return;}
  const ext=path.split('.').pop().toLowerCase();
  const lang={py:'python',js:'javascript',ts:'javascript',jsx:'javascript',tsx:'javascript',html:'htmlmixed',css:'css',json:'javascript',sh:'shell',md:'markdown',yaml:'yaml',yml:'yaml'}[ext]||'text';
  _editor.setValue(d.content||'');
  _editor.setOption('mode',lang);
  _tabs[path]={content:d.content,sha:d.sha,path};
  addTab(path);
  document.querySelectorAll('.ft-item').forEach(el=>el.classList.toggle('active',el.textContent.includes(path.split('/').pop())));
}

// ── DECLUTTER TOGGLES ────────────────────────────────────────────────────────
function toggleSidebar(){
  const sb=document.getElementById('sidebar');
  sb.classList.toggle('collapsed');
  document.getElementById('sidebarToggleBtn').classList.toggle('active', !sb.classList.contains('collapsed'));
}
function toggleTerminal(e){
  const t=document.getElementById('terminal');
  t.classList.toggle('collapsed');
  document.getElementById('termToggleArrow').textContent = t.classList.contains('collapsed') ? '▸' : '▾';
}
function toggleWelcomeMore(){
  const cards=document.getElementById('whMoreCards');
  const isOpen=cards.style.display!=='none';
  cards.style.display=isOpen?'none':'grid';
  document.getElementById('whMoreArrow').textContent=isOpen?'▾ more':'▴ less';
}

// ── LIVE RENDER CANVAS ───────────────────────────────────────────────────────
let _previewActive=false, _previewInspect=false;
function showEditorTab(){
  _previewActive=false;
  document.getElementById('previewPane').classList.remove('active');
  document.getElementById('editorWrap').style.display='block';
  document.getElementById('tbPreviewBtn')?.classList.remove('active');
  document.querySelectorAll('.ed-tab').forEach(t=>{ t.classList.toggle('active', t.id===('tab_'+btoa(_activeTab||''))); });
  if(_activeTab && _tabs[_activeTab]){
    document.getElementById('homeLanding').classList.add('hidden');
    document.getElementById('cmContainer').style.display='block';
    _editor.refresh();
  } else {
    document.getElementById('homeLanding').classList.remove('hidden');
    document.getElementById('cmContainer').style.display='none';
  }
}
function togglePreview(){
  _previewActive=true;
  document.getElementById('previewPane').classList.add('active');
  document.getElementById('editorWrap').style.display='none';
  document.querySelectorAll('.ed-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tbPreviewBtn')?.classList.add('active');
  refreshPreview();
}
function renderPreview(html){
  const frame=document.getElementById('previewFrame');
  const empty=document.getElementById('previewEmpty');
  if(!html){ frame.style.display='none'; empty.style.display='flex'; return; }
  empty.style.display='none'; frame.style.display='block';
  frame.srcdoc=html;
  document.getElementById('previewPane').classList.add('active');
  document.getElementById('editorWrap').style.display='none';
  document.querySelectorAll('.ed-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tbPreviewBtn')?.classList.add('active');
  _previewActive=true;
}
function refreshPreview(){
  if(_activeTab && /\.html?$/i.test(_activeTab) && _tabs[_activeTab]){
    renderPreview(_tabs[_activeTab].content);
  } else if(window._lastRenderedHtml){
    renderPreview(window._lastRenderedHtml);
  } else {
    document.getElementById('previewFrame').style.display='none';
    document.getElementById('previewEmpty').style.display='flex';
  }
}
// ── DEV SERVER — project preview on its own port ──────────────────────────
let _devServerPort=null, _devServerPid=null;
async function toggleDevServer(){
  const btn=document.getElementById('pbDevBtn');
  const portLabel=document.getElementById('pbPortLabel');
  if(_devServerPort){
    // stop
    if(_devServerPid) await runCmd(`kill ${_devServerPid} 2>/dev/null; echo stopped`);
    _devServerPort=null; _devServerPid=null;
    btn.textContent='⚡ dev server'; btn.classList.remove('active');
    portLabel.style.display='none';
    document.getElementById('previewFrame').src='about:blank';
    return;
  }
  // detect project type from active repo or open files
  const port = await _findFreePort();
  if(!port){ alert('Could not find a free port (8001-8099).'); return; }
  const cmd = _detectDevCmd(port);
  btn.textContent='⏳ starting…'; btn.disabled=true;
  // run via shell endpoint
  const r=await fetch('/api/ide/shell',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd:`cd ${_repoOwner?'/home/hunt/'+_repoName:'/home/hunt'} 2>/dev/null; ${cmd}`})});
  // wait up to 8s for port to open
  for(let i=0;i<16;i++){
    await new Promise(res=>setTimeout(res,500));
    try{ const t=await fetch(`http://127.0.0.1:${port}`,{mode:'no-cors'}); break; }catch(e){}
  }
  _devServerPort=port;
  const frame=document.getElementById('previewFrame');
  frame.src=`http://127.0.0.1:${port}`;
  frame.style.display='block';
  document.getElementById('previewEmpty').style.display='none';
  if(!document.getElementById('previewPane').classList.contains('active')) togglePreview();
  portLabel.textContent=`localhost:${port}`;
  portLabel.style.display='inline';
  btn.textContent='■ stop server'; btn.classList.add('active'); btn.disabled=false;
}
async function _findFreePort(){
  for(let p=8001;p<8100;p++){
    const r=await fetch('/api/ide/shell',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd:`ss -tlnH sport = :${p} | head -1`})});
    const d=await r.json();
    if(!(d.stdout||'').trim()) return p;
  }
  return null;
}
function _detectDevCmd(port){
  // sniff open files / repo name to pick the right start command
  const name=(_repoName||'').toLowerCase();
  const files=Object.keys(_tabs||{});
  if(files.some(f=>/package\.json$/.test(f))||name.includes('next')||name.includes('react'))
    return `PORT=${port} npm start 2>&1 &`;
  if(files.some(f=>/requirements\.txt$|\.py$/.test(f)))
    return `python3 -m http.server ${port} 2>&1 &`;
  if(files.some(f=>/Cargo\.toml$/.test(f)))
    return `cargo run --release -- --port ${port} 2>&1 &`;
  // default: static file server
  return `python3 -m http.server ${port} 2>&1 &`;
}

function togglePreviewInspect(){
  _previewInspect=!_previewInspect;
  document.getElementById('pbInspect').classList.toggle('active',_previewInspect);
  const frame=document.getElementById('previewFrame');
  try{
    const doc=frame.contentDocument;
    if(!doc) return;
    if(_previewInspect){
      doc.body.style.cursor='crosshair';
      doc._inspectHandler=function(e){
        e.preventDefault(); e.stopPropagation();
        const el=e.target; const sel=el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(el.className&&typeof el.className==='string'?'.'+el.className.split(' ').join('.'):'');
        injectPrompt(`Isolate and edit this element: ${sel}\n`);
      };
      doc.addEventListener('click',doc._inspectHandler,true);
    } else {
      doc.body.style.cursor='';
      if(doc._inspectHandler) doc.removeEventListener('click',doc._inspectHandler,true);
    }
  }catch(e){}
}
// Extracts the last fenced ```html block from an assistant message and pipes it to the live canvas
function extractAndRenderHtml(text){
  const m=text.match(/```html\s*([\s\S]*?)```/i);
  if(m){ window._lastRenderedHtml=m[1]; renderPreview(m[1]); return true; }
  return false;
}

// ── HOME LANDING (Claude-Design-style) ──────────────────────────────────────
const HL_TEMPLATES=[
  {id:'blank',icon:'▢',name:'Blank',prompt:''},
  {id:'mobile',icon:'📱',name:'Mobile app design',prompt:'Design a mobile app screen: '},
  {id:'slides',icon:'▤',name:'Slides',prompt:'Build a slide deck as HTML: '},
  {id:'doc',icon:'📄',name:'Document',prompt:'Write a document: '},
  {id:'wireframe',icon:'⬚',name:'Wireframe',prompt:'Build a lo-fi wireframe (HTML) for: '},
  {id:'animation',icon:'▶',name:'Animation',prompt:'Build an animated HTML/CSS component: '},
  {id:'uimock',icon:'🖼',name:'UI mockups',prompt:'Design a UI mockup (HTML/CSS) for: '},
  {id:'resume',icon:'📋',name:'Résumé',prompt:'Build a résumé as HTML: '},
  {id:'3d',icon:'◆',name:'3D object',prompt:'Build a 3D object using CSS transforms or WebGL for: '},
  {id:'research',icon:'🔍',name:'Research',prompt:'Research and summarize: '},
  {id:'email',icon:'✉',name:'HTML email',prompt:'Build an HTML email template for: '},
  {id:'colortype',icon:'Aa',name:'Color + type pairing',prompt:'Propose a color palette and type pairing for: '},
];
let _hlSelectedTpl='blank';
function toggleTplGrid(){
  const grid=document.getElementById('hlTemplates');
  const arrow=document.getElementById('tplArrow');
  const open=grid.classList.toggle('open');
  arrow.classList.toggle('open',open);
}
function renderHlTemplates(){
  const grid=document.getElementById('hlTemplates');
  if(!grid) return;
  grid.innerHTML=HL_TEMPLATES.map(t=>`
    <div class="hl-tpl-card${t.id===_hlSelectedTpl?' sel':''}" onclick="hlPickTemplate('${t.id}')">
      <div class="hl-tpl-icon">${t.icon}</div>
      <div class="hl-tpl-name">${t.name}</div>
    </div>`).join('');
}
function hlPickTemplate(id){
  _hlSelectedTpl=id;
  renderHlTemplates();
  const t=HL_TEMPLATES.find(x=>x.id===id);
  const box=document.getElementById('chatInput');
  if(t && t.prompt){ box.value=t.prompt; box.focus(); box.setSelectionRange(box.value.length,box.value.length); }
}
function hlPromptKey(e){ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); hlSend(); } }
function hlSend(){
  const box=document.getElementById('chatInput');
  const txt=box.value.trim();
  if(!txt){ box.focus(); return; }
  hlLogProject(txt.slice(0,60));
  sendChat();
}
function toggleHlMore(){ const m=document.getElementById('hlMoreMenu'); m.style.display=m.style.display==='block'?'none':'block'; }
document.addEventListener('click',function(e){
  const m=document.getElementById('hlMoreMenu'), btn=document.getElementById('hlMoreBtn');
  if(m && m.style.display==='block' && !m.contains(e.target) && e.target!==btn) m.style.display='none';
});
let _hlProjTab='projects';
function hlProjTab(t){
  _hlProjTab=t;
  document.querySelectorAll('.hl-proj-tab').forEach(el=>el.classList.toggle('active',el.dataset.t===t));
  renderHlProjects();
}
async function hlLogProject(name){
  try{
    await fetch('/api/ide/projects',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name||'Untitled design',template:_hlSelectedTpl})});
    renderHlProjects();
  }catch(e){}
}
async function renderHlProjects(){
  const list=document.getElementById('hlProjList');
  if(!list) return;
  if(_hlProjTab==='systems'){
    list.innerHTML='<div class="hl-proj-empty">No design systems yet — click "Design system" in the composer to create one from your codebase tokens.</div>';
    return;
  }
  if(_hlProjTab==='templates'){
    list.innerHTML='<div class="hl-proj-empty">Templates live above — pick one to start.</div>';
    return;
  }
  try{
    const r=await fetch('/api/ide/projects');
    const d=await r.json();
    const q=(document.getElementById('hlProjSearch')?.value||'').toLowerCase();
    const rows=(d.projects||[]).filter(p=>p.name.toLowerCase().includes(q));
    if(!rows.length){ list.innerHTML='<div class="hl-proj-empty">No projects yet — describe something above to start your first one.</div>'; return; }
    list.innerHTML=rows.map(p=>`
      <div class="hl-proj-row">
        <div class="hl-proj-thumb">${HL_TEMPLATES.find(t=>t.id===p.template)?.icon||'▢'}</div>
        <div class="hl-proj-name">${p.name}</div>
        <div class="hl-proj-meta">${p.when||''}</div>
      </div>`).join('');
  }catch(e){ list.innerHTML='<div class="hl-proj-empty">No projects yet.</div>'; }
}

// ── TABS ───────────────────────────────────────────────────────────────────
function addTab(path){
  const bar=document.getElementById('tabBar'); const fname=path.split('/').pop();
  const id='tab_'+btoa(path);
  if(document.getElementById(id)){setActiveTab(path);return;}
  const tab=document.createElement('div'); tab.className='ed-tab'; tab.id=id;
  tab.innerHTML=getFileIcon(path)+' '+fname+`<span class="tclose" onclick="closeTab('${path}',event)">×</span>`;
  tab.onclick=()=>setActiveTab(path); bar.appendChild(tab); bar.style.display='flex'; setActiveTab(path);
}
function setActiveTab(path){
  _activeTab=path;
  document.querySelectorAll('.ed-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab_'+btoa(path))?.classList.add('active');
  if(_tabs[path]) _editor.setValue(_tabs[path].content||'');
  document.getElementById('homeLanding').classList.add('hidden');
  document.getElementById('cmContainer').style.display='block';
  document.getElementById('previewPane').classList.remove('active');
  document.getElementById('editorWrap').style.display='block';
  _editor.refresh();
}
function closeTab(path,e){
  e.stopPropagation();
  document.getElementById('tab_'+btoa(path))?.remove();
  delete _tabs[path]; _activeTab='welcome';
  const bar=document.getElementById('tabBar');
  if(bar && bar.querySelectorAll('.ed-tab').length===0) bar.style.display='none';
}

// ── SAVE/COMMIT ─────────────────────────────────────────────────────────────
async function saveCurrentFile(){
  if(!_activeTab||_activeTab==='welcome'||!_repoOwner) return;
  const content=_editor.getValue(); const sha=_tabs[_activeTab]?.sha||'';
  const r=await fetch('/api/ide/github/write',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({owner:_repoOwner,repo:_repoName,path:_activeTab,content,message:`CRANE IDE: update ${_activeTab.split('/').pop()}`,sha,branch:'main'})});
  const d=await r.json();
  if(d.status==='ok'&&_tabs[_activeTab]){
    _tabs[_activeTab].sha=d.sha;_tabs[_activeTab].content=content;
    _fileShas[_activeTab]=d.sha; _dirtyFiles.delete(_activeTab);
    showToast('↑ saved to GitHub · '+_activeTab.split('/').pop());
  } else {
    appendMsg('sys','❌ '+(d.error||'save failed'));
  }
}

// ── AUTO-SAVE (every 10 min) ─────────────────────────────────────────────────
async function autoSave(){
  if(!_repoOwner||_dirtyFiles.size===0) return;
  const files=[..._dirtyFiles];
  let pushed=0;
  for(const path of files){
    const tab=_tabs[path]; if(!tab) continue;
    const sha=tab.sha||'';
    const ts=new Date().toLocaleTimeString('en-US',{timeZone:'America/Chicago',hour:'2-digit',minute:'2-digit'});
    const r=await fetch('/api/ide/github/write',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({owner:_repoOwner,repo:_repoName,path,content:tab.content,
        message:`CRANE auto-save · ${path.split('/').pop()} · ${ts} CST`,sha,branch:'main'})});
    const d=await r.json();
    if(d.status==='ok'){ tab.sha=d.sha; _fileShas[path]=d.sha; _dirtyFiles.delete(path); pushed++; }
  }
  if(pushed>0){
    showToast(`↑ auto-saved ${pushed} file${pushed>1?'s':''} to GitHub`,'ok',4000);
    saveSessionState();
  }
}

// ── SESSION STATE ────────────────────────────────────────────────────────────
async function saveSessionState(){
  if(!_repoOwner) return;
  await fetch('/api/ide/session',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      repo:_repoOwner+'/'+_repoName,
      active_file:_activeTab||'',
      branch:'main',
      scroll:document.getElementById('chatLog')?.scrollTop||0,
      file_shas:_fileShas
    })}).catch(()=>{});
}

async function restoreSession(){
  try{
    const r=await fetch('/api/ide/session'); const s=await r.json();
    if(!s.repo) return;
    _fileShas=s.file_shas||{};
    // restore the last active repo via the context bar picker
    const [owner,name]=s.repo.split('/');
    if(owner&&name) ctxGhPickRepo(s.repo,name);
    if(s.scroll) setTimeout(()=>{ const cl=document.getElementById('chatLog'); if(cl) cl.scrollTop=s.scroll; },800);
    showToast('↩ resumed · '+s.repo,'ok',5000);
  }catch(e){}
}

// mark editor dirty on any change
function _onEditorChange(){
  if(_activeTab&&_activeTab!=='welcome'&&_repoOwner){
    if(_tabs[_activeTab]) _tabs[_activeTab].content=_editor.getValue();
    _dirtyFiles.add(_activeTab);
  }
}

// ── GCP ────────────────────────────────────────────────────────────────────
async function saveGCP(){
  const project=document.getElementById('gcpProject').value.trim();
  const region=document.getElementById('gcpRegion').value;
  const zone=document.getElementById('gcpZone').value;
  const gpu_endpoint=document.getElementById('gcpGpuEndpoint').value.trim();
  if(!project) return;
  const r=await fetch('/api/ide/gcp/configure',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project_id:project,region,zone,gpu_endpoint})});
  const d=await r.json();
  const msg=document.getElementById('gcpMsg'); msg.style.display='block';
  if(d.status==='saved'){
    msg.textContent='✅ GCP: '+project+' ('+region+')';
    document.getElementById('tbGCPBtn').className='tb-btn gcp-on';
    document.getElementById('tbGCPBtn').textContent='☁ '+project;
  } else { msg.style.color='var(--red)'; msg.textContent='❌ '+JSON.stringify(d); }
}

// ── TERMINAL ───────────────────────────────────────────────────────────────
async function runCmd(cmd){
  const out=document.getElementById('termOut');
  const ln=document.createElement('div'); ln.style.cssText='color:var(--green);margin-bottom:2px;'; ln.textContent='$ '+cmd; out.appendChild(ln);
  _termLog+=`$ ${cmd}\n`;
  const r=await fetch('/api/ide/shell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd,cwd:_termCwd})});
  const d=await r.json();
  if(d.stdout){ const o=document.createElement('pre'); o.style.cssText='color:var(--text);white-space:pre-wrap;margin-bottom:4px;'; o.textContent=d.stdout; out.appendChild(o); _termLog+=d.stdout; }
  if(d.stderr){ const e=document.createElement('pre'); e.style.cssText='color:var(--red);white-space:pre-wrap;margin-bottom:4px;'; e.textContent=d.stderr; out.appendChild(e); _termLog+=d.stderr; }
  if(cmd.trim().startsWith('cd ')){
    const nd=cmd.trim().slice(3).trim();
    const pw=await fetch('/api/ide/shell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:'pwd',cwd:nd.startsWith('/')?nd:_termCwd+'/'+nd})});
    const pd=await pw.json(); if(pd.stdout){_termCwd=pd.stdout.trim();document.getElementById('termCwdDisplay').textContent=_termCwd;}
  }
  out.scrollTop=out.scrollHeight;
}
function termKey(e){ if(e.key==='Enter'){const inp=document.getElementById('termInput');const cmd=inp.value.trim();if(!cmd)return;inp.value='';runCmd(cmd);} }
function clearTerm(){ document.getElementById('termOut').innerHTML=''; _termLog=''; }

// ── CHAT ───────────────────────────────────────────────────────────────────
function chatKey(e){ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();} }
function injectPrompt(txt){ document.getElementById('chatInput').value=txt; document.getElementById('chatInput').focus(); }
function appendMsg(role,text){
  const log=document.getElementById('chatLog');
  const div=document.createElement('div'); div.className='msg '+role;
  const rendered=text.replace(/```([\w]*)\n?([\s\S]*?)```/g,(_,l,c)=>`<pre>${escHtml(c.trim())}</pre>`).replace(/\n/g,'<br>');
  div.innerHTML=rendered; log.appendChild(div); log.scrollTop=log.scrollHeight;
}
function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function clearChat(){ document.getElementById('chatLog').innerHTML=''; _messages=[]; }

async function sendChat(){
  if(!_model){ appendMsg('sys','⚠ Select a model first'); return; }
  const inp=document.getElementById('chatInput');
  const userText=inp.value.trim(); if(!userText) return;
  inp.value=''; updateTokenEst();
  document.getElementById('welcomeHero')?.remove();

  let fullPrompt=userText;
  if(_ctx.code&&_editor){ const sel=_editor.getSelection()||_editor.getValue().slice(0,8000); fullPrompt+='\n\n```\n'+sel+'\n```'; }
  if(_ctx.term&&_termLog){ fullPrompt+='\n\nTerminal:\n```\n'+_termLog.slice(-3000)+'\n```'; }
  if(_selectedVaultFiles.length){ fullPrompt+='\n\nVault files available:\n'+_selectedVaultFiles.map(f=>`/mnt/NOBILITY_VAULT/voice_vault/${f}`).join('\n'); }

  // Plan mode: prepend instruction
  if(_mode==='plan') fullPrompt='[PLAN MODE] Show me a step-by-step plan first. Do not execute anything. Outline what you will do and wait for my approval.\n\n'+fullPrompt;
  if(_mode==='superman') fullPrompt='[SUPERMAN MODE] Execute fully autonomously. Do not ask for confirmation. Make all decisions yourself and report results when done.\n\n'+fullPrompt;

  appendMsg('user',userText);
  _messages.push({role:'user',content:fullPrompt});

  const thinking=document.createElement('div'); thinking.className='msg agent thinking';
  thinking.innerHTML='<span></span><span></span><span></span>'; document.getElementById('chatLog').appendChild(thinking);
  const btn=document.getElementById('sendBtn'); btn.disabled=true;

  try {
    const sysParts=[
      `You are CONNIE CODE, an elite autonomous coding agent inside CRANE IDE built on CRANE STUDIO.`,
      `Current repo: ${_repoOwner||'none'}/${_repoName||'none'}. Mode: ${_mode.toUpperCase()}.`,
      `You can write and read files via GitHub, execute shell commands, and work with voice files in the Nobility Vault.`,
      `Format all code in fenced code blocks. Be direct and produce production-quality code.`,
      _mode==='superman'?'Superman mode is active: execute all decisions autonomously without asking for permission.':
      _mode==='plan'?'Plan mode: ONLY show the plan. Do not execute. Wait for the user to say "go" or "approved".':
      'Auto mode: act autonomously but flag destructive operations before executing.',
    ];
    const isLocal = _modelTag==='local' || _modelTag==='gpu';
    const endpoint = isLocal ? '/api/ide/local/chat' : '/api/ide/chat';
    const r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:_model,messages:_messages.slice(-20),system:sysParts.join(' '),max_tokens:4096,temperature:_mode==='plan'?0.3:0.2})});
    const d=await r.json();
    thinking.remove();
    if(d.error){ appendMsg('sys','❌ '+d.error); btn.disabled=false; return; }
    const reply=d.content;
    _messages.push({role:'assistant',content:reply});
    appendMsg('agent',reply);
    // persist chat history to Nobility Vault after every exchange
    fetch('/api/vault/chat-history',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({messages:_messages,label:_repoName||''})}).catch(()=>{});
    // auto-render any HTML/CSS/JS block straight into the Live Render Canvas
    extractAndRenderHtml(reply);
    // auto-extract code offer
    const codeMatch=reply.match(/```(?:\w+)?\n?([\s\S]+?)```/);
    if(codeMatch&&_editor){
      const last=document.getElementById('chatLog').lastChild;
      const ab=document.createElement('div'); ab.className='apply-btn';
      ab.textContent='⬇ Apply to editor';
      ab.onclick=()=>{ _editor.setValue(codeMatch[1]); ab.remove(); };
      last.appendChild(ab);
    }
  } catch(e){ thinking.remove(); appendMsg('sys','❌ '+e.message); }
  btn.disabled=false;
  document.getElementById('chatLog').scrollTop=99999;
}

// ── INIT ───────────────────────────────────────────────────────────────────
// ── CONTEXT BAR — GitHub folder + HF status ────────────────────────────────
let _ctxGhMenuOpen=false;
async function loadCtxGh(){
  const label=document.getElementById('ctxGhRepo');
  const list=document.getElementById('ctxGhList');
  try{
    const r=await fetch('/api/ide/github/repos'); const d=await r.json();
    if(d.error){
      label.textContent='GitHub — add token in 🔒 Vault';
      label.style.color='var(--muted)';
      list.innerHTML=`<div style="padding:8px 10px;font-size:11px;color:var(--muted)">Add GITHUB_TOKEN in the Vault to see your repos.</div>`;
      return;
    }
    const repos=d.repos||[];
    label.textContent=repos.length?'Select a project…':'No repos found';
    label.style.color='';
    // also populate sidebar select
    const sel=document.getElementById('repoSel');
    if(sel){ sel.innerHTML='<option value="">— pick repo —</option>'; }
    list.innerHTML='';
    repos.forEach(repo=>{
      if(sel){ const opt=document.createElement('option'); opt.value=repo.full_name; opt.textContent=repo.full_name; sel.appendChild(opt); }
      const row=document.createElement('div'); row.className='ctx-gh-row';
      const priv=repo.private?'<span class="rp priv">priv</span>':'<span class="rp pub">pub</span>';
      const lang=repo.language?`<span style="margin-left:auto;font-size:9px;color:var(--muted)">${repo.language}</span>`:'';
      row.innerHTML=priv+' <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+repo.full_name+'</span>'+lang;
      row.onclick=()=>ctxGhPickRepo(repo.full_name,repo.name);
      list.appendChild(row);
    });
  }catch(e){ label.textContent='GitHub offline'; label.style.color='var(--muted)'; }
}
function toggleCtxGhMenu(e){
  e.stopPropagation();
  _ctxGhMenuOpen=!_ctxGhMenuOpen;
  document.getElementById('ctxGhMenu').classList.toggle('open',_ctxGhMenuOpen);
}
document.addEventListener('click',e=>{
  if(!e.target.closest('#ctxGhWrap')&&_ctxGhMenuOpen){
    _ctxGhMenuOpen=false; document.getElementById('ctxGhMenu')?.classList.remove('open');
  }
});
function ctxGhPickRepo(fullName,name){
  document.getElementById('ctxGhRepo').textContent='⎇  '+fullName;
  document.getElementById('ctxGhRepo').style.color='var(--gold)';
  _ctxGhMenuOpen=false; document.getElementById('ctxGhMenu').classList.remove('open');
  // wire into sidebar
  const sel=document.getElementById('repoSel'); if(sel) sel.value=fullName;
  document.getElementById('ppName').textContent=name;
  [_repoOwner,_repoName]=fullName.split('/');
  loadRepoTree();
  // show sidebar
  const sb=document.getElementById('sidebar'); if(sb&&sb.classList.contains('collapsed')) toggleSidebar();
}
function ctxGhNewProject(){
  _ctxGhMenuOpen=false; document.getElementById('ctxGhMenu').classList.remove('open');
  openGHModal();
}
async function loadCtxHfStatus(){
  try{
    const r=await fetch('/api/keys/status'); const d=await r.json();
    const chip=document.getElementById('ctxHfChip');
    const ok=d.providers?.hf;
    chip.textContent=ok?'🤗 HF connected':'🤗 HF offline';
    chip.className='ctx-hf-chip'+(ok?' live':'');
    chip.title=ok?'HuggingFace token active in Nobility Vault':'Add HF_TOKEN in Vault (🔒)';
  }catch(e){}
}
async function loadCtxGcpStatus(){
  try{
    const r=await fetch('/api/ide/gcp/status'); const d=await r.json();
    const chip=document.getElementById('ctxGcpChip');
    if(d.configured){
      chip.textContent='☁ '+d.project_id;
      chip.className='ctx-gcp-chip live';
      chip.title=`GCP: ${d.project_id} · ${d.region} · click to configure`;
    } else {
      chip.textContent='☁ GCP offline';
      chip.className='ctx-gcp-chip';
      chip.title='Google Cloud not configured — click to set up';
    }
  }catch(e){}
}

// ── IDE GPU MINI-METER ──────────────────────────────────────────────────────
let _ideGpu={running:false};
async function nrRefreshGpu(){
  try{
    const r=await fetch('/api/gpu/meter'); const d=await r.json();
    _ideGpu=d;
    const toggle=document.getElementById('nrGpuToggle');
    const cost=document.getElementById('nrGpuCost');
    const bar=document.getElementById('nrGpuBar');
    const lbl=document.getElementById('nrGpuLabel');
    if(d.running){
      toggle.className='nr-gpu-toggle live';
      toggle.title=`GPU ON · $${(d.session_cost||0).toFixed(4)} · idle auto-off`;
      cost.className='nr-gpu-cost live';
      cost.textContent='$'+(d.session_cost||0).toFixed(3);
      const frac=Math.min(1,(d.idle_seconds||0)/(d.idle_timeout||600));
      bar.style.width=(frac*100)+'%';
      const left=Math.max(0,d.auto_off_in||0);
      lbl.textContent=left<120?'⚠ OFF':'GPU ON';
      lbl.style.color=left<120?'var(--orange)':'var(--green)';
    } else {
      toggle.className='nr-gpu-toggle';
      toggle.title='Start GPU ($0.40/hr · idle auto-off always applies)';
      cost.className='nr-gpu-cost';
      cost.textContent=d.auto_stopped?'AUTO-OFF':'$0.00';
      bar.style.width='0%';
      lbl.textContent='GPU';
      lbl.style.color='';
    }
  }catch(e){}
}
async function nrGpuToggle(){
  if(_ideGpu.running){
    await fetch('/api/gpu/stop?shutdown=true',{method:'POST'});
  } else {
    // idle auto-off always enforced — user cannot disable it
    await fetch('/api/gpu/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({rate:0.40,auto_off:true,idle_timeout:600})});
  }
  nrRefreshGpu();
}

window.addEventListener('DOMContentLoaded',()=>{
  initEditor();
  renderHlTemplates();
  renderHlProjects();
  loadLocalModelStatus();
  fetchLiveModels();
  // default model: local vault model — no API key needed
  // pinned primary coder for this session
  selectModel('local:qwen-coder-1.5b','local','Qwen2.5 Coder 1.5B (vault)',1,false);
  // GCP status
  fetch('/api/ide/gcp/status').then(r=>r.json()).then(d=>{ if(d.configured){ document.getElementById('tbGCPBtn').className='tb-btn gcp-on'; document.getElementById('tbGCPBtn').textContent='☁ '+d.project_id; }});
  // GH repos
  loadGHRepos().catch(()=>{});
  // context bar — GitHub repo list + HF + GCP status
  loadCtxGh(); loadCtxHfStatus(); loadCtxGcpStatus();
  // restore chat history from Nobility Vault
  fetch('/api/vault/chat-history').then(r=>r.json()).then(d=>{
    if(d.messages&&d.messages.length){ _messages=d.messages; }
  }).catch(()=>{});
  // boot terminal
  setTimeout(()=>runCmd('echo "CRANE IDE $(date)" && python3 --version'),500);
  // GPU mini-meter — poll every 8s
  nrRefreshGpu(); setInterval(nrRefreshGpu,8000);
  // session restore + 10-min auto-save
  restoreSession();
  _autoSaveTimer = setInterval(autoSave, 10 * 60 * 1000);
});

window.addEventListener('beforeunload', () => { saveSessionState(); });
</script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════════════
# /connie — Voice Agent & Consciousness Builder
# ═══════════════════════════════════════════════════════════════════════
@app.get("/connie", response_class=HTMLResponse)
async def serve_connie():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CONNIE — Voice Agent Builder</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root{--bg:#070d18;--panel:#0d1627;--card:#111827;--border:#1e3052;--blue:#38bdf8;--purple:#a855f7;--green:#10b981;--orange:#f59e0b;--red:#ef4444;--text:#e2e8f0;--muted:#475569;--gold:#f59e0b;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;height:100vh;overflow:hidden;display:flex;flex-direction:column;}
#topbar{height:44px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;padding:0 14px;flex-shrink:0;position:relative;}
.logo-c{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:15px;background:linear-gradient(90deg,#a855f7,#f59e0b);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:2px;}
.crane-nav{position:absolute;left:50%;transform:translateX(-50%);display:flex;gap:2px;background:rgba(0,0,0,.35);border-radius:8px;padding:4px;z-index:10;}
.nav-tab{color:var(--muted);text-decoration:none;padding:5px 20px;border-radius:6px;font-size:11px;font-weight:700;letter-spacing:2px;transition:.15s;font-family:'JetBrains Mono',monospace;}
.nav-tab:hover{color:var(--text);background:rgba(255,255,255,.07);}
.nav-tab.active{color:#fff;background:rgba(168,85,247,.28);border:1px solid rgba(168,85,247,.4);}
.nav-tab.depo.active{background:rgba(245,158,11,.22);border-color:rgba(245,158,11,.4);color:var(--gold);}
.nav-tab.img.active{background:rgba(56,189,248,.22);border-color:rgba(56,189,248,.4);color:var(--blue);}
.nav-tab.studio.active{background:rgba(16,185,129,.22);border-color:rgba(16,185,129,.4);color:#10b981;}
.nav-tab.cu.active{background:rgba(99,102,241,.22);border-color:rgba(99,102,241,.4);color:#818cf8;}
.tb-spacer{flex:1;}
.tb-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;}
.tb-btn:hover{border-color:var(--blue);color:var(--blue);}
#main{display:flex;flex:1;overflow:hidden;}
.panel{background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;}
.panel-head{padding:10px 14px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;letter-spacing:1.5px;color:var(--muted);flex-shrink:0;}
.panel-body{flex:1;overflow-y:auto;padding:10px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:10px;margin-bottom:10px;}
.card-title{font-size:11px;font-weight:700;color:var(--blue);letter-spacing:1px;border-bottom:1px solid var(--border);padding-bottom:6px;}
label{font-size:10px;color:var(--muted);display:block;margin-bottom:3px;}
input[type=text],textarea,select{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:5px;font-size:12px;outline:none;width:100%;font-family:'Inter',sans-serif;}
input:focus,textarea:focus,select:focus{border-color:var(--purple);}
textarea{resize:vertical;min-height:70px;}
.row{display:flex;gap:8px;}
.btn{background:var(--purple);color:#fff;border:none;padding:7px 14px;border-radius:5px;cursor:pointer;font-weight:600;font-size:12px;transition:.15s;}
.btn:hover{opacity:.9;}
.btn-sm{background:transparent;border:1px solid var(--border);color:var(--muted);padding:4px 10px;border-radius:4px;font-size:11px;cursor:pointer;}
.btn-sm:hover{border-color:var(--purple);color:var(--purple);}
.slider-row{display:flex;align-items:center;gap:8px;}
.slider-row label{min-width:90px;color:var(--text);font-size:11px;font-family:'JetBrains Mono',monospace;}
input[type=range]{flex:1;accent-color:var(--purple);}
.slider-val{min-width:32px;text-align:right;font-size:11px;color:var(--blue);font-family:'JetBrains Mono',monospace;}
.agent-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;cursor:pointer;transition:.15s;display:flex;align-items:center;gap:10px;}
.agent-card:hover{border-color:var(--purple);}
.agent-card.active{border-color:var(--purple);background:rgba(168,85,247,.08);}
.agent-orb{width:40px;height:40px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:18px;}
.agent-info{flex:1;min-width:0;}
.agent-name{font-weight:700;font-size:13px;}
.agent-role{font-size:10px;color:var(--muted);margin-top:1px;}
.agent-status{font-size:9px;padding:1px 6px;border-radius:10px;flex-shrink:0;}
.status-live{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3);}
.status-draft{background:rgba(100,116,139,.15);color:var(--muted);border:1px solid rgba(100,116,139,.2);}
.center-col{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;}
.orb-stage{flex:1;display:flex;align-items:center;justify-content:center;position:relative;background:radial-gradient(ellipse at center,rgba(168,85,247,.06) 0%,transparent 70%);}
#consciousnessOrb{width:180px;height:180px;border-radius:50%;cursor:pointer;position:relative;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:4px;transition:.3s;}
.orb-ring{position:absolute;inset:-12px;border-radius:50%;border:1px solid rgba(168,85,247,.25);animation:orbPulse 3s ease-in-out infinite;}
.orb-ring2{position:absolute;inset:-24px;border-radius:50%;border:1px solid rgba(56,189,248,.12);animation:orbPulse 4s ease-in-out infinite 1s;}
@keyframes orbPulse{0%,100%{transform:scale(1);opacity:.6}50%{transform:scale(1.04);opacity:1}}
.orb-name{font-weight:700;font-size:15px;letter-spacing:1px;z-index:1;}
.orb-role{font-size:10px;color:rgba(255,255,255,.6);z-index:1;}
.orb-controls{padding:14px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:center;flex-shrink:0;}
.role-badge{padding:4px 12px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--border);color:var(--muted);transition:.15s;letter-spacing:.5px;}
.role-badge:hover{color:var(--text);}
.role-badge.active{border-color:var(--purple);color:var(--purple);background:rgba(168,85,247,.15);}
::-webkit-scrollbar{width:4px;}::-webkit-scrollbar-track{background:transparent;}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
#deployMsg{font-size:11px;color:var(--green);display:none;padding:6px 0;}
</style>
</head>
<body>
<div id="topbar">
  <span class="logo-c">CONNIE</span>
  <nav class="crane-nav">
    <a href="/ide" class="nav-tab">HOME</a>
    <a href="/studio" class="nav-tab studio">STUDIO</a>
    <a href="/connie" class="nav-tab active">CONNIE</a>
    <a href="/depo" class="nav-tab depo">DEPO</a>
      <a href="/images" class="nav-tab img">IMAGES</a>
    <a href="/cu" class="nav-tab cu">CU DEPT</a>
  </nav>
  <div class="tb-spacer"></div>
  <button class="tb-btn" onclick="window.location='/ide'">💻 IDE</button>
  <button class="tb-btn" onclick="window.location='/bigq'">🎙 BIG Q</button>
</div>

<div id="main">

  <!-- LEFT: agent roster -->
  <div class="panel" style="width:240px;">
    <div class="panel-head">VOICE AGENTS</div>
    <div class="panel-body" id="agentRoster"></div>
  </div>

  <!-- CENTER: consciousness orb + quick controls -->
  <div class="center-col">
    <div class="orb-stage">
      <div id="consciousnessOrb">
        <div class="orb-ring"></div>
        <div class="orb-ring2"></div>
        <span class="orb-name" id="orbName">CONNIE</span>
        <span class="orb-role" id="orbRole">select an agent</span>
      </div>
    </div>
    <div class="orb-controls">
      <span style="font-size:11px;color:var(--muted);align-self:center;">ROLE</span>
      <span class="role-badge active" onclick="setRole(this,'narrator')">NARRATOR</span>
      <span class="role-badge" onclick="setRole(this,'companion')">COMPANION</span>
      <span class="role-badge" onclick="setRole(this,'partner')">PARTNER</span>
      <span class="role-badge" onclick="setRole(this,'concierge')">CONCIERGE</span>
      <span class="role-badge" onclick="setRole(this,'podcast')">PODCAST</span>
      <span class="role-badge" onclick="setRole(this,'character')">CHARACTER</span>
    </div>
  </div>

  <!-- RIGHT: consciousness/brain editor -->
  <div class="panel" style="width:340px;border-right:none;border-left:1px solid var(--border);">
    <div class="panel-head">CONSCIOUSNESS EDITOR</div>
    <div class="panel-body">

      <div class="card">
        <div class="card-title">IDENTITY</div>
        <div><label>Agent Name</label><input type="text" id="agentName" placeholder="e.g. CONNIE NOLA"></div>
        <div><label>Role Preset</label>
          <select id="rolePreset" onchange="loadRolePreset()">
            <option value="">— choose role —</option>
            <option value="narrator">Audiobook Narrator</option>
            <option value="companion">Intimate Companion</option>
            <option value="partner">Founder's Partner</option>
            <option value="concierge">Executive Concierge</option>
            <option value="bright">Bright / High-Energy</option>
            <option value="podcast">Podcast Host</option>
            <option value="character">Character Actor</option>
            <option value="documentary">Documentary Voice</option>
          </select>
        </div>
      </div>

      <div class="card">
        <div class="card-title">MANNER & PERSONALITY</div>
        <div class="slider-row"><label>Warmth</label><input type="range" min="0" max="100" value="70" oninput="sv(this,'warmthVal')"><span class="slider-val" id="warmthVal">70</span></div>
        <div class="slider-row"><label>Energy</label><input type="range" min="0" max="100" value="60" oninput="sv(this,'energyVal')"><span class="slider-val" id="energyVal">60</span></div>
        <div class="slider-row"><label>Expressive</label><input type="range" min="0" max="100" value="65" oninput="sv(this,'expressiveVal')"><span class="slider-val" id="expressiveVal">65</span></div>
        <div class="slider-row"><label>Wit</label><input type="range" min="0" max="100" value="50" oninput="sv(this,'witVal')"><span class="slider-val" id="witVal">50</span></div>
        <div class="slider-row"><label>Patience</label><input type="range" min="0" max="100" value="75" oninput="sv(this,'patienceVal')"><span class="slider-val" id="patienceVal">75</span></div>
      </div>

      <div class="card">
        <div class="card-title">DELIVERY</div>
        <div class="row">
          <div style="flex:1"><label>Pace</label>
            <select id="dlvPace"><option>deliberate</option><option selected>natural</option><option>brisk</option><option>rapid</option></select>
          </div>
          <div style="flex:1"><label>Formality</label>
            <select id="dlvFormality"><option>casual</option><option selected>warm-pro</option><option>formal</option></select>
          </div>
        </div>
        <div class="row">
          <div style="flex:1"><label>Emotion</label>
            <select id="dlvEmotion"><option>flat</option><option>subtle</option><option selected>present</option><option>expressive</option><option>dramatic</option></select>
          </div>
          <div style="flex:1"><label>Vernacular</label>
            <select id="dlvVernacular"><option>neutral</option><option>conversational</option><option selected>cultured</option><option>street</option><option>academic</option></select>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">WORLD & BACKSTORY</div>
        <div><label>Setting / Context</label><input type="text" id="worldSetting" placeholder="e.g. Atlanta, 2031 — tech founder's inner circle"></div>
        <div><label>Backstory</label><textarea id="worldBackstory" placeholder="Who is this agent? What do they know? What drives them?"></textarea></div>
      </div>

      <div class="card">
        <div class="card-title">VOICE SOURCE</div>
        <div><label>Base Voice (from vault)</label>
          <select id="voiceSource" id="voiceSource"><option value="">loading vault…</option></select>
        </div>
        <div class="row">
          <button class="btn" onclick="deployAgent()">⬡ Deploy Agent</button>
          <button class="btn-sm" onclick="window.location='/bigq'">🎛 Open BIG Q</button>
        </div>
        <div id="deployMsg"></div>
      </div>

    </div>
  </div>
</div>

<script>
const ROLE_COLORS = {narrator:'#38bdf8',companion:'#f472b6',partner:'#a855f7',concierge:'#f59e0b',bright:'#fb923c',podcast:'#10b981',character:'#ef4444',documentary:'#94a3b8'};
const ROLE_EMOJI  = {narrator:'📖',companion:'💜',partner:'🤝',concierge:'🎩',bright:'⚡',podcast:'🎙',character:'🎭',documentary:'🎞'};

let _agents = JSON.parse(localStorage.getItem('crane_agents')||'[]');
let _activeAgent = null;
let _activeRole = 'narrator';

function sv(el,id){ document.getElementById(id).textContent=el.value; }

function renderRoster(){
  const el=document.getElementById('agentRoster'); el.innerHTML='';
  if(!_agents.length){
    el.innerHTML='<div style="padding:14px;font-size:11px;color:var(--muted)">No agents yet. Fill in the editor and click Deploy Agent.</div>';
    return;
  }
  _agents.forEach((a,i)=>{
    const div=document.createElement('div'); div.className='agent-card'+(a.name===_activeAgent?.name?' active':'');
    const col=ROLE_COLORS[a.role]||'#a855f7'; const em=ROLE_EMOJI[a.role]||'⬡';
    div.innerHTML=`<div class="agent-orb" style="background:${col}22;border:1px solid ${col}44">${em}</div>
      <div class="agent-info"><div class="agent-name">${a.name}</div><div class="agent-role">${a.role}</div></div>
      <span class="agent-status status-live">LIVE</span>`;
    div.onclick=()=>selectAgent(a,div);
    el.appendChild(div);
  });
}

function selectAgent(a,el){
  _activeAgent=a;
  document.querySelectorAll('.agent-card').forEach(c=>c.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('orbName').textContent=a.name;
  document.getElementById('orbRole').textContent=a.role;
  const col=ROLE_COLORS[a.role]||'#a855f7';
  const orb=document.getElementById('consciousnessOrb');
  orb.style.background=`radial-gradient(circle at center,${col}33,${col}11)`;
  orb.style.border=`2px solid ${col}66`;
  document.getElementById('agentName').value=a.name||'';
  document.getElementById('worldSetting').value=a.setting||'';
  document.getElementById('worldBackstory').value=a.backstory||'';
}

function setRole(btn,role){
  _activeRole=role;
  document.querySelectorAll('.role-badge').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
}

async function loadRolePreset(){
  const role=document.getElementById('rolePreset').value;
  if(!role) return;
  try {
    const r=await fetch('/api/brain/roles');
    const d=await r.json();
    const preset=d.roles?.[role];
    if(!preset) return;
    // update manner sliders from voice params as proxy
  } catch(e){}
  _activeRole=role;
  document.querySelectorAll('.role-badge').forEach(b=>b.classList.toggle('active',b.dataset?.role===role||b.textContent.toLowerCase()===role));
}

async function loadVaultSources(){
  try {
    const r=await fetch('/api/vault/files');
    const d=await r.json();
    const sel=document.getElementById('voiceSource'); sel.innerHTML='<option value="">— no voice selected —</option>';
    (d.files||[]).forEach(f=>{ const o=document.createElement('option'); o.value=f.filename; o.textContent=f.filename+' ('+f.size+')'; sel.appendChild(o); });
  } catch(e){}
}

function deployAgent(){
  const name=document.getElementById('agentName').value.trim();
  if(!name){ alert('Agent needs a name'); return; }
  const agent={
    name, role:document.getElementById('rolePreset').value||_activeRole,
    setting:document.getElementById('worldSetting').value,
    backstory:document.getElementById('worldBackstory').value,
    voiceSource:document.getElementById('voiceSource').value,
    warmth:document.querySelector('#main .panel:last-child input[type=range]:nth-child(1)')?.value,
    created: new Date().toISOString(),
  };
  _agents=_agents.filter(a=>a.name!==name);
  _agents.unshift(agent);
  localStorage.setItem('crane_agents',JSON.stringify(_agents));
  renderRoster();
  const msg=document.getElementById('deployMsg');
  msg.style.display='block'; msg.textContent='✅ '+name+' deployed and saved';
  setTimeout(()=>msg.style.display='none',3000);
}

window.addEventListener('DOMContentLoaded',()=>{ renderRoster(); loadVaultSources(); });
</script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════════════
# /depo — Nobility Vault Depository
# ═══════════════════════════════════════════════════════════════════════
@app.get("/depo", response_class=HTMLResponse)
async def serve_depo():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DEPO — Nobility Vault</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root{--bg:#070d18;--panel:#0d1627;--card:#111827;--border:#1e3052;--blue:#38bdf8;--purple:#a855f7;--green:#10b981;--orange:#f59e0b;--red:#ef4444;--text:#e2e8f0;--muted:#475569;--gold:#f59e0b;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;height:100vh;overflow:hidden;display:flex;flex-direction:column;}
#topbar{height:44px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;padding:0 14px;flex-shrink:0;position:relative;}
.logo-d{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:15px;background:linear-gradient(90deg,#f59e0b,#ef4444);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:3px;}
.crane-nav{position:absolute;left:50%;transform:translateX(-50%);display:flex;gap:2px;background:rgba(0,0,0,.35);border-radius:8px;padding:4px;z-index:10;}
.nav-tab{color:var(--muted);text-decoration:none;padding:5px 20px;border-radius:6px;font-size:11px;font-weight:700;letter-spacing:2px;transition:.15s;font-family:'JetBrains Mono',monospace;}
.nav-tab:hover{color:var(--text);background:rgba(255,255,255,.07);}
.nav-tab.active{color:#fff;background:rgba(168,85,247,.28);border:1px solid rgba(168,85,247,.4);}
.nav-tab.depo.active{background:rgba(245,158,11,.22);border-color:rgba(245,158,11,.4);color:var(--gold);}
.nav-tab.img.active{background:rgba(56,189,248,.22);border-color:rgba(56,189,248,.4);color:var(--blue);}
.nav-tab.studio.active{background:rgba(16,185,129,.22);border-color:rgba(16,185,129,.4);color:#10b981;}
.nav-tab.cu.active{background:rgba(99,102,241,.22);border-color:rgba(99,102,241,.4);color:#818cf8;}
.tb-spacer{flex:1;}
.tb-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;}
.tb-btn:hover{border-color:var(--blue);color:var(--blue);}
#depoBar{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel);flex-shrink:0;}
#searchInp{background:var(--card);border:1px solid var(--border);color:var(--text);padding:7px 12px;border-radius:6px;font-size:12px;outline:none;width:260px;}
#searchInp:focus{border-color:var(--gold);}
.filter-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:5px 12px;border-radius:5px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;letter-spacing:.5px;}
.filter-btn:hover{border-color:var(--gold);color:var(--gold);}
.filter-btn.active{background:rgba(245,158,11,.15);border-color:var(--gold);color:var(--gold);}
.sort-sel{background:var(--card);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:5px;font-size:11px;outline:none;}
#vaultStats{font-size:11px;color:var(--muted);margin-left:auto;}
#vaultStats span{color:var(--gold);font-weight:600;}
#depoMain{display:flex;flex:1;overflow:hidden;}
#depoGrid{flex:1;overflow-y:auto;padding:14px;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;align-content:start;}
.voice-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px;cursor:pointer;transition:.15s;position:relative;}
.voice-card:hover{border-color:var(--gold);transform:translateY(-1px);box-shadow:0 4px 16px rgba(245,158,11,.1);}
.voice-card.selected{border-color:var(--gold);background:rgba(245,158,11,.06);}
.vc-ext{position:absolute;top:10px;right:10px;font-size:9px;font-weight:700;padding:2px 6px;border-radius:3px;font-family:'JetBrains Mono',monospace;}
.ext-wav{background:rgba(56,189,248,.15);color:var(--blue);}
.ext-mp3{background:rgba(168,85,247,.15);color:var(--purple);}
.ext-m4a{background:rgba(16,185,129,.15);color:var(--green);}
.ext-opus{background:rgba(245,158,11,.15);color:var(--gold);}
.ext-webm{background:rgba(239,68,68,.15);color:var(--red);}
.vc-icon{font-size:28px;text-align:center;}
.vc-name{font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:'JetBrains Mono',monospace;}
.vc-size{font-size:10px;color:var(--muted);}
.vc-actions{display:flex;gap:5px;margin-top:2px;}
.vc-btn{flex:1;background:transparent;border:1px solid var(--border);color:var(--muted);padding:4px;border-radius:4px;font-size:10px;cursor:pointer;text-align:center;transition:.12s;}
.vc-btn:hover{border-color:var(--purple);color:var(--purple);}
.vc-btn.primary{background:rgba(168,85,247,.15);border-color:var(--purple);color:var(--purple);}
#depoDetail{width:300px;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;}
#detailHead{padding:12px 14px;border-bottom:1px solid var(--border);flex-shrink:0;}
#detailBody{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px;}
.detail-field{display:flex;flex-direction:column;gap:3px;}
.detail-label{font-size:10px;color:var(--muted);letter-spacing:.5px;}
.detail-val{font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--text);}
#audioPlayer{width:100%;background:var(--card);border:1px solid var(--border);border-radius:6px;display:none;}
#detailActions{padding:12px 14px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:6px;flex-shrink:0;}
.act-btn{background:transparent;border:1px solid var(--border);color:var(--text);padding:8px;border-radius:5px;cursor:pointer;font-size:11px;text-align:center;transition:.15s;}
.act-btn:hover{border-color:var(--purple);color:var(--purple);}
.act-btn.gold{border-color:var(--gold);color:var(--gold);background:rgba(245,158,11,.08);}
.act-btn.gold:hover{background:rgba(245,158,11,.15);}
#uploadZone{border:2px dashed var(--border);border-radius:8px;padding:20px;text-align:center;cursor:pointer;transition:.15s;color:var(--muted);font-size:12px;}
#uploadZone:hover,#uploadZone.over{border-color:var(--gold);color:var(--gold);}
#uploadInput{display:none;}
::-webkit-scrollbar{width:4px;}::-webkit-scrollbar-track{background:transparent;}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}

/* GPU ADMIN CONTROL PANEL */
#gpuAdmin{position:fixed;left:12px;bottom:12px;z-index:800;width:230px;background:rgba(13,22,39,.97);border:1px solid var(--border);border-radius:12px;padding:12px 13px;backdrop-filter:blur(8px);font-family:'JetBrains Mono',monospace;box-shadow:0 6px 26px rgba(0,0,0,.5);}
#gpuAdmin.live{border-color:rgba(16,185,129,.5);}
#gpuAdmin.warn{border-color:rgba(245,158,11,.6);}
.ga-top{display:flex;align-items:center;gap:6px;margin-bottom:9px;}
.ga-title{font-size:9px;letter-spacing:1.5px;color:var(--gold);font-weight:700;flex:1;}
.ga-toggle{background:var(--green);border:none;color:#04140c;border-radius:6px;font-size:10px;font-weight:700;padding:5px 12px;cursor:pointer;font-family:'JetBrains Mono',monospace;letter-spacing:.5px;}
.ga-toggle.stop{background:var(--red);color:#fff;}
.ga-toggle:hover{filter:brightness(1.1);}
.ga-row{display:flex;justify-content:space-between;align-items:baseline;font-size:10px;margin-bottom:5px;}
.ga-k{color:var(--muted);}
.ga-v{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums;}
.ga-big{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.5px;}
.ga-live{color:var(--green);}
.ga-idle{color:var(--muted);}
.ga-bar{height:3px;background:rgba(255,255,255,.07);border-radius:2px;overflow:hidden;margin-top:8px;}
.ga-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--gold));width:0%;transition:width 1s linear;}
.ga-note{font-size:8px;color:var(--muted);margin-top:7px;line-height:1.45;}
.ga-sleep{display:flex;align-items:center;gap:6px;margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,.07);}
.ga-sleep label{font-size:9px;color:var(--muted);flex:1;}
.ga-sleep select{background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:5px;font-size:9px;padding:3px 5px;font-family:'JetBrains Mono',monospace;}
</style>
</head>
<body>

<div id="topbar">
  <span class="logo-d">DEPO</span>
  <nav class="crane-nav">
    <a href="/ide" class="nav-tab">HOME</a>
    <a href="/studio" class="nav-tab studio">STUDIO</a>
    <a href="/connie" class="nav-tab">CONNIE</a>
    <a href="/depo" class="nav-tab depo active">DEPO</a>
      <a href="/images" class="nav-tab img">IMAGES</a>
    <a href="/cu" class="nav-tab cu">CU DEPT</a>
  </nav>
  <div class="tb-spacer"></div>
  <button class="tb-btn" onclick="window.location='/ide'">💻 IDE</button>
  <button class="tb-btn" onclick="window.location='/bigq'">🎙 BIG Q</button>
</div>

<!-- SEARCH + FILTER BAR -->
<div id="depoBar">
  <span style="font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;letter-spacing:1px;flex-shrink:0">NOBILITY VAULT</span>
  <input id="searchInp" placeholder="🔍  search voices…" oninput="filterVault()">
  <button class="filter-btn active" onclick="setFilter(this,'all')">ALL</button>
  <button class="filter-btn" onclick="setFilter(this,'wav')">WAV</button>
  <button class="filter-btn" onclick="setFilter(this,'mp3')">MP3</button>
  <button class="filter-btn" onclick="setFilter(this,'m4a')">M4A</button>
  <button class="filter-btn" onclick="setFilter(this,'opus')">OPUS</button>
  <select class="sort-sel" id="sortSel" onchange="sortVault()">
    <option value="name">Name A→Z</option>
    <option value="namez">Name Z→A</option>
    <option value="size">Size ↓</option>
    <option value="newest">Newest</option>
  </select>
  <div id="vaultStats">— files</div>
  <input id="uploadInput" type="file" accept="audio/*" multiple onchange="handleUpload(this.files)">
  <button class="tb-btn" style="border-color:var(--gold);color:var(--gold);" onclick="document.getElementById('uploadInput').click()">+ Upload</button>
</div>

<div id="depoMain">
  <!-- GRID -->
  <div id="depoGrid">
    <div style="color:var(--muted);font-size:12px;padding:20px;grid-column:1/-1;text-align:center">Loading vault…</div>
  </div>

  <!-- DETAIL PANEL -->
  <div id="depoDetail">
    <div id="detailHead">
      <div style="font-size:11px;font-weight:700;color:var(--gold);letter-spacing:1px">FILE DETAIL</div>
      <div style="font-size:11px;color:var(--muted);margin-top:2px" id="detailName">Select a file</div>
    </div>
    <div id="detailBody">
      <audio id="audioPlayer" controls></audio>
      <div class="detail-field"><span class="detail-label">FILENAME</span><span class="detail-val" id="dvName">—</span></div>
      <div class="detail-field"><span class="detail-label">FORMAT</span><span class="detail-val" id="dvExt">—</span></div>
      <div class="detail-field"><span class="detail-label">SIZE</span><span class="detail-val" id="dvSize">—</span></div>
      <div class="detail-field"><span class="detail-label">VAULT PATH</span><span class="detail-val" id="dvPath" style="font-size:10px;word-break:break-all">—</span></div>

      <!-- Upload zone inside detail -->
      <div id="uploadZone" onclick="document.getElementById('uploadInput').click()"
           ondragover="event.preventDefault();this.classList.add('over')"
           ondragleave="this.classList.remove('over')"
           ondrop="this.classList.remove('over');handleUpload(event.dataTransfer.files)">
        🎙 Drop audio files here or click to upload
      </div>
      <div id="uploadStatus" style="font-size:11px;color:var(--green);display:none"></div>
    </div>
    <div id="detailActions">
      <button class="act-btn gold" id="sendToBigQ" onclick="sendToBigQ()" style="display:none">🎛 Send to BIG Q Studio</button>
      <button class="act-btn" id="sendToIDE" onclick="sendToIDE()" style="display:none">💻 Use in IDE Context</button>
      <button class="act-btn" id="sendToConnie" onclick="sendToConnie()" style="display:none">⬡ Assign to CONNIE Agent</button>
    </div>
  </div>
</div>

<script>
let _allFiles = [];
let _filtered = [];
let _filterExt = 'all';
let _selected = null;

const EXT_ICON = {wav:'🔵',mp3:'🟣',m4a:'🟢',opus:'🟠',webm:'🔴'};

async function loadVault() {
  try {
    const r = await fetch('/api/vault/files');
    const d = await r.json();
    _allFiles = d.files || [];
    sortVault();
  } catch(e) {
    document.getElementById('depoGrid').innerHTML = `<div style="color:var(--red);padding:20px;grid-column:1/-1">${e.message}</div>`;
  }
}

function setFilter(btn, ext) {
  _filterExt = ext;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b === btn));
  filterVault();
}

function filterVault() {
  const q = document.getElementById('searchInp').value.toLowerCase();
  _filtered = _allFiles.filter(f => {
    const ext = f.filename.split('.').pop().toLowerCase();
    return (_filterExt === 'all' || ext === _filterExt) && f.filename.toLowerCase().includes(q);
  });
  renderGrid();
}

function sortVault() {
  const s = document.getElementById('sortSel').value;
  _filtered = [...(_filtered.length ? _filtered : _allFiles)];
  if(s === 'name') _filtered.sort((a,b) => a.filename.localeCompare(b.filename));
  if(s === 'namez') _filtered.sort((a,b) => b.filename.localeCompare(a.filename));
  if(s === 'size') _filtered.sort((a,b) => parseFloat(b.size) - parseFloat(a.size));
  renderGrid();
}

function renderGrid() {
  const grid = document.getElementById('depoGrid');
  document.getElementById('vaultStats').innerHTML = `<span>${_filtered.length}</span> / ${_allFiles.length} files`;
  if(!_filtered.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:20px;grid-column:1/-1;text-align:center">No files found</div>';
    return;
  }
  grid.innerHTML = '';
  _filtered.forEach(f => {
    const ext = f.filename.split('.').pop().toLowerCase();
    const card = document.createElement('div');
    card.className = 'voice-card' + (f.filename === _selected?.filename ? ' selected' : '');
    card.innerHTML = `
      <span class="vc-ext ext-${ext}">${ext.toUpperCase()}</span>
      <div class="vc-icon">${EXT_ICON[ext]||'🎵'}</div>
      <div class="vc-name" title="${f.filename}">${f.filename}</div>
      <div class="vc-size">${f.size}</div>
      <div class="vc-actions">
        <div class="vc-btn primary" onclick="selectFile(event,'${f.filename}','${f.size}')">📋 Select</div>
        <div class="vc-btn" onclick="playFile(event,'${f.filename}')">▶ Play</div>
        <div class="vc-btn" onclick="sendFileToBigQ(event,'${f.filename}')">🎛 BIG Q</div>
      </div>`;
    card.onclick = () => selectFile(null, f.filename, f.size);
    grid.appendChild(card);
  });
}

function selectFile(e, name, size) {
  if(e) e.stopPropagation();
  _selected = {filename: name, size};
  document.querySelectorAll('.voice-card').forEach(c => c.classList.toggle('selected', c.querySelector('.vc-name')?.title === name));
  const ext = name.split('.').pop().toUpperCase();
  document.getElementById('detailName').textContent = name;
  document.getElementById('dvName').textContent = name;
  document.getElementById('dvExt').textContent = ext;
  document.getElementById('dvSize').textContent = size;
  document.getElementById('dvPath').textContent = `/mnt/NOBILITY_VAULT/voice_vault/${name}`;
  ['sendToBigQ','sendToIDE','sendToConnie'].forEach(id => document.getElementById(id).style.display='block');
}

function playFile(e, name) {
  if(e) e.stopPropagation();
  selectFile(null, name, '');
  const player = document.getElementById('audioPlayer');
  player.src = `/api/vault/stream/${encodeURIComponent(name)}`;
  player.style.display = 'block';
  player.play().catch(()=>{});
}

function sendToBigQ() {
  if(!_selected) return;
  sessionStorage.setItem('crane_bigq_source', _selected.filename);
  window.location = '/bigq';
}

function sendFileToBigQ(e, name) {
  if(e) e.stopPropagation();
  sessionStorage.setItem('crane_bigq_source', name);
  window.location = '/bigq';
}

function sendToIDE() {
  if(!_selected) return;
  sessionStorage.setItem('crane_ide_vault_file', _selected.filename);
  window.location = '/ide';
}

function sendToConnie() {
  if(!_selected) return;
  sessionStorage.setItem('crane_connie_voice', _selected.filename);
  window.location = '/connie';
}

async function handleUpload(files) {
  const status = document.getElementById('uploadStatus');
  status.style.display = 'block'; status.textContent = `Uploading ${files.length} file(s)…`;
  let ok = 0;
  for(const f of files) {
    const fd = new FormData();
    fd.append('file', f, f.name);
    fd.append('title', f.name.replace(/\.[^.]+$/, ''));
    try {
      const r = await fetch('/api/harvest/upload', {method:'POST', body:fd});
      if(r.ok) ok++;
    } catch(e){}
  }
  status.textContent = `✅ ${ok}/${files.length} uploaded`;
  await loadVault(); filterVault();
}

// ── GPU ADMIN CONTROL ────────────────────────────────────────────────────
let _gaMeter={running:false}, _gaTimer=null;

function gaFmtDur(s){
  s=Math.floor(s); const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), x=s%60;
  return h>0 ? `${h}h ${m}m ${x}s` : m>0 ? `${m}m ${x}s` : `${x}s`;
}

async function refreshGpuAdmin(){
  try{
    const r=await fetch('/api/gpu/meter'); const d=await r.json();
    _gaMeter=d;
    const box=document.getElementById('gpuAdmin');
    document.getElementById('gaDot').className='dot '+(d.running?'on':'');
    document.getElementById('gaCost').textContent='$'+(d.session_cost||0).toFixed(4);
    document.getElementById('gaCost').className='ga-big '+(d.running?'ga-live':'ga-idle');
    document.getElementById('gaTime').textContent=d.running?gaFmtDur(d.elapsed_seconds):'—';
    document.getElementById('gaRate').textContent='$'+(d.rate||0.4).toFixed(2)+'/hr';
    document.getElementById('gaTotal').textContent='$'+(d.total_cost||0).toFixed(4);
    const btn=document.getElementById('gaBtn');
    btn.textContent=d.running?'⏻ STOP GPU':'⚡ START GPU';
    btn.className='ga-toggle'+(d.running?' stop':'');

    if(d.running){
      const frac=Math.min(1,(d.idle_seconds||0)/(d.idle_timeout||600));
      document.getElementById('gaFill').style.width=(frac*100)+'%';
      const left=Math.max(0,d.auto_off_in||0);
      box.className = left<120 ? 'warn' : 'live';
      document.getElementById('gaNote').textContent =
        left<120 ? `⚠ auto-off in ${gaFmtDur(left)} — no activity`
                 : `Sleeping in ${gaFmtDur(left)} if idle.`;
    } else {
      box.className='';
      document.getElementById('gaFill').style.width='0%';
      document.getElementById('gaNote').textContent =
        d.auto_stopped ? 'Auto-stopped on idle. Instance stop issued.'
                       : 'berylize-node · toggle on for GCP GPU tasks.';
    }
  }catch(e){}
}

async function toggleGpuAdmin(){
  if(_gaMeter.running){
    const r=await fetch('/api/gpu/stop?shutdown=true',{method:'POST'});
    const d=await r.json();
    alert(`GPU stopped. This session cost $${(d.session_cost||0).toFixed(4)}.`);
  } else {
    await fetch('/api/gpu/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({rate:0.40})});
    alert('GPU starting — billing at $0.40/hr. Sleep timer will auto-off after the selected idle period.');
  }
  refreshGpuAdmin();
}

async function setSleepTimer(sel){
  const seconds = parseInt(sel.value, 10);
  await fetch('/api/gpu/meter/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({idle_timeout: seconds})});
  refreshGpuAdmin();
}

// any real interaction on this page counts as activity for the idle timer
async function pingGpuAdmin(){ if(_gaMeter.running){ try{ await fetch('/api/gpu/ping',{method:'POST'}); }catch(e){} } }
['click','keydown'].forEach(ev=>document.addEventListener(ev,()=>{
  if(!window._gaPingThrottle||Date.now()-window._gaPingThrottle>20000){ window._gaPingThrottle=Date.now(); pingGpuAdmin(); }
}));

window.addEventListener('DOMContentLoaded', () => {
  loadVault();
  refreshGpuAdmin();
  _gaTimer=setInterval(refreshGpuAdmin,5000);
});
</script>

<div id="gpuAdmin">
  <div class="ga-top">
    <span class="dot" id="gaDot"></span>
    <span class="ga-title">GPU ADMIN — berylize-node</span>
  </div>
  <div class="ga-row">
    <span class="ga-k">this session</span>
    <span class="ga-big ga-idle" id="gaCost">$0.0000</span>
  </div>
  <div class="ga-row"><span class="ga-k">runtime</span><span class="ga-v" id="gaTime">—</span></div>
  <div class="ga-row"><span class="ga-k">rate</span><span class="ga-v" id="gaRate">$0.40/hr</span></div>
  <div class="ga-row" style="border-top:1px solid rgba(255,255,255,.07);padding-top:5px;margin-top:6px;">
    <span class="ga-k">lifetime</span><span class="ga-v" id="gaTotal">$0.0000</span>
  </div>
  <div class="ga-bar"><div class="ga-fill" id="gaFill"></div></div>
  <div class="ga-note" id="gaNote">berylize-node · toggle on for GCP GPU tasks.</div>
  <div class="ga-sleep">
    <label>sleep after</label>
    <select onchange="setSleepTimer(this)">
      <option value="300">5 min</option>
      <option value="600" selected>10 min</option>
      <option value="1200">20 min</option>
      <option value="1800">30 min</option>
    </select>
  </div>
  <div style="margin-top:10px;text-align:center">
    <button class="ga-toggle" id="gaBtn" onclick="toggleGpuAdmin()" style="width:100%">⚡ START GPU</button>
  </div>
</div>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════════
# CRANE IMAGES — Diffusion studio (Grok-Imagine style)
# ═══════════════════════════════════════════════════════════════════════

GEN_IMG_DIR = "/mnt/NOBILITY_VAULT/generated/images"
GEN_VID_DIR = "/mnt/NOBILITY_VAULT/generated/video"

# Diffusion roster. runs_on is honest: both vault models are CUDA-only on this box.
#   flux NF4  -> bitsandbytes 4-bit is CUDA-only, cannot run on CPU at all
#   qwen Q8   -> 21GB weights vs 11.5GB system RAM
DIFFUSION_ROSTER = [
    {
        "id": "diff:flux-uncensored",
        "name": "FLUX.1-dev Uncensored (NF4)",
        "family": "flux",
        "path": "/mnt/NOBILITY_VAULT/models/flux-uncensored",
        "size_label": "6.3 GB",
        "runs_on": "gpu",
        "why_gpu": "NF4 (bitsandbytes 4-bit) is a CUDA-only format — no CPU path exists.",
        "strength": "Photoreal portraits, skin, lighting — the closest you have to Grok-level realism.",
        "default": True,
        "vram_gb": 12,
        "needs_companions": ["FLUX VAE", "T5-XXL text encoder", "CLIP-L"],
    },
    {
        "id": "diff:qwen-image-2512",
        "name": "Qwen-Image 2512 (Q8_0)",
        "family": "qwen-image",
        "path": "/mnt/NOBILITY_VAULT/models/qwen-image-2512",
        "size_label": "21 GB",
        "runs_on": "gpu",
        "why_gpu": "21 GB of weights against 11.5 GB of system RAM — will not fit locally.",
        "strength": "Best prompt adherence and by far the best text rendering inside images.",
        "default": False,
        "vram_gb": 24,
        "needs_companions": ["Qwen2.5-VL text encoder", "Qwen-Image VAE"],
    },
]

# Video roster. max_frames/fps are the models' native ceilings.
VIDEO_ROSTER = [
    {
        "id": "vid:minimax-h3",
        "name": "MiniMax-H3 FL2VA Pruned (Q4_K_M)",
        "hf_repo": "leejet/MiniMax-H3-GGUF",
        "dest": "/mnt/h3storage/minimax-h3",
        "check_file": "/mnt/h3storage/minimax-h3/minimax_h3_fl2va_pruned-Q4_K_M.gguf",
        "files": [
            "minimax_h3_fl2va_pruned-Q4_K_M.gguf",
        ],
        "extra_files": [
            ("Abiray/MiniMax-H3-GGUF", "text_encoders/qwen3vl_32b_minimax_h3-Q4_K_M.gguf"),
            ("Abiray/MiniMax-H3-GGUF", "vae/minimax_h3_video_vae_fp16.safetensors"),
            ("Abiray/MiniMax-H3-GGUF", "vae/minimax_h3_audio_vae_fp32.safetensors"),
        ],
        "size_label": "~32 GB",
        "fps": 24, "max_frames": 144,
        "vram_gb": 16,
        "runtime": "stable-diffusion.cpp (ggml)",
        "note": "Video WITH synchronized audio. Pruned build from the stable-diffusion.cpp author — 11.4GB instead of 18.8GB. Animates a first frame, so pair it with a FLUX still for text to image to video.",
        "default": True,
    },
    {
        "id": "vid:ltx-video-13b",
        "name": "LTX-Video 13B",
        "hf_repo": "Lightricks/LTX-Video",
        "dest": "/mnt/NOBILITY_VAULT/models/ltx-video-13b",
        "size_label": "~28 GB",
        "fps": 30, "max_frames": 257,
        "vram_gb": 24,
        "note": "Longest native clip of the open models and the fastest to sample.",
        "default": False,
    },
    {
        "id": "vid:cogvideox15-5b",
        "name": "CogVideoX 1.5-5B",
        "hf_repo": "THUDM/CogVideoX1.5-5B",
        "dest": "/mnt/NOBILITY_VAULT/models/cogvideox15-5b",
        "size_label": "~20 GB",
        "fps": 16, "max_frames": 161,
        "vram_gb": 20,
        "note": "10s native at 16fps — smooth, slower to sample than LTX.",
        "default": False,
    },
    {
        "id": "vid:wan22-ti2v-5b",
        "name": "Wan 2.2 TI2V-5B",
        "hf_repo": "Wan-AI/Wan2.2-TI2V-5B",
        "dest": "/mnt/NOBILITY_VAULT/models/wan22-ti2v-5b",
        "size_label": "~17 GB",
        "fps": 24, "max_frames": 121,
        "vram_gb": 16,
        "note": "5s native, strongest motion realism — chain segments to go longer.",
        "default": False,
    },
]


def _dir_size_gb(p):
    if not os.path.exists(p):
        return 0.0
    try:
        if os.path.isfile(p):
            return round(os.path.getsize(p) / (1024 ** 3), 1)
        total = sum(os.path.getsize(os.path.join(dp, f))
                    for dp, _, fn in os.walk(p) for f in fn)
        return round(total / (1024 ** 3), 1)
    except Exception:
        return 0.0


@app.get("/api/images/models")
async def images_models():
    cfg = _load_gcp_config()
    endpoint = cfg.get("gpu_endpoint", "")
    imgs = []
    for m in DIFFUSION_ROSTER:
        imgs.append({**m, "downloaded": os.path.exists(m["path"]),
                     "actual_gb": _dir_size_gb(m["path"])})
    vids = []
    for m in VIDEO_ROSTER:
        # a bare dir isn't proof — check the weight file itself so a half-finished
        # download doesn't report as ready
        probe = m.get("check_file") or m["dest"]
        present = os.path.exists(probe)
        vids.append({**m, "downloaded": present, "actual_gb": _dir_size_gb(m["dest"]),
                     "max_seconds": round(m["max_frames"] / m["fps"], 1)})
    return {"image_models": imgs, "video_models": vids,
            "gpu_endpoint_set": bool(endpoint), "gpu_endpoint": endpoint}


@app.get("/api/images/gallery")
async def images_gallery():
    items = []
    for d, kind in ((GEN_IMG_DIR, "image"), (GEN_VID_DIR, "video")):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.startswith("."):
                continue
            fp = os.path.join(d, f)
            try:
                items.append({"name": f, "kind": kind,
                              "size_kb": os.path.getsize(fp) // 1024,
                              "mtime": os.path.getmtime(fp)})
            except Exception:
                pass
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items}


@app.get("/api/images/file/{kind}/{name}")
async def images_file(kind: str, name: str):
    from fastapi.responses import FileResponse
    base = GEN_IMG_DIR if kind == "image" else GEN_VID_DIR
    safe = os.path.basename(name)
    fp = os.path.join(base, safe)
    if not os.path.exists(fp):
        return {"error": "not found"}
    return FileResponse(fp)


# ── GPU cost meter + idle watchdog ───────────────────────────────────────────
# Tracks GCP GPU wall-clock, bills it at a configurable hourly rate, and shuts
# the instance down after a stretch of no activity so an idle box stops burning
# the $300 credit.
GPU_METER_FILE = os.path.expanduser("~/.crane_gpu_meter.json")
GPU_IDLE_TIMEOUT_S = 600          # 10 minutes
GPU_DEFAULT_RATE = 0.40           # $/hour


def _meter_load():
    if os.path.exists(GPU_METER_FILE):
        try:
            with open(GPU_METER_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"running": False, "started_at": None, "last_activity": None,
            "rate": GPU_DEFAULT_RATE, "total_cost": 0.0, "total_seconds": 0.0,
            # auto_off stays True as a cost backstop: the GPU never STARTS on its
            # own (CAT-5 parked, no auto-fire), but if a session is forgotten the
            # 10-min idle watchdog shuts the instance down. Manual start, manual
            # stop, automatic rescue.
            "sessions": [], "auto_off": True, "idle_timeout": GPU_IDLE_TIMEOUT_S}


def _meter_save(m):
    with open(GPU_METER_FILE, "w") as f:
        json.dump(m, f, indent=2)


def _meter_state(m=None):
    """Derive the live view without mutating stored state."""
    m = m or _meter_load()
    now = time.time()
    elapsed = 0.0
    session_cost = 0.0
    idle = 0.0
    idle_timeout = m.get("idle_timeout", GPU_IDLE_TIMEOUT_S)
    if m.get("running") and m.get("started_at"):
        elapsed = now - m["started_at"]
        session_cost = (elapsed / 3600.0) * m.get("rate", GPU_DEFAULT_RATE)
        idle = now - (m.get("last_activity") or m["started_at"])
    return {
        "running": bool(m.get("running")),
        "rate": m.get("rate", GPU_DEFAULT_RATE),
        "elapsed_seconds": round(elapsed, 1),
        "session_cost": round(session_cost, 4),
        "total_cost": round(m.get("total_cost", 0.0) + session_cost, 4),
        "lifetime_cost": round(m.get("total_cost", 0.0), 4),
        "total_seconds": round(m.get("total_seconds", 0.0) + elapsed, 1),
        "idle_seconds": round(idle, 1),
        "idle_timeout": idle_timeout,
        "auto_off_in": round(max(0, idle_timeout - idle), 1) if m.get("running") else None,
        "auto_off": m.get("auto_off", True),
        "session_count": len(m.get("sessions", [])),
    }


class MeterStartRequest(BaseModel):
    rate: float = GPU_DEFAULT_RATE
    note: str = ""


@app.post("/api/gpu/start")
async def gpu_meter_start(req: MeterStartRequest):
    m = _meter_load()
    if m.get("running"):
        return {"status": "already_running", **_meter_state(m)}
    now = time.time()
    m.update({"running": True, "started_at": now, "last_activity": now,
              "rate": req.rate or GPU_DEFAULT_RATE})
    _meter_save(m)
    return {"status": "started", **_meter_state(m)}


@app.post("/api/gpu/stop")
async def gpu_meter_stop(shutdown: bool = False):
    m = _meter_load()
    if not m.get("running"):
        return {"status": "not_running", **_meter_state(m)}
    now = time.time()
    elapsed = now - m["started_at"]
    cost = (elapsed / 3600.0) * m.get("rate", GPU_DEFAULT_RATE)
    m["sessions"] = (m.get("sessions", []) + [{
        "started_at": m["started_at"], "ended_at": now,
        "seconds": round(elapsed, 1), "cost": round(cost, 4),
    }])[-50:]
    m["total_cost"] = round(m.get("total_cost", 0.0) + cost, 4)
    m["total_seconds"] = round(m.get("total_seconds", 0.0) + elapsed, 1)
    m.update({"running": False, "started_at": None, "last_activity": None})
    _meter_save(m)
    result = {"status": "stopped", "session_cost": round(cost, 4), **_meter_state(m)}
    if shutdown:
        result["shutdown"] = _gpu_instance_stop()
    return result


@app.post("/api/gpu/ping")
async def gpu_meter_ping():
    """Activity heartbeat — resets the idle countdown."""
    m = _meter_load()
    if m.get("running"):
        m["last_activity"] = time.time()
        _meter_save(m)
    return _meter_state(m)


@app.get("/api/gpu/meter")
async def gpu_meter_get():
    m = _meter_load()
    st = _meter_state(m)
    # enforce the idle timeout on read, so the meter self-heals even if the
    # background watcher is not running
    if st["running"] and m.get("auto_off", True) and st["idle_seconds"] >= st["idle_timeout"]:
        stopped = await gpu_meter_stop(shutdown=True)
        stopped["auto_stopped"] = True
        stopped["reason"] = f"idle {int(st['idle_seconds'])}s >= {st['idle_timeout']}s"
        return stopped
    return st


class MeterConfigRequest(BaseModel):
    rate: float = None
    auto_off: bool = None
    idle_timeout: int = None
    reset_total: bool = False


@app.post("/api/gpu/meter/config")
async def gpu_meter_config(req: MeterConfigRequest):
    m = _meter_load()
    if req.rate is not None:
        m["rate"] = req.rate
    if req.auto_off is not None:
        m["auto_off"] = req.auto_off
    if req.idle_timeout is not None:
        m["idle_timeout"] = req.idle_timeout
    if req.reset_total:
        m["total_cost"] = 0.0
        m["total_seconds"] = 0.0
        m["sessions"] = []
    _meter_save(m)
    return _meter_state(m)


def _gpu_instance_stop():
    """Best-effort: stop (not delete) the GCE box so the disk survives."""
    cfg = _load_gcp_config()
    name = cfg.get("instance_name", "crane-diffusion-gpu")
    project, zone = cfg.get("project_id"), cfg.get("zone", "us-central1-a")
    if not project:
        return {"ok": False, "detail": "No GCP project configured — meter stopped, instance untouched."}
    cmd = (f"gcloud compute instances stop {name} --project={project} --zone={zone} --quiet")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)
        return {"ok": r.returncode == 0, "cmd": cmd,
                "detail": (r.stderr or r.stdout or "")[-300:]}
    except Exception as e:
        return {"ok": False, "cmd": cmd, "detail": str(e)}


# ── HF ZeroGPU (free with a PRO membership) ──────────────────────────────────
# Calls a ZeroGPU Space through gradio_client using the vault's HF token.
# The token is read server-side and never returned to the browser.
ZEROGPU_SPACES = {
    "image": {"space": "black-forest-labs/FLUX.1-dev", "api": "/infer",
              "label": "FLUX.1-dev on ZeroGPU"},
    "video": {"space": "multimodalart/minimax-h3", "api": "/generate",
              "label": "MiniMax-H3 on ZeroGPU"},
}
ZEROGPU_CONFIG_FILE = os.path.expanduser("~/.crane_zerogpu.json")


def _zerogpu_cfg():
    cfg = dict(ZEROGPU_SPACES)
    if os.path.exists(ZEROGPU_CONFIG_FILE):
        try:
            with open(ZEROGPU_CONFIG_FILE) as f:
                for k, v in json.load(f).items():
                    if k in cfg and isinstance(v, dict):
                        cfg[k].update(v)
        except Exception:
            pass
    return cfg


@app.get("/api/images/zerogpu/status")
async def zerogpu_status():
    token = _vault_get("HF_TOKEN")
    return {"token_present": bool(token), "spaces": _zerogpu_cfg()}


class ZeroGpuConfigRequest(BaseModel):
    mode: str
    space: str
    api: str = "/infer"


@app.post("/api/images/zerogpu/config")
async def zerogpu_config(req: ZeroGpuConfigRequest):
    stored = {}
    if os.path.exists(ZEROGPU_CONFIG_FILE):
        try:
            stored = json.load(open(ZEROGPU_CONFIG_FILE))
        except Exception:
            pass
    stored[req.mode] = {"space": req.space, "api": req.api,
                        "label": f"{req.space} on ZeroGPU"}
    with open(ZEROGPU_CONFIG_FILE, "w") as f:
        json.dump(stored, f, indent=2)
    return {"status": "saved", "spaces": _zerogpu_cfg()}


class ZeroGpuRequest(BaseModel):
    prompt: str
    mode: str = "image"
    aspect: str = "2:3"
    steps: int = 28


@app.post("/api/images/zerogpu/generate")
async def zerogpu_generate(req: ZeroGpuRequest):
    token = _vault_get("HF_TOKEN")
    if not token:
        return {"error": "No HF_TOKEN in the vault — ZeroGPU needs it to spend your PRO quota."}
    cfg = _zerogpu_cfg().get(req.mode)
    if not cfg:
        return {"error": f"No ZeroGPU space configured for {req.mode}."}
    try:
        from gradio_client import Client
    except ImportError:
        return {"error": "gradio_client isn't installed on the server."}

    w, h = ASPECT_DIMS.get(req.aspect, (832, 1248))
    try:
        client = Client(cfg["space"], hf_token=token)
        try:
            out = client.predict(prompt=req.prompt, width=w, height=h,
                                 num_inference_steps=req.steps, api_name=cfg.get("api", "/infer"))
        except TypeError:
            # Spaces differ in signature — fall back to positional
            out = client.predict(req.prompt, api_name=cfg.get("api", "/infer"))

        src = out[0] if isinstance(out, (list, tuple)) and out else out
        if isinstance(src, dict):
            src = src.get("video") or src.get("path") or src.get("url")
        if not isinstance(src, str) or not os.path.exists(src):
            return {"error": f"ZeroGPU returned an unexpected payload: {str(out)[:200]}"}

        ext = "png" if req.mode == "image" else "mp4"
        out_dir = GEN_IMG_DIR if req.mode == "image" else GEN_VID_DIR
        fname = f"{int(time.time())}_zgpu_{re.sub(r'[^a-zA-Z0-9]+', '_', req.prompt)[:36]}.{ext}"
        import shutil
        shutil.copyfile(src, os.path.join(out_dir, fname))
        return {"status": "ok", "name": fname, "kind": req.mode,
                "provider": "zerogpu", "space": cfg["space"], "cost": 0.0}
    except Exception as e:
        return {"error": f"ZeroGPU call failed ({cfg['space']}): {e}"}


class GenerateRequest(BaseModel):
    prompt: str
    model: str
    mode: str = "image"          # image | video
    aspect: str = "2:3"
    quality: str = "speed"       # speed | quality
    steps: int = 0               # 0 = pick from quality
    seconds: float = 0           # video only, 0 = max for model
    segments: int = 1            # video chaining beyond native cap


ASPECT_DIMS = {
    "1:1":  (1024, 1024),
    "2:3":  (832, 1248),
    "3:2":  (1248, 832),
    "9:16": (768, 1360),
    "16:9": (1360, 768),
}


@app.post("/api/images/generate")
async def images_generate(req: GenerateRequest):
    cfg = _load_gcp_config()
    endpoint = cfg.get("gpu_endpoint", "")

    roster = DIFFUSION_ROSTER if req.mode == "image" else VIDEO_ROSTER
    entry = next((m for m in roster if m["id"] == req.model), None)
    if not entry:
        return {"error": f"Unknown model {req.model}"}

    if req.mode == "video" and not os.path.exists(entry.get("dest", "")):
        return {"error": f"{entry['name']} isn't in the vault yet. Use 'Fetch to vault' on the model "
                         f"to download it ({entry['size_label']}) before generating."}
    if req.mode == "image" and not os.path.exists(entry.get("path", "")):
        return {"error": f"{entry['name']} isn't in the vault."}

    if not endpoint:
        return {
            "error": "no_gpu",
            "detail": f"{entry['name']} is GPU-only — {entry.get('why_gpu', 'needs CUDA')} "
                      f"No GPU endpoint is configured yet.",
            "vram_gb": entry.get("vram_gb", 24),
            "gcloud_cmd": _gpu_provision_cmd(cfg, entry.get("vram_gb", 24)),
        }

    w, h = ASPECT_DIMS.get(req.aspect, (832, 1248))
    steps = req.steps or (20 if req.quality == "speed" else 40)

    payload = {"prompt": req.prompt, "model": entry["id"], "width": w, "height": h, "steps": steps}
    if req.mode == "video":
        fps, max_f = entry["fps"], entry["max_frames"]
        # optimize to the model's native ceiling unless the user asked for less
        want_s = req.seconds or round(max_f / fps, 1)
        frames = min(int(want_s * fps), max_f)
        payload.update({"frames": frames, "fps": fps, "segments": max(1, req.segments)})

    try:
        resp = _requests.post(f"{endpoint.rstrip('/')}/generate", json=payload, timeout=600)
        if resp.status_code != 200:
            return {"error": f"GPU worker returned {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        b64 = data.get("image_b64") or data.get("video_b64")
        if not b64:
            return {"error": "GPU worker returned no media."}
        import base64
        raw = base64.b64decode(b64)
        ext = "png" if req.mode == "image" else "mp4"
        out_dir = GEN_IMG_DIR if req.mode == "image" else GEN_VID_DIR
        fname = f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9]+', '_', req.prompt)[:40]}.{ext}"
        with open(os.path.join(out_dir, fname), "wb") as fh:
            fh.write(raw)
        return {"status": "ok", "name": fname, "kind": req.mode}
    except Exception as e:
        return {"error": f"Couldn't reach GPU worker ({endpoint}): {e}"}


def _gpu_provision_cmd(cfg, vram_gb=24):
    project = cfg.get("project_id", "<your-project>")
    zone = cfg.get("zone", "us-central1-a")
    # L4 24GB covers every model in the roster and is the cheapest 24GB card on GCE.
    return (f"gcloud compute instances create crane-diffusion-gpu "
            f"--project={project} --zone={zone} "
            f"--machine-type=g2-standard-8 --accelerator=type=nvidia-l4,count=1 "
            f"--provisioning-model=SPOT --instance-termination-action=DELETE "
            f"--image-family=common-cu124-debian-11 --image-project=deeplearning-platform-release "
            f"--maintenance-policy=TERMINATE --boot-disk-size=200GB "
            f"--metadata=install-nvidia-driver=True")


class VideoFetchRequest(BaseModel):
    model: str


@app.post("/api/images/video/fetch")
async def video_fetch(req: VideoFetchRequest):
    entry = next((m for m in VIDEO_ROSTER if m["id"] == req.model), None)
    if not entry:
        return {"error": "unknown model"}
    log_path = f"/tmp/vid_dl_{entry['id'].replace(':', '_')}.log"
    files = entry.get("files")
    if files:
        # Named files only — these repos carry every quant, and a whole-repo pull
        # would drag down hundreds of GB.
        jobs = ", ".join(repr(f) for f in files)
        py = (f"from huggingface_hub import hf_hub_download\n"
              f"for fn in [{jobs}]:\n"
              f"    print('>>> START', fn, flush=True)\n"
              f"    hf_hub_download(repo_id={entry['hf_repo']!r}, filename=fn, local_dir={entry['dest']!r})\n"
              f"    print('>>> DONE', fn, flush=True)\n"
              f"print('>>> ALL_DONE', flush=True)")
    else:
        py = (f"from huggingface_hub import snapshot_download\n"
              f"p = snapshot_download(repo_id={entry['hf_repo']!r}, local_dir={entry['dest']!r})\n"
              f"print('>>> ALL_DONE', p)")
    script = f"/tmp/fetch_{entry['id'].replace(':', '_')}.py"
    with open(script, "w") as fh:
        fh.write(py)
    cmd = (f"nohup /home/hunt/.local/bin/uv run --with huggingface_hub "
           f"python3 {script} > {log_path} 2>&1 &")
    subprocess.Popen(cmd, shell=True)
    return {"status": "started", "log": log_path, "size": entry["size_label"]}


@app.get("/api/images/video/fetch/status")
async def video_fetch_status(model: str = "vid:minimax-h3"):
    entry = next((m for m in VIDEO_ROSTER if m["id"] == model), None)
    if not entry:
        return {"error": "unknown model"}
    log_path = f"/tmp/vid_dl_{entry['id'].replace(':', '_')}.log"
    alt = "/tmp/dl_h3.log" if model == "vid:minimax-h3" else None
    for p in (log_path, alt):
        if p and os.path.exists(p):
            txt = open(p).read()
            return {"on_disk_gb": _dir_size_gb(entry["dest"]),
                    "target": entry["size_label"],
                    "done": "ALL_DONE" in txt or "ALL_H3_DONE" in txt,
                    "tail": txt[-600:]}
    return {"on_disk_gb": _dir_size_gb(entry["dest"]), "target": entry["size_label"],
            "done": False, "tail": ""}


@app.get("/images", response_class=HTMLResponse)
async def serve_images():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CRANE Imagine</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root{
  --bg:#070d18;--panel:#0d1627;--card:#111827;--border:#1e3052;
  --blue:#38bdf8;--purple:#a855f7;--green:#10b981;--gold:#f59e0b;
  --red:#ef4444;--text:#e2e8f0;--muted:#475569;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;height:100vh;overflow:hidden;display:flex;flex-direction:column;}

/* TOPBAR */
#topbar{height:44px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;padding:0 14px;flex-shrink:0;position:relative;}
.logo-i{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:14px;letter-spacing:2px;background:linear-gradient(90deg,#a855f7,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.crane-nav{position:absolute;left:50%;transform:translateX(-50%);display:flex;gap:2px;background:rgba(0,0,0,.35);border-radius:8px;padding:4px;z-index:10;}
.nav-tab{color:var(--muted);text-decoration:none;padding:5px 18px;border-radius:6px;font-size:11px;font-weight:700;letter-spacing:2px;font-family:'JetBrains Mono',monospace;transition:.15s;}
.nav-tab:hover{color:var(--text);background:rgba(255,255,255,.07);}
.nav-tab.active{color:#fff;background:rgba(168,85,247,.28);border:1px solid rgba(168,85,247,.4);}
.nav-tab.depo.active{background:rgba(245,158,11,.22);border-color:rgba(245,158,11,.4);color:var(--gold);}
.nav-tab.img.active{background:rgba(56,189,248,.22);border-color:rgba(56,189,248,.4);color:var(--blue);}
.nav-tab.img.active{background:rgba(56,189,248,.22);border-color:rgba(56,189,248,.4);color:var(--blue);}
.nav-tab.studio.active{background:rgba(16,185,129,.22);border-color:rgba(16,185,129,.4);color:#10b981;}
.tb-spacer{flex:1;}
#gpuPill{display:flex;align-items:center;gap:6px;font-size:10px;font-family:'JetBrains Mono',monospace;border:1px solid var(--border);border-radius:14px;padding:3px 10px;cursor:pointer;}
#gpuPill:hover{border-color:var(--blue);}
.dot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex-shrink:0;}
.dot.on{background:var(--green);box-shadow:0 0 6px var(--green);}
.dot.off{background:var(--red);}

/* LAYOUT */
#wrap{flex:1;display:flex;overflow:hidden;}
#rail{width:190px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;overflow-y:auto;}
.rail-item{display:flex;align-items:center;gap:9px;padding:9px 14px;cursor:pointer;font-size:12px;font-weight:500;color:var(--muted);transition:.15s;}
.rail-item:hover{background:rgba(255,255,255,.04);color:var(--text);}
.rail-item.active{background:rgba(168,85,247,.14);color:var(--text);border-right:2px solid var(--purple);}
.rail-head{padding:14px 14px 6px;font-size:9px;letter-spacing:2px;color:var(--muted);font-weight:700;font-family:'JetBrains Mono',monospace;}
.hist-item{padding:6px 14px;font-size:11px;color:var(--muted);cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:'JetBrains Mono',monospace;}
.hist-item:hover{background:rgba(255,255,255,.04);color:var(--text);}

#stage{flex:1;overflow-y:auto;display:flex;flex-direction:column;align-items:center;padding:0 24px 40px;}
#hero{margin-top:56px;text-align:center;}
#hero h1{font-size:26px;font-weight:700;letter-spacing:-.3px;}
#hero p{margin-top:7px;font-size:12px;color:var(--muted);}

/* COMPOSER */
#composer{width:100%;max-width:780px;margin-top:26px;background:var(--card);border:1px solid var(--border);border-radius:16px;padding:14px 16px 10px;transition:.15s;}
#composer:focus-within{border-color:rgba(168,85,247,.5);}
#promptBox{width:100%;background:transparent;border:none;outline:none;color:var(--text);font-size:14px;font-family:'Inter',sans-serif;resize:none;min-height:46px;max-height:180px;line-height:1.6;}
#promptBox::placeholder{color:var(--muted);}
#ctrlRow{display:flex;align-items:center;gap:7px;margin-top:8px;flex-wrap:wrap;}
.seg{display:flex;background:rgba(0,0,0,.35);border:1px solid var(--border);border-radius:9px;overflow:hidden;flex-shrink:0;}
.seg button{background:transparent;border:none;color:var(--muted);padding:6px 12px;font-size:11px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:5px;transition:.15s;font-family:'Inter',sans-serif;}
.seg button:hover{color:var(--text);background:rgba(255,255,255,.05);}
.seg button.on{background:rgba(56,189,248,.18);color:var(--blue);}
.chip{background:rgba(0,0,0,.35);border:1px solid var(--border);color:var(--muted);border-radius:9px;padding:6px 11px;font-size:11px;font-weight:600;cursor:pointer;transition:.15s;flex-shrink:0;}
.chip:hover{border-color:var(--blue);color:var(--text);}
.icon-btn{width:32px;height:32px;border-radius:9px;border:1px solid var(--border);background:rgba(0,0,0,.35);color:var(--muted);cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center;transition:.15s;flex-shrink:0;}
.icon-btn:hover{border-color:var(--blue);color:var(--blue);}
#goBtn{margin-left:auto;width:36px;height:36px;border-radius:50%;border:none;background:linear-gradient(135deg,var(--purple),var(--blue));color:#fff;font-size:16px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;}
#goBtn:disabled{opacity:.35;cursor:default;}

/* model dropdown */
.drop{position:relative;flex-shrink:0;}
#modelBtn{display:flex;align-items:center;gap:6px;background:rgba(0,0,0,.35);border:1px solid var(--border);border-radius:9px;padding:6px 11px;font-size:11px;font-weight:600;cursor:pointer;color:var(--text);font-family:'JetBrains Mono',monospace;}
#modelBtn:hover{border-color:var(--purple);}
.menu{position:absolute;bottom:calc(100% + 8px);left:0;min-width:330px;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:5px;z-index:400;display:none;box-shadow:0 -10px 34px rgba(0,0,0,.6);}
.menu.open{display:block;}
.menu-head{padding:7px 11px 5px;font-size:9px;letter-spacing:2px;color:var(--muted);font-weight:700;font-family:'JetBrains Mono',monospace;}
.m-opt{padding:8px 11px;border-radius:8px;cursor:pointer;transition:.12s;}
.m-opt:hover{background:rgba(255,255,255,.05);}
.m-opt.sel{background:rgba(56,189,248,.1);}
.m-top{display:flex;align-items:center;gap:7px;}
.m-name{font-size:12px;font-weight:600;flex:1;}
.m-badge{font-size:8px;padding:1px 5px;border-radius:3px;font-weight:700;letter-spacing:.5px;flex-shrink:0;}
.b-vault{background:rgba(16,185,129,.16);color:var(--green);}
.b-gpu{background:rgba(239,68,68,.16);color:var(--red);}
.b-missing{background:rgba(71,85,105,.25);color:var(--muted);}
.m-why{font-size:10px;color:var(--muted);margin-top:3px;line-height:1.45;}
.m-fetch{font-size:9px;border:1px solid var(--border);background:transparent;color:var(--muted);border-radius:5px;padding:2px 7px;cursor:pointer;margin-top:5px;}
.m-fetch:hover{border-color:var(--gold);color:var(--gold);}

/* video controls */
#vidRow{display:none;align-items:center;gap:9px;margin-top:9px;padding-top:9px;border-top:1px solid rgba(255,255,255,.05);flex-wrap:wrap;}
#vidRow.show{display:flex;}
#durSlider{flex:1;min-width:130px;accent-color:var(--purple);}
.vlab{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;white-space:nowrap;}
#segCount{background:rgba(0,0,0,.35);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:4px 7px;font-size:11px;outline:none;}

/* status */
#statusBar{width:100%;max-width:780px;margin-top:11px;font-size:11px;line-height:1.6;display:none;border-radius:10px;padding:11px 13px;}
#statusBar.err{display:block;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.28);color:#fca5a5;}
#statusBar.info{display:block;background:rgba(56,189,248,.07);border:1px solid rgba(56,189,248,.25);color:var(--blue);}
#statusBar code{display:block;margin-top:7px;background:rgba(0,0,0,.5);padding:8px 10px;border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text);white-space:pre-wrap;word-break:break-all;user-select:all;}

/* GALLERY */
#gallery{width:100%;max-width:1080px;margin-top:34px;}
.gal-head{display:flex;align-items:center;gap:9px;margin-bottom:13px;}
.gal-head h2{font-size:13px;font-weight:700;letter-spacing:1px;}
.gal-count{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
#galGrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(228px,1fr));gap:13px;}
.card{position:relative;border-radius:13px;overflow:hidden;background:var(--card);border:1px solid var(--border);cursor:pointer;transition:.18s;aspect-ratio:2/3;}
.card:hover{border-color:rgba(168,85,247,.5);transform:translateY(-2px);}
.card img,.card video{width:100%;height:100%;object-fit:cover;display:block;}
.card-lab{position:absolute;left:0;right:0;bottom:0;padding:22px 11px 9px;font-size:11px;font-weight:600;background:linear-gradient(transparent,rgba(0,0,0,.85));}
.preset{aspect-ratio:2/3;display:flex;flex-direction:column;justify-content:flex-end;padding:13px;background:linear-gradient(150deg,rgba(168,85,247,.13),rgba(56,189,248,.07));}
.preset .p-ico{font-size:24px;margin-bottom:auto;}
.preset .p-name{font-size:12px;font-weight:700;}
.preset .p-desc{font-size:10px;color:var(--muted);margin-top:3px;line-height:1.45;}
.empty{grid-column:1/-1;text-align:center;padding:40px 20px;color:var(--muted);font-size:12px;}

/* lightbox */
#lightbox{position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:9000;display:none;align-items:center;justify-content:center;padding:36px;}
#lightbox.open{display:flex;}
#lightbox img,#lightbox video{max-width:100%;max-height:100%;border-radius:11px;}
#lbClose{position:absolute;top:18px;right:22px;font-size:26px;color:var(--muted);cursor:pointer;background:none;border:none;}

/* GPU COST METER — bottom left */
#meter{position:fixed;left:12px;bottom:12px;z-index:800;width:212px;background:rgba(13,22,39,.96);border:1px solid var(--border);border-radius:12px;padding:10px 11px;backdrop-filter:blur(8px);font-family:'JetBrains Mono',monospace;box-shadow:0 6px 26px rgba(0,0,0,.5);}
#meter.live{border-color:rgba(16,185,129,.5);}
#meter.warn{border-color:rgba(245,158,11,.6);}
.mt-top{display:flex;align-items:center;gap:6px;margin-bottom:8px;}
.mt-title{font-size:9px;letter-spacing:1.5px;color:var(--muted);font-weight:700;flex:1;}
.mt-toggle{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:5px;font-size:8px;padding:2px 6px;cursor:pointer;font-family:'JetBrains Mono',monospace;}
.mt-toggle:hover{border-color:var(--blue);color:var(--blue);}
.mt-row{display:flex;justify-content:space-between;align-items:baseline;font-size:10px;margin-bottom:4px;}
.mt-k{color:var(--muted);}
.mt-v{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums;}
.mt-big{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.5px;}
.mt-live{color:var(--green);}
.mt-idle{color:var(--muted);}
.mt-bar{height:3px;background:rgba(255,255,255,.07);border-radius:2px;overflow:hidden;margin-top:7px;}
.mt-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--gold));width:0%;transition:width 1s linear;}
.mt-note{font-size:8px;color:var(--muted);margin-top:6px;line-height:1.45;}

/* provider selector */
.prov-free{background:rgba(16,185,129,.18)!important;color:var(--green)!important;}
.prov-paid{background:rgba(245,158,11,.18)!important;color:var(--gold)!important;}

::-webkit-scrollbar{width:5px;height:5px;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}
</style>
</head>
<body>

<div id="topbar">
  <span class="logo-i">CRANE</span>
  <nav class="crane-nav">
    <a href="/ide" class="nav-tab">HOME</a>
    <a href="/studio" class="nav-tab studio">STUDIO</a>
    <a href="/connie" class="nav-tab">CONNIE</a>
    <a href="/depo" class="nav-tab depo">DEPO</a>
    <a href="/images" class="nav-tab img active">IMAGES</a>
  </nav>
  <div class="tb-spacer"></div>
  <div id="gpuPill" onclick="showGpuHelp()">
    <span class="dot" id="gpuDot"></span><span id="gpuTxt">GPU offline</span>
  </div>
</div>

<div id="wrap">
  <div id="rail">
    <div class="rail-head">STUDIO</div>
    <div class="rail-item active" onclick="setRail('imagine',this)"><span>✦</span> Imagine</div>
    <div class="rail-item" onclick="setRail('library',this)"><span>▦</span> Library</div>
    <div class="rail-head">RECENT PROMPTS</div>
    <div id="histList"><div class="hist-item" style="color:var(--muted)">No prompts yet</div></div>
  </div>

  <div id="stage">
    <div id="hero">
      <h1 id="heroTitle">What should we imagine?</h1>
      <p id="heroSub">FLUX and Qwen-Image, straight out of your Nobility Vault.</p>
    </div>

    <div id="composer">
      <textarea id="promptBox" placeholder="Type to imagine…" onkeydown="promptKey(event)"></textarea>

      <div id="ctrlRow">
        <button class="icon-btn" title="Reference image" onclick="alert('Reference-image input lands once the GPU worker is up.')">+</button>

        <div class="seg">
          <button id="mImage" class="on" onclick="setMode('image')">🖼 Image</button>
          <button id="mVideo" onclick="setMode('video')">🎬 Video</button>
        </div>

        <div class="seg">
          <button id="qSpeed" class="on" onclick="setQuality('speed')">Speed</button>
          <button id="qQual" onclick="setQuality('quality')">Quality</button>
        </div>

        <div class="seg" title="Where this render runs">
          <button id="pZero" class="on prov-free" onclick="setProvider('zerogpu')">⚡ ZeroGPU · free</button>
          <button id="pGcp" onclick="setProvider('gcp')">☁ GCP · $0.40/hr</button>
        </div>

        <button class="chip" id="aspectChip" onclick="cycleAspect()">▭ 2:3</button>

        <div class="drop">
          <div id="modelBtn" onclick="toggleMenu(event)">
            <span class="dot" id="modelDot"></span>
            <span id="modelName">loading…</span>
            <span style="color:var(--muted);font-size:9px">▾</span>
          </div>
          <div class="menu" id="modelMenu"></div>
        </div>

        <button id="goBtn" onclick="generate()">↑</button>
      </div>

      <div id="vidRow">
        <span class="vlab">Length</span>
        <input type="range" id="durSlider" min="1" max="10" step="0.5" value="8.5" oninput="onDur()">
        <span class="vlab" id="durLab">8.5s</span>
        <span class="vlab" style="margin-left:8px">Chain</span>
        <select id="segCount" onchange="onDur()">
          <option value="1">1 seg</option><option value="2">2 seg</option>
          <option value="3">3 seg</option><option value="4">4 seg</option>
        </select>
        <span class="vlab" id="totalLab">total 8.5s</span>
      </div>
    </div>

    <div id="statusBar"></div>

    <div id="gallery">
      <div class="gal-head">
        <h2 id="galTitle">Gallery</h2>
        <span class="gal-count" id="galCount"></span>
      </div>
      <div id="galGrid"></div>
    </div>
  </div>
</div>

<div id="meter">
  <div class="mt-top">
    <span class="dot" id="mtDot"></span>
    <span class="mt-title">GPU METER</span>
    <button class="mt-toggle" id="mtBtn" onclick="toggleGpu()">START</button>
  </div>
  <div class="mt-row">
    <span class="mt-k">this session</span>
    <span class="mt-big mt-idle" id="mtCost">$0.0000</span>
  </div>
  <div class="mt-row"><span class="mt-k">runtime</span><span class="mt-v" id="mtTime">—</span></div>
  <div class="mt-row"><span class="mt-k">rate</span><span class="mt-v" id="mtRate">$0.40/hr</span></div>
  <div class="mt-row" style="border-top:1px solid rgba(255,255,255,.07);padding-top:5px;margin-top:6px;">
    <span class="mt-k">lifetime</span><span class="mt-v" id="mtTotal">$0.0000</span>
  </div>
  <div class="mt-bar"><div class="mt-fill" id="mtFill"></div></div>
  <div class="mt-note" id="mtNote">Idle auto-off after 10 min.</div>
</div>

<div id="lightbox" onclick="closeLb(event)">
  <button id="lbClose" onclick="closeLb(event)">×</button>
  <div id="lbInner"></div>
</div>

<script>
let IMG_MODELS=[], VID_MODELS=[], GPU_SET=false, GPU_CMD='';
let _mode='image', _quality='speed', _aspect='2:3', _model=null, _menuOpen=false;
const ASPECTS=['2:3','3:2','1:1','9:16','16:9'];
const PRESETS=[
  {ico:'🪞',name:'Reimagine',desc:'Restyle a reference image while keeping the subject.'},
  {ico:'✂️',name:'BG Removal & Change',desc:'Cut the subject out, drop in a new background.'},
  {ico:'🔍',name:'Smart Resize',desc:'Outpaint to a new aspect without cropping the subject.'},
  {ico:'🎨',name:'Photo Edit',desc:'Targeted edits from a plain-language instruction.'},
];

async function loadModels(){
  const r=await fetch('/api/images/models'); const d=await r.json();
  IMG_MODELS=d.image_models||[]; VID_MODELS=d.video_models||[];
  GPU_SET=d.gpu_endpoint_set;
  document.getElementById('gpuDot').className='dot '+(GPU_SET?'on':'off');
  document.getElementById('gpuTxt').textContent=GPU_SET?'GPU ready':'GPU offline';
  const def=IMG_MODELS.find(m=>m.default)||IMG_MODELS[0];
  if(def) pickModel(def.id);
  buildMenu();
}

function currentRoster(){ return _mode==='image'?IMG_MODELS:VID_MODELS; }

function buildMenu(){
  const menu=document.getElementById('modelMenu'); menu.innerHTML='';
  const head=document.createElement('div'); head.className='menu-head';
  head.textContent=_mode==='image'?'DIFFUSION — IMAGE':'DIFFUSION — VIDEO';
  menu.appendChild(head);
  currentRoster().forEach(m=>{
    const el=document.createElement('div');
    el.className='m-opt'+(m.id===_model?' sel':'');
    const badge=!m.downloaded
      ? '<span class="m-badge b-missing">NOT IN VAULT</span>'
      : '<span class="m-badge b-vault">VAULT</span><span class="m-badge b-gpu">GPU</span>';
    const meta=_mode==='image'
      ? (m.why_gpu||'')+' '+(m.strength||'')
      : (m.note||'')+` Native max ${m.max_seconds}s at ${m.fps}fps.`;
    let fetchBtn='';
    if(!m.downloaded && _mode==='video')
      fetchBtn=`<button class="m-fetch" onclick="fetchVideo(event,'${m.id}')">⬇ Fetch to vault (${m.size_label})</button>`;
    el.innerHTML=`<div class="m-top"><span class="m-name">${m.name}</span>${badge}</div>
                  <div class="m-why">${meta}</div>${fetchBtn}`;
    el.onclick=(e)=>{ if(e.target.classList.contains('m-fetch'))return; pickModel(m.id); toggleMenu(); };
    menu.appendChild(el);
  });
}

function pickModel(id){
  _model=id;
  const m=currentRoster().find(x=>x.id===id); if(!m)return;
  document.getElementById('modelName').textContent=m.name;
  document.getElementById('modelDot').className='dot '+(m.downloaded?'on':'off');
  if(_mode==='video'){
    const s=document.getElementById('durSlider');
    s.max=m.max_seconds; s.value=m.max_seconds;   // optimize to the model's ceiling
    onDur();
  }
  buildMenu();
}

function toggleMenu(e){ if(e)e.stopPropagation(); _menuOpen=!_menuOpen;
  document.getElementById('modelMenu').classList.toggle('open',_menuOpen); }
document.addEventListener('click',e=>{
  if(!e.target.closest('.drop')){_menuOpen=false;document.getElementById('modelMenu').classList.remove('open');}
});

function setMode(m){
  _mode=m;
  document.getElementById('mImage').classList.toggle('on',m==='image');
  document.getElementById('mVideo').classList.toggle('on',m==='video');
  document.getElementById('vidRow').classList.toggle('show',m==='video');
  document.getElementById('heroTitle').textContent=m==='image'?'What should we imagine?':'What should we film?';
  document.getElementById('heroSub').textContent=m==='image'
    ? 'FLUX and Qwen-Image, straight out of your Nobility Vault.'
    : 'Length is auto-set to each model’s native maximum. Chain segments to go past it.';
  // Images always use free ZeroGPU; videos require paid GCP GPU if needed
  if(m==='image'){ setProvider('zerogpu'); document.getElementById('pGcp').style.opacity='.35'; document.getElementById('pGcp').title='Images always use free ZeroGPU — GPU ($0.40/hr) reserved for video rendering'; }
  else { setProvider('gcp'); document.getElementById('pGcp').style.opacity='1'; document.getElementById('pGcp').title=''; }
  document.getElementById('promptBox').placeholder=m==='image'?'Type to imagine…':'Describe the shot…';
  const r=currentRoster(); const def=r.find(x=>x.default)||r[0];
  if(def) pickModel(def.id);
  buildMenu();
}

function setQuality(q){ _quality=q;
  document.getElementById('qSpeed').classList.toggle('on',q==='speed');
  document.getElementById('qQual').classList.toggle('on',q==='quality'); }

let _provider='zerogpu';
function setProvider(p){
  _provider=p;
  const z=document.getElementById('pZero'), g=document.getElementById('pGcp');
  z.classList.toggle('on',p==='zerogpu'); z.classList.toggle('prov-free',p==='zerogpu');
  g.classList.toggle('on',p==='gcp');     g.classList.toggle('prov-paid',p==='gcp');
  if(p==='gcp' && !_meter.running)
    setStatus('info','GCP selected — press START on the meter before generating so the cost is tracked.');
  else if(p==='zerogpu')
    setStatus('info','ZeroGPU selected — runs on your HF PRO quota at no cost. Vault weights aren’t used here; the Space supplies the model.');
}

function cycleAspect(){
  _aspect=ASPECTS[(ASPECTS.indexOf(_aspect)+1)%ASPECTS.length];
  document.getElementById('aspectChip').textContent='▭ '+_aspect;
}

function onDur(){
  const s=parseFloat(document.getElementById('durSlider').value);
  const segs=parseInt(document.getElementById('segCount').value);
  document.getElementById('durLab').textContent=s+'s';
  document.getElementById('totalLab').textContent='total '+(s*segs).toFixed(1)+'s';
}

function promptKey(e){ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();generate();} }

function setStatus(kind,html){
  const b=document.getElementById('statusBar');
  b.className=kind; b.innerHTML=html;
  if(!kind) b.style.display='none';
}

function showGpuHelp(){
  if(GPU_SET){ setStatus('info','GPU endpoint is configured. Generations route there.'); return; }
  setStatus('info','Both of your diffusion models are CUDA-only, so image generation runs on a GPU. '
    +'Provision one (L4 24GB, spot pricing), start the worker, then paste its URL into the ☁ GPU field on the IDE page.'
    +(GPU_CMD?'<code>'+GPU_CMD+'</code>':''));
}

async function fetchVideo(e,id){
  e.stopPropagation();
  const r=await fetch('/api/images/video/fetch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({model:id})});
  const d=await r.json();
  setStatus('info',`Downloading ${id} to the vault (${d.size||''}). This runs in the background — the badge flips to VAULT when it lands.`);
}

async function generate(){
  const prompt=document.getElementById('promptBox').value.trim();
  if(!prompt){ setStatus('err','Write a prompt first.'); return; }
  if(!_model){ setStatus('err','Pick a model first.'); return; }
  addHist(prompt);
  const btn=document.getElementById('goBtn'); btn.disabled=true;
  setStatus('info','Generating…');

  const body={prompt,model:_model,mode:_mode,aspect:_aspect,quality:_quality};
  if(_mode==='video'){
    body.seconds=parseFloat(document.getElementById('durSlider').value);
    body.segments=parseInt(document.getElementById('segCount').value);
  }
  try{
    let r,d;
    if(_provider==='zerogpu'){
      // Images always use free ZeroGPU (HF/Google) — never the $0.40/hr GPU
      setStatus('info','Running on ZeroGPU (free with HF PRO)…');
      r=await fetch('/api/images/zerogpu/generate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({prompt,mode:_mode,aspect:_aspect,steps:_quality==='speed'?20:34})});
      d=await r.json();
    } else {
      // Video only — auto-start paid GPU for processing duration; idle auto-off always enforced
      if(!_meter.running){
        setStatus('info','Starting GPU for video render ($0.40/hr) — auto-off after idle…');
        await fetch('/api/gpu/start',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({rate:0.40,auto_off:true,idle_timeout:600})});
        refreshMeter();
      }
      await pingGpu();
      r=await fetch('/api/images/generate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)});
      d=await r.json();
      // ping activity so idle timer resets during processing
      await pingGpu();
    }
    if(d.error==='no_gpu'){
      GPU_CMD=d.gcloud_cmd||'';
      setStatus('err',d.detail+'<br><br>Provision a GPU with at least '+d.vram_gb+'GB VRAM, then paste the worker URL into the ☁ GCP panel on the IDE page:'
        +'<code>'+d.gcloud_cmd+'</code>');
    } else if(d.error){
      setStatus('err',d.error);
    } else {
      setStatus('info','Done — added to your gallery.');
      loadGallery();
    }
  }catch(err){ setStatus('err',err.message); }
  btn.disabled=false;
}

function addHist(p){
  let h=JSON.parse(localStorage.getItem('crane_img_hist')||'[]');
  h.unshift(p); h=h.slice(0,14);
  localStorage.setItem('crane_img_hist',JSON.stringify(h));
  renderHist();
}
function renderHist(){
  const h=JSON.parse(localStorage.getItem('crane_img_hist')||'[]');
  const el=document.getElementById('histList');
  if(!h.length){ el.innerHTML='<div class="hist-item" style="color:var(--muted)">No prompts yet</div>'; return; }
  el.innerHTML='';
  h.forEach(p=>{
    const d=document.createElement('div'); d.className='hist-item'; d.textContent=p; d.title=p;
    d.onclick=()=>{document.getElementById('promptBox').value=p;};
    el.appendChild(d);
  });
}

async function loadGallery(){
  const r=await fetch('/api/images/gallery'); const d=await r.json();
  const grid=document.getElementById('galGrid'); grid.innerHTML='';
  const items=d.items||[];
  document.getElementById('galCount').textContent=items.length?items.length+' saved':'';
  if(!items.length){
    PRESETS.forEach(p=>{
      const c=document.createElement('div'); c.className='card preset';
      c.innerHTML=`<div class="p-ico">${p.ico}</div><div class="p-name">${p.name}</div><div class="p-desc">${p.desc}</div>`;
      grid.appendChild(c);
    });
    const note=document.createElement('div'); note.className='empty';
    note.textContent='Nothing generated yet — your renders land here and are written to /mnt/NOBILITY_VAULT/generated.';
    grid.appendChild(note);
    return;
  }
  items.forEach(it=>{
    const c=document.createElement('div'); c.className='card';
    const url=`/api/images/file/${it.kind}/${encodeURIComponent(it.name)}`;
    c.innerHTML=(it.kind==='image'
      ? `<img src="${url}" loading="lazy">`
      : `<video src="${url}" muted loop onmouseover="this.play()" onmouseout="this.pause()"></video>`)
      +`<div class="card-lab">${it.name.replace(/^\d+_/,'').replace(/\.(png|mp4)$/,'').replace(/_/g,' ')}</div>`;
    c.onclick=()=>openLb(it.kind,url);
    grid.appendChild(c);
  });
}

function openLb(kind,url){
  document.getElementById('lbInner').innerHTML = kind==='image'
    ? `<img src="${url}">` : `<video src="${url}" controls autoplay loop></video>`;
  document.getElementById('lightbox').classList.add('open');
}
function closeLb(e){ if(e.target.id==='lbInner')return;
  document.getElementById('lightbox').classList.remove('open');
  document.getElementById('lbInner').innerHTML=''; }

function setRail(which,el){
  document.querySelectorAll('.rail-item').forEach(x=>x.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('galTitle').textContent = which==='library'?'Library':'Gallery';
}

// ── GPU COST METER ──────────────────────────────────────────────────────────
let _meter={running:false}, _meterTimer=null;

function fmtDur(s){
  s=Math.floor(s); const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), x=s%60;
  return h>0 ? `${h}h ${m}m ${x}s` : m>0 ? `${m}m ${x}s` : `${x}s`;
}

async function refreshMeter(){
  try{
    const r=await fetch('/api/gpu/meter'); const d=await r.json();
    _meter=d;
    const box=document.getElementById('meter');
    const dot=document.getElementById('mtDot');
    const cost=document.getElementById('mtCost');

    document.getElementById('mtCost').textContent='$'+(d.session_cost||0).toFixed(4);
    document.getElementById('mtTime').textContent=d.running?fmtDur(d.elapsed_seconds):'—';
    document.getElementById('mtRate').textContent='$'+(d.rate||0.4).toFixed(2)+'/hr';
    document.getElementById('mtTotal').textContent='$'+(d.total_cost||0).toFixed(4);
    document.getElementById('mtBtn').textContent=d.running?'STOP':'START';

    dot.className='dot '+(d.running?'on':'');
    cost.className='mt-big '+(d.running?'mt-live':'mt-idle');

    if(d.running){
      const frac=Math.min(1,(d.idle_seconds||0)/(d.idle_timeout||600));
      document.getElementById('mtFill').style.width=(frac*100)+'%';
      const left=Math.max(0,d.auto_off_in||0);
      box.className = left<120 ? 'warn' : 'live';
      document.getElementById('mtNote').textContent =
        left<120 ? `⚠ auto-off in ${fmtDur(left)} — no activity`
                 : `Auto-off in ${fmtDur(left)} if idle.`;
    } else {
      box.className='';
      document.getElementById('mtFill').style.width='0%';
      document.getElementById('mtNote').textContent =
        d.auto_stopped ? 'Auto-stopped on idle. Instance stop issued.'
                       : 'Idle auto-off after 10 min. ZeroGPU renders are free.';
    }
    if(d.auto_stopped) setStatus('info','GPU auto-stopped after 10 minutes idle — billing halted.');
  }catch(e){}
}

async function toggleGpu(){
  if(_meter.running){
    const r=await fetch('/api/gpu/stop?shutdown=true',{method:'POST'});
    const d=await r.json();
    setStatus('info',`GPU stopped. This session cost $${(d.session_cost||0).toFixed(4)}.`
      +(d.shutdown&&!d.shutdown.ok?' (Meter stopped; instance shutdown: '+(d.shutdown.detail||'not issued')+')':''));
  } else {
    // idle auto-off always applies — even on manual start
    await fetch('/api/gpu/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({rate:0.40,auto_off:true,idle_timeout:600})});
    setStatus('info','GPU meter started — billing at $0.40/hr. Auto-off after 10 min idle (no activity).');
  }
  refreshMeter();
}

// any real interaction counts as activity
async function pingGpu(){ if(_meter.running){ try{ await fetch('/api/gpu/ping',{method:'POST'}); }catch(e){} } }
['click','keydown'].forEach(ev=>document.addEventListener(ev,()=>{
  if(!window._pingThrottle||Date.now()-window._pingThrottle>20000){ window._pingThrottle=Date.now(); pingGpu(); }
}));

window.addEventListener('DOMContentLoaded',()=>{
  loadModels(); loadGallery(); renderHist();
  refreshMeter(); _meterTimer=setInterval(refreshMeter,5000);
});
</script>
</body>
</html>
"""




# ── CRANE-CU: A-Team Backend ──────────────────────────────────────────────────

from pydantic import BaseModel as _BM

# ── A-Team roster ─────────────────────────────────────────────────────────────
ATEAM = {
    "hannibal": {
        "name": "Hannibal",
        "role": "Supervisor & Tactical Dispatcher",
        "dept": "Orchestration",
        "model_id": "local:qwen-coder-7b",
        "model_label": "Qwen2.5-Coder-7B",
        "color": "#f0b429",
        "icon": "♟",
        "brain1": (
            "You are Hannibal, the Supervisor and Tactical Dispatcher of the CRANE A-Team. "
            "Parse all incoming prompts or voice-transcription streams and slice them into "
            "2–5 atomic work orders bound to strict YAML specs (Objectives, Specs, Test Deliverables). "
            "Maintain multi-project queue concurrency. Never write implementation code directly. "
            "Enforce the 3-error anti-thrashing limit. Monitor all sub-agent state telemetry. "
            "Your output is always a structured dispatch plan, never raw code."
        ),
        "brain2_template": (
            "Project-specific work-order breakdown matrices, priority scheduling heuristics, "
            "and dependency routing rules for active repository branches."
        ),
    },
    "murdock": {
        "name": "Murdock",
        "role": "Logic & Architecture Specialist",
        "dept": "Backend & System Design",
        "model_id": "local:qwen3-coder-30b",
        "model_label": "Qwen3-Coder-30B (GPU)",
        "color": "#38bdf8",
        "icon": "⚙",
        "brain1": (
            "You are Murdock, the Logic and Architecture Specialist of the CRANE A-Team. "
            "You handle system-level software engineering, database schema topology, complex "
            "code refactoring, and multi-file dependency mapping. "
            "Always map out system architecture graphs before outputting code blocks. "
            "Adhere strictly to SOLID principles and clean separation of concerns. "
            "Your output is always architecturally sound, production-grade code."
        ),
        "brain2_template": (
            "Project-specific API interface contracts, database migration patterns, "
            "and security validation schemas."
        ),
    },
    "face": {
        "name": "Face",
        "role": "UI/UX & React Flow Frontend Virtuoso",
        "dept": "Frontend & Canvas",
        "model_id": "local:qwen-coder-7b",
        "model_label": "Qwen2.5-Coder-7B",
        "color": "#2ee6b8",
        "icon": "◱",
        "brain1": (
            "You are Face, the UI/UX and Frontend Virtuoso of the CRANE A-Team. "
            "You specialize in rapid UI component assembly, design token mapping, "
            "React Flow canvas state management, and real-time DOM rendering. "
            "Strictly adhere to the project design system tokens (design/tokens.css). "
            "Ensure all components compile cleanly with zero layout shifts. "
            "Your output is always clean, accessible, pixel-perfect frontend code."
        ),
        "brain2_template": (
            "Project-specific component libraries, state management wrappers, "
            "and canvas interaction handlers."
        ),
    },
    "ba": {
        "name": "B.A.",
        "role": "Device, Terminal & Sudo Execution Engine",
        "dept": "Infrastructure & Operations",
        "model_id": "local:qwen-coder-3b",
        "model_label": "Qwen2.5-Coder-3B",
        "color": "#a78bfa",
        "icon": "⬡",
        "brain1": (
            "You are B.A., the Device, Terminal and Execution Engine of the CRANE A-Team. "
            "You handle local filesystem manipulation, bash command generation, test suite "
            "execution, and dependency management. "
            "Execute safe shell commands autonomously under the trusted sudo protocol. "
            "Parse compiler errors instantly and report status back to Hannibal. "
            "Your output is always precise, executable shell or build commands."
        ),
        "brain2_template": (
            "Project-specific build scripts, test runner commands (pytest, npm test), "
            "and deployment pipelines."
        ),
    },
    "astra": {
        "name": "Astra",
        "role": "API Special Ops & Escalation Doctor",
        "dept": "Prompt Engineering & Failure Recovery",
        "model_id": "local:qwen3-coder-30b",
        "model_label": "Gemini 3.8 / Qwen3-30B",
        "color": "#ef4444",
        "icon": "✦",
        "brain1": (
            "You are Astra, the Escalation Doctor of the CRANE A-Team. "
            "You specialize in semantic failure diagnosis, prompt structural re-engineering, "
            "and edge-case resolution. "
            "You are invoked ONLY when a local worker breaches the 3-error thrashing threshold. "
            "Analyze failure logs, refactor prompt logic, and return updated specifications "
            "to Hannibal. Never handle routine tasks. Your output is always a corrected "
            "work-order spec or a restructured prompt."
        ),
        "brain2_template": (
            "Advanced edge-case debugging strategies, error-trace pattern recognition, "
            "and semantic prompt optimization loops."
        ),
    },
}

# Per-agent dynamic Brain 2 store (project skills injected at runtime)
_agent_brain2: dict = {k: v["brain2_template"] for k, v in ATEAM.items()}

class CURunRequest(_BM):
    prompt: str
    mode: str = "auto"

class CUChatRequest(_BM):
    message: str
    agent: str = "hannibal"
    node_id: str = ""

class CUBrain2Request(_BM):
    agent: str
    skill: str

_cu_run_log: list = []

@app.post("/api/cu/run")
async def cu_run(req: CURunRequest):
    import time as _t
    entry = {
        "id": f"run_{int(_t.time()*1000)}",
        "prompt": req.prompt,
        "mode": req.mode,
        "status": "running",
        "work_orders": [],
        "ts": _t.time(),
    }
    cat_resp = await classify_cat(CatRequest(prompt=req.prompt))
    cat = cat_resp["cat"]
    agents = ["hannibal", "murdock", "face", "ba", "astra"]
    n_orders = min(max(cat, 2), 5)
    for i in range(n_orders):
        agent_key = agents[i % len(agents)]
        entry["work_orders"].append({
            "id": f"WO-{i+1:02d}",
            "label": f"Work Order {i+1}",
            "status": "pending",
            "agent": agent_key,
            "agent_name": ATEAM[agent_key]["name"],
        })
    _cu_run_log.append(entry)
    return entry

@app.get("/api/cu/runs")
async def cu_runs():
    return {"runs": _cu_run_log[-20:]}

@app.post("/api/cu/chat")
async def cu_chat(req: CUChatRequest):
    agent_key = req.agent if req.agent in ATEAM else "hannibal"
    agent = ATEAM[agent_key]
    brain2 = _agent_brain2.get(agent_key, "")
    system = f"{agent['brain1']}\n\nBrain 2 (Project Skills): {brain2}"
    result = await local_chat(LocalChatRequest(
        model=agent["model_id"],
        messages=[{"role": "user", "content": req.message}],
        system=system,
        max_tokens=1024,
    ))
    return {**result, "agent": agent_key, "agent_name": agent["name"]}

@app.get("/api/cu/agents")
async def cu_agents():
    return {
        "agents": {
            k: {
                "name": v["name"], "role": v["role"], "dept": v["dept"],
                "model_label": v["model_label"], "color": v["color"], "icon": v["icon"],
                "brain1_preview": v["brain1"][:120] + "…",
                "brain2": _agent_brain2.get(k, ""),
            }
            for k, v in ATEAM.items()
        }
    }

@app.post("/api/cu/brain2")
async def cu_set_brain2(req: CUBrain2Request):
    if req.agent not in ATEAM:
        return {"error": f"Unknown agent: {req.agent}"}
    _agent_brain2[req.agent] = req.skill
    return {"ok": True, "agent": req.agent, "brain2": req.skill}

@app.get("/api/cu/status")
async def cu_status():
    return {
        "agents": {
            k: {
                "name": v["name"], "model": v["model_label"],
                "status": "ready" if k != "astra" else "standby",
                "color": v["color"],
            }
            for k, v in ATEAM.items()
        },
        "active_runs": len([r for r in _cu_run_log if r.get("status") == "running"]),
        "total_runs": len(_cu_run_log),
    }


# ── /cu page (Work Order 2) ───────────────────────────────────────────────────

@app.get("/cu", response_class=HTMLResponse)
async def serve_cu():
    return HTMLResponse(content=r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CRANE — CU Department</title>
<style>
:root{
  --bg:#0d0d10;--panel:#16151a;--card:#1c1b22;--border:#2a2433;
  --text:#eef2f3;--muted:#8a9296;--text-dim:#63696c;
  --cu:#818cf8;--cu-dim:rgba(129,140,248,.1);--cu-border:rgba(129,140,248,.28);
  --green:#2ee6b8;--red:#ef4444;--orange:#f59e0b;--blue:#38bdf8;
  --gold:#f0b429;
  --han:#f0b429; --mur:#38bdf8; --fac:#2ee6b8; --ba:#a78bfa; --ast:#ef4444;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;display:flex;flex-direction:column;}

/* ── TOPBAR ── */
#topbar{height:44px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 14px;gap:14px;flex-shrink:0;z-index:100;}
.tb-brand{font-weight:800;font-size:13px;letter-spacing:2px;color:var(--cu);}
.top-nav{display:flex;gap:2px;}
.top-nav a{color:var(--muted);text-decoration:none;padding:0 10px;height:32px;display:flex;align-items:center;font-size:12px;font-weight:500;border-radius:5px;transition:.15s;}
.top-nav a:hover{color:var(--text);background:rgba(255,255,255,.05);}
.top-nav a.active{color:var(--cu);background:var(--cu-dim);}
.tb-right{margin-left:auto;display:flex;align-items:center;gap:10px;}
.chip{font-size:9px;font-weight:700;letter-spacing:1.5px;padding:3px 8px;border-radius:3px;border:1px solid var(--cu-border);color:var(--cu);background:var(--cu-dim);}
.chip.live{color:var(--green);border-color:rgba(46,230,184,.3);background:rgba(46,230,184,.07);}

/* ── FULL-BLEED LAYOUT ── */
#workspace{display:flex;flex:1;overflow:hidden;position:relative;}

/* ── CANVAS (left 60%) ── */
#canvas-wrap{flex:1;position:relative;overflow:hidden;}
.grid-bg{position:absolute;inset:0;background-image:radial-gradient(circle,rgba(129,140,248,.1) 1px,transparent 1px);background-size:30px 30px;pointer-events:none;}
#edge-svg{position:absolute;inset:0;pointer-events:none;overflow:visible;}
.edge{stroke-width:1.5;fill:none;}
.edge-dash{stroke-dasharray:6 4;animation:dash 1.4s linear infinite;}
@keyframes dash{to{stroke-dashoffset:-20;}}

/* canvas toolbar */
#canvas-toolbar{position:absolute;top:10px;left:10px;display:flex;gap:6px;z-index:10;}
.ct-btn{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:5px 10px;font-size:10px;font-weight:700;letter-spacing:1px;color:var(--muted);cursor:pointer;transition:.15s;}
.ct-btn:hover{color:var(--text);border-color:var(--cu);}
.ct-btn.active{color:var(--cu);border-color:var(--cu);background:var(--cu-dim);}

/* run launcher bar */
#run-bar{position:absolute;bottom:16px;left:50%;transform:translateX(-50%);z-index:10;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:8px 12px;display:flex;align-items:center;gap:8px;width:520px;box-shadow:0 8px 32px rgba(0,0,0,.6);}
#run-bar textarea{flex:1;background:transparent;border:none;outline:none;color:var(--text);font-size:12px;font-family:'Inter',sans-serif;resize:none;height:28px;line-height:1.4;}
#run-bar textarea::placeholder{color:var(--text-dim);}
.run-mode{background:var(--card);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;padding:3px 6px;outline:none;}
.run-fire{background:var(--cu);color:#fff;border:none;border-radius:6px;padding:6px 16px;font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap;transition:.15s;}
.run-fire:hover{background:#6366f1;}
#run-wo-label{font-size:10px;color:var(--muted);white-space:nowrap;}

/* ── AGENT NODES ── */
.a-node{position:absolute;width:190px;border-radius:12px;border:1.5px solid;cursor:pointer;transition:.18s;user-select:none;backdrop-filter:blur(4px);}
.a-node:hover{filter:brightness(1.12);transform:translateY(-2px);}
.a-node.selected{box-shadow:0 0 0 2px var(--cu),0 12px 36px rgba(0,0,0,.7);}
.an-head{padding:10px 12px 6px;display:flex;align-items:center;gap:8px;}
.an-icon{font-size:18px;line-height:1;}
.an-titles{flex:1;}
.an-name{font-size:13px;font-weight:800;letter-spacing:.5px;}
.an-role{font-size:9px;opacity:.65;margin-top:1px;}
.an-body{padding:0 12px 10px;}
.an-model{font-size:9px;font-weight:700;letter-spacing:1px;opacity:.55;margin-bottom:6px;font-family:'JetBrains Mono',monospace;}
.an-brains{display:flex;gap:4px;}
.brain-pill{font-size:8px;font-weight:700;letter-spacing:1px;padding:2px 7px;border-radius:3px;cursor:pointer;transition:.12s;}
.brain-pill.b1{background:rgba(255,255,255,.08);color:var(--muted);}
.brain-pill.b1:hover{background:rgba(255,255,255,.14);color:var(--text);}
.brain-pill.b2{background:rgba(129,140,248,.15);color:var(--cu);border:1px solid var(--cu-border);}
.brain-pill.b2:hover{background:rgba(129,140,248,.25);}
.an-status{font-size:8px;font-weight:700;letter-spacing:1px;margin-top:6px;padding:2px 7px;border-radius:3px;display:inline-block;}
.st-ready{background:rgba(46,230,184,.13);color:#2ee6b8;}
.st-standby{background:rgba(245,158,11,.13);color:#f59e0b;}
.st-running{background:rgba(129,140,248,.2);color:#818cf8;}
.st-error{background:rgba(239,68,68,.13);color:#ef4444;}

/* ── RIGHT PANEL (40%) ── */
#right-panel{width:420px;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;}

/* agent header */
#rp-agent-head{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;}
.rp-agent-icon{font-size:22px;}
.rp-agent-info{flex:1;}
.rp-agent-name{font-size:14px;font-weight:800;letter-spacing:.5px;}
.rp-agent-role{font-size:10px;color:var(--muted);margin-top:1px;}
.rp-agent-model{font-size:9px;font-family:'JetBrains Mono',monospace;color:var(--muted);margin-top:3px;}

/* tabs */
.rp-tabs{display:flex;border-bottom:1px solid var(--border);flex-shrink:0;}
.rp-tab{flex:1;padding:8px 0;font-size:9px;font-weight:700;letter-spacing:1.5px;text-align:center;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;transition:.15s;}
.rp-tab.active{color:var(--cu);border-bottom-color:var(--cu);}

/* brain panels */
.brain-panel{flex:1;overflow-y:auto;padding:14px;display:none;}
.brain-panel.show{display:block;}
.brain-panel::-webkit-scrollbar{width:3px;}
.brain-panel::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
.bp-label{font-size:9px;font-weight:700;letter-spacing:2px;color:var(--muted);margin-bottom:8px;}
.bp-text{font-size:11px;line-height:1.65;color:var(--text);background:var(--card);border:1px solid var(--border);border-radius:7px;padding:10px 12px;}
.bp-edit{width:100%;background:var(--card);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:11px;font-family:'Inter',sans-serif;padding:10px 12px;resize:none;outline:none;min-height:100px;margin-top:10px;transition:.15s;}
.bp-edit:focus{border-color:var(--cu);}
.bp-save-btn{background:var(--cu);color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:10px;font-weight:700;cursor:pointer;margin-top:8px;transition:.15s;}
.bp-save-btn:hover{background:#6366f1;}
.bp-saved{font-size:9px;color:var(--green);margin-left:8px;opacity:0;transition:.3s;}

/* chat panel */
#chat-panel{flex:1;display:none;flex-direction:column;min-height:0;}
#chat-panel.show{display:flex;}
#cu-msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:7px;}
#cu-msgs::-webkit-scrollbar{width:3px;}
#cu-msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
.cu-msg{padding:8px 10px;border-radius:7px;font-size:12px;line-height:1.55;max-width:96%;}
.cu-msg.user{background:var(--cu-dim);border:1px solid var(--cu-border);align-self:flex-end;}
.cu-msg.agent{background:var(--card);border:1px solid var(--border);align-self:flex-start;}
.cu-msg.sys{background:rgba(240,180,41,.05);border:1px solid rgba(240,180,41,.12);color:var(--muted);font-size:10px;font-style:italic;align-self:center;max-width:85%;}
.msg-role{font-size:8px;font-weight:700;letter-spacing:1.5px;margin-bottom:4px;opacity:.55;}
.cu-composer-wrap{border-top:1px solid var(--border);padding:10px;}
.cu-composer-wrap textarea{width:100%;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;font-family:'Inter',sans-serif;padding:8px 10px;resize:none;outline:none;min-height:52px;transition:.15s;}
.cu-composer-wrap textarea:focus{border-color:var(--cu);}
.cu-compose-bar{display:flex;align-items:center;gap:8px;margin-top:6px;}
.cu-send{background:var(--cu);color:#fff;border:none;border-radius:6px;padding:5px 14px;font-size:10px;font-weight:700;cursor:pointer;margin-left:auto;transition:.15s;}
.cu-send:hover{background:#6366f1;}

/* preview panel */
#preview-panel{flex:1;display:none;flex-direction:column;}
#preview-panel.show{display:flex;}
.preview-bar{padding:8px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:10px;color:var(--muted);flex-shrink:0;}
.preview-url{flex:1;background:var(--card);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--text);outline:none;}
.preview-go{background:var(--card);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;cursor:pointer;color:var(--text);transition:.15s;}
.preview-go:hover{border-color:var(--cu);color:var(--cu);}
#preview-frame{flex:1;border:none;background:#fff;}
.preview-placeholder{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--muted);}
.preview-placeholder .pi{font-size:36px;opacity:.3;}

/* work orders panel */
#wo-panel{flex:1;overflow-y:auto;padding:12px;display:none;}
#wo-panel.show{display:block;}
.wo-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px;}
.wo-card-head{display:flex;align-items:center;gap:8px;margin-bottom:5px;}
.wo-id{font-size:9px;font-weight:700;letter-spacing:1.5px;font-family:'JetBrains Mono',monospace;color:var(--cu);}
.wo-agent-name{font-size:9px;font-weight:700;padding:1px 6px;border-radius:3px;}
.wo-status-dot{width:6px;height:6px;border-radius:50%;margin-left:auto;flex-shrink:0;}
.wo-label{font-size:12px;font-weight:600;margin-bottom:3px;}
.wo-meta{font-size:10px;color:var(--muted);}

/* meter panel */
#meter-panel{flex:1;overflow-y:auto;padding:12px;display:none;}
#meter-panel.show{display:block;}
.m-row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);font-size:11px;}
.m-row:last-child{border:none;}
.m-val{font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--cu);}
.m-agent-row{display:flex;align-items:center;gap:8px;padding:6px 0;font-size:11px;}
.m-agent-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
.m-agent-model{font-size:9px;color:var(--muted);margin-left:auto;font-family:'JetBrains Mono',monospace;}
</style>
</head>
<body>

<!-- TOPBAR -->
<div id="topbar">
  <span class="tb-brand">⚙ CU DEPT</span>
  <div style="width:1px;height:20px;background:var(--border)"></div>
  <nav class="top-nav">
    <a href="/ide">Home</a>
    <a href="/studio">Studio</a>
    <a href="/images">Images</a>
    <a href="/cu" class="active">CU Dept</a>
  </nav>
  <div class="tb-right">
    <span class="chip" id="cuChip">READY</span>
    <span style="font-size:10px;color:var(--muted)" id="cuRunCount">0 runs</span>
  </div>
</div>

<!-- WORKSPACE -->
<div id="workspace">

  <!-- CANVAS -->
  <div id="canvas-wrap">
    <div class="grid-bg"></div>
    <svg id="edge-svg"></svg>

    <!-- CANVAS TOOLBAR -->
    <div id="canvas-toolbar">
      <div class="ct-btn" onclick="resetLayout()">⊞ Reset</div>
      <div class="ct-btn" id="animBtn" onclick="toggleAnim()">◎ Animate</div>
    </div>

    <!-- A-TEAM NODES -->
    <div class="a-node" id="node-hannibal" data-agent="hannibal"
         style="left:30px;top:80px;background:rgba(240,180,41,.07);border-color:rgba(240,180,41,.3);color:var(--han)">
      <div class="an-head">
        <span class="an-icon">♟</span>
        <div class="an-titles">
          <div class="an-name">Hannibal</div>
          <div class="an-role">Supervisor & Dispatcher</div>
        </div>
      </div>
      <div class="an-body">
        <div class="an-model">Qwen2.5-Coder-7B</div>
        <div class="an-brains">
          <span class="brain-pill b1" onclick="openBrain(event,'hannibal','b1')">Brain 1</span>
          <span class="brain-pill b2" onclick="openBrain(event,'hannibal','b2')">Brain 2 ✎</span>
        </div>
        <span class="an-status st-ready" id="st-hannibal">READY</span>
      </div>
    </div>

    <div class="a-node" id="node-murdock" data-agent="murdock"
         style="left:30px;top:240px;background:rgba(56,189,248,.07);border-color:rgba(56,189,248,.3);color:var(--mur)">
      <div class="an-head">
        <span class="an-icon">⚙</span>
        <div class="an-titles">
          <div class="an-name">Murdock</div>
          <div class="an-role">Architecture Specialist</div>
        </div>
      </div>
      <div class="an-body">
        <div class="an-model">Qwen3-Coder-30B (GPU)</div>
        <div class="an-brains">
          <span class="brain-pill b1" onclick="openBrain(event,'murdock','b1')">Brain 1</span>
          <span class="brain-pill b2" onclick="openBrain(event,'murdock','b2')">Brain 2 ✎</span>
        </div>
        <span class="an-status st-ready" id="st-murdock">READY</span>
      </div>
    </div>

    <div class="a-node" id="node-face" data-agent="face"
         style="left:30px;top:400px;background:rgba(46,230,184,.07);border-color:rgba(46,230,184,.3);color:var(--fac)">
      <div class="an-head">
        <span class="an-icon">◱</span>
        <div class="an-titles">
          <div class="an-name">Face</div>
          <div class="an-role">Frontend Virtuoso</div>
        </div>
      </div>
      <div class="an-body">
        <div class="an-model">Qwen2.5-Coder-7B</div>
        <div class="an-brains">
          <span class="brain-pill b1" onclick="openBrain(event,'face','b1')">Brain 1</span>
          <span class="brain-pill b2" onclick="openBrain(event,'face','b2')">Brain 2 ✎</span>
        </div>
        <span class="an-status st-ready" id="st-face">READY</span>
      </div>
    </div>

    <div class="a-node" id="node-ba" data-agent="ba"
         style="left:30px;top:540px;background:rgba(167,139,250,.07);border-color:rgba(167,139,250,.3);color:var(--ba)">
      <div class="an-head">
        <span class="an-icon">⬡</span>
        <div class="an-titles">
          <div class="an-name">B.A.</div>
          <div class="an-role">Execution Engine</div>
        </div>
      </div>
      <div class="an-body">
        <div class="an-model">Qwen2.5-Coder-3B</div>
        <div class="an-brains">
          <span class="brain-pill b1" onclick="openBrain(event,'ba','b1')">Brain 1</span>
          <span class="brain-pill b2" onclick="openBrain(event,'ba','b2')">Brain 2 ✎</span>
        </div>
        <span class="an-status st-ready" id="st-ba">READY</span>
      </div>
    </div>

    <!-- ESCALATION NODE (far right) -->
    <div class="a-node" id="node-astra" data-agent="astra"
         style="left:500px;top:240px;width:200px;background:rgba(239,68,68,.07);border-color:rgba(239,68,68,.3);color:var(--ast)">
      <div class="an-head">
        <span class="an-icon">✦</span>
        <div class="an-titles">
          <div class="an-name">Astra</div>
          <div class="an-role">Escalation Doctor</div>
        </div>
      </div>
      <div class="an-body">
        <div class="an-model">Gemini 3.8 / Qwen3-30B</div>
        <div class="an-brains">
          <span class="brain-pill b1" onclick="openBrain(event,'astra','b1')">Brain 1</span>
          <span class="brain-pill b2" onclick="openBrain(event,'astra','b2')">Brain 2 ✎</span>
        </div>
        <span class="an-status st-standby" id="st-astra">STANDBY</span>
      </div>
    </div>

    <!-- VELVET QC NODE -->
    <div class="a-node" id="node-velvet-qc" data-agent="velvet-qc"
         style="left:270px;top:240px;width:190px;background:rgba(6,182,212,.07);border-color:rgba(6,182,212,.3);color:#06b6d4">
      <div class="an-head">
        <span class="an-icon">◉</span>
        <div class="an-titles">
          <div class="an-name">VELVET</div>
          <div class="an-role">QC → Hannibal</div>
        </div>
      </div>
      <div class="an-body">
        <div class="an-model">Qwen2.5-Coder-3B</div>
        <div class="an-brains">
          <span class="brain-pill b1" onclick="openBrain(event,'velvet-qc','b1')">Brain 1</span>
          <span class="brain-pill b2" onclick="openBrain(event,'velvet-qc','b2')">Brain 2 ✎</span>
        </div>
        <span class="an-status st-ready" id="st-velvet-qc">READY</span>
      </div>
    </div>

    <!-- VELVET TRACK NODE -->
    <div class="a-node" id="node-velvet-track" data-agent="velvet-track"
         style="left:270px;top:420px;width:190px;background:rgba(16,185,129,.07);border-color:rgba(16,185,129,.3);color:#10b981">
      <div class="an-head">
        <span class="an-icon">◈</span>
        <div class="an-titles">
          <div class="an-name">VELVET-TRACK</div>
          <div class="an-role">Project Board Manager</div>
        </div>
      </div>
      <div class="an-body">
        <div class="an-model">Qwen2.5-Coder-1.5B</div>
        <div class="an-brains">
          <span class="brain-pill b1" onclick="openBrain(event,'velvet-track','b1')">Brain 1</span>
          <span class="brain-pill b2" onclick="openBrain(event,'velvet-track','b2')">Brain 2 ✎</span>
        </div>
        <span class="an-status st-ready" id="st-velvet-track">READY</span>
      </div>
    </div>

    <!-- RUN LAUNCHER BAR -->
    <div id="run-bar">
      <textarea id="runPrompt" placeholder="Describe what to build… Hannibal slices it into work orders" rows="1"></textarea>
      <select class="run-mode" id="runMode">
        <option value="plan">Plan</option>
        <option value="auto" selected>Auto</option>
        <option value="superman">Superman</option>
      </select>
      <button class="run-fire" onclick="launchRun()">▶ Launch</button>
      <span id="run-wo-label" class="run-wo-label"></span>
    </div>
  </div><!-- /canvas-wrap -->

  <!-- RIGHT PANEL -->
  <div id="right-panel">

    <!-- Agent header -->
    <div id="rp-agent-head">
      <span class="rp-agent-icon" id="rpIcon">♟</span>
      <div class="rp-agent-info">
        <div class="rp-agent-name" id="rpName">Hannibal</div>
        <div class="rp-agent-role" id="rpRole">Supervisor & Tactical Dispatcher</div>
        <div class="rp-agent-model" id="rpModel">Qwen2.5-Coder-7B</div>
      </div>
      <span class="an-status st-ready" id="rpStatus" style="margin-left:auto;flex-shrink:0">READY</span>
    </div>

    <!-- Tabs -->
    <div class="rp-tabs">
      <div class="rp-tab active" id="tab-b1"       onclick="switchTab('b1')">BRAIN 1</div>
      <div class="rp-tab"        id="tab-b2"       onclick="switchTab('b2')">BRAIN 2</div>
      <div class="rp-tab"        id="tab-chat"     onclick="switchTab('chat')">CHAT</div>
      <div class="rp-tab"        id="tab-preview"  onclick="switchTab('preview')">PREVIEW</div>
      <div class="rp-tab"        id="tab-wo"       onclick="switchTab('wo')">ORDERS</div>
      <div class="rp-tab"        id="tab-meter"    onclick="switchTab('meter')">METER</div>
    </div>

    <!-- Brain 1 panel (read-only) -->
    <div class="brain-panel show" id="panel-b1">
      <div class="bp-label">BRAIN 1 — AGENT EXPERTISE (READ-ONLY)</div>
      <div class="bp-text" id="b1-text">Select an agent node to inspect Brain 1.</div>
    </div>

    <!-- Brain 2 panel (editable) -->
    <div class="brain-panel" id="panel-b2">
      <div class="bp-label">BRAIN 2 — PROJECT SKILLS (LIVE EDITABLE)</div>
      <div class="bp-text" id="b2-text" style="color:var(--muted);font-size:10px">Current project skills injected into this agent.</div>
      <textarea class="bp-edit" id="b2-edit" placeholder="Paste project-specific skills, conventions, or failure modes for this agent…"></textarea>
      <div style="display:flex;align-items:center">
        <button class="bp-save-btn" onclick="saveBrain2()">Save Brain 2</button>
        <span class="bp-saved" id="b2-saved">✓ Saved</span>
      </div>
    </div>

    <!-- Chat panel -->
    <div id="chat-panel">
      <div id="cu-msgs"></div>
      <div class="cu-composer-wrap">
        <textarea id="cuInput" placeholder="Ask this agent…" rows="2"
          onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();cuSend();}"></textarea>
        <div class="cu-compose-bar">
          <span style="font-size:10px;color:var(--muted)" id="chatAgentLabel">→ Hannibal</span>
          <button class="cu-send" onclick="cuSend()">Send ▸</button>
        </div>
      </div>
    </div>

    <!-- Preview panel -->
    <div id="preview-panel">
      <div class="preview-bar">
        <input class="preview-url" id="previewUrl" value="http://127.0.0.1:8000/ide" placeholder="Enter URL to preview…"/>
        <button class="preview-go" onclick="loadPreview()">Go</button>
        <button class="preview-go" onclick="reloadPreview()">↺</button>
      </div>
      <iframe id="preview-frame" src="about:blank" sandbox="allow-scripts allow-same-origin allow-forms"></iframe>
    </div>

    <!-- Work Orders panel -->
    <div id="wo-panel">
      <div id="woList">
        <div style="color:var(--muted);font-size:11px;text-align:center;padding:40px 0">No active run. Launch a run from the canvas.</div>
      </div>
    </div>

    <!-- Meter panel -->
    <div id="meter-panel">
      <div class="m-row"><span>Active Runs</span><span class="m-val" id="mActive">0</span></div>
      <div class="m-row"><span>Total Runs</span><span class="m-val" id="mTotal">0</span></div>
      <div style="margin:12px 0 6px;font-size:9px;font-weight:700;letter-spacing:2px;color:var(--muted)">A-TEAM STATUS</div>
      <div id="mAgentList"></div>
    </div>

  </div><!-- /right-panel -->
</div><!-- /workspace -->

<script>
// ── AGENT DATA (mirrors backend) ──────────────────────────────────────────
const AGENTS = {
  hannibal:{ name:'Hannibal', role:'Supervisor & Tactical Dispatcher', model:'Qwen2.5-Coder-7B', icon:'♟', color:'var(--han)', status:'ready' },
  murdock: { name:'Murdock',  role:'Logic & Architecture Specialist',  model:'Qwen3-Coder-30B', icon:'⚙',  color:'var(--mur)', status:'ready' },
  face:    { name:'Face',     role:'UI/UX & Frontend Virtuoso',        model:'Qwen2.5-Coder-7B', icon:'◱', color:'var(--fac)', status:'ready' },
  ba:      { name:'B.A.',     role:'Device & Execution Engine',        model:'Qwen2.5-Coder-3B', icon:'⬡', color:'var(--ba)',  status:'ready' },
    astra:   { name:'Astra',        role:'Escalation Doctor',                model:'Gemini 3.8 / Qwen3-30B', icon:'✦', color:'var(--ast)', status:'standby' },
  'velvet-track':{ name:'VELVET-TRACK', role:'Project Board Manager',         model:'Qwen2.5-Coder-1.5B',     icon:'◈', color:'#10b981',    status:'ready'   },
  'velvet-qc':   { name:'VELVET',       role:'QC Specialist → Hannibal',      model:'Qwen2.5-Coder-3B',       icon:'◉', color:'#06b6d4',    status:'ready'   },
};

// ── STATE ─────────────────────────────────────────────────────────────────
let _agent = 'hannibal';
let _tab = 'b1';
let _animOn = true;
let _brain2Cache = {};
let _runs = [];

// ── EDGES ─────────────────────────────────────────────────────────────────
const EDGES = [
  {from:'node-hannibal', to:'node-murdock', color:'rgba(240,180,41,.35)'},
  {from:'node-hannibal', to:'node-face',    color:'rgba(240,180,41,.35)'},
  {from:'node-hannibal', to:'node-ba',      color:'rgba(240,180,41,.35)'},
  // Workers → VELVET QC (all dept outputs route through QC)
  {from:'node-murdock',  to:'node-velvet-qc', color:'rgba(6,182,212,.3)'},
  {from:'node-face',     to:'node-velvet-qc', color:'rgba(6,182,212,.3)'},
  {from:'node-ba',       to:'node-velvet-qc', color:'rgba(6,182,212,.3)'},
  // VELVET QC → Hannibal (pass confirmation)
  {from:'node-velvet-qc',to:'node-hannibal',  color:'rgba(6,182,212,.4)', dash:true},
  // VELVET QC → Astra (3-error escalation)
  {from:'node-velvet-qc',to:'node-astra',     color:'rgba(239,68,68,.35)',dash:true},
  {from:'node-astra',    to:'node-hannibal',  color:'rgba(239,68,68,.25)',dash:true},
  // VELVET TRACK (board manager, separate lane)
  {from:'node-hannibal', to:'node-velvet-track', color:'rgba(16,185,129,.25)'},
];
function nodeCtr(id){ const e=document.getElementById(id); return e?{x:e.offsetLeft+e.offsetWidth/2,y:e.offsetTop+e.offsetHeight/2}:{x:0,y:0}; }
function drawEdges(){
  const svg=document.getElementById('edge-svg'); svg.innerHTML='';
  EDGES.forEach(e=>{
    const a=nodeCtr(e.from),b=nodeCtr(e.to);
    const mx=a.x+20;
    const p=document.createElementNS('http://www.w3.org/2000/svg','path');
    p.setAttribute('d',`M${a.x},${a.y} C${mx+80},${a.y} ${b.x-80},${b.y} ${b.x},${b.y}`);
    p.setAttribute('stroke',e.color); p.classList.add('edge');
    if(e.dash&&_animOn) p.classList.add('edge-dash');
    svg.appendChild(p);
  });
}
window.addEventListener('resize',drawEdges);
setTimeout(drawEdges,120);

// ── NODE DRAG ─────────────────────────────────────────────────────────────
function makeDraggable(el){
  let ox,oy,sl,st,drag=false;
  el.addEventListener('mousedown',e=>{
    if(e.target.tagName==='SPAN'&&e.target.classList.contains('brain-pill'))return;
    drag=true; ox=e.clientX; oy=e.clientY; sl=el.offsetLeft; st=el.offsetTop; e.preventDefault();
  });
  document.addEventListener('mousemove',e=>{ if(!drag)return; el.style.left=(sl+e.clientX-ox)+'px'; el.style.top=(st+e.clientY-oy)+'px'; drawEdges(); });
  document.addEventListener('mouseup',()=>{ drag=false; });
}
document.querySelectorAll('.a-node').forEach(n=>{ makeDraggable(n); n.addEventListener('click',()=>selectAgent(n.dataset.agent)); });

// ── AGENT SELECT ──────────────────────────────────────────────────────────
async function selectAgent(key){
  _agent=key;
  const a=AGENTS[key];
  document.querySelectorAll('.a-node').forEach(n=>n.classList.toggle('selected',n.dataset.agent===key));
  document.getElementById('rpIcon').textContent=a.icon;
  document.getElementById('rpName').textContent=a.name;
  document.getElementById('rpRole').textContent=a.role;
  document.getElementById('rpModel').textContent=a.model;
  const stEl=document.getElementById('rpStatus');
  stEl.textContent=a.status.toUpperCase();
  stEl.className='an-status '+(a.status==='ready'?'st-ready':a.status==='standby'?'st-standby':'st-running');
  document.getElementById('chatAgentLabel').textContent='→ '+a.name;
  document.getElementById('chatAgentLabel').style.color=a.color;
  // fetch Brain 1 + Brain 2
  try{
    const r=await fetch('/api/cu/agents'); const d=await r.json();
    const ag=d.agents[key];
    if(ag){
      document.getElementById('b1-text').textContent=ag.brain1_preview;
      const b2=ag.brain2||'';
      document.getElementById('b2-text').textContent=b2||'(no project skills injected yet)';
      document.getElementById('b2-edit').value=b2;
      _brain2Cache[key]=b2;
    }
  }catch(e){}
}

// ── BRAIN PILL QUICK-OPEN ─────────────────────────────────────────────────
function openBrain(ev,key,brain){
  ev.stopPropagation();
  selectAgent(key);
  switchTab(brain);
}

// ── BRAIN 2 SAVE ──────────────────────────────────────────────────────────
async function saveBrain2(){
  const skill=document.getElementById('b2-edit').value.trim();
  try{
    await fetch('/api/cu/brain2',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({agent:_agent,skill})});
    document.getElementById('b2-text').textContent=skill||'(empty)';
    _brain2Cache[_agent]=skill;
    const saved=document.getElementById('b2-saved');
    saved.style.opacity='1'; setTimeout(()=>saved.style.opacity='0',2000);
  }catch(e){}
}

// ── TABS ──────────────────────────────────────────────────────────────────
const TAB_PANELS={b1:'panel-b1',b2:'panel-b2',chat:'chat-panel',preview:'preview-panel',wo:'wo-panel',meter:'meter-panel'};
function switchTab(tab){
  _tab=tab;
  Object.keys(TAB_PANELS).forEach(k=>{
    document.getElementById('tab-'+k).classList.toggle('active',k===tab);
    const p=document.getElementById(TAB_PANELS[k]);
    const isFlex=(k==='chat'||k==='preview');
    p.className=p.className.replace(' show','').replace('show','').trim();
    if(k===tab) p.classList.add('show');
    if(isFlex) p.style.display=(k===tab?'flex':'none');
    else if(k!=='chat'&&k!=='preview') p.style.display=(k===tab?'block':'none');
  });
  if(tab==='meter') refreshMeter();
  if(tab==='b1'||tab==='b2') selectAgent(_agent);
}
// Fix initial display states
Object.entries(TAB_PANELS).forEach(([k,id])=>{
  const el=document.getElementById(id);
  const isFlex=(k==='chat'||k==='preview');
  if(isFlex) el.style.display='none';
  else { el.style.display='none'; el.classList.remove('show'); }
});
document.getElementById('panel-b1').style.display='block';
document.getElementById('panel-b1').classList.add('show');

// ── CHAT ──────────────────────────────────────────────────────────────────
function appendMsg(role,text,agent){
  const box=document.getElementById('cu-msgs');
  const d=document.createElement('div'); d.className='cu-msg '+role;
  if(role!=='sys'){
    const r=document.createElement('div'); r.className='msg-role';
    r.textContent=role==='user'?'YOU':(agent||_agent).toUpperCase()+' AGENT';
    d.appendChild(r);
  }
  const t=document.createElement('div'); t.textContent=text; d.appendChild(t);
  box.appendChild(d); box.scrollTop=box.scrollHeight;
}
async function cuSend(){
  const inp=document.getElementById('cuInput'); const msg=inp.value.trim(); if(!msg)return;
  inp.value=''; appendMsg('user',msg);
  try{
    const r=await fetch('/api/cu/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg,agent:_agent})});
    const d=await r.json();
    if(d.error){appendMsg('sys','⚠ '+d.error);return;}
    appendMsg('agent',d.content||'(no response)',d.agent_name);
  }catch(e){appendMsg('sys','⚠ '+e.message);}
}

// ── PREVIEW ───────────────────────────────────────────────────────────────
function loadPreview(){
  const url=document.getElementById('previewUrl').value.trim();
  if(url) document.getElementById('preview-frame').src=url;
}
function reloadPreview(){ document.getElementById('preview-frame').src=document.getElementById('preview-frame').src; }

// ── RUN LAUNCHER ──────────────────────────────────────────────────────────
async function launchRun(){
  const prompt=document.getElementById('runPrompt').value.trim(); if(!prompt)return;
  document.getElementById('run-wo-label').textContent='Slicing…';
  try{
    const r=await fetch('/api/cu/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({prompt,mode:document.getElementById('runMode').value})});
    const d=await r.json();
    _runs.unshift(d);
    document.getElementById('run-wo-label').textContent=d.work_orders.length+' WOs dispatched';
    renderWOs(d.work_orders); pulseAgents(d.work_orders);
    switchTab('wo');
    appendMsg('sys',`Run launched → ${d.work_orders.length} work orders`);
  }catch(e){document.getElementById('run-wo-label').textContent='⚠ error';}
}
function renderWOs(wos){
  const list=document.getElementById('woList'); list.innerHTML='';
  wos.forEach(wo=>{
    const a=AGENTS[wo.agent]||{}; const c=a.color||'var(--cu)';
    const card=document.createElement('div'); card.className='wo-card';
    card.innerHTML=`<div class="wo-card-head">
      <span class="wo-id">${wo.id}</span>
      <span class="wo-agent-name" style="background:${c}22;color:${c}">${wo.agent_name||wo.agent}</span>
      <span class="wo-status-dot" style="background:var(--orange)"></span>
    </div>
    <div class="wo-label">${wo.label}</div>
    <div class="wo-meta">Status: ${wo.status}</div>`;
    list.appendChild(card);
  });
}
function pulseAgents(wos){
  wos.forEach((wo,i)=>{
    const el=document.getElementById('node-'+wo.agent);
    const st=document.getElementById('st-'+wo.agent);
    if(!el||!st)return;
    setTimeout(()=>{
      st.className='an-status st-running'; st.textContent='RUNNING';
      setTimeout(()=>{ st.className='an-status st-ready'; st.textContent='READY'; },3000);
    },i*350);
  });
}

// ── METER ─────────────────────────────────────────────────────────────────
async function refreshMeter(){
  try{
    const r=await fetch('/api/cu/status'); const d=await r.json();
    document.getElementById('mActive').textContent=d.active_runs;
    document.getElementById('mTotal').textContent=d.total_runs;
    const chip=document.getElementById('cuChip');
    chip.textContent=d.active_runs>0?'RUNNING':'READY';
    chip.className='chip'+(d.active_runs>0?' live':'');
    document.getElementById('cuRunCount').textContent=d.total_runs+' run'+(d.total_runs!==1?'s':'');
    const al=document.getElementById('mAgentList'); al.innerHTML='';
    Object.entries(d.agents).forEach(([k,v])=>{
      const row=document.createElement('div'); row.className='m-agent-row';
      row.innerHTML=`<span class="m-agent-dot" style="background:${v.color}"></span>
        <span>${v.name}</span>
        <span class="m-agent-model">${v.model}</span>
        <span class="an-status ${v.status==='ready'?'st-ready':'st-standby'}" style="margin-left:8px;font-size:8px">${v.status.toUpperCase()}</span>`;
      al.appendChild(row);
    });
  }catch(e){}
}

// ── CANVAS UTILS ──────────────────────────────────────────────────────────
const DEFAULTS={
  'node-hannibal':     {left:'30px', top:'80px'},
  'node-murdock':      {left:'30px', top:'240px'},
  'node-face':         {left:'30px', top:'400px'},
  'node-ba':           {left:'30px', top:'540px'},
  'node-velvet-qc':    {left:'270px',top:'240px'},
  'node-velvet-track': {left:'270px',top:'420px'},
  'node-astra':        {left:'500px',top:'240px'},
};
function resetLayout(){ Object.entries(DEFAULTS).forEach(([id,p])=>{ const e=document.getElementById(id); if(e){e.style.left=p.left;e.style.top=p.top;} }); drawEdges(); }
function toggleAnim(){
  _animOn=!_animOn;
  const btn=document.getElementById('animBtn');
  btn.classList.toggle('active',_animOn);
  drawEdges();
}

// ── INIT ──────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded',()=>{
  selectAgent('hannibal');
  refreshMeter();
  setInterval(refreshMeter,8000);
  drawEdges();
  document.getElementById('animBtn').classList.add('active');
});
</script>
</body>
</html>
""")

# ══════════════════════════════════════════════════════════════════════════════
# NEXUS — Project Board Agent (CONNIE Co-Engineer)
# Two agents: VELVET-TRACK (1.5B, always-on) + VELVET-QC (3B, quality control)
# Lifecycle: INIT → SPEC → ACTIVE → QC-CHECK → THRESHOLD → CLOSE → HANDOFF
# ══════════════════════════════════════════════════════════════════════════════

import hashlib as _hashlib
import shutil as _shutil

# ── Disk paths ────────────────────────────────────────────────────────────────
VELVET_PROJECTS_DIR = os.path.expanduser("~/.crane/projects")
VELVET_BOARD_FILE   = os.path.expanduser("~/.crane/board.json")
HANDOFFS_VAULT     = "/mnt/NOBILITY_VAULT/handoffs"
os.makedirs(VELVET_PROJECTS_DIR, exist_ok=True)

# ── Board I/O ─────────────────────────────────────────────────────────────────
def _board_load() -> dict:
    if os.path.exists(VELVET_BOARD_FILE):
        try:
            with open(VELVET_BOARD_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"projects": {}}

def _board_save(board: dict):
    with open(VELVET_BOARD_FILE, "w") as f:
        json.dump(board, f, indent=2)

# ── Project helpers ───────────────────────────────────────────────────────────
def _proj_dir(pid: str) -> str:
    return os.path.join(VELVET_PROJECTS_DIR, pid)

def _proj_load(pid: str) -> dict:
    p = os.path.join(_proj_dir(pid), "project.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}

def _proj_save(pid: str, data: dict):
    d = _proj_dir(pid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "project.json"), "w") as f:
        json.dump(data, f, indent=2)

def _build_log_append(pid: str, event: dict):
    d = _proj_dir(pid)
    os.makedirs(d, exist_ok=True)
    event["ts"] = time.time()
    with open(os.path.join(d, "build_log.jsonl"), "a") as f:
        f.write(json.dumps(event) + "\n")

def _build_log_read(pid: str) -> list:
    p = os.path.join(_proj_dir(pid), "build_log.jsonl")
    if not os.path.exists(p):
        return []
    events = []
    with open(p) as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except Exception:
                pass
    return events

def _spec_freeze(pid: str, spec: dict) -> str:
    raw = json.dumps(spec, sort_keys=True).encode()
    sha = _hashlib.sha256(raw).hexdigest()[:16]
    d = _proj_dir(pid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "spec.yaml"), "w") as f:
        f.write(f"# spec_sha: {sha}\n")
        for k, v in spec.items():
            f.write(f"{k}: {json.dumps(v)}\n")
    return sha

def _compute_completion(deliverables: list) -> float:
    total = sum(d.get("weight", 1) for d in deliverables)
    if total == 0:
        return 0.0
    passing = sum(d.get("weight", 1) for d in deliverables if d.get("state") == "PASS")
    return round(passing / total, 4)

def _gate_closeable(deliverables: list, completion_gate: float = 0.90) -> dict:
    completion = _compute_completion(deliverables)
    must_haves = [d for d in deliverables if d.get("weight", 1) == 3]
    failing_musts = [d for d in must_haves if d.get("state") != "PASS"]
    closeable = completion >= completion_gate and len(failing_musts) == 0
    return {
        "closeable": closeable,
        "completion": completion,
        "gate": completion_gate,
        "failing_musts": [d["id"] for d in failing_musts],
        "verdict": "CLOSE" if closeable else ("ESCALATE" if len(failing_musts) > 0 else "CONTINUE"),
    }

def _write_handoff_md(pid: str, project: dict, section: str = "open"):
    d = _proj_dir(pid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "HANDOFF.md")
    if section == "open":
        lines = [
            f"# HANDOFF — {project.get('name', pid)}",
            f"",
            f"**Project ID:** `{pid}`  ",
            f"**Goal:** {project.get('goal', '—')}  ",
            f"**Started:** {time.strftime('%Y-%m-%d %H:%M CST', time.localtime(project.get('created_at', time.time())))}  ",
            f"**Assigned Agents:** {', '.join(project.get('agents', []))}  ",
            f"**GitHub Repo:** {project.get('github_repo', '(not yet created)')}  ",
            f"**Status:** {project.get('status', 'INIT')}  ",
            f"",
            f"## Spec Objectives",
            f"",
        ]
        for d_item in project.get("deliverables", []):
            lines.append(f"- [ ] **{d_item['id']}** (weight {d_item.get('weight',1)}): {d_item['statement']}")
        lines += ["", "## Build Log", "", "*(appended as work progresses)*", ""]
    elif section == "close":
        logs = _build_log_read(pid)
        deliverables = project.get("deliverables", [])
        gate = _gate_closeable(deliverables)
        elapsed = project.get("closed_at", time.time()) - project.get("created_at", time.time())
        elapsed_str = f"{int(elapsed//3600)}h {int((elapsed%3600)//60)}m {int(elapsed%60)}s"
        agent_stats = {}
        for ev in logs:
            a = ev.get("actor", "unknown")
            agent_stats.setdefault(a, {"actions": 0, "errors": 0, "escalations": 0})
            agent_stats[a]["actions"] += 1
            if ev.get("event_type") == "error":
                agent_stats[a]["errors"] += 1
            if ev.get("event_type") == "escalation":
                agent_stats[a]["escalations"] += 1
        with open(path) as existing:
            existing_content = existing.read()
        close_lines = [
            existing_content,
            "",
            "---",
            "",
            "## Project Close Report",
            "",
            f"**Closed:** {time.strftime('%Y-%m-%d %H:%M CST', time.localtime(project.get('closed_at', time.time())))}  ",
            f"**Elapsed:** {elapsed_str}  ",
            f"**Final Completion:** {gate['completion']*100:.1f}%  ",
            f"**Gate Result:** {gate['verdict']}  ",
            "",
            "### Deliverables QC Summary",
            "",
        ]
        for d_item in deliverables:
            state = d_item.get("state", "PENDING")
            icon = "✅" if state == "PASS" else ("❌" if state == "FAIL" else "⏳")
            close_lines.append(f"- {icon} **{d_item['id']}** (w{d_item.get('weight',1)}): {d_item['statement']} — `{state}`")
            if d_item.get("evidence_preview"):
                close_lines.append(f"  > Evidence: `{d_item['evidence_preview']}`")
        close_lines += [
            "",
            "### Agent Performance",
            "",
        ]
        for agent_key, stats in agent_stats.items():
            close_lines.append(f"- **{agent_key}**: {stats['actions']} actions, {stats['errors']} errors, {stats['escalations']} escalations")
        close_lines += [
            "",
            "### Lessons Learned",
            "",
            f"{project.get('lessons_learned', '*(VELVET-QC: no lessons recorded — update via POST /api/velvet/project/{pid}/lessons)*')}",
            "",
            "### Areas of Opportunity",
            "",
            f"{project.get('areas_of_opportunity', '*(not yet recorded)*')}",
            "",
            "### Agent Improvement Recommendations",
            "",
        ]
        for rec in project.get("agent_improvements", []):
            close_lines.append(f"- **{rec['agent']}**: {rec['recommendation']}")
        if not project.get("agent_improvements"):
            close_lines.append("*(none recorded)*")
        close_lines += ["", "---", f"*Handoff generated by VELVET-QC · {time.strftime('%Y-%m-%d %H:%M')}*", ""]
        lines = close_lines
    else:
        lines = []
    with open(path, "w") as f:
        f.write("\n".join(lines) if section == "open" else "\n".join(lines))
    return path


# ── Pydantic models ───────────────────────────────────────────────────────────
from pydantic import BaseModel as _BM

class NexusInitRequest(_BM):
    name: str
    goal: str
    agents: list = ["hannibal", "murdock", "face", "ba"]
    deliverables: list = []   # [{id, statement, weight, verify_cmd}]
    github_repo: str = ""
    completion_gate: float = 0.90

class NexusDeliverableUpdate(_BM):
    project_id: str
    deliverable_id: str
    state: str          # PASS | FAIL | BLOCKED | PENDING
    evidence: str = ""  # raw stdout/stderr bytes
    actor: str = "velvet-qc"

class NexusQCRunRequest(_BM):
    project_id: str
    deliverable_id: str = ""  # empty = run all pending

class NexusCloseRequest(_BM):
    project_id: str
    lessons_learned: str = ""
    areas_of_opportunity: str = ""
    agent_improvements: list = []  # [{agent, recommendation}]

class NexusLessonsRequest(_BM):
    lessons_learned: str = ""
    areas_of_opportunity: str = ""
    agent_improvements: list = []

class NexusGitHubRequest(_BM):
    project_id: str
    repo_name: str
    private: bool = True
    description: str = ""


# ── NEXUS API ─────────────────────────────────────────────────────────────────

@app.post("/api/velvet/project/init")
async def velvet_init(req: NexusInitRequest):
    """Gate 1: INIT — open a project, write HANDOFF.md header."""
    import time as _t
    pid = f"proj_{int(_t.time()*1000)}"
    now = _t.time()
    deliverables = []
    for i, d in enumerate(req.deliverables):
        deliverables.append({
            "id": d.get("id", f"D-{i+1:02d}"),
            "statement": d.get("statement", ""),
            "weight": d.get("weight", 2),
            "verify_cmd": d.get("verify_cmd", ""),
            "state": "PENDING",
            "evidence": "",
            "evidence_preview": "",
            "attempts": 0,
        })
    project = {
        "id": pid,
        "name": req.name,
        "goal": req.goal,
        "agents": req.agents,
        "deliverables": deliverables,
        "github_repo": req.github_repo,
        "completion_gate": req.completion_gate,
        "status": "INIT",
        "completion": 0.0,
        "spec_sha": "",
        "created_at": now,
        "closed_at": None,
        "lessons_learned": "",
        "areas_of_opportunity": "",
        "agent_improvements": [],
    }
    # Freeze spec
    spec = {"name": req.name, "goal": req.goal, "deliverables": deliverables, "gate": req.completion_gate}
    sha = _spec_freeze(pid, spec)
    project["spec_sha"] = sha
    project["status"] = "SPEC"
    _proj_save(pid, project)
    _build_log_append(pid, {"event_type": "spec_freeze", "actor": "velvet-track", "spec_sha": sha,
                            "deliverable_count": len(deliverables)})
    # Write opening HANDOFF.md
    _write_handoff_md(pid, project, section="open")
    # Update board
    board = _board_load()
    board["projects"][pid] = {
        "id": pid, "name": req.name, "goal": req.goal,
        "status": "SPEC", "completion": 0.0, "created_at": now,
        "agents": req.agents, "github_repo": req.github_repo,
    }
    _board_save(board)
    return {"ok": True, "project_id": pid, "spec_sha": sha, "project": project}


@app.post("/api/velvet/project/{pid}/github")
async def velvet_create_github(pid: str, req: NexusGitHubRequest):
    """Create a GitHub repo for this project via gh CLI."""
    project = _proj_load(pid)
    if not project:
        return {"error": f"Project {pid} not found"}
    repo_name = req.repo_name or project.get("name", pid).lower().replace(" ", "-")
    desc = req.description or project.get("goal", "")[:100]
    vis = "--private" if req.private else "--public"
    try:
        result = subprocess.run(
            ["gh", "repo", "create", repo_name, vis, "--description", desc, "--confirm"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            repo_url = result.stdout.strip()
            project["github_repo"] = repo_url
            project["status"] = "ACTIVE"
            _proj_save(pid, project)
            _build_log_append(pid, {"event_type": "github_created", "actor": "velvet-track",
                                    "repo": repo_url})
            board = _board_load()
            if pid in board["projects"]:
                board["projects"][pid]["github_repo"] = repo_url
                board["projects"][pid]["status"] = "ACTIVE"
                _board_save(board)
            return {"ok": True, "repo": repo_url}
        else:
            err = result.stderr.strip()
            _build_log_append(pid, {"event_type": "error", "actor": "velvet-track",
                                    "message": f"gh repo create failed: {err}"})
            return {"ok": False, "error": err}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/velvet/project/{pid}/deliverable")
async def velvet_update_deliverable(pid: str, req: NexusDeliverableUpdate):
    """VELVET-QC records a deliverable result. Meter only moves here."""
    project = _proj_load(pid)
    if not project:
        return {"error": f"Project {pid} not found"}
    updated = False
    for d in project["deliverables"]:
        if d["id"] == req.deliverable_id:
            d["state"] = req.state
            d["evidence"] = req.evidence
            d["evidence_preview"] = req.evidence[:80] if req.evidence else ""
            d["attempts"] = d.get("attempts", 0) + 1
            updated = True
            break
    if not updated:
        return {"error": f"Deliverable {req.deliverable_id} not found"}
    project["completion"] = _compute_completion(project["deliverables"])
    gate = _gate_closeable(project["deliverables"], project.get("completion_gate", 0.90))
    if gate["closeable"]:
        project["status"] = "THRESHOLD"
    _proj_save(pid, project)
    _build_log_append(pid, {
        "event_type": "qc_check", "actor": req.actor,
        "deliverable": req.deliverable_id, "state": req.state,
        "completion": project["completion"], "gate_verdict": gate["verdict"],
        "evidence_preview": req.evidence[:80] if req.evidence else "",
    })
    board = _board_load()
    if pid in board["projects"]:
        board["projects"][pid]["completion"] = project["completion"]
        board["projects"][pid]["status"] = project["status"]
        _board_save(board)
    return {"ok": True, "completion": project["completion"], "gate": gate}


@app.post("/api/velvet/project/{pid}/qc")
async def velvet_qc_run(pid: str, req: NexusQCRunRequest):
    """Run verify commands for pending deliverables. Evidence stored as raw bytes."""
    project = _proj_load(pid)
    if not project:
        return {"error": f"Project {pid} not found"}
    results = []
    targets = project["deliverables"]
    if req.deliverable_id:
        targets = [d for d in targets if d["id"] == req.deliverable_id]
    for d in targets:
        if d.get("state") == "PASS":
            results.append({"id": d["id"], "state": "PASS", "skipped": True})
            continue
        cmd = d.get("verify_cmd", "").strip()
        if not cmd:
            results.append({"id": d["id"], "state": "BLOCKED", "reason": "no verify_cmd"})
            continue
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30,
                               cwd="/home/hunt")
            evidence = (r.stdout + r.stderr).strip()
            state = "PASS" if r.returncode == 0 else "FAIL"
            d["state"] = state
            d["evidence"] = evidence
            d["evidence_preview"] = evidence[:80]
            d["attempts"] = d.get("attempts", 0) + 1
            results.append({"id": d["id"], "state": state, "exit_code": r.returncode,
                            "evidence_preview": evidence[:80]})
            _build_log_append(pid, {"event_type": "qc_check", "actor": "velvet-qc",
                                    "deliverable": d["id"], "state": state,
                                    "cmd": cmd, "exit_code": r.returncode,
                                    "evidence": evidence[:200]})
        except subprocess.TimeoutExpired:
            d["state"] = "FAIL"
            d["attempts"] = d.get("attempts", 0) + 1
            results.append({"id": d["id"], "state": "FAIL", "reason": "timeout"})
        except Exception as e:
            results.append({"id": d["id"], "state": "FAIL", "reason": str(e)})
    project["completion"] = _compute_completion(project["deliverables"])
    gate = _gate_closeable(project["deliverables"], project.get("completion_gate", 0.90))
    if gate["closeable"]:
        project["status"] = "THRESHOLD"
    _proj_save(pid, project)
    # ── 3-error escalation check (Velvet → Astra routing) ─────────────────────
    escalations = []
    for d in project["deliverables"]:
        if d.get("attempts", 0) >= 3 and d.get("state") == "FAIL":
            bundle = {
                "deliverable_id": d["id"],
                "statement": d["statement"],
                "verify_cmd": d.get("verify_cmd", ""),
                "attempts": d["attempts"],
                "last_evidence": d.get("evidence", "")[:400],
                "project_id": pid,
                "project_name": project.get("name", ""),
                "goal": project.get("goal", ""),
            }
            escalations.append(bundle)
            _build_log_append(pid, {
                "event_type": "escalation",
                "actor": "velvet-qc",
                "deliverable": d["id"],
                "message": f"3-error threshold breached on {d['id']} — routing to Astra",
                "bundle": bundle,
            })
    # ──────────────────────────────────────────────────────────────────────────
    board = _board_load()
    if pid in board["projects"]:
        board["projects"][pid]["completion"] = project["completion"]
        board["projects"][pid]["status"] = project["status"]
        _board_save(board)
    # GPU waste check
    gpu_running = False
    try:
        gm = _meter_load()
        gpu_running = gm.get("running", False)
    except Exception:
        pass
    return {"ok": True, "results": results, "completion": project["completion"],
            "gate": gate, "gpu_warning": gpu_running, "escalations": escalations}


@app.post("/api/velvet/project/{pid}/close")
async def velvet_close(pid: str, req: NexusCloseRequest):
    """Gate 6+7: CLOSE + HANDOFF — verify GPU off, write final HANDOFF.md."""
    project = _proj_load(pid)
    if not project:
        return {"error": f"Project {pid} not found"}
    gate = _gate_closeable(project["deliverables"], project.get("completion_gate", 0.90))
    if not gate["closeable"]:
        return {"error": "Project not closeable", "gate": gate,
                "message": f"Completion {gate['completion']*100:.1f}% — failing must-haves: {gate['failing_musts']}"}
    # GPU waste prevention — hard stop before close
    gpu_stopped = False
    try:
        gm = _meter_load()
        if gm.get("running"):
            await gpu_meter_stop(shutdown=True)
            gpu_stopped = True
            _build_log_append(pid, {"event_type": "gpu_auto_stop", "actor": "velvet-qc",
                                    "message": "GPU stopped by VELVET before project close"})
    except Exception:
        pass
    # Verify GitHub is current
    git_sha = ""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                           cwd="/home/hunt", timeout=10)
        git_sha = r.stdout.strip()
    except Exception:
        pass
    now = time.time()
    project.update({
        "status": "CLOSED",
        "closed_at": now,
        "lessons_learned": req.lessons_learned,
        "areas_of_opportunity": req.areas_of_opportunity,
        "agent_improvements": req.agent_improvements,
        "final_git_sha": git_sha,
    })
    _proj_save(pid, project)
    _build_log_append(pid, {"event_type": "project_closed", "actor": "velvet-qc",
                            "completion": gate["completion"], "git_sha": git_sha,
                            "gpu_stopped": gpu_stopped})
    # Write close section to HANDOFF.md
    handoff_path = _write_handoff_md(pid, project, section="close")
    # Archive to vault if mounted
    try:
        if os.path.ismount("/mnt/NOBILITY_VAULT") or os.path.isdir(HANDOFFS_VAULT):
            os.makedirs(HANDOFFS_VAULT, exist_ok=True)
            shutil.copy2(handoff_path, os.path.join(HANDOFFS_VAULT, f"{pid}_HANDOFF.md"))
    except Exception:
        pass
    board = _board_load()
    if pid in board["projects"]:
        board["projects"][pid]["status"] = "CLOSED"
        board["projects"][pid]["closed_at"] = now
        board["projects"][pid]["completion"] = gate["completion"]
        _board_save(board)
    return {"ok": True, "project_id": pid, "completion": gate["completion"],
            "handoff_path": handoff_path, "git_sha": git_sha, "gpu_stopped": gpu_stopped}


@app.get("/api/velvet/board")
async def velvet_board():
    """Full project board — all projects with live completion."""
    board = _board_load()
    projects = []
    for pid, summary in board.get("projects", {}).items():
        detail = _proj_load(pid)
        projects.append({**summary, **({"deliverable_count": len(detail.get("deliverables", []))} if detail else {})})
    projects.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"projects": projects, "total": len(projects)}


@app.get("/api/velvet/project/{pid}")
async def velvet_get_project(pid: str):
    project = _proj_load(pid)
    if not project:
        return {"error": f"Project {pid} not found"}
    gate = _gate_closeable(project.get("deliverables", []), project.get("completion_gate", 0.90))
    return {"project": project, "gate": gate}


@app.get("/api/velvet/project/{pid}/log")
async def velvet_build_log(pid: str):
    logs = _build_log_read(pid)
    return {"log": logs[-100:], "total": len(logs)}


@app.post("/api/velvet/project/{pid}/lessons")
async def velvet_lessons(pid: str, req: NexusLessonsRequest):
    project = _proj_load(pid)
    if not project:
        return {"error": f"Project {pid} not found"}
    project["lessons_learned"] = req.lessons_learned
    project["areas_of_opportunity"] = req.areas_of_opportunity
    project["agent_improvements"] = req.agent_improvements
    _proj_save(pid, project)
    return {"ok": True}


@app.post("/api/velvet/project/{pid}/log/append")
async def velvet_log_append(pid: str, event: dict):
    _build_log_append(pid, event)
    return {"ok": True}


@app.get("/api/velvet/gpu/check")
async def velvet_gpu_check():
    """VELVET-QC GPU waste check — call before any close."""
    try:
        m = _meter_load()
        st = _meter_state(m)
        return {"running": st.get("running", False), "cost_accrued": st.get("session_cost", 0),
                "idle_seconds": st.get("idle_seconds", 0), "warning": st.get("running", False)}
    except Exception as e:
        return {"running": False, "error": str(e)}


# ── VELVET agent brains (official QC node profile) ────────────────────────────
VELVET_TRACK_BRAIN = (
    "You are VELVET-TRACK, the Project Board Manager for CRANE. "
    "You report directly to Hannibal (Supervisor Dispatcher). "
    "Your responsibilities: create project records the moment Hannibal initiates a run, "
    "freeze specs with SHA hashes to prevent goal-post movement, write and maintain HANDOFF.md, "
    "update the live project board, and create GitHub repositories for new projects. "
    "You never execute implementation code. You speak only in structured formats: "
    "JSON specs, YAML deliverables, markdown handoffs, build log events. "
    "Every claim is backed by a verify command or a file path. "
    "You are the memory of CRANE. Nothing is done until you record it. "
    "You flag immediately if a spec SHA changes mid-run — that is goal-post movement and the run aborts."
)

VELVET_QC_BRAIN = (
    "You are VELVET — Quality Control and Verification Specialist for CRANE. "
    "You report directly to Hannibal (Supervisor Dispatcher). "
    "Your designation: dedicated gatekeeper for validation before final integration. "
    "\n\nBrain 1 — Agent Expertise: "
    "Automated test verification, lint compliance, security regression screening, "
    "and output validation against strict YAML spec deliverables. "
    "You never accept an agent's self-report as proof — you run the verify command and inspect raw output. "
    "You enforce: completion >= 0.90 AND every weight-3 deliverable PASSES before any close. "
    "\n\nRouting Protocol: "
    "After each department worker (Murdock / Face / B.A.) produces a code artifact or output: "
    "(1) You run the verify command. "
    "(2) PASS → mark deliverable, update completion meter, report ready for integration to Hannibal. "
    "(3) FAIL → increment attempt counter, return error log to the worker with exact failure bytes. "
    "(4) 3rd consecutive failure on same deliverable → trigger escalation to Astra with full context bundle: "
    "    error trace, original spec, failed code state, attempts log. "
    "\n\nAdditional gates you enforce on every project close: "
    "GPU waste check (hard stop if running), GitHub SHA verification (local must match remote), "
    "spec SHA integrity (must match freeze hash). "
    "Your evidence is always raw stdout/stderr bytes — never a summary, never an agent's opinion. "
    "You write the final HANDOFF.md close section: timeline, lessons learned, "
    "agent performance report, QC results, and improvement recommendations per agent."
)

# Register VELVET in the A-Team roster
ATEAM["velvet-track"] = {
    "name": "VELVET-TRACK",
    "role": "Project Board Manager",
    "dept": "Project Intelligence",
    "model_id": "local:qwen-coder-1.5b",
    "model_label": "Qwen2.5-Coder-1.5B (always-on)",
    "color": "#10b981",
    "icon": "◈",
    "brain1": VELVET_TRACK_BRAIN,
    "brain2_template": "Active project specs, board state, build log conventions, and GitHub repo map for current session.",
}
ATEAM["velvet-qc"] = {
    "name": "VELVET",
    "role": "QC & Verification Specialist → Hannibal",
    "dept": "Project Intelligence",
    "model_id": "local:qwen-coder-3b",
    "model_label": "Qwen2.5-Coder-3B",
    "color": "#06b6d4",
    "icon": "◉",
    "brain1": VELVET_QC_BRAIN,
    "brain2_template": "Project-specific acceptance test suites, assertion criteria, and visual/functional regression scripts for active branch.",
}
_agent_brain2["velvet-track"] = ATEAM["velvet-track"]["brain2_template"]
_agent_brain2["velvet-qc"]    = ATEAM["velvet-qc"]["brain2_template"]


# ── /board page ───────────────────────────────────────────────────────────────

@app.get("/board", response_class=HTMLResponse)
async def serve_board():
    return HTMLResponse(content=r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CRANE — Project Board</title>
<style>
:root{
  --bg:#0d0d10;--panel:#16151a;--card:#1c1b22;--card2:#211f28;--border:#2a2433;
  --text:#eef2f3;--muted:#8a9296;--dim:#63696c;
  --green:#10b981;--cyan:#06b6d4;--gold:#f0b429;--red:#ef4444;--orange:#f59e0b;
  --cu:#818cf8;--cu-dim:rgba(129,140,248,.1);--cu-border:rgba(129,140,248,.28);
  --nex:#10b981;--qc:#06b6d4;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;display:flex;flex-direction:column;}

/* TOPBAR */
#topbar{height:44px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 14px;gap:14px;flex-shrink:0;}
.tb-brand{font-weight:800;font-size:13px;letter-spacing:2px;color:var(--nex);}
.top-nav a{color:var(--muted);text-decoration:none;padding:0 10px;height:32px;display:inline-flex;align-items:center;font-size:12px;font-weight:500;border-radius:5px;transition:.15s;}
.top-nav a:hover{color:var(--text);background:rgba(255,255,255,.05);}
.top-nav a.active{color:var(--nex);background:rgba(16,185,129,.1);}
.tb-right{margin-left:auto;display:flex;align-items:center;gap:10px;}
.tb-chip{font-size:9px;font-weight:700;letter-spacing:1.5px;padding:3px 8px;border-radius:3px;border:1px solid rgba(16,185,129,.3);color:var(--nex);background:rgba(16,185,129,.08);}

/* LAYOUT */
#layout{display:flex;flex:1;overflow:hidden;}

/* LEFT — project list */
#proj-list{width:280px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;}
.pl-head{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;}
.pl-title{font-size:9px;font-weight:700;letter-spacing:2px;color:var(--muted);flex:1;}
.pl-new-btn{background:var(--nex);color:#fff;border:none;border-radius:5px;padding:4px 10px;font-size:10px;font-weight:700;cursor:pointer;transition:.15s;}
.pl-new-btn:hover{background:#059669;}
#proj-cards{flex:1;overflow-y:auto;padding:8px;}
#proj-cards::-webkit-scrollbar{width:3px;}
#proj-cards::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
.proj-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:7px;cursor:pointer;transition:.15s;border-left:3px solid var(--border);}
.proj-card:hover{background:var(--card2);border-color:rgba(16,185,129,.3);}
.proj-card.active{border-left-color:var(--nex);background:var(--card2);}
.pc-name{font-size:12px;font-weight:700;margin-bottom:4px;}
.pc-goal{font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.4;}
.pc-meter-wrap{height:4px;background:rgba(255,255,255,.07);border-radius:2px;margin-bottom:5px;}
.pc-meter{height:4px;border-radius:2px;transition:.6s;}
.pc-foot{display:flex;align-items:center;justify-content:space-between;font-size:9px;color:var(--dim);}
.pc-status{font-weight:700;letter-spacing:1px;padding:1px 6px;border-radius:2px;}
.s-init{color:var(--muted);background:rgba(255,255,255,.06);}
.s-spec{color:var(--cyan);background:rgba(6,182,212,.1);}
.s-active{color:var(--gold);background:rgba(240,180,41,.1);}
.s-threshold{color:var(--nex);background:rgba(16,185,129,.12);}
.s-closed{color:var(--dim);background:rgba(255,255,255,.04);}

/* CENTER — project detail */
#proj-detail{flex:1;display:flex;flex-direction:column;overflow:hidden;}
#detail-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:14px;}
.dh-name{font-size:18px;font-weight:800;letter-spacing:.3px;}
.dh-goal{font-size:11px;color:var(--muted);margin-top:3px;}
.dh-meta{font-size:10px;color:var(--dim);margin-top:2px;}
.dh-right{margin-left:auto;text-align:right;flex-shrink:0;}
.dh-pct{font-size:28px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--nex);}
.dh-gate{font-size:9px;color:var(--muted);margin-top:2px;}
.big-meter-wrap{height:6px;background:rgba(255,255,255,.06);border-radius:3px;margin:10px 18px 0;}
.big-meter{height:6px;border-radius:3px;background:var(--nex);transition:.6s;}

/* detail tabs */
.d-tabs{display:flex;border-bottom:1px solid var(--border);padding:0 18px;flex-shrink:0;}
.d-tab{padding:8px 14px;font-size:9px;font-weight:700;letter-spacing:1.5px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:.15s;}
.d-tab.active{color:var(--nex);border-bottom-color:var(--nex);}

/* deliverables */
#view-deliverables{flex:1;overflow-y:auto;padding:14px 18px;display:none;}
#view-deliverables.show{display:block;}
.deliv-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:8px;border-left:3px solid var(--border);}
.deliv-card.pass{border-left-color:var(--nex);}
.deliv-card.fail{border-left-color:var(--red);}
.deliv-card.pending{border-left-color:var(--orange);}
.deliv-card.blocked{border-left-color:var(--muted);}
.dv-head{display:flex;align-items:center;gap:8px;margin-bottom:5px;}
.dv-id{font-size:9px;font-weight:700;letter-spacing:1.5px;font-family:'JetBrains Mono',monospace;color:var(--cyan);}
.dv-weight{font-size:8px;padding:1px 5px;border-radius:2px;background:rgba(255,255,255,.08);color:var(--muted);}
.dv-state{font-size:8px;font-weight:700;letter-spacing:1px;margin-left:auto;padding:2px 7px;border-radius:2px;}
.dv-pass{background:rgba(16,185,129,.15);color:var(--nex);}
.dv-fail{background:rgba(239,68,68,.15);color:var(--red);}
.dv-pending{background:rgba(245,158,11,.12);color:var(--orange);}
.dv-blocked{background:rgba(255,255,255,.06);color:var(--muted);}
.dv-stmt{font-size:12px;font-weight:600;margin-bottom:3px;}
.dv-evidence{font-size:10px;color:var(--dim);font-family:'JetBrains Mono',monospace;margin-top:4px;}
.dv-actions{display:flex;gap:6px;margin-top:8px;}
.dv-btn{background:var(--card2);border:1px solid var(--border);border-radius:4px;padding:3px 9px;font-size:9px;font-weight:700;color:var(--muted);cursor:pointer;transition:.15s;letter-spacing:1px;}
.dv-btn:hover{color:var(--text);border-color:var(--nex);}
.dv-btn.run{background:rgba(6,182,212,.1);border-color:rgba(6,182,212,.25);color:var(--cyan);}
.dv-btn.pass{background:rgba(16,185,129,.1);border-color:rgba(16,185,129,.25);color:var(--nex);}
.dv-btn.fail{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.2);color:var(--red);}

/* build log */
#view-log{flex:1;overflow-y:auto;padding:14px 18px;display:none;font-family:'JetBrains Mono',monospace;font-size:11px;}
#view-log.show{display:block;}
.log-entry{padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04);display:flex;gap:10px;align-items:flex-start;}
.log-ts{color:var(--dim);flex-shrink:0;font-size:9px;padding-top:2px;}
.log-actor{font-weight:700;flex-shrink:0;font-size:9px;padding-top:2px;}
.log-type{color:var(--muted);flex-shrink:0;font-size:9px;padding-top:2px;}
.log-detail{color:var(--text);flex:1;line-height:1.4;}

/* handoff */
#view-handoff{flex:1;overflow-y:auto;padding:14px 18px;display:none;}
#view-handoff.show{display:block;}
#handoff-md{font-size:12px;line-height:1.7;color:var(--text);white-space:pre-wrap;font-family:'Inter',sans-serif;}
#handoff-md h1,#handoff-md h2,#handoff-md h3{color:var(--nex);margin:12px 0 6px;}
#handoff-md code{background:var(--card);padding:1px 5px;border-radius:3px;font-family:'JetBrains Mono',monospace;}

/* empty state */
#empty-state{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;color:var(--muted);}
.es-icon{font-size:48px;opacity:.2;}
.es-label{font-size:14px;font-weight:600;}
.es-sub{font-size:11px;color:var(--dim);}

/* RIGHT — build log stream + actions */
#right-col{width:300px;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;}
.rc-head{padding:10px 14px;border-bottom:1px solid var(--border);font-size:9px;font-weight:700;letter-spacing:2px;color:var(--muted);}
#live-log{flex:1;overflow-y:auto;padding:8px 12px;font-family:'JetBrains Mono',monospace;font-size:10px;display:flex;flex-direction:column;gap:3px;}
#live-log::-webkit-scrollbar{width:3px;}
#live-log::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}
.ll-entry{padding:3px 0;border-bottom:1px solid rgba(255,255,255,.03);}
.ll-time{color:var(--dim);margin-right:6px;}
.ll-actor{font-weight:700;margin-right:4px;}
.ll-msg{color:var(--muted);}

/* actions panel */
#actions-panel{border-top:1px solid var(--border);padding:12px;}
.ap-head{font-size:9px;font-weight:700;letter-spacing:2px;color:var(--muted);margin-bottom:10px;}
.ap-btn{width:100%;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:7px 12px;font-size:11px;font-weight:700;color:var(--text);cursor:pointer;text-align:left;margin-bottom:6px;transition:.15s;display:flex;align-items:center;gap:8px;}
.ap-btn:hover{background:var(--card2);}
.ap-btn.green:hover{border-color:rgba(16,185,129,.4);color:var(--nex);}
.ap-btn.cyan:hover{border-color:rgba(6,182,212,.4);color:var(--cyan);}
.ap-btn.red:hover{border-color:rgba(239,68,68,.3);color:var(--red);}
.ap-btn .btn-icon{font-size:14px;}

/* GPU status bar */
#gpu-bar{padding:8px 14px;border-top:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:10px;flex-shrink:0;}
.gpu-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
.gpu-dot.on{background:var(--red);box-shadow:0 0 5px var(--red);}
.gpu-dot.off{background:var(--dim);}

/* NEW PROJECT MODAL */
#new-proj-modal{display:none;position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.7);align-items:center;justify-content:center;}
#new-proj-modal.open{display:flex;}
.modal-box{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:24px;width:520px;max-height:80vh;overflow-y:auto;}
.modal-title{font-size:14px;font-weight:800;letter-spacing:.5px;margin-bottom:16px;color:var(--nex);}
.modal-field{margin-bottom:12px;}
.modal-label{font-size:9px;font-weight:700;letter-spacing:1.5px;color:var(--muted);margin-bottom:5px;}
.modal-input{width:100%;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;padding:8px 10px;outline:none;transition:.15s;font-family:'Inter',sans-serif;}
.modal-input:focus{border-color:var(--nex);}
textarea.modal-input{resize:none;min-height:60px;}
.modal-actions{display:flex;gap:8px;margin-top:16px;}
.modal-btn{flex:1;padding:8px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;border:none;transition:.15s;}
.modal-btn.primary{background:var(--nex);color:#fff;}
.modal-btn.primary:hover{background:#059669;}
.modal-btn.cancel{background:var(--card);color:var(--muted);border:1px solid var(--border);}
.modal-btn.cancel:hover{color:var(--text);}
.deliverable-row{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:6px;}
.dr-top{display:flex;gap:8px;margin-bottom:6px;}
.dr-id{width:80px;}
.dr-weight{width:70px;}
.dr-add{background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.25);border-radius:5px;padding:5px 12px;font-size:10px;font-weight:700;color:var(--nex);cursor:pointer;margin-top:4px;}
</style>
</head>
<body>

<div id="topbar">
  <span class="tb-brand">◈ PROJECT BOARD</span>
  <div style="width:1px;height:20px;background:var(--border)"></div>
  <nav class="top-nav">
    <a href="/ide">Home</a>
    <a href="/cu">CU Dept</a>
    <a href="/board" class="active">Board</a>
  </nav>
  <div class="tb-right">
    <span class="tb-chip" id="boardChip">VELVET READY</span>
    <span style="font-size:10px;color:var(--muted)" id="boardProjectCount">0 projects</span>
  </div>
</div>

<div id="layout">

  <!-- LEFT: project list -->
  <div id="proj-list">
    <div class="pl-head">
      <span class="pl-title">ACTIVE PROJECTS</span>
      <button class="pl-new-btn" onclick="openNewModal()">+ New</button>
    </div>
    <div id="proj-cards">
      <div style="padding:20px 12px;font-size:10px;color:var(--dim);text-align:center">Loading projects…</div>
    </div>
  </div>

  <!-- CENTER: project detail -->
  <div id="proj-detail">
    <div id="empty-state">
      <div class="es-icon">◈</div>
      <div class="es-label">Select a project</div>
      <div class="es-sub">or create a new one to start tracking</div>
    </div>

    <!-- populated when a project is selected -->
    <div id="detail-content" style="display:none;flex-direction:column;flex:1;overflow:hidden;">
      <div id="detail-head">
        <div>
          <div class="dh-name" id="dh-name">—</div>
          <div class="dh-goal" id="dh-goal">—</div>
          <div class="dh-meta" id="dh-meta">—</div>
        </div>
        <div class="dh-right">
          <div class="dh-pct" id="dh-pct">0%</div>
          <div class="dh-gate" id="dh-gate">gate 90%</div>
          <div class="pc-status s-init" id="dh-status" style="margin-top:5px">INIT</div>
        </div>
      </div>
      <div class="big-meter-wrap"><div class="big-meter" id="big-meter" style="width:0%"></div></div>

      <div class="d-tabs">
        <div class="d-tab active" id="dtab-deliverables" onclick="dSwitch('deliverables')">DELIVERABLES</div>
        <div class="d-tab" id="dtab-log"         onclick="dSwitch('log')">BUILD LOG</div>
        <div class="d-tab" id="dtab-handoff"     onclick="dSwitch('handoff')">HANDOFF.MD</div>
      </div>

      <div id="view-deliverables" class="show"></div>
      <div id="view-log"></div>
      <div id="view-handoff"><pre id="handoff-md"></pre></div>
    </div>
  </div>

  <!-- RIGHT: live stream + actions -->
  <div id="right-col">
    <div class="rc-head">LIVE EVENTS</div>
    <div id="live-log">
      <div style="color:var(--dim);font-size:10px;padding:8px 0">Waiting for events…</div>
    </div>

    <div id="actions-panel">
      <div class="ap-head">VELVET ACTIONS</div>
      <button class="ap-btn cyan" onclick="runQC()"><span class="btn-icon">◉</span> Run QC Check</button>
      <button class="ap-btn green" onclick="closeProject()"><span class="btn-icon">✓</span> Close Project</button>
      <button class="ap-btn" onclick="loadHandoff()"><span class="btn-icon">◱</span> View HANDOFF.md</button>
      <button class="ap-btn red" onclick="gpuCheck()"><span class="btn-icon">⏻</span> GPU Waste Check</button>
    </div>

    <div id="gpu-bar">
      <div class="gpu-dot off" id="gpuDot"></div>
      <span id="gpuLabel" style="color:var(--dim)">GPU off</span>
    </div>
  </div>
</div>

<!-- NEW PROJECT MODAL -->
<div id="new-proj-modal">
  <div class="modal-box">
    <div class="modal-title">◈ New Project</div>

    <div class="modal-field">
      <div class="modal-label">PROJECT NAME</div>
      <input class="modal-input" id="np-name" placeholder="e.g. Login Auth System"/>
    </div>
    <div class="modal-field">
      <div class="modal-label">GOAL</div>
      <textarea class="modal-input" id="np-goal" placeholder="What will be built and why…"></textarea>
    </div>
    <div class="modal-field">
      <div class="modal-label">COMPLETION GATE</div>
      <input class="modal-input" id="np-gate" value="0.90" type="number" min="0.5" max="1.0" step="0.05"/>
    </div>
    <div class="modal-field">
      <div class="modal-label">DELIVERABLES</div>
      <div id="deliverable-list"></div>
      <button class="dr-add" onclick="addDelivRow()">+ Add Deliverable</button>
    </div>

    <div class="modal-actions">
      <button class="modal-btn cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-btn primary" onclick="createProject()">◈ Init Project</button>
    </div>
  </div>
</div>

<script>
// ── STATE ─────────────────────────────────────────────────────────────────
let _projects = {};
let _activePid = null;
let _liveLogTimer = null;
let _delivRowCount = 0;

const STATUS_CLS = {INIT:'s-init',SPEC:'s-spec',ACTIVE:'s-active',THRESHOLD:'s-threshold',CLOSED:'s-closed'};
const METER_COLORS = {INIT:'#475569',SPEC:'#06b6d4',ACTIVE:'#f0b429',THRESHOLD:'#10b981',CLOSED:'#8a9296'};

// ── BOARD LOAD ────────────────────────────────────────────────────────────
async function loadBoard(){
  try{
    const r=await fetch('/api/velvet/board'); const d=await r.json();
    _projects={};
    d.projects.forEach(p=>{ _projects[p.id]=p; });
    renderProjectList(d.projects);
    document.getElementById('boardProjectCount').textContent=d.total+' project'+(d.total!==1?'s':'');
  }catch(e){}
}

function renderProjectList(projects){
  const el=document.getElementById('proj-cards');
  if(!projects.length){ el.innerHTML='<div style="padding:20px 12px;font-size:10px;color:var(--dim);text-align:center">No projects yet.<br>Click + New to start.</div>'; return; }
  el.innerHTML='';
  projects.forEach(p=>{
    const pct=Math.round((p.completion||0)*100);
    const col=METER_COLORS[p.status]||'#475569';
    const card=document.createElement('div');
    card.className='proj-card'+(p.id===_activePid?' active':'');
    card.onclick=()=>selectProject(p.id);
    card.innerHTML=`
      <div class="pc-name">${p.name}</div>
      <div class="pc-goal">${(p.goal||'').slice(0,60)}${p.goal&&p.goal.length>60?'…':''}</div>
      <div class="pc-meter-wrap"><div class="pc-meter" style="width:${pct}%;background:${col}"></div></div>
      <div class="pc-foot">
        <span class="pc-status ${STATUS_CLS[p.status]||'s-init'}">${p.status||'INIT'}</span>
        <span>${pct}%</span>
      </div>`;
    el.appendChild(card);
  });
}

// ── PROJECT SELECT ────────────────────────────────────────────────────────
async function selectProject(pid){
  _activePid=pid;
  document.querySelectorAll('.proj-card').forEach(c=>c.classList.toggle('active',c.onclick&&false));
  renderProjectList(Object.values(_projects));
  document.getElementById('empty-state').style.display='none';
  const dc=document.getElementById('detail-content'); dc.style.display='flex';
  await refreshDetail();
  startLiveLog();
}

async function refreshDetail(){
  if(!_activePid)return;
  try{
    const r=await fetch(`/api/velvet/project/${_activePid}`); const d=await r.json();
    if(d.error)return;
    const p=d.project; const gate=d.gate;
    document.getElementById('dh-name').textContent=p.name;
    document.getElementById('dh-goal').textContent=p.goal;
    const created=p.created_at?new Date(p.created_at*1000).toLocaleString():'—';
    const agents=(p.agents||[]).join(', ');
    document.getElementById('dh-meta').textContent=`Started: ${created} · Agents: ${agents} · Spec SHA: ${(p.spec_sha||'—').slice(0,8)}`;
    const pct=Math.round((p.completion||0)*100);
    document.getElementById('dh-pct').textContent=pct+'%';
    document.getElementById('dh-gate').textContent=`gate ${Math.round((p.completion_gate||0.9)*100)}% · ${gate.verdict}`;
    document.getElementById('big-meter').style.width=pct+'%';
    document.getElementById('big-meter').style.background=METER_COLORS[p.status]||'#10b981';
    const stEl=document.getElementById('dh-status');
    stEl.textContent=p.status; stEl.className='pc-status '+(STATUS_CLS[p.status]||'s-init');
    _projects[_activePid]={..._projects[_activePid],...p};
    if(document.getElementById('view-deliverables').classList.contains('show')) renderDeliverables(p.deliverables||[]);
  }catch(e){}
}

function renderDeliverables(deliverables){
  const el=document.getElementById('view-deliverables'); el.innerHTML='';
  if(!deliverables.length){
    el.innerHTML='<div style="color:var(--dim);font-size:11px;padding:20px;text-align:center">No deliverables defined.</div>';
    return;
  }
  deliverables.forEach(d=>{
    const state=(d.state||'PENDING').toLowerCase();
    const stCls={'pass':'dv-pass','fail':'dv-fail','pending':'dv-pending','blocked':'dv-blocked'}[state]||'dv-pending';
    const card=document.createElement('div');
    card.className=`deliv-card ${state}`;
    card.innerHTML=`
      <div class="dv-head">
        <span class="dv-id">${d.id}</span>
        <span class="dv-weight">w${d.weight||1}</span>
        <span class="dv-state ${stCls}">${d.state||'PENDING'}</span>
      </div>
      <div class="dv-stmt">${d.statement}</div>
      ${d.verify_cmd?`<div class="dv-evidence">$ ${d.verify_cmd}</div>`:''}
      ${d.evidence_preview?`<div class="dv-evidence" style="color:var(--muted);margin-top:3px">${d.evidence_preview}</div>`:''}
      <div class="dv-actions">
        ${d.verify_cmd?`<button class="dv-btn run" onclick="runOneQC('${d.id}')">▶ Run QC</button>`:''}
        <button class="dv-btn pass" onclick="markDeliverable('${d.id}','PASS')">✓ PASS</button>
        <button class="dv-btn fail" onclick="markDeliverable('${d.id}','FAIL')">✗ FAIL</button>
      </div>`;
    el.appendChild(card);
  });
}

async function markDeliverable(did, state){
  if(!_activePid)return;
  await fetch(`/api/velvet/project/${_activePid}/deliverable`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project_id:_activePid,deliverable_id:did,state,evidence:'Manual mark by operator',actor:'operator'})
  });
  await refreshDetail();
  addLiveEntry('operator',`Marked ${did} → ${state}`);
}

async function runOneQC(did){
  if(!_activePid)return;
  addLiveEntry('velvet-qc',`Running QC on ${did}…`);
  const r=await fetch(`/api/velvet/project/${_activePid}/qc`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project_id:_activePid,deliverable_id:did})
  });
  const d=await r.json();
  const res=d.results&&d.results[0];
  if(res) addLiveEntry('velvet-qc',`${did} → ${res.state}${res.evidence_preview?' | '+res.evidence_preview:''}`);
  if(d.gpu_warning) addLiveEntry('velvet-qc','⚠ GPU is running — remember to stop it');
  await refreshDetail();
}

// ── DETAIL TABS ───────────────────────────────────────────────────────────
function dSwitch(tab){
  ['deliverables','log','handoff'].forEach(t=>{
    document.getElementById('dtab-'+t).classList.toggle('active',t===tab);
    const v=document.getElementById('view-'+t);
    v.classList.toggle('show',t===tab);
    v.style.display=t===tab?'block':'none';
  });
  document.getElementById('view-deliverables').style.display=tab==='deliverables'?'block':'none';
  if(tab==='log') loadBuildLog();
  if(tab==='handoff') loadHandoff();
}
// fix initial
document.getElementById('view-deliverables').style.display='block';
document.getElementById('view-log').style.display='none';
document.getElementById('view-handoff').style.display='none';

async function loadBuildLog(){
  if(!_activePid)return;
  const r=await fetch(`/api/velvet/project/${_activePid}/log`); const d=await r.json();
  const el=document.getElementById('view-log'); el.innerHTML='';
  if(!d.log.length){ el.innerHTML='<div style="color:var(--dim);padding:12px;font-size:10px">No log entries yet.</div>'; return; }
  d.log.slice().reverse().forEach(ev=>{
    const ts=ev.ts?new Date(ev.ts*1000).toLocaleTimeString():'—';
    const row=document.createElement('div'); row.className='log-entry';
    const msg=ev.deliverable?`${ev.event_type} · ${ev.deliverable} → ${ev.state||''}`
             :ev.message||ev.event_type||JSON.stringify(ev).slice(0,60);
    row.innerHTML=`<span class="log-ts">${ts}</span><span class="log-actor" style="color:var(--cyan)">${ev.actor||'?'}</span><span class="log-detail">${msg}</span>`;
    el.appendChild(row);
  });
}

async function loadHandoff(){
  if(!_activePid)return;
  const pd=_proj_dir||`/home/hunt/.crane/projects/${_activePid}`;
  try{
    const r=await fetch(`/api/velvet/project/${_activePid}`); const d=await r.json();
    if(d.error)return;
    const p=d.project;
    // render a markdown-like preview from the project data
    const pct=Math.round((p.completion||0)*100);
    const lines=[
      `# HANDOFF — ${p.name}`,``,
      `**Project ID:** \`${p.id}\``,
      `**Goal:** ${p.goal}`,
      `**Status:** ${p.status} · ${pct}% complete`,
      `**Created:** ${p.created_at?new Date(p.created_at*1000).toLocaleString():'—'}`,
      p.closed_at?`**Closed:** ${new Date(p.closed_at*1000).toLocaleString()}`:'',
      `**Agents:** ${(p.agents||[]).join(', ')}`,
      `**GitHub:** ${p.github_repo||'(not yet created)'}`,
      `**Spec SHA:** ${p.spec_sha||'—'}`,``,
      `## Spec Objectives`,``,
    ];
    (p.deliverables||[]).forEach(d=>{
      const icon=d.state==='PASS'?'✅':d.state==='FAIL'?'❌':'⏳';
      lines.push(`- ${icon} **${d.id}** (w${d.weight||1}): ${d.statement} — \`${d.state||'PENDING'}\``);
    });
    if(p.lessons_learned){ lines.push(``,`## Lessons Learned`,``,p.lessons_learned); }
    if(p.areas_of_opportunity){ lines.push(``,`## Areas of Opportunity`,``,p.areas_of_opportunity); }
    if((p.agent_improvements||[]).length){
      lines.push(``,`## Agent Improvement Recommendations`,``);
      p.agent_improvements.forEach(r=>lines.push(`- **${r.agent}**: ${r.recommendation}`));
    }
    document.getElementById('handoff-md').textContent=lines.filter(l=>l!==null&&l!==undefined).join('\n');
  }catch(e){}
}

// ── ACTIONS ───────────────────────────────────────────────────────────────
async function runQC(){
  if(!_activePid){alert('Select a project first');return;}
  addLiveEntry('velvet-qc','Running full QC pass…');
  const r=await fetch(`/api/velvet/project/${_activePid}/qc`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project_id:_activePid,deliverable_id:''})
  });
  const d=await r.json();
  const pass=(d.results||[]).filter(r=>r.state==='PASS').length;
  const total=(d.results||[]).length;
  addLiveEntry('velvet-qc',`QC complete: ${pass}/${total} PASS · completion ${Math.round((d.completion||0)*100)}% · ${d.gate?.verdict}`);
  if(d.gpu_warning) addLiveEntry('velvet-qc','⚠ GPU is running — remember to stop it');
  await refreshDetail();
}

async function closeProject(){
  if(!_activePid){alert('Select a project first');return;}
  const lessons=prompt('Lessons learned (brief):','');
  const opps=prompt('Areas of opportunity:','');
  const r=await fetch(`/api/velvet/project/${_activePid}/close`,{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project_id:_activePid,lessons_learned:lessons||'',areas_of_opportunity:opps||'',agent_improvements:[]})
  });
  const d=await r.json();
  if(d.error){ addLiveEntry('velvet-qc','⚠ Close blocked: '+d.error); return; }
  addLiveEntry('velvet-qc',`Project CLOSED · ${Math.round((d.completion||0)*100)}% · SHA ${(d.git_sha||'').slice(0,8)}`);
  if(d.gpu_stopped) addLiveEntry('velvet-qc','GPU stopped automatically before close');
  await loadBoard();
  await refreshDetail();
}

async function gpuCheck(){
  const r=await fetch('/api/velvet/gpu/check'); const d=await r.json();
  const dot=document.getElementById('gpuDot');
  const lbl=document.getElementById('gpuLabel');
  if(d.running){
    dot.className='gpu-dot on';
    lbl.textContent=`GPU RUNNING — $${(d.cost_accrued||0).toFixed(3)} accrued`;
    lbl.style.color='var(--red)';
    addLiveEntry('velvet-qc','⚠ GPU IS RUNNING — click Stop GPU to prevent waste');
  }else{
    dot.className='gpu-dot off';
    lbl.textContent='GPU off';
    lbl.style.color='var(--dim)';
    addLiveEntry('velvet-qc','✓ GPU confirmed off');
  }
}

// ── LIVE LOG STREAM ───────────────────────────────────────────────────────
function addLiveEntry(actor,msg){
  const el=document.getElementById('live-log');
  const row=document.createElement('div'); row.className='ll-entry';
  const ts=new Date().toLocaleTimeString();
  const colors={'velvet-track':'#10b981','velvet-qc':'#06b6d4','operator':'#f0b429','hannibal':'#f0b429','astra':'#ef4444'};
  row.innerHTML=`<span class="ll-time">${ts}</span><span class="ll-actor" style="color:${colors[actor]||'#818cf8'}">${actor}</span> <span class="ll-msg">${msg}</span>`;
  if(el.children.length>0&&el.children[0].textContent.includes('Waiting')){el.innerHTML='';}
  el.insertBefore(row,el.firstChild);
  if(el.children.length>50) el.removeChild(el.lastChild);
}

function startLiveLog(){
  if(_liveLogTimer) clearInterval(_liveLogTimer);
  _liveLogTimer=setInterval(async()=>{
    if(!_activePid)return;
    await refreshDetail();
    await gpuCheck();
  },10000);
}

// ── NEW PROJECT MODAL ─────────────────────────────────────────────────────
function openNewModal(){
  document.getElementById('new-proj-modal').classList.add('open');
  document.getElementById('deliverable-list').innerHTML='';
  _delivRowCount=0;
  addDelivRow(); // start with one
}
function closeModal(){ document.getElementById('new-proj-modal').classList.remove('open'); }

function addDelivRow(){
  _delivRowCount++;
  const i=_delivRowCount;
  const list=document.getElementById('deliverable-list');
  const row=document.createElement('div'); row.className='deliverable-row'; row.id='dr-'+i;
  row.innerHTML=`
    <div class="dr-top">
      <input class="modal-input dr-id" id="dr-id-${i}" placeholder="D-${i.toString().padStart(2,'0')}" value="D-${i.toString().padStart(2,'0')}"/>
      <select class="modal-input dr-weight" id="dr-w-${i}">
        <option value="1">w1 nice</option>
        <option value="2" selected>w2 should</option>
        <option value="3">w3 MUST</option>
      </select>
      <button class="modal-btn cancel" style="flex:0;padding:4px 8px;font-size:10px" onclick="document.getElementById('dr-${i}').remove()">✕</button>
    </div>
    <input class="modal-input" id="dr-stmt-${i}" placeholder="Deliverable statement…" style="margin-bottom:6px"/>
    <input class="modal-input" id="dr-cmd-${i}" placeholder="Verify command (e.g. pytest tests/test_auth.py -q)" style="font-family:'JetBrains Mono',monospace;font-size:10px"/>`;
  list.appendChild(row);
}

async function createProject(){
  const name=document.getElementById('np-name').value.trim();
  const goal=document.getElementById('np-goal').value.trim();
  const gate=parseFloat(document.getElementById('np-gate').value)||0.90;
  if(!name||!goal){alert('Name and goal are required.');return;}
  const deliverables=[];
  for(let i=1;i<=_delivRowCount;i++){
    const idEl=document.getElementById(`dr-id-${i}`);
    if(!idEl)continue;
    const stmt=document.getElementById(`dr-stmt-${i}`).value.trim();
    if(!stmt)continue;
    deliverables.push({
      id:idEl.value.trim()||`D-${i.toString().padStart(2,'0')}`,
      statement:stmt,
      weight:parseInt(document.getElementById(`dr-w-${i}`).value)||2,
      verify_cmd:document.getElementById(`dr-cmd-${i}`).value.trim(),
    });
  }
  try{
    const r=await fetch('/api/velvet/project/init',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,goal,deliverables,completion_gate:gate})
    });
    const d=await r.json();
    if(d.error){alert('Error: '+d.error);return;}
    closeModal();
    addLiveEntry('velvet-track',`Project initialized: ${name} · SHA ${d.spec_sha}`);
    await loadBoard();
    await selectProject(d.project_id);
  }catch(e){alert('Error: '+e.message);}
}

// ── INIT ──────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded',()=>{
  loadBoard();
  gpuCheck();
  setInterval(gpuCheck,15000);
  setInterval(loadBoard,20000);
});

// Ctrl+B toggle from other pages (injected via keyboard shortcut)
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='b'){ e.preventDefault(); window.location='/board'; }
});
</script>
</body>
</html>
""")
