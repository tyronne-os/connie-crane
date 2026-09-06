# CRANE STUDIO — Handoff Notes

Running log of what changed, why, and where things live. Read this first
before poking at the nav or the voice pages — it'll save you a re-discovery
pass like the one that produced this doc.

## Source conversation for the Voice Studio build

The extractor, Quick Mix, Voice Fusion & Track Mixer, Hollywood Vernacular
Presets, Quincy Jones Advanced sculpting panel, and BIG Q node canvas — all
of it (see the entry below) — was built in one Claude Desktop chat, working
directly against this repo and the `berylize-node` GPU box. That chat also
kicked off the harvest run for what the founder calls the **Vernacular
Vibration Library**: a working target of ~50 sourced voice clips (family
therapy / clinical, romance-audiobook, Black-intellectual-lecture, true-crime
narration, etc. — see the "Elite Female Voices," "Black Intellectuals," and
"True Crime" sidebar categories baked into `/studio`) to seed the Hollywood
Vernacular preset system (`NOLA NOIR`, `HARLEM ORATORY`, `ATL TRAP CADENCE`,
`DEEP SOUTH WARMTH`, `PULPIT`, `BROADCAST CLEAN`, `ACADEMIC LECTURE`,
`KITCHEN TABLE` — `app.py` around line 1188).

- **Chat title, original:** "Connecting Nobility Depository vault to Connie
  Crane"
- **Chat title, current (renamed by the founder, 2026-09-06):**
  **"CONNIE CRANE IDE"**
- This doc can't inline that chat's transcript — it lives in Claude Desktop
  history, not in this repo or in anything this session can fetch — so this
  entry is the pointer: **if you need the exact extraction/mixing decisions
  from that build session, open "CONNIE CRANE IDE" in Claude Desktop.** Everything
  it produced in code is committed here; the reasoning and back-and-forth
  behind it is only in that chat.
- The harvested audio itself is **not** in this git repo (by design — see
  `.gitignore`, which ignores everything except a short allow-list of source
  files). It lives on disk at `/mnt/NOBILITY_VAULT/voice_vault` on
  `berylize-node` (see `docs/GPU_RECOVERY.md` for that instance's specs and
  disk layout). Nothing in this repo currently counts or lists what's
  actually landed in that directory — if the "50 voices" number matters for
  planning, verify it directly on the box (`ls /mnt/NOBILITY_VAULT/voice_vault
  | wc -l`) rather than trusting this doc or the chat.

## 2026-09-06 — STUDIO restored to nav, `/bigq` dead-link fixed

**This is the problem that just got solved.** The nav rebuild that happened
after the "CONNIE CRANE IDE" build session orphaned everything that chat had
just built — see below for the fix.

### What was wrong

The Voice Studio page (voice cloning + extractor + Quick Mix + Voice Fusion
mixer + Quincy Jones sculpting + BIG Q node canvas) was **never removed** —
it was just orphaned. When the CRANE IDE nav got wired up (commit
`15b5277`), the center nav on every page was hard-coded to three tabs
(`HOME` → `/ide`, `CONNIE` → `/connie`, `DEPO` → `/depo`, later `IMAGES` →
`/images`), and nothing pointed at `/studio` anymore. `/` also got
redirected to `/ide` instead of the studio. The page itself was fully
intact in `app.py` the whole time — just unreachable from any button.

Separately, every "🎙 BIG Q" button in the IDE/CONNIE/DEPO pages pointed at
`/`, which now landed on the IDE, and one button on the DEPO page pointed
at `/bigq`, which had no route at all (404).

Also checked: the Hugging Face Space at
`huggingface.co/spaces/AIBRUH/connie-crane` was never actually deployed —
it's still the default static-template `index.html`/`style.css` scaffold.
It is **not** a backup of this app. If a HF Space mirror is wanted, that's
still a from-scratch job, not a restore.

### What changed (`app.py`)

1. **New route** `GET /bigq` → redirects to `/studio`. Fixes the dead link
   on the DEPO page and gives the app a stable, memorable alias for the
   voice workspace.
2. **STUDIO tab added to the center nav on all 5 pages** (`/studio`,
   `/ide`, `/connie`, `/depo`, `/images`), positioned right after `HOME` —
   it's the main entrance to the voice workflow, so it comes first in the
   flow: harvest/clone a voice → open Quincy Jones / BIG Q on the same
   page to master it.
3. **New CSS rule** `.nav-tab.studio.active` (green, `#10b981`) added
   alongside the existing `.depo`/`.img` active-color rules in each page's
   `<style>` block, so the STUDIO tab gets its own accent color instead of
   falling back to the default purple.
4. `/` still redirects to `/ide` — **not changed**. Only `/bigq` was
   re-pointed. If you want the site's root to open straight into the
   Voice Studio instead of the IDE, that's a one-line follow-up
   (`RedirectResponse(url="/studio")` in `serve_root`) — ask before doing
   it, since it changes the default landing experience for everyone.

### Verified

- `python3 -m py_compile` / `ast.parse` clean.
- Booted the app locally with `uvicorn app:app`; confirmed by curl:
  - `/studio` → 200, STUDIO tab renders with `active` class.
  - `/bigq` → 307 → `/studio`.
  - `/`, `/ide`, `/connie`, `/depo` → unaffected, still 200/307 as before.

### Workflow this restores

1. **STUDIO** (main entrance) — Extraction Matrix / extractor tool to pull
   or upload source audio, Quick Mix for light-touch EQ, Voice Fusion &
   Track Mixer with Hollywood Vernacular presets (NOLA NOIR, HARLEM
   ORATORY, etc.) to build/clone a voice.
2. Once a voice is dialed in, open **BIG Q** (same page, node-canvas panel)
   or the **Quincy Jones Advanced** panel (also same page,
   `#quincyPanel`) for surgical sculpting — pitch, formant, breathiness,
   EQ, room — before mastering out.

### Known pre-existing issues (not touched by this change)

- `app.py` has a duplicated `.nav-tab.img.active { ... }` CSS rule on the
  `/images` page (harmless — same rule twice, second one is a no-op) that
  predates this change. Left as-is to keep this diff minimal; worth a
  cleanup pass later.
- `requirements.txt` doesn't list `python-multipart`, which FastAPI needs
  for the multipart upload endpoints (`/api/harvest/upload`) — the app
  won't boot without it installed separately. Pre-existing, not introduced
  here.

### Where things are

| What | Route | app.py line (approx, will drift) |
|---|---|---|
| Voice Studio (main entrance) | `/studio` | ~923 |
| BIG Q alias | `/bigq` → `/studio` | ~919 |
| CRANE IDE | `/ide` | ~3363 |
| CONNIE chat | `/connie` | ~4417 |
| DEPO vault | `/depo` | ~4703 |
| Images | `/images` | ~5538 |
| Quincy Jones panel | inside `/studio`, `#quincyPanel` | ~1382 |
| BIG Q node canvas | inside `/studio` | ~1592 |

### If the studio "disappears" again

Check two things first, in this order:
1. Is `/studio` itself still returning 200? (`curl -I <host>/studio`)
2. Is the nav tab still present on the page you're navigating *from*?
   Orphaning happens by nav edit, not by deleting the page — the page is
   331KB of HTML/CSS/JS embedded in `app.py` and nobody has actually
   deleted a route yet in this project's history.
