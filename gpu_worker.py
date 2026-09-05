"""
CRANE GPU Worker — runs on berylize-node (L4 24GB)
Serves POST /generate  →  {"image_b64": "..."}

Start:
    pip install fastapi uvicorn diffusers transformers accelerate torch Pillow
    python gpu_worker.py [--port 8765] [--model flux-schnell|flux-dev]

The model downloads to /mnt/h3storage/diffusion/ on first use.
Add the worker URL (http://<external-ip>:8765) as the GCP endpoint on the
CRANE IDE page so the Images page can route to it.
"""

import argparse
import base64
import io
import logging
import os
import time
from typing import Optional

import torch
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gpu-worker")

MODEL_DIR = os.environ.get("CRANE_DIFFUSION_DIR", "/mnt/h3storage/diffusion")
os.makedirs(MODEL_DIR, exist_ok=True)

# ── model registry ────────────────────────────────────────────────────────────
MODELS = {
    "flux-schnell": {
        "hf_id": "black-forest-labs/FLUX.1-schnell",
        "pipeline": "FluxPipeline",
        "steps_default": 4,
        "steps_max": 8,
        "guidance": 0.0,
    },
    "flux-dev": {
        "hf_id": "black-forest-labs/FLUX.1-dev",
        "pipeline": "FluxPipeline",
        "steps_default": 20,
        "steps_max": 50,
        "guidance": 3.5,
    },
}

# ── pipeline loader ───────────────────────────────────────────────────────────
_pipe = None
_loaded_model = None


def _load_pipe(model_key: str):
    global _pipe, _loaded_model
    if _loaded_model == model_key and _pipe is not None:
        return _pipe

    from diffusers import FluxPipeline

    cfg = MODELS[model_key]
    cache = os.path.join(MODEL_DIR, model_key.replace("/", "_"))
    log.info("Loading %s → %s", cfg["hf_id"], cache)

    _pipe = FluxPipeline.from_pretrained(
        cfg["hf_id"],
        torch_dtype=torch.bfloat16,
        cache_dir=cache,
    )

    if torch.cuda.is_available():
        _pipe = _pipe.to("cuda")
        log.info("Model on CUDA (VRAM %.1f GB)", torch.cuda.get_device_properties(0).total_memory / 1e9)
    else:
        _pipe.enable_sequential_cpu_offload()
        log.warning("CUDA not available — CPU offload engaged (slow)")

    _loaded_model = model_key
    return _pipe


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="CRANE GPU Worker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEFAULT_MODEL = "flux-schnell"


class GenRequest(BaseModel):
    prompt: str
    model: str = ""        # diff:flux-uncensored, diff:qwen-image-2512, or bare "flux-schnell"
    width: int = 832
    height: int = 1248
    steps: int = 0
    guidance: float = -1.0
    seed: Optional[int] = None


class GenResponse(BaseModel):
    image_b64: str
    model: str
    elapsed_s: float
    width: int
    height: int


@app.get("/health")
async def health():
    cuda = torch.cuda.is_available()
    vram = round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1) if cuda else 0
    return {"status": "ok", "cuda": cuda, "vram_gb": vram, "loaded_model": _loaded_model}


@app.post("/generate", response_model=GenResponse)
async def generate(req: GenRequest):
    # normalize model key — CRANE sends "diff:flux-uncensored" etc.
    raw = req.model or DEFAULT_MODEL
    if raw.startswith("diff:"):
        raw = raw[5:]
    # map vault names → worker keys
    _map = {"flux-uncensored": "flux-dev", "qwen-image-2512": "flux-schnell"}
    model_key = _map.get(raw, raw if raw in MODELS else DEFAULT_MODEL)

    cfg = MODELS[model_key]
    steps = req.steps if req.steps > 0 else cfg["steps_default"]
    steps = min(steps, cfg["steps_max"])
    guidance = req.guidance if req.guidance >= 0 else cfg["guidance"]

    t0 = time.time()
    pipe = _load_pipe(model_key)

    gen = torch.Generator("cuda" if torch.cuda.is_available() else "cpu")
    if req.seed is not None:
        gen.manual_seed(req.seed)

    kwargs = dict(
        prompt=req.prompt,
        width=req.width,
        height=req.height,
        num_inference_steps=steps,
        generator=gen,
    )
    if guidance > 0:
        kwargs["guidance_scale"] = guidance

    result = pipe(**kwargs)
    img: Image.Image = result.images[0]

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    elapsed = round(time.time() - t0, 2)
    log.info("Generated %dx%d in %.1fs (%s, %d steps)", req.width, req.height, elapsed, model_key, steps)
    return GenResponse(image_b64=b64, model=model_key, elapsed_s=elapsed, width=req.width, height=req.height)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="flux-schnell", choices=list(MODELS))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    DEFAULT_MODEL = args.model
    log.info("CRANE GPU Worker starting on %s:%d (model=%s)", args.host, args.port, args.model)
    log.info("CUDA: %s | Model dir: %s", torch.cuda.is_available(), MODEL_DIR)

    uvicorn.run(app, host=args.host, port=args.port)
