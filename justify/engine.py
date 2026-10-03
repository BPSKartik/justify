"""
The pipeline, end to end: ingest → facts → candidates → attribution → justify
and challenge → proof → payoff → ledger.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from dataclasses import dataclass, field
from typing import Any

from .attribution import Attribution
from .candidates import find_candidates
from .facts import repo_facts
from .ingest import ingest
from .judge import judge
from .llm import Model
from .model import KEEP, REMOVE, SIMPLIFY, Finding
from .proof import prove


@dataclass
class Result:
    root: str
    started: str
    files: int
    lines: int
    unparsed: list[str]
    findings: list[Finding]
    metrics: dict[str, Any]
    judging: dict[str, Any] | None = None
    proof: dict[str, Any] | None = None
    hashes: dict[str, str] = field(default_factory=dict)
    run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"root": self.root, "started": self.started, "files": self.files, "lines": self.lines,
                "unparsed": self.unparsed, "metrics": self.metrics, "judging": self.judging,
                "proof": self.proof, "run_id": self.run_id,
                "findings": [f.as_dict() for f in self.findings]}


MIN_SAMPLE_LINES = 500


def scoreboard(findings: list[Finding]) -> dict:
    """The tests grade the jury. Where the proof settled a unit — removed and the tests passed,
    or removed and they failed — each juror's vote is marked right or wrong. Over many runs this
    says which model is actually good at judging code, measured rather than claimed."""
    board: dict[str, dict[str, int]] = {}
    for f in findings:
        votes = (f.judgement or {}).get("jury") or []
        if f.proof == "passed":
            truth = "remove"
        elif f.proof.startswith("failed"):
            truth = "keep"
        else:
            continue
        for v in votes:
            if "error" in v:
                continue
            said = "remove" if v["verdict"] == "remove" and v["confidence"] >= 0.7 else "keep"
            row = board.setdefault(v["model"], {"right": 0, "wrong": 0})
            row["right" if said == truth else "wrong"] += 1
    return board


def _payoff(findings: list[Finding], total_lines: int, att: Attribution, files: list[str]) -> dict[str, Any]:
    dead = [f for f in findings if f.final == REMOVE and f.kind in ("import", "function", "class")]
    proved = [f for f in dead if f.proof == "passed"]
    dead_lines = sum(f.lines for f in dead)
    pending = [f for f in findings if f.verdict == "AMBIGUOUS" and not f.judgement and f.final == KEEP]
    dup_lines = sum(f.lines for f in findings if f.final == SIMPLIFY)
    jlr = round(100.0 * (total_lines - dead_lines) / total_lines, 2) if total_lines else None

    m: dict[str, Any] = {
        "jlr_percent": jlr,
        "dead_weight_units": len(dead),
        "dead_weight_lines": dead_lines,
        "proved_removals": len(proved),
        "duplicate_lines": dup_lines,
        "pending_judgement": len(pending),
        "per_1000_lines": round(1000.0 * dead_lines / total_lines, 2) if total_lines else None,
        "attribution": None,
    }
    if att.enabled:
        by_kind = att.repo_lines(files)
        ai_lines, human_lines = by_kind.get("ai", 0), by_kind.get("human", 0)
        ai_dead = sum(f.lines for f in dead if f.authored_by == "ai")
        human_dead = sum(f.lines for f in dead if f.authored_by == "human")
        ai_rate = round(1000.0 * ai_dead / ai_lines, 2) if ai_lines else None
        human_rate = round(1000.0 * human_dead / human_lines, 2) if human_lines else None
        # a ratio from a handful of lines is noise: below this many lines on either side, none is given
        enough = ai_lines >= MIN_SAMPLE_LINES and human_lines >= MIN_SAMPLE_LINES
        ratio = round(ai_rate / human_rate, 2) if (enough and ai_rate is not None and human_rate) else None
        dup = [f for f in findings if f.final == SIMPLIFY]
        ai_dup = sum(f.lines for f in dup if f.authored_by == "ai")
        human_dup = sum(f.lines for f in dup if f.authored_by == "human")
        ai_commits, all_commits = att.ai_commits()
        m["attribution"] = {
            "ai_lines": ai_lines, "human_lines": human_lines,
            "uncommitted_lines": by_kind.get("uncommitted", 0),
            "ai_dead_lines": ai_dead, "human_dead_lines": human_dead,
            "ai_dead_per_1000": ai_rate, "human_dead_per_1000": human_rate,
            "ai_to_human_ratio": ratio,
            "ai_duplicate_lines": ai_dup, "human_duplicate_lines": human_dup,
            "ai_dup_per_1000": round(1000.0 * ai_dup / ai_lines, 2) if ai_lines else None,
            "human_dup_per_1000": round(1000.0 * human_dup / human_lines, 2) if human_lines else None,
            "ratio_needs_lines": MIN_SAMPLE_LINES,
            "ai_commits": ai_commits, "commits": all_commits,
            "rework": att.rework(files),
            "note": "AI-assisted means the commit carries an assistant trailer (e.g. Co-Authored-By: Claude). "
                    "Assistants used without a trailer count as human, so the AI share is a lower bound.",
        }
    return m


def run(root: str | pathlib.Path, *, model: Model | None = None, prove_command: str | None = None,
        record: bool = True, judge_limit: int = 40, progress=None, on_stage=None) -> Result:
    """`progress` gets one human line per model call; `on_stage(stage, message, **numbers)` is
    called as each stage finishes, so a caller can show live progress (the hosted GUI does)."""
    root = pathlib.Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a folder: {root}")
    started = _dt.datetime.now().isoformat(timespec="seconds")

    def stage(name: str, message: str, **numbers) -> None:
        if on_stage:
            on_stage(name, message, **numbers)

    sources = ingest(root)                                    # stage 1
    stage("ingest", f"{len(sources)} Python files read and fingerprinted", files=len(sources),
          lines=sum(s.lines for s in sources))
    rf = repo_facts(root, sources)                            # stage 2
    stage("facts", "syntax trees parsed; who uses what is mapped")
    findings = find_candidates(rf)                            # stage 3
    stage("candidates", f"{sum(1 for f in findings if f.verdict == REMOVE)} removal candidates, "
          f"{sum(1 for f in findings if f.verdict == SIMPLIFY)} duplicates", findings=len(findings))
    for f in findings:                    # a test's own helpers cannot be proved by running it
        ff = rf.files.get(f.file)
        if ff is not None and ff.is_test and f.kind != "import" and f.proof == "not run":
            f.proof = "not provable (test code)"
    att = Attribution(root)
    att.prefetch([s.rel for s in sources])     # every file is blamed for the AI/human line counts anyway
    stage("attribution", "every line traced to an AI-assisted or a human commit" if att.enabled
          else "not a git repository: authorship skipped")
    for f in findings:
        f.authored_by = att.span(f.file, f.line, f.end_line) if f.kind != "dependency" else "n/a"
    reused = 0
    if model is not None and record:
        # a unit whose file has not changed keeps last run's judgement: no model call
        from .ledger import Ledger
        old_hashes, old_judged = Ledger().previous_judgements(str(root))
        now_hashes = {s.rel: s.sha for s in sources}
        for f in findings:
            prev = old_judged.get(f.key())
            if prev and old_hashes.get(f.file) == now_hashes.get(f.file):
                f.final, f.judgement = prev[0], {**prev[1], "reused_from_last_run": True}
                reused += 1
    pending = [f for f in findings if not (f.judgement or {}).get("reused_from_last_run")]
    judging = judge(rf, pending, model, limit=judge_limit, progress=progress)   # stages 4-5
    judging["reused_from_last_run"] = reused
    if model is not None:
        stage("judge", f"{judging.get('judged', 0)} units judged by {model.name}")

    proof = None
    if prove_command:                                         # stage 6
        proof = prove(root, findings, prove_command)
        for f in findings:                # asked for proof: only a removal that passed may go
            if f.final == REMOVE and f.kind in ("import", "function", "class") and f.proof != "passed":
                f.final = KEEP
        stage("proof", f"{proof.get('passed', 0)} of {proof.get('candidates', 0)} removals proved")
        if model is not None:
            judging["scoreboard"] = scoreboard(findings)

    py_files = [s.rel for s in sources]
    metrics = _payoff(findings, rf.total_lines, att, py_files)
    stage("metrics", f"Justified Line Ratio {metrics['jlr_percent']}%")
    res = Result(root=str(root), started=started, files=len(sources), lines=rf.total_lines,
                 unparsed=[s.rel for s in sources if s.error], findings=findings, metrics=metrics,
                 judging=judging if model else None, proof=proof, hashes={s.rel: s.sha for s in sources})
    if record:                                                # stage 7: the ledger
        from .ledger import Ledger
        res.run_id = Ledger().record(res)
    return res
