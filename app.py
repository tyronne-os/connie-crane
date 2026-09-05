import json
import os
import re
import subprocess
import tempfile
import time
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

import voice_engine

app = FastAPI(title="CRANE STUDIO - Voice Foundry (CONNIE NOLA)")

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

@app.get("/", response_class=HTMLResponse)
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
        header { height: 50px; background: var(--bg-panel); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 20px; flex-shrink: 0; }
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
        <div class="logo">🏗️ CRANE STUDIO // VOICE FOUNDRY</div>
        <div class="badge">CONNIE NOLA : CURATED MINING INTEL</div>
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

@app.get("/api/ide/gcp/status")
async def ide_gcp_status():
    cfg = _load_gcp_config()
    has_creds = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or cfg.get("credentials_path"))
    return {"configured": bool(cfg.get("project_id")),
            "project_id": cfg.get("project_id",""),
            "region": cfg.get("region","us-central1"),
            "has_credentials": has_creds}

@app.post("/api/ide/gcp/configure")
async def ide_gcp_configure(req: GCPConfigRequest):
    cfg = _load_gcp_config()
    cfg.update({"project_id": req.project_id, "region": req.region, "zone": req.zone})
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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono&display=swap">
<style>
/* CodeMirror inline theme */
.CodeMirror{height:100%;font-family:'JetBrains Mono',monospace;font-size:13px;background:#070d18;color:#e2e8f0;line-height:1.6;}
.CodeMirror-gutters{background:#0d1627;border-right:1px solid #1e3052;}
.CodeMirror-linenumber{color:#334155;padding:0 8px;}
.CodeMirror-cursor{border-left:2px solid #38bdf8;}
.cm-keyword{color:#c084fc;} .cm-string{color:#86efac;} .cm-comment{color:#475569;font-style:italic;}
.cm-number{color:#fb923c;} .cm-def{color:#38bdf8;} .cm-variable{color:#e2e8f0;}
.cm-operator{color:#f472b6;} .cm-atom{color:#fb923c;} .cm-property{color:#7dd3fc;}
.CodeMirror-selected{background:rgba(56,189,248,0.18)!important;}

:root {
  --bg: #070d18; --panel: #0d1627; --card: #111827; --border: #1e3052;
  --blue: #38bdf8; --purple: #a855f7; --green: #10b981; --orange: #f59e0b;
  --red: #ef4444; --text: #e2e8f0; --muted: #475569;
  --nvidia: #76b900;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;height:100vh;overflow:hidden;display:flex;flex-direction:column;}

/* ── TOPBAR ── */
#topbar{height:44px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;padding:0 14px;flex-shrink:0;}
.logo-ide{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:15px;background:linear-gradient(90deg,#38bdf8,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:2px;margin-right:6px;}
.tb-badge{background:rgba(118,185,0,0.15);color:var(--nvidia);padding:2px 8px;border-radius:4px;font-size:10px;border:1px solid var(--nvidia);font-weight:600;letter-spacing:1px;}
.tb-sep{width:1px;height:22px;background:var(--border);margin:0 4px;}
#tbModelSel{background:var(--card);border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:5px;font-size:11px;font-family:'JetBrains Mono',monospace;outline:none;cursor:pointer;max-width:280px;}
#tbModelSel:focus{border-color:var(--nvidia);}
.tb-status{font-size:11px;display:flex;align-items:center;gap:5px;}
.dot{width:7px;height:7px;border-radius:50%;background:var(--muted);}
.dot.on{background:var(--green);box-shadow:0 0 6px var(--green);}
.dot.warn{background:var(--orange);}
.tb-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;transition:.15s;}
.tb-btn:hover{border-color:var(--blue);color:var(--blue);}
.tb-btn.active{border-color:var(--purple);color:var(--purple);}
#tbGHBtn{border-color:rgba(255,255,255,.2);}
#tbGHBtn.connected{border-color:var(--green);color:var(--green);}
#tbGCPBtn.connected{border-color:var(--nvidia);color:var(--nvidia);}
.tb-spacer{flex:1;}
#voiceBtn{background:linear-gradient(135deg,#7c3aed,#f59e0b);color:#fff;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:1px;}

/* ── LAYOUT ── */
#main{display:flex;flex:1;overflow:hidden;}

/* ── SIDEBAR ── */
#sidebar{width:220px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;transition:.25s;}
#sidebar.collapsed{width:0;}
#sideHead{padding:8px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px;flex-shrink:0;}
#sideHead select{flex:1;background:var(--card);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:4px;font-size:11px;outline:none;}
#fileTree{flex:1;overflow-y:auto;padding:4px 0;}
.ft-item{padding:4px 10px 4px 14px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:5px;color:var(--text);}
.ft-item:hover{background:rgba(56,189,248,.08);}
.ft-item.active{background:rgba(56,189,248,.15);color:var(--blue);}
.ft-dir{color:var(--orange);font-weight:600;}
.ft-indent{display:inline-block;}

/* ── EDITOR AREA ── */
#editorArea{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;}
#tabBar{height:34px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;overflow-x:auto;flex-shrink:0;}
.ed-tab{padding:0 14px;height:34px;display:flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:11px;cursor:pointer;border-right:1px solid var(--border);white-space:nowrap;color:var(--muted);flex-shrink:0;}
.ed-tab.active{background:var(--bg);color:var(--text);border-top:2px solid var(--blue);}
.ed-tab .tab-close{opacity:.4;font-size:13px;line-height:1;}
.ed-tab .tab-close:hover{opacity:1;color:var(--red);}
#editorWrap{flex:1;overflow:hidden;position:relative;}
#terminal{height:180px;background:#020a0f;border-top:1px solid var(--border);flex-shrink:0;display:flex;flex-direction:column;overflow:hidden;}
#termHead{padding:4px 10px;border-bottom:1px solid var(--border);font-size:10px;color:var(--muted);display:flex;gap:10px;align-items:center;}
#termOut{flex:1;overflow-y:auto;padding:6px 10px;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.6;}
#termInputRow{display:flex;border-top:1px solid var(--border);flex-shrink:0;}
#termCwd{padding:4px 8px;color:var(--green);font-family:'JetBrains Mono',monospace;font-size:11px;flex-shrink:0;}
#termInput{flex:1;background:transparent;border:none;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:11px;outline:none;padding:4px 0;}

/* ── AGENT PANEL ── */
#agentPanel{width:340px;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;}
#agentHead{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-shrink:0;}
#agentHead .ah-title{font-weight:700;font-size:13px;letter-spacing:.5px;}
.model-tag{font-size:9px;background:rgba(118,185,0,.15);color:var(--nvidia);padding:1px 6px;border-radius:3px;border:1px solid var(--nvidia);margin-left:auto;font-family:'JetBrains Mono',monospace;}
#chatLog{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:10px;}
.msg{border-radius:8px;padding:8px 12px;font-size:12px;line-height:1.6;max-width:100%;}
.msg.user{background:rgba(56,189,248,.1);border:1px solid rgba(56,189,248,.2);align-self:flex-end;color:var(--text);}
.msg.agent{background:rgba(168,85,247,.08);border:1px solid rgba(168,85,247,.2);align-self:flex-start;color:var(--text);}
.msg.sys{background:rgba(16,185,129,.07);border:1px solid rgba(16,185,129,.2);align-self:center;color:var(--green);font-size:11px;text-align:center;}
.msg pre{background:rgba(0,0,0,.4);padding:6px 8px;border-radius:4px;overflow-x:auto;font-family:'JetBrains Mono',monospace;font-size:11px;margin-top:6px;}
#chatComposer{border-top:1px solid var(--border);padding:8px;display:flex;flex-direction:column;gap:6px;flex-shrink:0;}
#composerTools{display:flex;gap:5px;flex-wrap:wrap;}
.ctx-btn{background:var(--card);border:1px solid var(--border);color:var(--muted);padding:3px 8px;border-radius:4px;font-size:10px;cursor:pointer;font-family:'JetBrains Mono',monospace;}
.ctx-btn:hover{border-color:var(--purple);color:var(--purple);}
.ctx-btn.active{border-color:var(--purple);color:var(--purple);background:rgba(168,85,247,.15);}
#chatInput{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px 10px;color:var(--text);font-size:12px;font-family:'Inter',sans-serif;outline:none;resize:none;width:100%;min-height:70px;max-height:160px;}
#chatInput:focus{border-color:var(--blue);}
#sendRow{display:flex;align-items:center;gap:6px;}
#sendBtn{background:linear-gradient(135deg,var(--purple),var(--blue));color:#fff;border:none;padding:6px 16px;border-radius:5px;cursor:pointer;font-weight:600;font-size:12px;transition:.15s;}
#sendBtn:hover{opacity:.9;}
#sendBtn:disabled{opacity:.4;cursor:default;}
.thinking{display:flex;gap:4px;align-items:center;padding:6px;}
.thinking span{width:6px;height:6px;border-radius:50%;background:var(--purple);animation:blink 1.2s infinite;}
.thinking span:nth-child(2){animation-delay:.3s;}
.thinking span:nth-child(3){animation-delay:.6s;}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}

/* ── MODALS ── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center;}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:24px;min-width:380px;max-width:500px;display:flex;flex-direction:column;gap:14px;}
.modal h3{font-size:15px;font-weight:700;}
.modal input,.modal select{background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:5px;font-size:12px;font-family:'JetBrains Mono',monospace;outline:none;width:100%;}
.modal input:focus{border-color:var(--blue);}
.modal-row{display:flex;gap:8px;}
.modal-btn{background:var(--purple);color:#fff;border:none;padding:8px 16px;border-radius:5px;cursor:pointer;font-weight:600;font-size:12px;flex:1;}
.modal-btn.sec{background:transparent;border:1px solid var(--border);color:var(--muted);}
.modal-label{font-size:11px;color:var(--muted);margin-bottom:2px;}
.modal-hint{font-size:10px;color:var(--muted);line-height:1.5;}

/* scrollbars */
::-webkit-scrollbar{width:4px;height:4px;} ::-webkit-scrollbar-track{background:transparent;} ::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}

/* GH panel */
#ghPanel{padding:8px 10px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:3px;}
.repo-row{padding:5px 8px;border-radius:4px;cursor:pointer;display:flex;align-items:center;gap:6px;font-size:11px;font-family:'JetBrains Mono',monospace;}
.repo-row:hover{background:rgba(56,189,248,.08);}
.repo-priv{font-size:9px;background:rgba(168,85,247,.2);color:var(--purple);padding:0 4px;border-radius:3px;}
.repo-pub{font-size:9px;background:rgba(16,185,129,.15);color:var(--green);padding:0 4px;border-radius:3px;}
</style>
</head>
<body>

<!-- TOP BAR -->
<div id="topbar">
  <span class="logo-ide">CRANE&nbsp;IDE</span>
  <span class="tb-badge">NVIDIA&nbsp;NIM</span>
  <div class="tb-sep"></div>
  <select id="tbModelSel" title="Select NVIDIA NIM model">
    <option value="">⟳ Loading models…</option>
  </select>
  <div class="tb-sep"></div>
  <div class="tb-status" id="nvStatus"><div class="dot" id="nvDot"></div><span id="nvLabel">NIM</span></div>
  <div class="tb-sep"></div>
  <button class="tb-btn" id="tbGHBtn" onclick="openGHModal()">⎇ GitHub</button>
  <button class="tb-btn" id="tbGCPBtn" onclick="openGCPModal()">☁ GCP GPU</button>
  <div class="tb-spacer"></div>
  <span style="font-size:10px;color:var(--muted);" id="activeModel"></span>
  <div class="tb-sep"></div>
  <button class="tb-btn active" onclick="window.location='/'">🎙 Voice Foundry</button>
  <button id="voiceBtn" onclick="window.location='/'">BIG Q</button>
</div>

<!-- MAIN LAYOUT -->
<div id="main">

  <!-- SIDEBAR: file tree -->
  <div id="sidebar">
    <div id="sideHead">
      <span style="font-size:10px;color:var(--muted);flex-shrink:0">REPO</span>
      <select id="repoSel" onchange="loadRepoTree()">
        <option value="">connect GitHub…</option>
      </select>
    </div>
    <div id="fileTree"><div style="padding:12px 10px;font-size:11px;color:var(--muted);">Connect GitHub to browse files</div></div>
  </div>

  <!-- EDITOR + TERMINAL -->
  <div id="editorArea">
    <div id="tabBar">
      <div class="ed-tab active" id="welcomeTab">✦ welcome</div>
    </div>
    <div id="editorWrap"></div>
    <div id="terminal">
      <div id="termHead">
        <span style="color:var(--green);font-weight:700;font-size:11px;">TERMINAL</span>
        <span id="termCwdDisplay" style="color:var(--muted);font-size:10px;">/home/hunt</span>
        <span style="flex:1"></span>
        <button class="ctx-btn" onclick="clearTerm()">clear</button>
      </div>
      <div id="termOut"></div>
      <div id="termInputRow">
        <span id="termCwd">~/</span>
        <input id="termInput" placeholder="enter command…" onkeydown="termKey(event)">
      </div>
    </div>
  </div>

  <!-- AGENT PANEL -->
  <div id="agentPanel">
    <div id="agentHead">
      <span>⬡</span>
      <span class="ah-title">CONNIE&nbsp;CODE</span>
      <span class="model-tag" id="agentModelTag">NVIDIA NIM</span>
    </div>
    <div id="chatLog">
      <div class="msg sys">CRANE IDE is online. Select a model above, connect GitHub, and start coding.</div>
    </div>
    <div id="chatComposer">
      <div id="composerTools">
        <button class="ctx-btn" id="ctxCodeBtn" onclick="toggleCtx('code')" title="Include open file">&lt;/&gt; code</button>
        <button class="ctx-btn" id="ctxTermBtn" onclick="toggleCtx('term')" title="Include terminal output">$ term</button>
        <button class="ctx-btn" id="ctxGitBtn" onclick="toggleCtx('git')" title="Include git diff">⎇ diff</button>
        <button class="ctx-btn" onclick="injectPrompt('Write tests for the selected code')">🧪 tests</button>
        <button class="ctx-btn" onclick="injectPrompt('Explain this code step by step')">💡 explain</button>
        <button class="ctx-btn" onclick="injectPrompt('Find and fix bugs in this code')">🐛 fix</button>
        <button class="ctx-btn" onclick="injectPrompt('Refactor this code for clarity and performance')">♻ refactor</button>
        <button class="ctx-btn" onclick="commitCurrentFile()">📤 commit</button>
      </div>
      <textarea id="chatInput" placeholder="Ask CONNIE CODE anything… (Shift+Enter for newline, Enter to send)" onkeydown="chatKey(event)"></textarea>
      <div id="sendRow">
        <button id="sendBtn" onclick="sendChat()">Send ↑</button>
        <span style="font-size:10px;color:var(--muted);flex:1;" id="tokenEstimate"></span>
        <button class="ctx-btn" onclick="clearChat()">clear</button>
      </div>
    </div>
  </div>
</div>

<!-- GITHUB MODAL -->
<div id="ghModal" class="modal-overlay" style="display:none">
  <div class="modal">
    <h3>⎇ GitHub Connection</h3>
    <div>
      <div class="modal-label">Personal Access Token (stored server-side in env)</div>
      <input id="ghTokenInput" type="password" placeholder="ghp_xxxxxxxxxxxxxxxxxxxx">
      <div class="modal-hint">Needs repo + contents scopes. Token is saved to ~/.crane_gh (never sent to browser).</div>
    </div>
    <div class="modal-row">
      <button class="modal-btn" onclick="saveGHToken()">Save Token</button>
      <button class="modal-btn sec" onclick="closeModal('ghModal')">Cancel</button>
    </div>
    <div id="ghStatus" style="font-size:11px;color:var(--green);display:none;"></div>
    <div id="ghRepoList" style="max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:3px;"></div>
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
    <div class="modal-row" style="gap:8px">
      <div style="flex:1">
        <div class="modal-label">Region</div>
        <select id="gcpRegion" style="background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:5px;width:100%;outline:none;">
          <option>us-central1</option><option>us-east4</option><option>us-west4</option>
          <option>europe-west4</option><option>asia-northeast1</option>
        </select>
      </div>
      <div style="flex:1">
        <div class="modal-label">Zone</div>
        <select id="gcpZone" style="background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:5px;width:100%;outline:none;">
          <option>us-central1-a</option><option>us-central1-b</option><option>us-central1-c</option>
          <option>us-east4-a</option><option>europe-west4-a</option>
        </select>
      </div>
    </div>
    <div class="modal-hint">
      Set GOOGLE_APPLICATION_CREDENTIALS env var to your service account JSON path before starting CRANE.<br>
      GPU types available via NGC enterprise: A100 80GB, H100, L4, T4.
    </div>
    <div class="modal-row">
      <button class="modal-btn" onclick="saveGCP()">Save Config</button>
      <button class="modal-btn sec" onclick="closeModal('gcpModal')">Cancel</button>
    </div>
    <div id="gcpMsg" style="font-size:11px;color:var(--green);display:none;"></div>
  </div>
</div>

<script>
// ── STATE ──────────────────────────────────────────────────────────────────
let _editor = null;
let _model = '';
let _ctx = {code: false, term: false, git: false};
let _messages = [];
let _tabs = {};           // path → {content, sha, lang, editor}
let _activeTab = 'welcome';
let _termCwd = '/home/hunt';
let _termHistory = [];
let _repoOwner = '';
let _repoName = '';
let _ghToken = '';
let _termLog = '';

// ── EDITOR INIT ────────────────────────────────────────────────────────────
function initEditor() {
  const wrap = document.getElementById('editorWrap');
  wrap.style.flex = '1';
  wrap.style.overflow = 'hidden';
  _editor = CodeMirror(wrap, {
    value: `// Welcome to CRANE IDE\n// Powered by NVIDIA NIM — Select a model above to start coding\n// Connect GitHub (top bar) to open files\n\nconsole.log("Let's build something great.");`,
    mode: 'javascript',
    theme: 'crane',
    lineNumbers: true,
    tabSize: 2,
    indentWithTabs: false,
    lineWrapping: false,
    autofocus: true,
    extraKeys: {
      'Ctrl-S': saveCurrentFile,
      'Ctrl-Enter': () => sendChat(),
    }
  });
  _editor.setSize('100%', '100%');
}

// ── MODEL LOADING ──────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const r = await fetch('/api/ide/nvidia/models');
    const d = await r.json();
    const sel = document.getElementById('tbModelSel');
    sel.innerHTML = '';
    (d.models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `[${m.tag}] ${m.label}`;
      sel.appendChild(opt);
    });
    // Default to DeepSeek Coder
    const coding = (d.models || []).find(m => m.tag === 'CODE');
    if (coding) sel.value = coding.id;
    _model = sel.value;
    updateModelDisplay();
    const nvDot = document.getElementById('nvDot');
    const nvLabel = document.getElementById('nvLabel');
    if (d.source === 'live') {
      nvDot.className = 'dot on'; nvLabel.textContent = 'NIM live';
    } else {
      nvDot.className = 'dot warn'; nvLabel.textContent = 'NIM offline';
    }
  } catch(e) {
    console.error(e);
  }
}

document.getElementById('tbModelSel').addEventListener('change', function() {
  _model = this.value;
  updateModelDisplay();
});

function updateModelDisplay() {
  const label = document.getElementById('tbModelSel').selectedOptions[0]?.textContent || _model;
  document.getElementById('activeModel').textContent = label;
  document.getElementById('agentModelTag').textContent = _model.split('/').pop()?.slice(0,20) || 'NIM';
}

// ── GITHUB ─────────────────────────────────────────────────────────────────
function openGHModal() { document.getElementById('ghModal').style.display='flex'; loadGHRepos(); }
function openGCPModal() {
  document.getElementById('gcpModal').style.display='flex';
  fetch('/api/ide/gcp/status').then(r=>r.json()).then(d=>{
    if(d.project_id) document.getElementById('gcpProject').value = d.project_id;
    if(d.region) document.getElementById('gcpRegion').value = d.region;
  });
}
function closeModal(id) { document.getElementById(id).style.display='none'; }

async function saveGHToken() {
  const tok = document.getElementById('ghTokenInput').value.trim();
  if(!tok) return;
  // Save to server via a temp env approach (write to ~/.crane_gh)
  const r = await fetch('/api/ide/shell', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd:`echo '${tok.replace(/'/g,"'\\''")}' > ~/.crane_gh && chmod 600 ~/.crane_gh`})});
  const d = await r.json();
  const st = document.getElementById('ghStatus');
  if(d.rc === 0) {
    st.style.display='block'; st.textContent='✅ Token saved. Reload to activate.';
    document.getElementById('tbGHBtn').classList.add('connected');
    loadGHRepos();
  } else {
    st.style.display='block'; st.style.color='var(--red)'; st.textContent='❌ '+d.stderr;
  }
}

async function loadGHRepos() {
  const r = await fetch('/api/ide/github/repos');
  const d = await r.json();
  if(d.error) {
    document.getElementById('ghRepoList').innerHTML = `<div style="color:var(--red);font-size:11px;padding:6px">${d.error}</div>`;
    return;
  }
  document.getElementById('tbGHBtn').classList.add('connected');
  const sel = document.getElementById('repoSel');
  sel.innerHTML = '<option value="">— pick repo —</option>';
  const list = document.getElementById('ghRepoList');
  list.innerHTML = '';
  (d.repos || []).forEach(repo => {
    const opt = document.createElement('option');
    opt.value = repo.full_name;
    opt.textContent = repo.full_name;
    sel.appendChild(opt);
    const row = document.createElement('div');
    row.className = 'repo-row';
    const badge = repo.private
      ? '<span class="repo-priv">priv</span>'
      : '<span class="repo-pub">pub</span>';
    row.innerHTML = badge + ' ' + repo.name + (repo.language?`<span style="margin-left:auto;font-size:9px;color:var(--muted)">${repo.language}</span>`:'');
    row.onclick = () => { sel.value = repo.full_name; loadRepoTree(); closeModal('ghModal'); };
    list.appendChild(row);
  });
}

async function loadRepoTree() {
  const full = document.getElementById('repoSel').value;
  if(!full) return;
  const [owner, repo] = full.split('/');
  _repoOwner = owner; _repoName = repo;
  const tree = document.getElementById('fileTree');
  tree.innerHTML = '<div style="padding:10px;color:var(--muted);font-size:11px">Loading…</div>';
  const r = await fetch('/api/ide/github/tree', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({owner, repo, branch:'main'})});
  const d = await r.json();
  if(d.error) { tree.innerHTML = `<div style="color:var(--red);padding:10px;font-size:11px">${d.error}</div>`; return; }
  renderTree(d.tree || []);
}

