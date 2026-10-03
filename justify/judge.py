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
import re
from concurrent.futures import ThreadPoolExecutor

from .llm import Jury, Model, ModelError
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


def _vote(model: Model, rf: RepoFacts, f: Finding, ctx: str) -> dict:
    """One juror's stage-4 answer, with its evidence checked against the repository."""
    try:
        j = model.ask(JUSTIFY_SYSTEM, ctx)
    except ModelError as exc:
        return {"model": model.name, "error": str(exc)[:200]}
    verdict = str(j.get("verdict", "")).strip().lower()
    if verdict not in ("keep", "remove"):
        # an answer outside the format is no vote at all — not a quiet "keep"
        return {"model": model.name, "error": f"answered off-format: {str(j)[:120]}"}
    ok, bad = _check_evidence(rf, f, j.get("evidence"))
    try:
        conf = float(j.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return {"model": model.name, "verdict": verdict, "confidence": conf,
            "reason": j.get("reason"), "evidence_checked": ok, "evidence_rejected": bad,
            "seconds": getattr(model, "seconds", None)}


def _jury(rf: RepoFacts, f: Finding, jury: Jury, ctx: str) -> tuple[str, dict, int]:
    """The jury's rule, written for code that is about to be deleted:
      * every juror answers on its own, in parallel;
      * one juror that says keep AND cites evidence that checks out keeps the code;
      * a removal needs every juror but one to say remove with confidence (at least two);
      * then the strongest model, as challenger, tries to prove the code is needed;
      * anything else — a split, too few answers — keeps the code and is flagged for a person.
    Returns (final verdict, record, number of model calls)."""
    with ThreadPoolExecutor(max_workers=len(jury.members)) as pool:
        votes = list(pool.map(lambda m: _vote(m, rf, f, ctx), jury.members))
    calls = len(jury.members)
    answered = [v for v in votes if "error" not in v]
    removes = [v for v in answered if v["verdict"] == "remove" and v["confidence"] >= CONFIDENCE_TO_REMOVE]
    vetoes = [v for v in answered if v["verdict"] != "remove" and v["evidence_checked"]]
    need = max(2, len(answered) - 1)
    record = {"model": jury.name, "jury": votes, "need": need, "remove_votes": len(removes),
              "justify": {"verdict": "remove" if len(removes) >= need else "keep",
                          "reason": f"{len(removes)} of {len(answered)} jurors said remove"}}
    if f.kind == "dependency":
        record["decision"] = "dependencies are reported, never removed"
        return KEEP, record, calls
    if len(answered) < 2:
        record["decision"] = "the jury could not sit: fewer than two jurors answered"
        return KEEP, record, calls
    if vetoes:
        record["decision"] = f"kept: {vetoes[0]['model']} cited evidence of use ({vetoes[0]['evidence_checked'][0]})"
        return KEEP, record, calls
    if len(removes) < need:
        record["decision"] = f"kept: the jury split {len(removes)}–{len(answered) - len(removes)}; a person should look"
        record["split"] = bool(removes)
        return KEEP, record, calls
    try:
        c = jury.challenger.ask(CHALLENGE_SYSTEM, ctx)
        calls += 1
    except ModelError as exc:
        c = {"refuted": True, "reason": f"challenge could not run ({exc}) — keeping"}
    cok, cbad = _check_evidence(rf, f, c.get("evidence"))
    refuted = bool(c.get("refuted"))
    record["challenge"] = {"model": jury.challenger.name, "refuted": refuted, "reason": c.get("reason"),
                           "evidence_checked": cok, "evidence_rejected": cbad}
    record["decision"] = "kept: the challenger found a use" if refuted else \
        f"remove: {len(removes)} of {len(answered)} jurors agreed and the challenge failed"
    return (KEEP if refuted else REMOVE), record, calls


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
    """Evidence is a file:line that names the unit — as a whole word, outside the unit's own
    lines (the line `import io` sits on is not evidence that io is used), and, for an import,
    in the same file (an import is only ever used by the file that makes it). A model citing
    anything else has invented it."""
    ok, rejected = [], []
    texts = {rel: ff.src.source_lines for rel, ff in rf.files.items()}
    texts.update({rel: t.split("\n") for rel, t in rf.other})
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(f.name)}(?![A-Za-z0-9_])")
    for item in (evidence or [])[:10]:
        item = str(item).strip()
        rel, _, num = item.rpartition(":")
        try:
            n = int(num)
        except ValueError:
            rejected.append(item)
            continue
        src = texts.get(rel)
        own = rel == f.file and f.line <= n <= f.end_line
        elsewhere = f.kind == "import" and rel != f.file
        if src and 1 <= n <= len(src) and not own and not elsewhere and word.search(src[n - 1]):
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
        if isinstance(model, Jury):
            final, record, n = _jury(rf, f, model, ctx)
            calls += n
            failed_in_a_row = failed_in_a_row + 1 if all("error" in v for v in record["jury"]) else 0
            if failed_in_a_row:
                errors.append(record["jury"][0].get("error", "every juror failed"))
            f.final, f.judgement = final, record
            if final == KEEP and f.verdict == REMOVE:
                vetoed += 1
            continue
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
