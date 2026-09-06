# CRANE Studio — Agent Instructions

## DESIGN LAW: Load crane-easels before any UI work

This project has a mandatory design system. Before writing any HTML, CSS, or frontend code:

1. Run `/crane-easels` — governs all visual output (tokens, spacing, type scale, anti-slop gates, interactive states)
2. Run `/crane-dataviz` if the task involves charts or dashboards
3. Run `/crane-knowledge-graph` if the task involves graphs, networks, or entity maps

These are not suggestions. The skills contain the exit criteria (mandatory checklists) for every UI deliverable.

## Architecture

- All UI lives in `/home/hunt/app.py` — a single FastAPI file returning inline HTML
- Design tokens: `design/tokens.css` → edit here, run `bash design/apply.sh` to patch app.py
- Component reference: `design/components/*.css`
- Revert to any prior version: `cp design/versions/<snapshot>.css design/tokens.css && bash design/apply.sh`
- Agent handoff doc: `design/HANDOFF.md`

## Security (non-negotiable)

- Credential values NEVER go to the browser. `/api/keys/status` returns booleans only.
- Never write `vault.json` directly. Never create `.env` files.
- `~/NobilityDepository/` must NOT be published — contains live hardcoded keys.

## Server

```bash
cd /home/hunt
/home/hunt/.local/bin/uv run --python /home/hunt/.venv/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
```
