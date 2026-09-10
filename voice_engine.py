"""
CRANE STUDIO — swappable voice synthesis engine.

A registry of TTS backends that can be hot-swapped from the UI, plus a porting
path for pulling any model off the Hugging Face Hub.

Design rule: a backend declares what it needs (RAM, GPU, licence) and this
module refuses to pretend a model will run when the hardware says otherwise.
A model that crawls at 40x realtime is not "working" — it is a hang with extra
steps, and reporting it as available wastes the user's afternoon.
"""

import os
import json
import shutil
import subprocess
import threading
import time

PY = "/home/hunt/.venv/bin/python"
UV = "/home/hunt/.local/bin/uv"

MODELS_DIR = os.environ.get("CRANE_MODELS_DIR", os.path.expanduser("~/crane_models"))
REGISTRY_FILE = os.path.join(MODELS_DIR, "registry.json")
os.makedirs(MODELS_DIR, exist_ok=True)

_LOCK = threading.Lock()
_JOBS = {}


# ── hardware probe ──────────────────────────────────────────────────────────

def hardware():
    """What this box actually is. Drives the 'will it run' verdicts."""
    info = {"cores": os.cpu_count() or 1, "ram_gb": 0.0, "gpu": None, "cuda": False}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    info["ram_gb"] = round(int(line.split()[1]) / 1048576, 1)
                    break
    except Exception:
        pass
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            info["gpu"] = p.stdout.strip().splitlines()[0]
            info["cuda"] = True
    except Exception:
        pass
    if not info["gpu"]:
        try:
            p = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
            for line in p.stdout.splitlines():
                if "VGA" in line or "3D controller" in line:
                    info["gpu"] = line.split(":", 2)[-1].strip()
                    break
        except Exception:
            pass
    return info


# ── backend catalogue ───────────────────────────────────────────────────────
# needs_ram_gb is the working set at inference, not the download size.
# rtf = realtime factor on a modest CPU; >1.0 means slower than realtime.

CATALOG = {
    "piper": {
        "label": "Piper (VITS)",
        "repo": "rhasspy/piper-voices",
        "kind": "tts",
        "clone": False,
        "needs_ram_gb": 0.4,
        "needs_gpu": False,
        "cpu_rtf": 0.15,
        "licence": "MIT",
        "install": "pip",
        "package": "piper-tts",
        "note": "Fast CPU synthesis, fixed voices. The reliable workhorse — "
                "runs realtime on a Raspberry Pi. No cloning.",
    },
    "pocket-tts": {
        "label": "Kyutai Pocket TTS (100M)",
        "repo": "kyutai/pocket-tts",
        "kind": "tts",
        "clone": True,
        "needs_ram_gb": 0.8,
        "needs_gpu": False,
        "cpu_rtf": 0.6,
        "licence": "CC-BY / see repo",
        "install": "pip",
        "package": "moshi",
        "gated": True,
        "note": "100M params, faster than realtime on CPU, voice cloning from "
                "a short reference. Gated — request access on the HF page first.",
    },
    "xtts-v2": {
        "label": "XTTS-v2 (Coqui)",
        "repo": "coqui/XTTS-v2",
        "kind": "tts",
        "clone": True,
        "needs_ram_gb": 4.0,
        "needs_gpu": False,
        "cpu_rtf": 4.0,
        "licence": "CPML (non-commercial)",
        "install": "pip",
        "package": "TTS",
        "note": "Strong zero-shot cloning from ~6s of reference audio. Usable "
                "on CPU but slow — budget ~4s of compute per second of speech.",
    },

    "breeze-tts-2": {
        "label": "Breeze TTS 2",
        "repo": "BreezeBlue/Breeze-TTS-2",
        "kind": "tts",
        "clone": True,
        "needs_ram_gb": 8.0,
        "needs_gpu": True,
        "cpu_rtf": 25.0,
        "licence": "Research / non-commercial",
        "install": "pip",
        "package": "breeze-tts",
        "note": "#1 open-weight on the Artificial Analysis TTS leaderboard. "
                "Voice design from a text description, no reference needed. CUDA.",
    },
    "csm-1b": {
        "label": "Sesame CSM-1B (Maya base)",
        "repo": "sesame/csm-1b",
        "kind": "tts",
        "clone": True,
        "needs_ram_gb": 6.0,
        "needs_gpu": True,
        "cpu_rtf": 20.0,
        "licence": "Apache-2.0",
        "install": "pip",
        "package": "transformers",
        "note": "The model behind Maya. Conversational prosody with breath and "
                "hesitation. Wants a GPU.",
    },
}


def _load_registry():
    try:
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    except Exception:
        return {"installed": {}, "active": None, "custom": {}}


def _save_registry(reg):
    with open(REGISTRY_FILE, "w") as f:
        json.dump(reg, f, indent=2)


