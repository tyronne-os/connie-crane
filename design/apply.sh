#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# CRANE Design → app.py token patcher
# Usage: bash design/apply.sh [--dry-run]
#
# Reads design/tokens.css, extracts the :root{...} block, and replaces
# the matching :root{} block in app.py (line ~3612).
# Creates a timestamped backup in design/versions/ before every patch.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

APP="/home/hunt/app.py"
TOKENS="/home/hunt/design/tokens.css"
VERSIONS="/home/hunt/design/versions"
TS=$(date +"%Y-%m-%d_%H%M")

# ── Snapshot current app.py tokens before touching anything ──────────────────
snapshot_current() {
  local snap="$VERSIONS/v_${TS}.css"
  python3 - "$APP" "$snap" <<'PYEOF'
import sys, re
src = open(sys.argv[1]).read()
# Find the IDE :root block (the one at line ~3612, not the other pages)
# It's the first :root{ that appears after the string "CRANE LOFT"
# We identify it as the block between the first :root{ and its closing }
# after position of "CRANE LOFT" comment or after the first IDE CSS marker
blocks = list(re.finditer(r':root\{[^}]*\}', src))
if not blocks:
    print("ERROR: no :root{} found in app.py", file=sys.stderr)
    sys.exit(1)
# The IDE root is the one that contains --bg and --gold
ide_block = None
for m in blocks:
    if '--bg:' in m.group() and '--gold:' in m.group():
        ide_block = m.group()
        break
if not ide_block:
    print("ERROR: IDE :root{} not found", file=sys.stderr)
    sys.exit(1)
open(sys.argv[2], 'w').write(f"/* Snapshot from app.py on {sys.argv[2]} */\n\n{ide_block}\n")
print(f"Snapshot saved: {sys.argv[2]}")
PYEOF
}

# ── Extract the new :root block from tokens.css ──────────────────────────────
extract_new_root() {
  python3 - "$TOKENS" <<'PYEOF'
import sys, re
src = open(sys.argv[1]).read()
m = re.search(r':root\s*\{(.+?)\}', src, re.DOTALL)
if not m:
    print("ERROR: :root{} not found in tokens.css", file=sys.stderr)
    sys.exit(1)
# Flatten to single-line compact format that app.py uses
body = m.group(1)
# Extract all --var: value; pairs
pairs = re.findall(r'(--[\w-]+)\s*:\s*([^;]+?)\s*;', body)
compact = ':root{' + ';'.join(f'{k}:{v.strip()}' for k,v in pairs) + '}'
print(compact)
PYEOF
}

# ── Patch app.py ─────────────────────────────────────────────────────────────
patch_app() {
  local new_root="$1"
  python3 - "$APP" "$new_root" <<'PYEOF'
import sys, re
src = open(sys.argv[1]).read()
new_root = sys.argv[2]
# Find the IDE :root block (contains --bg and --gold)
def replace_ide_root(text, replacement):
    blocks = list(re.finditer(r':root\{[^}]*\}', text))
    for m in blocks:
        if '--bg:' in m.group() and '--gold:' in m.group():
            return text[:m.start()] + replacement + text[m.end():]
    return None
result = replace_ide_root(src, new_root)
if result is None:
    print("ERROR: could not find IDE :root{} to patch", file=sys.stderr)
    sys.exit(1)
open(sys.argv[1], 'w').write(result)
print("app.py patched successfully.")
PYEOF
}

echo "CRANE Design Token Patcher"
echo "  tokens: $TOKENS"
echo "  target: $APP"
echo ""

if $DRY_RUN; then
  echo "[DRY RUN] Would snapshot current tokens to: $VERSIONS/v_${TS}.css"
  new_root=$(extract_new_root)
  echo "[DRY RUN] New :root block:"
  echo "$new_root"
  exit 0
fi

echo "Step 1/3  Snapshotting current tokens…"
snapshot_current

echo "Step 2/3  Extracting new tokens from design/tokens.css…"
new_root=$(extract_new_root)

echo "Step 3/3  Patching app.py…"
patch_app "$new_root"

echo ""
echo "Done. Restart CRANE to see changes:"
echo "  pkill -f 'uvicorn app:app' && cd /home/hunt && /home/hunt/.local/bin/uv run --python /home/hunt/.venv/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000 &"