function renderTree(items) {
  const tree = document.getElementById('fileTree');
  tree.innerHTML = '';
  // Build directory structure
  const dirs = {};
  items.forEach(item => {
    const parts = item.path.split('/');
    const depth = parts.length - 1;
    const div = document.createElement('div');
    div.className = 'ft-item' + (item.type==='tree' ? ' ft-dir' : '');
    div.style.paddingLeft = (14 + depth * 12) + 'px';
    const icon = item.type === 'tree' ? '📁' : getFileIcon(item.path);
    div.innerHTML = `${icon} ${parts[parts.length-1]}`;
    if(item.type === 'blob') {
      div.onclick = () => openGHFile(item.path);
    }
    tree.appendChild(div);
  });
}

function getFileIcon(path) {
  const ext = path.split('.').pop().toLowerCase();
  const m = {py:'🐍',js:'⚡',ts:'💙',tsx:'⚛',jsx:'⚛',html:'🌐',css:'🎨',md:'📝',json:'{}',sh:'$',yaml:'📋',yml:'📋',txt:'📄',png:'🖼',jpg:'🖼',svg:'✦'};
  return m[ext] || '📄';
}

async function openGHFile(path) {
  if(!_repoOwner) return;
  const r = await fetch('/api/ide/github/file', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({owner:_repoOwner, repo:_repoName, path, branch:'main'})});
  const d = await r.json();
  if(d.error) { appendMsg('sys','❌ '+d.error); return; }
  // detect language
  const ext = path.split('.').pop().toLowerCase();
  const langMap = {py:'python',js:'javascript',ts:'javascript',jsx:'javascript',tsx:'javascript',
    html:'htmlmixed',css:'css',json:'javascript',sh:'shell',md:'markdown',yaml:'yaml',yml:'yaml'};
  const lang = langMap[ext] || 'text';
  _editor.setValue(d.content || '');
  _editor.setOption('mode', lang);
  _tabs[path] = {content: d.content, sha: d.sha, path};
  addTab(path);
  // mark active in tree
  document.querySelectorAll('.ft-item').forEach(el => {
    el.classList.toggle('active', el.textContent.includes(path.split('/').pop()));
  });
}

