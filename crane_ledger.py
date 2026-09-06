"""
CRANE Computer Use — orchestration ledger.

Implements specs/computer-use-accountability.md §2 (completion math) and
§3 (orchestration accountability).

Core invariant: completion is COMPUTED from criteria that each pass an
independently runnable verification. No agent writes its own grade.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

DB_PATH = Path.home() / ".crane" / "ledger.db"

State = Literal["PENDING", "PASS", "FAIL", "BLOCKED"]
Gate = Literal["CLOSE", "CONTINUE", "ESCALATE"]

MUST_WEIGHT = 3
DEFAULT_GATE = 0.90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  goal          TEXT NOT NULL,
  spec_sha      TEXT NOT NULL,
  spec_path     TEXT NOT NULL,
  operator_mode TEXT NOT NULL,
  gate          REAL NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  status        TEXT NOT NULL,
  completion    REAL,
  token_cost    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     TEXT NOT NULL REFERENCES runs(run_id),
  ts         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  branch_id  TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  criterion  TEXT,
  payload    TEXT NOT NULL,
  reversible INTEGER NOT NULL DEFAULT 0,
  undo_ref   TEXT
);

CREATE TABLE IF NOT EXISTS criteria_state (
  run_id     TEXT NOT NULL,
  criterion  TEXT NOT NULL,
  statement  TEXT NOT NULL,
  state      TEXT NOT NULL,
  weight     INTEGER NOT NULL,
  verify     TEXT NOT NULL,
  last_check TEXT,
  evidence   TEXT,
  PRIMARY KEY (run_id, criterion)
);

CREATE TABLE IF NOT EXISTS restore_points (
  name       TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  created_at TEXT NOT NULL,
  plain_desc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run_ts ON events(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_criterion ON events(run_id, criterion);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ── spec freeze ──────────────────────────────────────────────────────────────


class SpecViolation(Exception):
    """Spec was weakened, or a criterion lacks runnable verification."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_spec(spec_path: str | Path, run_id: str | None = None) -> str:
    """Load SPEC.yaml, validate every criterion is machine-verifiable, freeze it.

    Rejects any criterion without a runnable verify block — that is a wish,
    not a criterion (§1.2).
    """
    spec_path = Path(spec_path)
    spec = yaml.safe_load(spec_path.read_text())

    run_id = run_id or spec.get("run_id") or f"cu_{datetime.now():%Y%m%d_%H%M%S}"
    criteria = spec.get("criteria") or []
    if not criteria:
        raise SpecViolation("spec has no criteria")

    for c in criteria:
        if not c.get("verify"):
            raise SpecViolation(
                f"{c.get('id', '?')} has no verify block — rejected at freeze"
            )
        if c.get("weight") not in (1, 2, 3):
            raise SpecViolation(f"{c['id']} weight must be 1, 2, or 3")

    conn = connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, goal, spec_sha, spec_path, operator_mode, gate, started_at, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                spec["goal"],
                _sha(spec_path),
                str(spec_path),
                spec.get("operator_mode", "engineer"),
                float(spec.get("completion_gate", DEFAULT_GATE)),
                _now(),
                "RUNNING",
            ),
        )
        for c in criteria:
            conn.execute(
                "INSERT OR REPLACE INTO criteria_state "
                "(run_id, criterion, statement, state, weight, verify) "
                "VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    c["id"],
                    c["statement"],
                    "PENDING",
                    int(c["weight"]),
                    json.dumps(c["verify"]),
                ),
            )

    record(
        run_id,
        actor="supervisor",
        event_type="spec_freeze",
        payload={
            "spec_sha": _sha(spec_path),
            "criteria": len(criteria),
            "total_weight": sum(int(c["weight"]) for c in criteria),
        },
    )
    return run_id


def assert_spec_unchanged(run_id: str) -> None:
    """§2.2 anti-gaming. A worker that edits the spec mid-run moves the goalposts."""
    conn = connect()
    row = conn.execute(
        "SELECT spec_sha, spec_path FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row and _sha(Path(row["spec_path"])) != row["spec_sha"]:
        raise SpecViolation(
            f"SPEC.yaml changed after freeze for {run_id} — abort, goalposts moved"
        )


# ── events ───────────────────────────────────────────────────────────────────


def record(
    run_id: str,
    actor: Literal["supervisor", "worker"],
    event_type: str,
    payload: dict[str, Any],
    branch_id: str = "main",
    attempt: int = 1,
    criterion: str | None = None,
    reversible: bool = False,
    undo_ref: str | None = None,
) -> int:
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO events "
            "(run_id, ts, actor, branch_id, attempt, event_type, criterion, "
            " payload, reversible, undo_ref) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                _now(),
                actor,
                branch_id,
                attempt,
                event_type,
                criterion,
                json.dumps(payload),
                int(reversible),
                undo_ref,
            ),
        )
        return cur.lastrowid


