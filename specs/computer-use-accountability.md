# CRANE Computer Use — Spec-Driven Orchestration & Accountability

**Version 1.0 · 2026-09-06 · tyronne-os/connie-crane**

Companion to the `crane-computer-use` skill. The skill defines *how* an agent
executes. This spec defines *what it is accountable to* and *how completion is
proven rather than asserted*.

---

## 0. The governing principle

> An agent's self-reported progress is worthless. Completion is a computed
> number derived from criteria that each pass an independently runnable
> verification command.

No agent in CRANE ever writes its own grade. The supervisor computes it from
observed results. A run that claims done without a computed number is rejected.

---

## 1. SPEC-DRIVEN — the contract comes first

Nothing executes without a spec. The spec is generated from conversation, then
frozen for the run.

### 1.1 Two artifacts, one truth

| File | Audience | Role |
|---|---|---|
| `INTENT.md` | Human, plain English | What we're building and why. Source of truth. |
| `SPEC.yaml` | Machine | Derived from INTENT.md. Executable acceptance criteria. |

`SPEC.yaml` is generated from `INTENT.md` and must be regenerated whenever
INTENT changes. If they disagree, INTENT wins and SPEC is rebuilt.

### 1.2 SPEC.yaml schema

```yaml
run_id: cu_2026_0906_1642
goal: "Add email/password auth to the booking app"
operator_mode: non_technical      # or: engineer
completion_gate: 0.90             # run cannot close below this

criteria:
  - id: AUTH-01
    statement: "A new user can register with email + password"
    weight: 3                     # 1=nice, 2=should, 3=must
    verify:
      kind: command               # command | http | visual | file
      run: "pytest tests/test_auth.py::test_register -q"
      expect_exit: 0
    reversible: true
    blast_radius: ["app.py", "tests/test_auth.py"]

  - id: AUTH-02
    statement: "Passwords are stored hashed, never plaintext"
    weight: 3
    verify:
      kind: command
      run: "grep -rn 'bcrypt\\|argon2' app.py"
      expect_exit: 0
    reversible: true
    blast_radius: ["app.py"]

  - id: AUTH-03
    statement: "Login page renders correctly on mobile"
    weight: 2
    verify:
      kind: visual
      url: "http://127.0.0.1:8000/login"
      viewport: [375, 812]
      assert: "email field, password field, and submit button all visible"
    reversible: true
    blast_radius: ["app.py"]
```

**Rule: every criterion must carry a `verify` block that a machine can run
without an agent's opinion.** A criterion with no runnable verification is not a
criterion — it's a wish, and it is rejected at spec-freeze time.

### 1.3 Verification kinds

| kind | Mechanism | Passes when |
|---|---|---|
| `command` | Shell exec | Exit code matches `expect_exit` |
| `http` | Request to endpoint | Status + body assertion match |
| `visual` | Headless screenshot + VLM assert | Assertion confirmed against image |
| `file` | Path/content check | Pattern present or absent as stated |

`visual` is the only kind involving model judgment, and it is judged by a
*different context* than the one that wrote the code. Never self-grade.

---

## 2. COMPLETION MATH

Completion is weighted, computed, and re-derived on every check. It is never
incremented by an agent.

```
completion = Σ(weight of PASSING criteria) / Σ(weight of ALL criteria)
```

State per criterion: `PENDING` → `PASS` | `FAIL` | `BLOCKED`

### 2.1 The 90% gate

A run **cannot be closed** below `completion_gate` (default 0.90).

Additional hard rule: **every `weight: 3` criterion must PASS regardless of
aggregate.** 91% aggregate with a failing must-have is a failed run. Musts are
not tradeable against nice-to-haves.

At gate evaluation the supervisor takes exactly one of three actions:

| Condition | Action |
|---|---|
| ≥ gate AND all weight-3 pass | `CLOSE` — run complete |
| < gate, budget remains | `CONTINUE` — target the highest-weight failing criterion |
| < gate, budget exhausted or all branches severed | `ESCALATE` — §5 brief |

### 2.2 Anti-gaming

The supervisor owns the number and the worker cannot touch it:

- Workers may not edit `SPEC.yaml` mid-run. Spec is frozen at run start.
- A worker editing a `verify` command is an immediate `abort` — that is
  moving the goalposts, and it is the single most likely failure mode.
- Criteria may only be added mid-run, never removed or weakened.
- Verification runs in a clean process, not in the worker's context.

---

## 3. ORCHESTRATION ACCOUNTABILITY

Every action is attributable to an actor, a branch, and an attempt.

### 3.1 Ledger — SQLite at `~/.crane/ledger.db`