// ── TABS ───────────────────────────────────────────────────────────────────
function addTab(path) {
  const bar = document.getElementById('tabBar');
  const fname = path.split('/').pop();
  if(document.getElementById('tab_'+btoa(path))) {
    setActiveTab(path); return;
  }
  const tab = document.createElement('div');
  tab.className = 'ed-tab';
  tab.id = 'tab_'+btoa(path);
  tab.innerHTML = getFileIcon(path)+' '+fname+'<span class="tab-close" onclick="closeTab(\''+path+'\',event)">×</span>';
  tab.onclick = () => setActiveTab(path);
  bar.appendChild(tab);
  setActiveTab(path);
}

function setActiveTab(path) {
  _activeTab = path;
  document.querySelectorAll('.ed-tab').forEach(t => t.classList.remove('active'));
  const t = document.getElementById('tab_'+btoa(path));
  if(t) t.classList.add('active');
  if(_tabs[path]) {
    _editor.setValue(_tabs[path].content || '');
  }
}

function closeTab(path, e) {
  e.stopPropagation();
  const t = document.getElementById('tab_'+btoa(path));
  if(t) t.remove();
  delete _tabs[path];
  _activeTab = 'welcome';
}

// ── SAVE / COMMIT ──────────────────────────────────────────────────────────
async function saveCurrentFile() {
  if(!_activeTab || _activeTab==='welcome' || !_repoOwner) return;
  const content = _editor.getValue();
  const sha = _tabs[_activeTab]?.sha || '';
  const msg = `CRANE IDE: update ${_activeTab.split('/').pop()}`;
  const r = await fetch('/api/ide/github/write', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({owner:_repoOwner, repo:_repoName, path:_activeTab, content, message:msg, sha, branch:'main'})});
  const d = await r.json();
  if(d.status === 'ok') {
    appendMsg('sys','✅ Saved to GitHub: '+_activeTab);
    if(_tabs[_activeTab]) { _tabs[_activeTab].sha = d.sha; _tabs[_activeTab].content = content; }
  } else {
    appendMsg('sys','❌ Save failed: '+(d.error||'unknown'));
  }
}

