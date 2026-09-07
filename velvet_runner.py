"""
VELVET RUNNER — autonomous QC engine for CRANE-CU.

Consumes the onAgentComplete signal, runs four assertion gates, and writes
handoff.doc without human interaction.

The load-bearing rule: an agent reporting "done" proves nothing. Only a passing
gate run flips a node to CERTIFIED. Evidence is always raw stdout/stderr bytes,
never a summary — a summary is an opinion, the audit needs the actual bytes.

Gates, in order. A gate that cannot run returns BLOCKED, never PASS.
    1. SPEC VALIDATION  — deliverable assertions from the frozen work order
    2. LINT COMPLIANCE  — compile/syntax check on touched files
    3. TEST GATEKEEPER  — the project's test command
    4. SECURITY AUDIT   — credential leakage + unsafe shell constructs
"""

import json
import os
import re
import subprocess
import time

PROJECT_ROOT = "/home/hunt"
CRANE_HOME   = os.path.expanduser("~/.crane")
HANDOFF_DOC  = os.path.join(CRANE_HOME, "handoff.doc")
QC_LOG       = os.path.join(CRANE_HOME, "velvet_qc.jsonl")

PASS    = "PASS"
FAIL    = "FAIL"
BLOCKED = "BLOCKED"
CLEAR   = "CLEAR"
NA      = "N/A"      # nothing to run and nothing declared — recorded, not blocking

_SECRET_PATTERNS = [
    (r'(?i)(api[_-]?key|secret|token|password)\s*=\s*["\'][A-Za-z0-9_\-]{16,}["\']', "hardcoded credential"),
    (r'gh[pous]_[A-Za-z0-9]{20,}', "GitHub token"),
    (r'sk-[A-Za-z0-9]{20,}',       "OpenAI-style key"),
    (r'hf_[A-Za-z0-9]{20,}',       "HuggingFace token"),
    (r'AKIA[0-9A-Z]{16}',          "AWS access key"),
]

_UNSAFE_PATTERNS = [
    (r'rm\s+-rf\s+/(?:\s|$)',                        "recursive root delete"),
    (r'git\s+push\s+.*--force.*\b(main|master)\b',   "force push to main"),
    (r'(?<!\w)eval\s*\(\s*(?:input|request)',        "eval on untrusted input"),
]