def verdict(spec, hw):
    """Will this actually run here? Returns (state, human explanation)."""
    if spec.get("needs_gpu") and not hw["cuda"]:
        return "blocked", (f"Needs CUDA. This box has "
                           f"{hw['gpu'] or 'no discrete GPU'} — would run at "
                           f"~{spec['cpu_rtf']:.0f}x realtime on CPU, which is "
                           f"unusable for interactive work.")
    if spec["needs_ram_gb"] > hw["ram_gb"] * 0.6:
        return "blocked", (f"Needs ~{spec['needs_ram_gb']} GB working set; "
                           f"this box has {hw['ram_gb']} GB total.")
    rtf = spec["cpu_rtf"]
    if not hw["cuda"] and rtf > 2.0:
        return "slow", (f"Will run, but at ~{rtf:.0f}x realtime on this CPU — "
                        f"about {rtf * 10:.0f}s of compute for 10s of speech. "
                        f"Fine for batch renders, not for live conversation.")
    if not hw["cuda"] and rtf > 1.0:
        return "slow", f"Roughly realtime on CPU (~{rtf:.1f}x)."
    return "ready", (f"Runs comfortably here (~{rtf:.2f}x realtime — "
                     f"{1/rtf:.0f}x faster than playback).")


def catalog_status():
    """The full model list with install state and an honest runs-here verdict."""
    hw = hardware()
    reg = _load_registry()
    out = []
    for key, spec in {**CATALOG, **reg.get("custom", {})}.items():
        state, why = verdict(spec, hw)
        inst = reg["installed"].get(key)
        out.append({
            "id": key,
            "label": spec["label"],
            "repo": spec["repo"],
            "clone": spec.get("clone", False),
            "licence": spec.get("licence"),
            "gated": spec.get("gated", False),
            "note": spec.get("note", ""),
            "needs_gpu": spec.get("needs_gpu", False),
            "needs_ram_gb": spec.get("needs_ram_gb"),
            "runs_here": state,
            "verdict": why,
            "installed": bool(inst),
            "installed_at": (inst or {}).get("at"),
            "active": reg.get("active") == key,
            "custom": key in reg.get("custom", {}),
        })
    order = {"ready": 0, "slow": 1, "blocked": 2}
    out.sort(key=lambda m: (order.get(m["runs_here"], 3), not m["installed"]))
    return {"hardware": hw, "models": out, "active": reg.get("active")}


# ── porting a model in from Hugging Face ────────────────────────────────────

def job_status(job_id):
    with _LOCK:
        return dict(_JOBS.get(job_id, {}))


def _set(job_id, **kw):
    with _LOCK:
        _JOBS.setdefault(job_id, {}).update(kw)


def _do_install(job_id, key, spec, token):
    try:
        pkg = spec.get("package")
        if pkg:
            _set(job_id, state="installing", detail=f"installing {pkg}")
            p = subprocess.run(
                [UV, "pip", "install", "--python", PY, "-q", pkg],
                capture_output=True, text=True, timeout=1800)
            if p.returncode != 0:
                raise RuntimeError((p.stderr or "install failed").strip()[-400:])

        _set(job_id, state="downloading", detail=f"pulling {spec['repo']} from HF")
        env = dict(os.environ)
        if token:
            env["HF_TOKEN"] = token
        code = (
            "from huggingface_hub import snapshot_download;"
            f"p=snapshot_download({spec['repo']!r},"
            f"local_dir={os.path.join(MODELS_DIR, key)!r});"
            "print(p)"
        )
        p = subprocess.run([PY, "-c", code],
                           capture_output=True, text=True, timeout=7200, env=env)
        if p.returncode != 0:
            err = (p.stderr or "").strip()
            if "gated" in err.lower() or "restricted" in err.lower() or "403" in err:
                raise RuntimeError(
                    f"{spec['repo']} is gated. Open "
                    f"https://huggingface.co/{spec['repo']} and request access, "
                    f"then retry.")
            raise RuntimeError(err[-400:] or "download failed")

        path = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else \
            os.path.join(MODELS_DIR, key)
        size = 0
        for root, _, files in os.walk(path):
            for fn in files:
                try:
                    size += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass

        reg = _load_registry()
        reg["installed"][key] = {"path": path, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "size_mb": round(size / 1048576, 1)}
        if not reg.get("active"):
            reg["active"] = key
        _save_registry(reg)
        _set(job_id, state="done", detail=f"installed ({round(size/1048576,1)} MB)",
             path=path)
    except subprocess.TimeoutExpired:
        _set(job_id, state="error", detail="timed out")
    except Exception as e:
        _set(job_id, state="error", detail=str(e)[:400])