async function commitCurrentFile() {
  const content = _editor.getValue();
  const msg = prompt('Commit message:', `CRANE IDE: update ${_activeTab.split('/').pop()}`);
  if(!msg) return;
  const sha = _tabs[_activeTab]?.sha || '';
  const r = await fetch('/api/ide/github/write', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({owner:_repoOwner, repo:_repoName, path:_activeTab, content, message:msg, sha, branch:'main'})});
  const d = await r.json();
  appendMsg('sys', d.status==='ok' ? '✅ Committed: '+msg : '❌ '+(d.error||''));
}

// ── GCP ────────────────────────────────────────────────────────────────────
async function saveGCP() {
  const project = document.getElementById('gcpProject').value.trim();
  const region = document.getElementById('gcpRegion').value;
  const zone = document.getElementById('gcpZone').value;
  if(!project) return;
  const r = await fetch('/api/ide/gcp/configure', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({project_id:project, region, zone})});
  const d = await r.json();
  const msg = document.getElementById('gcpMsg');
  msg.style.display='block';
  if(d.status==='saved') {
    msg.textContent = '✅ GCP configured: '+project+' ('+region+')';
    document.getElementById('tbGCPBtn').classList.add('connected');
    document.getElementById('tbGCPBtn').textContent = '☁ '+project;
  } else {
    msg.style.color='var(--red)'; msg.textContent = '❌ '+JSON.stringify(d);
  }
}

