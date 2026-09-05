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

        # Noise gate — cleans up bad mic noise floor
        if payload.get("gate"):
            parts.append("agate=threshold=0.02:ratio=10:attack=5:release=200")

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

        # De-esser — notch at 7 kHz
        if payload.get("deess"):
            parts.append("equalizer=f=7000:t=q:w=1:g=-4")

        # Tape warmth — gentle low-pass softening + harmonic coloration via acrusher
        tape = float(payload.get("tape") or 0)
        if tape > 20:
            cutoff = int(16000 - tape * 60)  # 16kHz→10kHz as tape goes 0→100
            parts.append(f"lowpass=f={max(cutoff,8000)}")

        # Glue compression
        if payload.get("compress"):
            parts.append("acompressor=threshold=-18dB:ratio=3:attack=10:release=100:makeup=2dB")

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

            <!-- ══════════ VOICE FOUNDRY MIXER ══════════ -->
            <div class="mixer">
                <div class="mixer-head">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span class="mixer-title">// Voice Fusion &amp; Track Mixer</span>
                        <span class="chip chip-dsp">DSP ENGINE : 24kHz</span>
                    </div>
                    <div style="display:flex; gap:6px;">
                        <button class="btn-secondary" onclick="clearChannels()">Clear Channels</button>
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
        const _origLoadVault = typeof loadVaultFiles === 'function' ? loadVaultFiles : null;
        document.addEventListener('vaultLoaded', qjPopulateSource);

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

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
