"""
Stages 4 and 5 — justify, then challenge.

Stage 4 asks the model one structured question per candidate, with the facts
from the graph attached: why does this exist, who depends on it, what breaks
without it. Stage 5 is a second, independent call whose only job is to argue
the code IS needed.

The model is never trusted on its word:

  * every piece of evidence must be a file:line that exists and mentions the
    unit — anything else is discarded as invented;
  * a removal needs stage 4 to say "remove" with confidence AND stage 5 to fail
    to refute it;
  * whenever the model says "keep", the code stays — it can veto a removal,
    never force one. Removal still has to pass the proof in stage 6.
"""

from __future__ import annotations

from .facts import RepoFacts
from .llm import Model, ModelError
from .model import AMBIGUOUS, KEEP, REMOVE, SIMPLIFY, Finding

JUSTIFY_SYSTEM = (
    "You are stage 4 of Justify, an auditor that decides whether a unit of Python code is worth "
    "keeping. Use only the CONTEXT you are given. Cite evidence as file:line strings taken from the "
    "context. If you are not sure, choose keep. Answer with one JSON object: "
    '{"verdict": "keep" or "remove", "reason": one sentence, "evidence": [file:line, ...], '
    '"confidence": number from 0 to 1}.'
)

CHALLENGE_SYSTEM = (
    "You are stage 5 of Justify. Another reviewer proposed REMOVING the unit below. Your only job is "
    "to find a reason it IS needed: a framework that calls it by decorator or by name, access through "
    "getattr or importlib, a side effect on import, public API used by other projects, a reference in "
    "configuration, or a test. Use only the CONTEXT. If you find any credible use, refute the removal. "
    'Answer with one JSON object: {"refuted": true or false, "reason": one sentence, '
    '"evidence": [file:line, ...]}.'
)

CONFIDENCE_TO_REMOVE = 0.7


def _context(rf: RepoFacts, f: Finding, max_lines: int = 40) -> str:
    ff = rf.files.get(f.file)
    lines = ff.src.source_lines if ff else []
    start = max(1, f.line - (2 if f.kind == "import" else 0))
    end = min(len(lines), f.end_line + (2 if f.kind == "import" else 0), start + max_lines - 1)
    code = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1)) if lines else "(not a Python file)"
    refs = [f"{a}:{b}" for a, b in rf.refs.get(f.name, [])
            if not (a == f.file and f.line <= b <= f.end_line)][:12]
    facts = [f"static analysis said: {f.verdict} — {f.reason}"]
    if ff:
        if ff.dynamic:
            facts.append("this file uses getattr/importlib/eval (dynamic access is possible)")
        if ff.is_init:
            facts.append("this is an __init__.py")
        if ff.is_test:
            facts.append("this is a test file")
        if ff.dunder_all:
            facts.append(f"__all__ = {sorted(ff.dunder_all)[:12]}")
    facts.append("repository looks like a library" if rf.is_library else "repository looks like an application")
    return (f"UNIT: {f.kind} '{f.name}' at {f.file}:{f.line}\n"
            f"FACTS:\n- " + "\n- ".join(facts) + "\n"
            f"OTHER REFERENCES TO '{f.name}': {', '.join(refs) if refs else 'none found'}\n"
            f"CODE:\n{code}")


def _check_evidence(rf: RepoFacts, f: Finding, evidence) -> tuple[list[str], list[str]]:
    ok, rejected = [], []
    texts = {rel: ff.src.source_lines for rel, ff in rf.files.items()}
    texts.update({rel: t.split("\n") for rel, t in rf.other})
    for item in (evidence or [])[:10]:
        item = str(item).strip()
        rel, _, num = item.rpartition(":")
        try:
            n = int(num)
        except ValueError:
            rejected.append(item)
            continue
        src = texts.get(rel)
        if src and 1 <= n <= len(src) and f.name in src[n - 1]:
            ok.append(item)
        else:
            rejected.append(item)
    return ok, rejected


def judge(rf: RepoFacts, findings: list[Finding], model: Model | None, limit: int = 40,
          progress=None) -> dict:
    """Sets f.final on every finding. Returns a summary of the model's work. `progress`, when given,
    is called with one line per unit — a model call takes seconds, and silence looks like a hang."""
    calls = judged = vetoed = 0
    errors: list[str] = []
    failed_in_a_row = 0
    to_judge = sum(1 for f in findings if f.verdict != SIMPLIFY)
    for f in findings:
        if f.verdict == SIMPLIFY:
            f.final = SIMPLIFY
            continue
        if failed_in_a_row >= 2 and model is not None:
            # the model is unreachable or not signed in: asking again for every unit only wastes time
            f.final = REMOVE if f.verdict == REMOVE else KEEP
            f.judgement = {"error": "not judged — the model failed twice in a row"}
            continue
        if model is None or judged >= limit:
            # no model, or over budget: what the graph proved stays proved; the rest stays put
            f.final = REMOVE if f.verdict == REMOVE else KEEP
            if model is not None and f.verdict == AMBIGUOUS:
                f.judgement = {"note": "not judged — model budget reached"}
            continue

        judged += 1
        if progress:
            progress(f"judging {judged}/{min(to_judge, limit)}: {f.file}:{f.line} {f.name}")
        ctx = _context(rf, f)
        try:
            j = model.ask(JUSTIFY_SYSTEM, ctx)
            calls += 1
            failed_in_a_row = 0
        except ModelError as exc:
            errors.append(str(exc))
            failed_in_a_row += 1
            f.final = REMOVE if f.verdict == REMOVE else KEEP
            f.judgement = {"error": str(exc)}
            continue
        ok, bad = _check_evidence(rf, f, j.get("evidence"))
        record = {"model": model.name, "justify": {"verdict": j.get("verdict"), "reason": j.get("reason"),
                                                   "confidence": j.get("confidence")},
                  "evidence_checked": ok, "evidence_rejected": bad}
        verdict = str(j.get("verdict", "keep")).lower()
        try:
            conf = float(j.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0

        if f.kind == "dependency":
            f.final = KEEP          # never provable here; the opinion goes in the report
        elif verdict != "remove" or conf < CONFIDENCE_TO_REMOVE:
            f.final = KEEP
            if f.verdict == REMOVE:
                vetoed += 1
        else:
            try:
                c = model.ask(CHALLENGE_SYSTEM, ctx)
                calls += 1
            except ModelError as exc:
                errors.append(str(exc))
                c = {"refuted": True, "reason": f"challenge could not run ({exc}) — keeping"}
            cok, cbad = _check_evidence(rf, f, c.get("evidence"))
            refuted = bool(c.get("refuted"))
            record["challenge"] = {"refuted": refuted, "reason": c.get("reason"),
                                   "evidence_checked": cok, "evidence_rejected": cbad}
            f.final = KEEP if refuted else REMOVE
            if refuted and f.verdict == REMOVE:
                vetoed += 1
        f.judgement = record
    return {"model": model.name if model else None, "judged": judged, "calls": calls,
            "vetoed_static_removals": vetoed, "errors": errors[:5]}