// ── TERMINAL ───────────────────────────────────────────────────────────────
async function runCmd(cmd) {
  const out = document.getElementById('termOut');
  const line = document.createElement('div');
  line.style.cssText = 'color:var(--green);margin-bottom:2px;';
  line.textContent = '$ ' + cmd;
  out.appendChild(line);
  _termLog += '$ '+cmd+'\n';
  const r = await fetch('/api/ide/shell', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd, cwd:_termCwd})});
  const d = await r.json();
  if(d.stdout) {
    const o = document.createElement('pre');
    o.style.cssText = 'color:var(--text);white-space:pre-wrap;margin-bottom:4px;';
    o.textContent = d.stdout;
    out.appendChild(o);
    _termLog += d.stdout;
  }
  if(d.stderr) {
    const e = document.createElement('pre');
    e.style.cssText = 'color:var(--red);white-space:pre-wrap;margin-bottom:4px;';
    e.textContent = d.stderr;
    out.appendChild(e);
    _termLog += d.stderr;
  }
  // update cwd if cd command
  if(cmd.trim().startsWith('cd ')) {
    const newDir = cmd.trim().slice(3).trim();
    if(d.rc === 0) {
      const pw = await fetch('/api/ide/shell',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({cmd:'pwd',cwd:newDir.startsWith('/')?newDir:_termCwd+'/'+newDir})});
      const pd = await pw.json();
      if(pd.stdout) { _termCwd = pd.stdout.trim(); document.getElementById('termCwdDisplay').textContent = _termCwd; }
    }
  }
  out.scrollTop = out.scrollHeight;
}

