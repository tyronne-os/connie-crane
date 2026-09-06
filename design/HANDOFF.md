# CRANE IDE — Design Handoff
**Last updated: September 06, 2026**
**Project: tyronne-os/connie-crane**
**Design folder: `/home/hunt/design/`**

---

## For any design agent entering this codebase

### Step 1 — Load the CRANE skills BEFORE touching anything

Three design skills govern all visual output in this project. Load them first — they are your design brief, your anti-slop checklist, and your token system.

```
/crane-easels          ← MANDATORY for all UI work
/crane-dataviz         ← load when working on charts/dashboards
/crane-knowledge-graph ← load when working on graphs/networks
```

Skills live at `~/.claude/skills/`. They were built from real September 2026 GitHub trending data:
- **crane-easels** — synthesizes Hallmark (11,200★ Together AI anti-AI-slop gates), DaisyUI v5 (42,300★), Mantine (30,600★), Refactoring UI, Apple HIG
- **crane-dataviz** — synthesizes Apache Superset, lieflat-charts (agent house-style), ECharts, Tufte
- **crane-knowledge-graph** — synthesizes Graphify (AST-first, no vectors), VeritasGraph (GraphRAG+MCP), D3 force

Do not begin any design work until you've read at least `crane-easels` in full. The mandatory checklists at the bottom of each skill are your exit criteria.

---

### Step 2 — Understand the architecture

**All UI lives in one file:** `/home/hunt/app.py` (~6400+ lines)
No build step. No framework. Pure Python f-strings returning HTML responses. All CSS and JS is embedded inline in those strings.

This means:
- To change a color → edit `design/tokens.css` → run `bash design/apply.sh` → restart server
- To change a component → find its selector in `design/components/*.css` → locate the matching CSS in `app.py` and apply the changes
- **Never hardcode hex values in app.py components.** All colors must reference `var(--token)` from the `:root` block.

The server runs at `http://127.0.0.1:8000/ide`. Start it with:
```bash
cd /home/hunt
/home/hunt/.local/bin/uv run --python /home/hunt/.venv/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

---

### Step 3 — The token system is your single source of truth

`design/tokens.css` is authoritative for all design tokens. It maps directly to the `:root{}` block in `app.py` around line 3612.

**To change the color theme:**
1. Edit `design/tokens.css`
2. Run `bash design/apply.sh` — it auto-snapshots the current tokens before patching
3. Restart the server to see changes

**To revert to an earlier version:**
1. Open `design/versions/` — each snapshot is timestamped
2. Copy the `:root{...}` block you want to restore into `design/tokens.css`
3. Run `bash design/apply.sh`

---

## Design folder map

```
design/
├── HANDOFF.md              ← this file — read first
├── tokens.css              ← SINGLE SOURCE OF TRUTH for all CSS tokens
├── apply.sh                ← patches tokens.css → app.py (auto-snapshots first)
├── components/
│   ├── composer.css        ← chat composer, context bar, template section
│   ├── nav-rail.css        ← left navigation strip + GPU meter
│   ├── context-bar.css     ← GitHub repo picker, HF/GCP chips, vault lock
│   └── preview-pane.css    ← in-app dev server preview iframe
└── versions/
    └── v1-2026-09-06_loft-dark.css   ← Version 1 snapshot (current production)
```

Component CSS files are **reference docs, not live stylesheets.** They show the intended styles for each section of app.py. When you redesign a component:
1. Update the component's `.css` file in `design/components/` with your changes
2. Find the matching CSS in `app.py` and apply the same changes there
3. If you change tokens, update `tokens.css` and run `apply.sh`

---

## The CRANE Loft aesthetic (TJ's brief)

This IDE is named after CONNIE, TJ's AI partner. The visual identity is called **"The Loft"** — inspired by a photo of Ilya Sutskever on a dark stage with gold hex-pattern lighting.

**Feel:** A serious workspace. Dark, focused, slightly luxurious. Like working late in a well-lit studio where everything is exactly where it should be.

**What this is NOT:**
- Not a general-purpose design system
- Not Material Design or HIG out of the box
- Not purple-gradient SaaS
- Not minimal white (there is one white surface: the parchment composer interior)

**The visual rules that are non-negotiable:**
1. Background is always near-black with indigo undertone (`--bg: #08050E`)
2. Gold (`--gold: #C8A82A`) is the one accent — everywhere that needs emphasis
3. Parchment (`#F2E8CC`) lives only inside the chat composer input. Nowhere else.
4. Icons float directly on dark background — no colored icon containers
5. Template cards and navigation items have no filled backgrounds — only gold border on hover
6. The composer is lower on the page (`padding-top: 14vh`) — it's the center of gravity

---

## Current UI sections

| Section | Location in app.py | Component file |
|---|---|---|
| Left nav rail + GPU meter | Line ~3622 | `components/nav-rail.css` |
| Context bar (GitHub/HF/GCP/Vault) | Line ~3721 | `components/context-bar.css` |
| Chat composer (parchment box) | Line ~3744 | `components/composer.css` |
| Options row below composer | Line ~3748 | `components/composer.css` |
| Template chooser (collapsed) | Line ~3760 | `components/composer.css` |
| Preview pane (iframe + dev server) | Line ~3700 | `components/preview-pane.css` |
| IDE code editor (CodeMirror) | Line ~3601 | (editor theme, not in components) |

---

## Version history

| Version | Snapshot file | Date | Description |
|---|---|---|---|
| v1 · Loft Dark | `versions/v1-2026-09-06_loft-dark.css` | 2026-09-06 | Original — indigo-black ground (`#08050E`), old gold (`#C8A82A`), parchment composer (`#F2E8CC`). |
| v2 · Charcoal / Gold / Jade | `versions/v2-2026-09-06_charcoal-gold-jade.css` | 2026-09-06 | Current production. Designed in Claude Design (Figma-style mockup). Charcoal ground (`#0d0d10`), warm gold (`#f0b429`), jade secondary (`#2ee6b8`). Composer is now dark surface — no parchment. Full rgba replacement applied throughout app.py. |

### Note on Claude Design access
Claude Design produced the mockup and the `tokens-update.css` palette file but did **not** have direct write access to this GitHub repo. All token changes from the mockup were applied manually here via `design/apply.sh` + a surgical patch script. If Claude Design gains repo access in a future session, it should still read `design/HANDOFF.md` first, update `design/tokens.css`, and run `bash design/apply.sh` — never edit the `:root{}` block in app.py directly.

---

## Revert procedure (when something breaks)

```bash
# 1. See available snapshots
ls /home/hunt/design/versions/

# 2. Dry-run the revert (shows what will change, touches nothing)
bash design/apply.sh --dry-run

# 3. Apply a specific version
cp design/versions/v1-2026-09-06_loft-dark.css design/tokens.css
bash design/apply.sh

# 4. Restart CRANE
pkill -f "uvicorn app:app" || true
cd /home/hunt && /home/hunt/.local/bin/uv run --python /home/hunt/.venv/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000 &
```

---

## Security rules (from TJ — non-negotiable)

- **Never send credential values to the browser.** `/api/keys/status` returns booleans only.
- **Never write `vault.json` directly.** The Nobility Depository Electron app owns it.
- **Never create `.env` files.** All keys come from the vault via `vault.py`.
- **`~/NobilityDepository/` must NOT be published.** Two live hardcoded keys are in that repo.

---

## Contact

This project belongs to TJ (tyronne-os). The app is called CRANE Studio. The AI inside it is CONNIE. Don't rename either of them.
