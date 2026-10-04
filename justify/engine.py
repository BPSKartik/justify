"""
The pipeline, end to end: ingest → facts → candidates → attribution → justify
and challenge → proof → payoff → ledger.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from dataclasses import dataclass, field
from typing import Any

from . import polyglot
from .attribution import Attribution
from .candidates import find_candidates
from .facts import repo_facts
from .ingest import ingest
from .judge import judge
from .llm import Jury, Model, usage_of
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
    languages: list[dict] = field(default_factory=list)
    files_detail: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"root": self.root, "started": self.started, "files": self.files, "lines": self.lines,
                "unparsed": self.unparsed, "metrics": self.metrics, "judging": self.judging,
                "proof": self.proof, "run_id": self.run_id, "languages": self.languages,
                "files_detail": self.files_detail,
                "findings": [f.as_dict() for f in self.findings]}


MIN_SAMPLE_LINES = 500
BLAME_ALL_UP_TO = 1500      # blame every code file (not just Python) for the city's colours, up to this many
FILES_DETAIL_MAX = 3000


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


def _is_py(f: Finding) -> bool:
    return f.file.endswith((".py", ".pyi"))


def _payoff(findings: list[Finding], total_lines: int, att: Attribution, files: list[str],
            code_files: list[str] | None = None) -> dict[str, Any]:
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
        dup = [f for f in findings if f.final == SIMPLIFY and _is_py(f)]     # rates are per Python line
        ai_dup = sum(f.lines for f in dup if f.authored_by == "ai")
        human_dup = sum(f.lines for f in dup if f.authored_by == "human")
        ai_commits, all_commits = att.ai_commits()
        everywhere = att.repo_lines([f for f in (code_files or []) if f in att._blame or f in files])
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
            "all_code": {"ai_lines": everywhere.get("ai", 0), "human_lines": everywhere.get("human", 0)},
            "tools": att.tools(),
            "history": att.history_shape([f for f in (code_files or files) if f in att._blame]),
            "note": "AI-assisted means the commit carries an assistant's signature: a Co-Authored-By or "
                    "Assisted-by trailer, a 'Generated with' line, or an agent's own account (Copilot coding "
                    "agent, Devin, Jules, Cursor Agent, Aider). Code pasted from a chat window carries no "
                    "signature and counts as 'no AI trace', so the AI share is a lower bound.",
        }
    return m


def _files_detail(files_all: list, findings: list[Finding], att: Attribution) -> list[dict]:
    """One row per code file — what the 3D city on the hosted page is built from: its size, how
    much of it is dead weight or a copy, and who wrote its lines (when it was blamed)."""
    dead: dict[str, int] = {}
    dup: dict[str, int] = {}
    for f in findings:
        if f.final == REMOVE and f.kind in ("import", "function", "class"):
            dead[f.file] = dead.get(f.file, 0) + f.lines
        elif f.final == SIMPLIFY:
            dup[f.file] = dup.get(f.file, 0) + f.lines
    rows = []
    for c in files_all:
        if c.lang != "Python" and c.lang not in polyglot.COPY_LANGS:
            continue
        row = {"path": c.rel, "lang": c.lang, "lines": c.lines, "dead": dead.get(c.rel, 0), "dup": dup.get(c.rel, 0)}
        if att.enabled and c.rel in att._blame:
            kinds = att.repo_lines([c.rel])
            row["ai"], row["human"] = kinds.get("ai", 0), kinds.get("human", 0)
        rows.append(row)
    rows.sort(key=lambda r: -r["lines"])
    return rows[:FILES_DETAIL_MAX]


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
    files_all = polyglot.census(root)
    other, copy_note = polyglot.copies(files_all)
    if copy_note["files"]:
        stage("copies", f"{len(other)} copied blocks across {copy_note['files']} files in other languages",
              findings=len(findings) + len(other))
    for f in findings:                    # a test's own helpers cannot be proved by running it
        ff = rf.files.get(f.file)
        if ff is not None and ff.is_test and f.kind != "import" and f.proof == "not run":
            f.proof = "not provable (test code)"
    att = Attribution(root)
    code_rels = [c.rel for c in files_all if c.lang in polyglot.COPY_LANGS or c.lang == "Python"]
    blame = [s.rel for s in sources] + sorted({f.file for f in other})
    if len(code_rels) <= BLAME_ALL_UP_TO:
        blame += code_rels
    att.prefetch(list(dict.fromkeys(blame)))  # every Python file is blamed for the AI/human counts anyway
    stage("attribution", "every line traced: an AI-signed commit, or no AI trace" if att.enabled
          else "not a git repository: authorship skipped")
    for f in findings + other:
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
    judging["usage"] = usage_of(model)                 # tokens per model: what the jury cost, measured
    if model is not None:
        stage("judge", f"{judging.get('judged', 0)} units judged by {model.name}")

    proof = None
    if prove_command:                                         # stage 6
        # a jury is graded on everything the graph flagged: removals it kept are proved too, in the
        # copy, so a wrong "keep" shows up on the scoreboard — but the jury's KEEP still stands
        grade_only = []
        if isinstance(model, Jury):
            for f in findings:
                if f.verdict == REMOVE and f.final == KEEP and f.kind in ("import", "function", "class") \
                        and f.proof == "not run":
                    f.final = REMOVE
                    grade_only.append(f)
        proof = prove(root, findings, prove_command)
        for f in grade_only:
            f.final = KEEP
        if grade_only:
            proof["proved_for_grading_only"] = len(grade_only)
        for f in findings:                # asked for proof: only a removal that passed may go
            if f.final == REMOVE and f.kind in ("import", "function", "class") and f.proof != "passed":
                f.final = KEEP
        stage("proof", f"{proof.get('passed', 0)} of {proof.get('candidates', 0)} removals proved")
        if model is not None:
            judging["scoreboard"] = scoreboard(findings)

    findings = sorted(findings + other, key=lambda f: (f.file, f.line, f.name))
    py_files = [s.rel for s in sources]
    metrics = _payoff(findings, rf.total_lines, att, py_files, code_rels)
    metrics["other_languages"] = copy_note
    stage("metrics", f"Justified Line Ratio {metrics['jlr_percent']}%" if metrics["jlr_percent"] is not None
          else "no Python to audit for dead code; census and copies done")
    res = Result(root=str(root), started=started, files=len(sources), lines=rf.total_lines,
                 unparsed=[s.rel for s in sources if s.error], findings=findings, metrics=metrics,
                 judging=judging if model else None, proof=proof, hashes={s.rel: s.sha for s in sources},
                 languages=polyglot.summary(files_all), files_detail=_files_detail(files_all, findings, att))
    if record:                                                # stage 7: the ledger
        from .ledger import Ledger
        res.run_id = Ledger().record(res)
    return res