function termKey(e) {
  if(e.key === 'Enter') {
    const inp = document.getElementById('termInput');
    const cmd = inp.value.trim();
    if(!cmd) return;
    _termHistory.push(cmd);
    inp.value = '';
    runCmd(cmd);
  }
}

function clearTerm() { document.getElementById('termOut').innerHTML=''; _termLog=''; }

// ── CHAT / AGENT ───────────────────────────────────────────────────────────
function toggleCtx(key) {
  _ctx[key] = !_ctx[key];
  document.getElementById('ctx'+key.charAt(0).toUpperCase()+key.slice(1)+'Btn').classList.toggle('active', _ctx[key]);
}

function injectPrompt(text) {
  const inp = document.getElementById('chatInput');
  inp.value = text;
  inp.focus();
}

function chatKey(e) {
  if(e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

function appendMsg(role, text) {
  const log = document.getElementById('chatLog');
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  // render code blocks
  const rendered = text.replace(/```([\s\S]*?)```/g, (_,c)=>`<pre>${escHtml(c)}</pre>`).replace(/\n/g,'<br>');
  div.innerHTML = rendered;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function clearChat() { document.getElementById('chatLog').innerHTML=''; _messages=[]; }

async function sendChat() {
  if(!_model) { appendMsg('sys','⚠ Select a model first'); return; }
  const inp = document.getElementById('chatInput');
  const userText = inp.value.trim();
  if(!userText) return;
  inp.value = '';

  // build context
  let fullPrompt = userText;
  if(_ctx.code && _editor) {
    const sel = _editor.getSelection();
    const code = sel || _editor.getValue().slice(0, 8000);
    fullPrompt += '\n\n```\n' + code + '\n```';
  }
  if(_ctx.term && _termLog) {
    fullPrompt += '\n\nTerminal output:\n```\n' + _termLog.slice(-3000) + '\n```';
  }

  appendMsg('user', userText);
  _messages.push({role:'user', content: fullPrompt});

  // thinking indicator
  const thinking = document.createElement('div');
  thinking.className = 'thinking';
  thinking.innerHTML = '<span></span><span></span><span></span>';
  document.getElementById('chatLog').appendChild(thinking);

  const btn = document.getElementById('sendBtn');
  btn.disabled = true;

  try {
    const systemPrompt = `You are CONNIE CODE, an elite autonomous coding agent inside CRANE IDE. You have access to the user's codebase via GitHub and can execute shell commands. When writing code, format it in fenced code blocks. Be direct, precise, and build production-quality code. The user is building CRANE STUDIO — a FastAPI voice foundry and AI agent platform. When you generate code, offer to write it directly to the file. Repository: ${_repoOwner}/${_repoName || 'not connected'}.`;

    const r = await fetch('/api/ide/chat', {method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        model: _model,
        messages: _messages.slice(-20), // keep last 20 for context window
        system: systemPrompt,
        max_tokens: 4096,
        temperature: 0.2
      })
    });
    const d = await r.json();
    thinking.remove();
    if(d.error) { appendMsg('sys','❌ '+d.error); btn.disabled=false; return; }
    const reply = d.content;
    _messages.push({role:'assistant', content: reply});
    appendMsg('agent', reply);
    // auto-extract and offer to insert code
    const codeMatch = reply.match(/```(?:\w+)?\n([\s\S]+?)```/);
    if(codeMatch && _editor) {
      const applyBtn = document.createElement('button');
      applyBtn.className = 'ctx-btn';
      applyBtn.style.cssText = 'background:rgba(16,185,129,.15);border-color:var(--green);color:var(--green);margin-top:6px;';
      applyBtn.textContent = '⬇ Apply code to editor';
      applyBtn.onclick = () => { _editor.setValue(codeMatch[1]); applyBtn.remove(); };
      document.getElementById('chatLog').lastChild.appendChild(applyBtn);
    }
  } catch(e) {
    thinking.remove();
    appendMsg('sys','❌ '+e.message);
  }
  btn.disabled = false;
  document.getElementById('chatLog').scrollTop = 9999;
}

// ── GCP STATUS ─────────────────────────────────────────────────────────────
async function checkGCPStatus() {
  const r = await fetch('/api/ide/gcp/status');
  const d = await r.json();
  if(d.configured) {
    document.getElementById('tbGCPBtn').classList.add('connected');
    document.getElementById('tbGCPBtn').textContent = '☁ '+d.project_id;
  }
}

// ── INIT ───────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  initEditor();
  loadModels();
  checkGCPStatus();
  // auto-load GH repos silently
  loadGHRepos().catch(()=>{});
  // Run a quick status check in terminal
  setTimeout(() => runCmd('echo "CRANE IDE ready — $(date)" && python3 --version && git --version'), 500);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
