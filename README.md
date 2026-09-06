# CONNIE CRANE

**Last updated: September 06, 2026 · 3:30 PM CST**

Local-first AI IDE and voice foundry. FastAPI backend (`app.py`) serving a full single-page IDE at `http://127.0.0.1:8000/ide`, wired directly to the **NOBILITY DEPOSITORY** credential vault. No cloud dependency for inference — all models run locally or on a GCP L4 you spin up and down.

---

## Session Progress — September 06, 2026

### IDE Redesign (completed this session)

**Color palette — "The Loft"**
Rethemed the entire IDE away from Claude's default black/teal palette to a custom palette pulled from the Ilya Sutskever stage photo:
- `--bg: #08050E` — near-black with deep indigo undertone
- `--panel: #100C1A` — dark violet-navy panels
- `--card: #1A1228` — indigo-charcoal cards
- `--border: #2A1845` — muted purple trim
- `--blue: #C0A87C` — antique manuscript amber (replaces sky blue)
- Chat composer interior: `#F2E8CC` — warm cream (light surface in dark IDE)
- Gold accents unchanged: `--gold: #C8A82A`

**IDE landing page — clean build surface**
- Brand header and headline hidden on load (zero clutter)
- Chat composer (`#hlComposer`) centered lower on the viewport (`padding-top: 14vh`)
- Cream interior (`#F2E8CC`), dark text — readable, loft-journal feel
- `▾ CHOOSE A TEMPLATE` section collapsed by default, arrow-toggle to expand
- Template cards: transparent background, icons float directly on dark ground, gold border on hover only

**Context bar (above composer)**
Permanent hardwired connection bar — always visible:
- `⎇ [GitHub repo dropdown]` — auto-loads all repos from `GITHUB_TOKEN` vault key on page open; click any repo to activate it as the project context (loads sidebar file tree); `＋ Start new project` at bottom
- `🤗 HF connected/offline` — reads `HF_TOKEN` boolean from vault, no credential exposed to client
- `☁ GCP project-id` — reads `/api/ide/gcp/status`, click to open GCP config modal
- `🔒` — lock icon always visible; click to open Nobility Vault panel

**GPU mini-meter (bottom-left navRail)**
- `⏻` power button to manually toggle paid GPU on/off
- Idle progress bar, cost display, status label
- Polls `/api/gpu/meter` every 8 seconds
- **Idle auto-off always enforced** — even on manual start, 10-min no-activity cutoff cannot be bypassed

**CAT-5 GPU protocol (images/video page)**
- Images (`/images` page): always route to ZeroGPU free (HF/Google) — GCP GPU locked out for images
- Videos: auto-start paid GPU meter on submit, ping activity during render so idle timer resets, stop after idle
- Manual GPU on/off: idle auto-off still applies

---

## Data Lake Architecture

| Data | Primary | Backup |
|------|---------|--------|
| Code / project files | GitHub (`tyronne-os/connie-crane`) | GitHub |
| Project log | `~/.crane_projects.json` (local) | `tyronne-os/crane-data-lake` (HF private dataset) |
| Chat history (IDE CONNIE) | `/mnt/NOBILITY_VAULT/voice_vault/crane_chat_history.json` | HF data-lake |
| Voice agent memory (BIG Q) | `/mnt/NOBILITY_VAULT/voice_vault/crane_voice_memory.json` | local only |
| API keys | Nobility Vault (`vault.json`) | **never** backed up |

Chat history auto-saves to Nobility Vault after every reply and restores on page load. HF backup fires async in the background. The `tyronne-os/crane-data-lake` private dataset repo is created automatically on first save.

**Session restore + 10-min auto-save (wired September 06, 2026 · 3:30 PM CST)**
- `restoreSession()` fires on `DOMContentLoaded` — resumes last active repo and file, restores `_fileShas` map
- CodeMirror `onChange` event calls `_onEditorChange()` — marks files dirty immediately on any keystroke
- `setInterval(autoSave, 600000)` runs every 10 minutes — pushes all dirty files to GitHub with CST-timestamped commit messages
- `window.beforeunload` calls `saveSessionState()` — persists active repo/file/scroll/shas to `~/.crane_session.json` on close
- Toast notifications confirm every save and restore

**Vault memory endpoints:**
- `GET/POST/DELETE /api/vault/voice-memory`
- `GET/POST/DELETE /api/vault/chat-history`

---

## Pending Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | IDE UI — loft theme, composer, context bar, GPU meter | ✅ Complete |
| 2 | Token extraction → `DESIGN.md` pipeline; video/image stitching | Planned |
| 3 | Tactile slider overlay for padding/spacing tuning | Planned |
| 4 | Persistent memory ledger | Planned |
| 5 | Swarm orchestration (parallel sub-agents) | Planned |
| 6 | Computer use trigger UI (AT-SPI2 layer) | Planned |

---

## Run

```bash
cd /home/hunt
/home/hunt/.local/bin/uv run --python /home/hunt/.venv/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Or via the helper script:
```bash
bash /home/hunt/crane.sh start
```

IDE opens at `http://127.0.0.1:8000/ide`

---

## CAT-5 Model Routing

| Tier | Model | VRAM | Trigger keywords |
|------|-------|------|-----------------|
| CAT-1 | Qwen2.5 Coder 1.5B (local CPU) | — | fix typo, rename, quick |
| CAT-2 | Qwen2.5 Coder 3B (local CPU) | — | add button, write test |
| CAT-3 | Qwen2.5 Coder 7B (local CPU) | — | build page, api route |
| CAT-4 | Qwen2.5 Coder 14B (GCP L4) | 24GB | full feature, auth, deploy |
| CAT-5 | Qwen2.5 Coder 32B (GCP L4) | 24GB | build the entire app, autonomous |

---

## The vault bridge

CONNIE holds no credentials of its own. `vault.py` reads the Nobility Depository vault from disk at request time — a key added in the Depository app is live immediately, no restart, no `.env`, no second copy.

**Credential values never reach the browser.** `/api/keys/status` returns booleans only. The vault panel in the IDE shows key presence, not values.

Read [VAULT-ACCESS.md](VAULT-ACCESS.md) before touching credentials. Never create `.env` files, never write `vault.json` directly — the Electron app owns that file and rewrites it whole on save.

## Security

The vault is plaintext on disk at mode `600`. Appropriate for local single-user work only — must never be copied into a repo, container image, or deployed environment. Two live keys are hardcoded in `~/NobilityDepository/src/renderer/renderer.js` — **that repo must NOT be published.**