# ── verification ─────────────────────────────────────────────────────────────


@dataclass
class VerifyResult:
    state: State
    evidence: str  # raw output — never a summary (§3.2)


def _verify_command(v: dict) -> VerifyResult:
    proc = subprocess.run(
        v["run"], shell=True, capture_output=True, text=True, timeout=v.get("timeout", 120)
    )
    evidence = f"$ {v['run']}\nexit={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    ok = proc.returncode == v.get("expect_exit", 0)
    return VerifyResult("PASS" if ok else "FAIL", evidence)


def _verify_http(v: dict) -> VerifyResult:
    try:
        with urllib.request.urlopen(v["url"], timeout=v.get("timeout", 30)) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        return VerifyResult("FAIL", f"GET {v['url']}\nerror: {e!r}")

    evidence = f"GET {v['url']}\nstatus={status}\n--- body (first 2k) ---\n{body[:2000]}"
    ok = status == v.get("expect_status", 200)
    if ok and (needle := v.get("expect_contains")):
        ok = needle in body
    return VerifyResult("PASS" if ok else "FAIL", evidence)


def _verify_file(v: dict) -> VerifyResult:
    p = Path(v["path"])
    if not p.exists():
        return VerifyResult("FAIL", f"{p} does not exist")
    text = p.read_text(errors="replace")
    pattern = v.get("pattern")
    if pattern is None:
        return VerifyResult("PASS", f"{p} exists ({len(text)} bytes)")
    found = re.search(pattern, text) is not None
    want = v.get("expect", "present") == "present"
    evidence = f"{p}: pattern {pattern!r} {'found' if found else 'not found'}"
    return VerifyResult("PASS" if found == want else "FAIL", evidence)


def verify(criterion_row: sqlite3.Row) -> VerifyResult:
    """Run one criterion's verification in a clean process (§2.2).

    'visual' returns BLOCKED — it requires a VLM assertion made by a context
    other than the one that wrote the code. Never self-grade.
    """
    v = json.loads(criterion_row["verify"])
    kind = v.get("kind", "command")
    try:
        if kind == "command":
            return _verify_command(v)
        if kind == "http":
            return _verify_http(v)
        if kind == "file":
            return _verify_file(v)
        if kind == "visual":
            return VerifyResult("BLOCKED", f"visual criterion awaiting external VLM assert: {v}")
        return VerifyResult("BLOCKED", f"unknown verify kind: {kind}")
    except subprocess.TimeoutExpired:
        return VerifyResult("FAIL", f"verification timed out: {v}")


def check(run_id: str, criterion: str | None = None) -> dict[str, State]:
    """Verify one or all criteria and persist state + raw evidence."""
    assert_spec_unchanged(run_id)
    conn = connect()
    q = "SELECT * FROM criteria_state WHERE run_id=?"
    args: tuple = (run_id,)
    if criterion:
        q += " AND criterion=?"
        args += (criterion,)

    results: dict[str, State] = {}
    for row in conn.execute(q, args).fetchall():
        res = verify(row)
        results[row["criterion"]] = res.state
        with conn:
            conn.execute(
                "UPDATE criteria_state SET state=?, last_check=?, evidence=? "
                "WHERE run_id=? AND criterion=?",
                (res.state, _now(), res.evidence, run_id, row["criterion"]),
            )
        record(
            run_id,
            actor="supervisor",
            event_type="criterion_check",
            criterion=row["criterion"],
            payload={"state": res.state, "evidence": res.evidence},
        )
    return results


# ── completion math (§2) ─────────────────────────────────────────────────────


@dataclass
class Completion:
    ratio: float
    passing_weight: int
    total_weight: int
    failing_musts: list[tuple[str, str]]
    gate: float

    @property
    def meets_gate(self) -> bool:
        return self.ratio >= self.gate and not self.failing_musts

    def __str__(self) -> str:
        pct = f"{self.ratio:.1%} ({self.passing_weight}/{self.total_weight})"
        if self.failing_musts:
            ids = ", ".join(i for i, _ in self.failing_musts)
            return f"{pct} — BLOCKED on must-haves: {ids}"
        return pct