def _run(cmd: str, timeout: int = 60, cwd: str = PROJECT_ROOT) -> dict:
    """Execute a shell command, capture raw bytes. Never raises."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return {"ran": True, "exit_code": r.returncode,
                "evidence": (r.stdout + r.stderr).strip()}
    except subprocess.TimeoutExpired:
        return {"ran": True, "exit_code": 124,
                "evidence": f"TIMEOUT after {timeout}s: {cmd}"}
    except Exception as e:
        return {"ran": False, "exit_code": -1, "evidence": f"COULD NOT RUN: {e}"}


# ── Gate 1: SPEC VALIDATION ───────────────────────────────────────────────────
def gate_spec_validation(deliverables: list) -> dict:
    if not deliverables:
        return {"gate": "SPEC VALIDATION", "state": BLOCKED,
                "detail": "no deliverables in work order", "checks": [], "completion": 0.0}

    checks, total_w, passing_w = [], 0, 0
    for d in deliverables:
        w = d.get("weight", 2)
        total_w += w
        cmd = (d.get("verify_cmd") or "").strip()
        if not cmd:
            checks.append({"id": d.get("id"), "state": BLOCKED, "weight": w,
                           "evidence": "no verify command — that is a wish, not a criterion"})
            continue
        r = _run(cmd, timeout=30)
        ok = r["ran"] and r["exit_code"] == 0
        if ok:
            passing_w += w
        checks.append({"id": d.get("id"), "state": PASS if ok else FAIL, "weight": w,
                       "exit_code": r["exit_code"], "evidence": r["evidence"][:400]})

    failing_musts = [c["id"] for c in checks if c.get("weight") == 3 and c["state"] != PASS]
    ratio = round(passing_w / total_w, 4) if total_w else 0.0
    # A failing must-have fails the gate regardless of aggregate.
    state = PASS if (ratio >= 0.90 and not failing_musts) else FAIL
    return {"gate": "SPEC VALIDATION", "state": state,
            "detail": f"{passing_w}/{total_w} weight ({ratio*100:.1f}%)",
            "completion": ratio, "failing_musts": failing_musts, "checks": checks,
            "evidence": "\n".join(c.get("evidence", "") for c in checks if c["state"] == FAIL)[:600]}


# ── Gate 2: LINT COMPLIANCE ───────────────────────────────────────────────────
def gate_lint(files: list = None) -> dict:
    files = files or []
    py = [f for f in files if f.endswith(".py") and os.path.isfile(f)]
    js = [f for f in files if f.endswith((".js", ".jsx", ".ts", ".tsx")) and os.path.isfile(f)]

    if not py and not js:
        app = os.path.join(PROJECT_ROOT, "app.py")
        if os.path.isfile(app):
            py = [app]
        else:
            return {"gate": "LINT COMPLIANCE", "state": BLOCKED,
                    "detail": "no lintable files in scope", "checks": []}

    checks, failed = [], False
    for f in py:
        r = _run(f"python3 -m py_compile {f!r}", timeout=45)
        ok = r["exit_code"] == 0
        failed = failed or not ok
        checks.append({"file": os.path.basename(f), "state": PASS if ok else FAIL,
                       "evidence": r["evidence"][:300]})
    for f in js:
        r = _run(f"node --check {f!r}", timeout=20)
        ok = r["exit_code"] == 0
        failed = failed or not ok
        checks.append({"file": os.path.basename(f), "state": PASS if ok else FAIL,
                       "evidence": r["evidence"][:300]})

    clean = len([c for c in checks if c["state"] == PASS])
    return {"gate": "LINT COMPLIANCE", "state": FAIL if failed else PASS,
            "detail": f"{clean}/{len(checks)} file(s) clean", "checks": checks,
            "evidence": "\n".join(c["evidence"] for c in checks if c["state"] == FAIL)[:600]}


# ── Gate 3: TEST GATEKEEPER ───────────────────────────────────────────────────
def gate_tests(test_cmd: str = "") -> dict:
    """
    Run the declared test command.

    A command that was declared and fails blocks certification. But when nothing
    is declared and no suite exists on disk, the deliverable verify commands in
    SPEC VALIDATION *are* the assertions — this returns N/A rather than parking
    every agent at BLOCKED forever. handoff.doc records it as unasserted so the
    gap is visible, never silently treated as a pass.
    """
    declared = bool(test_cmd)
    if not test_cmd:
        for candidate, probe in (("python3 -m pytest -q", "tests"),
                                 ("npm test --silent", "package.json")):
            if os.path.exists(os.path.join(PROJECT_ROOT, probe)):
                test_cmd = candidate
                break

    if not test_cmd:
        return {"gate": "TEST GATEKEEPER", "state": NA,
                "detail": "no suite declared — asserted by deliverable verify commands",
                "evidence": ""}

    r = _run(test_cmd, timeout=180)
    ok = r["exit_code"] == 0
    return {"gate": "TEST GATEKEEPER", "state": PASS if ok else FAIL,
            "detail": f"`{test_cmd}` exited {r['exit_code']}"
                      + ("" if declared else " (auto-discovered)"),
            "evidence": r["evidence"][-1200:]}


# ── Gate 4: SECURITY AUDIT ────────────────────────────────────────────────────
def gate_security(files: list = None) -> dict:
    files = files or []
    scan = [f for f in files if os.path.isfile(f)]
    if not scan:
        app = os.path.join(PROJECT_ROOT, "app.py")
        if os.path.isfile(app):
            scan = [app]

    findings = []
    for f in scan:
        try:
            with open(f, "r", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    for pat, label in _SECRET_PATTERNS:
                        if re.search(pat, line):
                            findings.append({"file": os.path.basename(f), "line": lineno,
                                             "severity": "HIGH", "issue": label})
                    for pat, label in _UNSAFE_PATTERNS:
                        if re.search(pat, line):
                            findings.append({"file": os.path.basename(f), "line": lineno,
                                             "severity": "MEDIUM", "issue": label})
        except Exception:
            continue

    high = [x for x in findings if x["severity"] == "HIGH"]
    if high:
        state, detail = FAIL, f"{len(high)} credential exposure(s)"
    elif findings:
        state, detail = FAIL, f"{len(findings)} unsafe pattern(s)"
    else:
        state, detail = CLEAR, f"{len(scan)} file(s) clean"
    return {"gate": "SECURITY AUDIT", "state": state, "detail": detail,
            "findings": findings[:20],
            "evidence": "\n".join(f"{x['file']}:{x['line']} {x['issue']}" for x in findings[:10])}


# ── handoff.doc ───────────────────────────────────────────────────────────────
def update_handoff_doc(report: dict) -> str:
    """Append this QC run to handoff.doc. Append-only — never rewrites history."""
    os.makedirs(CRANE_HOME, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report["ts"]))

    lines = [
        "",
        "=" * 78,
        f"VELVET QC CERTIFICATION — {ts}",
        "=" * 78,
        f"  Agent        : {report.get('agent','?')}",
        f"  Work Order   : {report.get('work_order','(none)')}",
        f"  Project      : {report.get('project_id','(unscoped)')}",
        f"  Verdict      : {report['verdict']}",
        f"  Node State   : {report.get('node_state','?')}",
        f"  Completion   : {report.get('completion',0.0)*100:.1f}%",
        f"  Attempt      : {report.get('attempts',1)}",
        f"  Build Time   : {report.get('build_seconds',0)}s",
        f"  QC Duration  : {report.get('duration_ms',0)} ms",
        "",
        "  Gates",
    ]
    for g in report.get("gates", []):
        lines.append(f"    [{g['state']:<7}] {g['gate']:<18} {g.get('detail','')}")

    if report.get("unasserted"):
        lines += ["", "  CAVEAT — gate(s) had nothing to run, behaviour unasserted:",
                  *[f"    - {g}" for g in report["unasserted"]],
                  "    Certification rests on the deliverable verify commands alone."]

    if report["verdict"] == PASS:
        lines += ["", f"  CERTIFIED — {report.get('agent','agent')} task marked DONE by VELVET.",
                  f"  Node transitioned to CERTIFIED (jade) at {ts}."]
    if report.get("escalation"):
        e = report["escalation"]
        lines += ["", f"  ESCALATED -> {e['to'].upper()}", f"    {e['reason']}",
                  f"    Failing gates: {', '.join(e['failing_gates'])}"]

    for g in report.get("gates", []):
        if g["state"] == FAIL and g.get("evidence"):
            lines += ["", f"  Evidence — {g['gate']}:"]
            for ln in str(g["evidence"]).splitlines()[:12]:
                lines.append(f"    | {ln[:108]}")
    lines.append("")

    fresh = not os.path.exists(HANDOFF_DOC)
    with open(HANDOFF_DOC, "a") as f:
        if fresh:
            f.write("CRANE — HANDOFF DOCUMENT\n"
                    "Autonomous QC certification log maintained by VELVET. Append-only.\n")
        f.write("\n".join(lines))
    return HANDOFF_DOC


def _log_qc(report: dict):
    os.makedirs(CRANE_HOME, exist_ok=True)
    with open(QC_LOG, "a") as f:
        f.write(json.dumps(report) + "\n")


# ── The onAgentComplete consumer ──────────────────────────────────────────────
def on_agent_complete(agent: str, project_id: str = "", work_order: str = "",
                      deliverables: list = None, files: list = None,
                      test_cmd: str = "", attempts: int = 1,
                      build_seconds: int = 0) -> dict:
    """
    Fired when a worker agent claims completion.

    Runs all four gates. Returns node_state:
        CERTIFIED  — all gates pass → node goes jade
        WORKING    — a gate failed, under 3 attempts → node stays gold
        ESCALATED  — gate failed on attempt 3+ → node goes red, Astra gets the bundle
    """
    started = time.time()
    deliverables = deliverables or []
    files = files or []

    gates = [
        gate_spec_validation(deliverables),
        gate_lint(files),
        gate_tests(test_cmd),
        gate_security(files),
    ]

    # FAIL blocks. BLOCKED blocks (a gate that should have run could not).
    # N/A does not block — it means there was genuinely nothing to run, and the
    # gap is written into handoff.doc rather than hidden behind a green node.
    hard_fail = any(g["state"] == FAIL for g in gates)
    blocked   = any(g["state"] == BLOCKED for g in gates)
    verdict   = FAIL if hard_fail else (BLOCKED if blocked else PASS)
    unasserted = [g["gate"] for g in gates if g["state"] == NA]

    spec = next((g for g in gates if g["gate"] == "SPEC VALIDATION"), {})
    completion = spec.get("completion", 0.0)

    if verdict == PASS:
        node_state = "CERTIFIED"
    elif attempts >= 3:
        node_state = "ESCALATED"
    else:
        node_state = "WORKING"

    report = {
        "ts": started,
        "agent": agent,
        "project_id": project_id,
        "work_order": work_order,
        "attempts": attempts,
        "build_seconds": build_seconds,
        "gates": gates,
        "verdict": verdict,
        "node_state": node_state,
        "completion": completion,
        "certified_at": started if verdict == PASS else None,
        "duration_ms": int((time.time() - started) * 1000),
        "unasserted": unasserted,
        "escalation": None,
    }

    if node_state == "ESCALATED":
        report["escalation"] = {
            "to": "astra",
            "reason": f"{agent} breached 3-attempt threshold on {work_order or 'work order'}",
            "failing_gates": [g["gate"] for g in gates if g["state"] == FAIL],
            "evidence": {g["gate"]: str(g.get("evidence", ""))[:400]
                         for g in gates if g["state"] == FAIL},
        }

    report["handoff_doc"] = update_handoff_doc(report)
    _log_qc(report)
    return report


def read_handoff_doc(tail_lines: int = 300) -> str:
    if not os.path.exists(HANDOFF_DOC):
        return "(handoff.doc not yet created — no QC certifications recorded)"
    with open(HANDOFF_DOC) as f:
        return "".join(f.readlines()[-tail_lines:])


def recent_qc_runs(limit: int = 20) -> list:
    if not os.path.exists(QC_LOG):
        return []
    out = []
    with open(QC_LOG) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out[-limit:]