def install(key, token=None, repo=None, label=None):
    """Install a catalogued model, or port in any HF repo by id."""
    reg = _load_registry()
    spec = CATALOG.get(key) or reg.get("custom", {}).get(key)
    if not spec and repo:
        # Porting a model the catalogue does not know about.
        key = repo.replace("/", "--")
        spec = {
            "label": label or repo.split("/")[-1],
            "repo": repo, "kind": "tts", "clone": True,
            "needs_ram_gb": 4.0, "needs_gpu": False, "cpu_rtf": 5.0,
            "licence": "see model card", "install": "pip", "package": None,
            "note": "Ported from Hugging Face. Capabilities unverified — "
                    "check the model card for how to call it.",
        }
        reg.setdefault("custom", {})[key] = spec
        _save_registry(reg)
    if not spec:
        return None, f"Unknown model '{key}'."

    job_id = f"inst_{int(time.time())}"
    _set(job_id, id=job_id, key=key, repo=spec["repo"], state="queued",
         detail="starting", started=time.time())
    threading.Thread(target=_do_install, args=(job_id, key, spec, token),
                     daemon=True).start()
    return job_id, None


def set_active(key):
    reg = _load_registry()
    if key not in reg["installed"]:
        return False, f"'{key}' is not installed yet."
    reg["active"] = key
    _save_registry(reg)
    return True, f"Active voice model set to {key}."


def active_model():
    reg = _load_registry()
    key = reg.get("active")
    if not key:
        return None
    spec = CATALOG.get(key) or reg.get("custom", {}).get(key, {})
    return {"id": key, **spec, **reg["installed"].get(key, {})}


# ── synthesis ───────────────────────────────────────────────────────────────



def synthesize(text, out_path, reference_wav=None, speed=1.0):
    """Generate speech with the active backend. Returns (ok, message)."""
    m = active_model()
    if not m:
        return False, ("No voice model is active yet. Install one from the "
                       "Model Foundry panel, then set it active.")

    key = m["id"]
    try:
        if key == "piper":
            return _synth_piper(m, text, out_path)
        if key == "xtts-v2":
            return _synth_xtts(m, text, out_path, reference_wav, speed)
        if key == "pocket-tts":
            return _synth_pocket(m, text, out_path, reference_wav)
        return False, (f"{m.get('label', key)} is installed but has no adapter "
                       f"wired in this build. Piper, XTTS-v2 and Pocket TTS "
                       f"generate today.")
    except Exception as e:
        return False, str(e)[:400]


def _to_vault_wav(src, out_path):
    """Normalise anything the backend produced to 24 kHz mono PCM."""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", out_path],
        capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "conversion failed").strip()[-300:])


def _synth_piper(m, text, out_path):
    onnx = None
    for root, _, files in os.walk(m["path"]):
        for fn in files:
            if fn.endswith(".onnx"):
                onnx = os.path.join(root, fn)
                break
        if onnx:
            break
    if not onnx:
        return False, "No .onnx voice found in the Piper download."
    raw = out_path + ".raw.wav"
    p = subprocess.run(["/home/hunt/.venv/bin/piper", "-m", onnx, "-f", raw],
                       input=text, capture_output=True, text=True, timeout=300)
    if p.returncode != 0 or not os.path.exists(raw):
        return False, (p.stderr or "piper failed").strip()[-300:]
    _to_vault_wav(raw, out_path)
    os.remove(raw)
    return True, "Generated with Piper."


def _synth_xtts(m, text, out_path, reference_wav, speed):
    if not reference_wav or not os.path.exists(reference_wav):
        return False, "XTTS-v2 needs a reference WAV to clone from."
    raw = out_path + ".raw.wav"
    code = f'''
import torch
from TTS.api import TTS
t = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)
t.tts_to_file(text={text!r}, speaker_wav={reference_wav!r},
              language="en", file_path={raw!r}, speed={float(speed)})
'''
    p = subprocess.run([PY, "-c", code],
                       capture_output=True, text=True, timeout=3600,
                       env={**os.environ, "COQUI_TOS_AGREED": "1"})
    if p.returncode != 0 or not os.path.exists(raw):
        return False, (p.stderr or "xtts failed").strip()[-400:]
    _to_vault_wav(raw, out_path)
    os.remove(raw)
    return True, "Generated with XTTS-v2 (cloned from reference)."


def _synth_pocket(m, text, out_path, reference_wav):
    raw = out_path + ".raw.wav"
    code = f'''
from moshi.models.tts import TTSModel
tts = TTSModel.from_pretrained({m["path"]!r})
tts.generate_to_file({text!r}, {raw!r}, voice={reference_wav!r})
'''
    p = subprocess.run([PY, "-c", code],
                       capture_output=True, text=True, timeout=1800)
    if p.returncode != 0 or not os.path.exists(raw):
        return False, (p.stderr or "pocket-tts failed").strip()[-400:]
    _to_vault_wav(raw, out_path)
    os.remove(raw)
    return True, "Generated with Kyutai Pocket TTS."