def completion(run_id: str) -> Completion:
    """completion = Σ(weight of PASSING) / Σ(weight of ALL). Computed, never claimed."""
    conn = connect()
    rows = conn.execute(
        "SELECT criterion, statement, state, weight FROM criteria_state WHERE run_id=?",
        (run_id,),
    ).fetchall()
    if not rows:
        raise SpecViolation(f"no criteria for run {run_id}")

    total = sum(r["weight"] for r in rows)
    passing = sum(r["weight"] for r in rows if r["state"] == "PASS")
    musts = [
        (r["criterion"], r["statement"])
        for r in rows
        if r["weight"] == MUST_WEIGHT and r["state"] != "PASS"
    ]
    gate = conn.execute("SELECT gate FROM runs WHERE run_id=?", (run_id,)).fetchone()["gate"]

    c = Completion(passing / total, passing, total, musts, gate)
    record(
        run_id,
        actor="supervisor",
        event_type="completion_calc",
        payload={
            "ratio": c.ratio,
            "passing_weight": passing,
            "total_weight": total,
            "failing_musts": [i for i, _ in musts],
        },
    )
    return c


def evaluate_gate(run_id: str, budget_remaining: bool = True) -> tuple[Gate, Completion]:
    """§2.1. Three outcomes, no fourth. 91% with a failing must-have is a failed run."""
    c = completion(run_id)
    if c.meets_gate:
        gate: Gate = "CLOSE"
    elif budget_remaining:
        gate = "CONTINUE"
    else:
        gate = "ESCALATE"

    if gate == "CLOSE":
        conn = connect()
        with conn:
            conn.execute(
                "UPDATE runs SET status='CLOSED', ended_at=?, completion=? WHERE run_id=?",
                (_now(), c.ratio, run_id),
            )
    return gate, c


def next_target(run_id: str) -> sqlite3.Row | None:
    """Highest-weight failing criterion — what CONTINUE should attack next."""
    conn = connect()
    return conn.execute(
        "SELECT * FROM criteria_state WHERE run_id=? AND state!='PASS' "
        "ORDER BY weight DESC, criterion ASC LIMIT 1",
        (run_id,),
    ).fetchone()


# ── restore points (§4) ──────────────────────────────────────────────────────


def restore_point(name: str, run_id: str, commit_sha: str, plain_desc: str) -> None:
    """Conversational name → real SHA, so rollback needs no git knowledge."""
    conn = connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO restore_points VALUES (?,?,?,?,?)",
            (name.lower().strip(), run_id, commit_sha, _now(), plain_desc),
        )


def resolve_restore(name: str) -> sqlite3.Row | None:
    conn = connect()
    return conn.execute(
        "SELECT * FROM restore_points WHERE name LIKE ?", (f"%{name.lower().strip()}%",)
    ).fetchone()


# ── audit (§3.3) ─────────────────────────────────────────────────────────────


def audit(run_id: str, at: str | None = None) -> list[dict]:
    """The audit question, one query: what did CRANE do, serving which criterion,
    on whose authority, what did it observe, and how do I undo it."""
    conn = connect()
    q = """
    SELECT e.ts, e.actor, e.branch_id, e.attempt, e.event_type,
           e.criterion, cs.statement, cs.state, e.payload,
           e.reversible, e.undo_ref
    FROM events e
    LEFT JOIN criteria_state cs
      ON cs.run_id = e.run_id AND cs.criterion = e.criterion
    WHERE e.run_id = ?
    """
    args: tuple = (run_id,)
    if at:
        q += " AND e.ts <= ?"
        args += (at,)
    q += " ORDER BY e.ts DESC LIMIT 100"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


# ── operator surface (§6) ────────────────────────────────────────────────────


def report(run_id: str, plain: bool = False) -> str:
    """Same computed number in both views. No flattering variant (§6)."""
    conn = connect()
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM criteria_state WHERE run_id=? ORDER BY weight DESC, criterion",
        (run_id,),
    ).fetchall()
    c = completion(run_id)

    mark = {"PASS": "✓", "FAIL": "✗", "PENDING": "·", "BLOCKED": "⊘"}
    lines = [f"{run['goal']}", f"Progress: {c}", ""]
    for r in rows:
        if plain:
            lines.append(f"  {mark[r['state']]} {r['statement']}")
        else:
            lines.append(
                f"  {mark[r['state']]} [{r['criterion']}] w{r['weight']}  {r['statement']}"
            )
    if not plain:
        lines += ["", f"Gate: {run['gate']:.0%} · Status: {run['status']}"]
    return "\n".join(lines)