```sql
CREATE TABLE runs (
  run_id        TEXT PRIMARY KEY,
  goal          TEXT NOT NULL,
  spec_sha      TEXT NOT NULL,      -- hash of frozen SPEC.yaml
  operator_mode TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  status        TEXT NOT NULL,      -- RUNNING|CLOSED|ESCALATED|ABORTED
  completion    REAL,               -- final computed value
  token_cost    INTEGER
);

CREATE TABLE events (
  event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,        -- supervisor | worker
  branch_id   TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  event_type  TEXT NOT NULL,
  criterion   TEXT,                 -- FK to SPEC criterion id, nullable
  payload     TEXT NOT NULL,        -- JSON
  reversible  INTEGER NOT NULL,     -- 0|1
  undo_ref    TEXT                  -- git SHA, backup path, or NULL
);

CREATE TABLE criteria_state (
  run_id      TEXT NOT NULL,
  criterion   TEXT NOT NULL,
  state       TEXT NOT NULL,        -- PENDING|PASS|FAIL|BLOCKED
  weight      INTEGER NOT NULL,
  last_check  TEXT,
  evidence    TEXT,                 -- raw verify output, never a summary
  PRIMARY KEY (run_id, criterion)
);

CREATE TABLE restore_points (
  name        TEXT PRIMARY KEY,     -- conversational: "before login"
  run_id      TEXT NOT NULL,
  commit_sha  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  plain_desc  TEXT NOT NULL         -- operator-facing, no jargon
);
```

### 3.2 Event types

| Type | Actor | Recorded |
|---|---|---|
| `spec_freeze` | supervisor | SPEC.yaml sha, criteria count, total weight |
| `plan` | worker | Approach vector, target criteria, blast radius |
| `verify_pre` | worker | Preconditions checked, predicted post-state |
| `action` | worker | Command/edit, args, undo_ref |
| `verify_post` | worker | Predicted vs observed |
| `criterion_check` | supervisor | Criterion id, verify output, PASS/FAIL |
| `completion_calc` | supervisor | Computed %, passing weight / total weight |
| `steer` | supervisor | Trigger, guidance issued |
| `abort` | supervisor | Reason, branch severed |
| `branch` | supervisor | From decision point, failure context carried |
| `research` | worker | Query, sources hit, solution + confidence |
| `escalate` | supervisor | Full brief per §5 |
| `skill_write` | supervisor | Procedure/convention/failure_mode distilled |

**`evidence` stores raw verify output, never a summary.** A summary is an
agent's opinion. The audit needs the actual bytes.

### 3.3 The audit question

The ledger must answer, for any timestamp, without an agent interpreting it:

> *What did CRANE do at 15:42, which criterion was it serving, who authorized
> the branch it was on, what did it observe afterward, and how do I undo it?*

One query against `events` joined to `criteria_state` and `restore_points`
returns all five. If it can't, the schema is wrong.

---

## 4. REVERSIBILITY

Every state-changing action records an `undo_ref` before it fires.

| Action class | undo_ref | Reversible |
|---|---|---|
| File edit | git SHA prior to edit | Yes |
| File create | path (delete to undo) | Yes |
| Package install | lockfile SHA prior | Yes |
| Git commit | prior HEAD | Yes |
| Git push | prior remote SHA | Yes — revert commit, never force |
| Schema migration | migration down-script | Only with a down-script |
| Outbound message / spend | — | **No — §5 gate** |

An action with no `undo_ref` and `reversible: 0` does not execute
autonomously. It escalates.

**Restore points** are named conversationally and mapped to real SHAs, so
"go back to before you added login" resolves without the operator knowing git.

---

## 5. ESCALATION

The only interrupt. Fires when: below gate with budget exhausted, all branches
severed with no research result, or an irreversible action is required.

```
BLOCKED: <goal, one line>
Completion: <X>% (<passing_weight>/<total_weight>)
Failing must-haves: <ids + statements>
Approaches tried: <N> — one line each
Last error: <exact bytes>
Researched: <queries run, what came back>
Options:
  A) <action> — <time>, <tradeoff>
  B) <action> — <time>, <tradeoff>
Recommend: <letter, one line why>
```

Non-technical operator mode strips ids, errors, and package names; options
carry time and money only.

---

## 6. OPERATOR SURFACE

Two views over the same ledger. Nothing is hidden — the difference is framing.

**Engineer view:** raw events, verify output, branch tree, token cost.

**Non-technical view:**
- A progress bar showing computed completion %
- Each criterion as a plain-English line with pass/fail
- Screenshots as evidence for `visual` criteria
- Named restore points, no SHAs
- Escalations translated per §5

The completion % shown is the same computed number in both views. There is no
"operator-friendly" number that flatters the run.

---

## 7. EXIT CRITERIA

A run closes only when all hold:

- [ ] `completion >= completion_gate`
- [ ] Every `weight: 3` criterion `PASS`
- [ ] Every PASS backed by raw `evidence` in the ledger
- [ ] No criterion weakened or removed after `spec_freeze`
- [ ] `INTENT.md` reconciled with what was actually built
- [ ] Restore point named for the run's final state
- [ ] Skill library updated with distilled procedures and failure modes
- [ ] Nothing irreversible executed without an explicit operator message
