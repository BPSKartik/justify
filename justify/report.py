"""Stage 7 — the report: one reviewable change, with the reason next to every line."""

from __future__ import annotations

from . import NAME
from .model import KEEP, REMOVE, SIMPLIFY


AUDIT = {"full": "dead code, duplicates, authorship", "copies": "copied blocks, authorship",
         "prose": "counted", "data": "counted", "none": "counted"}
ROWS = 300                                    # GitHub drops a job summary over 1 MiB
WHO = {"ai": "AI-assisted", "human": "human"}
FENCE = "`" * 3


def _n(value) -> str:
    return "n/a" if value is None else str(value)


def _more(rows: list) -> list[str]:
    return [f"_…and {len(rows) - ROWS:,} more._"] if len(rows) > ROWS else []


def _languages(res) -> list[str]:
    langs = getattr(res, "languages", None) or []
    if not langs:
        return []
    out = ["### What the repository is made of", "", "| Language | Files | Lines | Audited for |", "|---|---:|---:|---|"]
    out += [f"| {l['name']} | {l['files']:,} | {l['lines']:,} | {AUDIT.get(l['audit'], 'counted')} |" for l in langs[:10]]
    return out + [""]


def _attr_line(m: dict) -> str:
    a = m.get("attribution")
    if not a:
        return "_Not a git repository — authorship could not be attributed._"
    code = a.get("all_code") or {}
    ai, human = (code.get("ai_lines", 0), code.get("human_lines", 0)) if code else (a["ai_lines"], a["human_lines"])
    parts = [f"Lines from AI-signed commits: **{ai:,}** · no AI trace: **{human:,}**"]
    if a.get("tools"):
        parts.append("Assistants that signed commits: " + ", ".join(f"{k} ({v})" for k, v in a["tools"].items()))
    h = a.get("history") or {}
    if h.get("shallow"):
        parts.append("**This is a shallow clone**, so most lines look like they came from one commit and authorship "
                     "is not reliable. Check out with `fetch-depth: 0`.")
    elif h.get("thin"):
        parts.append(f"History is thin — {h['commits']} commit(s); one commit wrote {h['largest_commit_percent']}% "
                     "of the lines that exist today — so it cannot show how this code was written.")
    if a["ai_dead_per_1000"] is not None or a["human_dead_per_1000"] is not None:
        parts.append(f"Dead weight per 1,000 lines — AI-assisted: **{_n(a['ai_dead_per_1000'])}**, "
                     f"human: **{_n(a['human_dead_per_1000'])}**")
    if a["ai_to_human_ratio"] is not None:
        parts.append(f"AI-assisted code carries **{a['ai_to_human_ratio']}×** the dead weight of human code here.")
    parts.append(f"_{a['note']}_")
    return "\n\n".join(parts)


def markdown(res) -> str:
    m = res.metrics
    p = res.proof or {}
    remove = [f for f in res.findings if f.final == REMOVE]
    simplify = [f for f in res.findings if f.final == SIMPLIFY]
    kept = [f for f in res.findings if f.final == KEEP]
    # found by the graph, kept only because the tests could not settle the removal either way
    unproved = [f for f in kept if f.verdict == REMOVE and str(f.proof).startswith("not provable")]
    blind = p.get("batch") == "baseline failed"
    whole = "_The whole repository at this commit, not only this pull request's changes._"
    if blind:
        out = [f"## {NAME}: nothing proved — the tests fail before any change", "", whole, "",
               f"> **`{p['command']}` failed on the unchanged code**, so none of the {p.get('candidates', 0)} "
               "removal candidates could be proved. **This is not a clean result.** Install the project and its test "
               "tools before Justify runs (the Action's `install-command`), or leave the test command out to scan "
               "without proof.", ""]
        if p.get("batch_output"):
            out += [FENCE, p["batch_output"].strip().replace(FENCE, "'" * 3), FENCE, ""]
    else:
        out = [f"## {NAME}: {len(remove)} unit(s) to remove, {len(simplify)} to simplify", "", whole, ""]
    if m["jlr_percent"] is None:
        out += ["**No Python to audit for dead code.** Justify finds dead code in Python, where it knows the "
                f"language's rules; every other language is checked for copied blocks ({m['duplicate_lines']} "
                "duplicate lines found).", ""]
    elif blind:
        out += [f"**Justified Line Ratio:** not measured — {len(unproved)} unit(s), "
                f"{sum(f.lines for f in unproved)} lines, were found but none could be proved · "
                f"{res.files} Python files, {res.lines:,} lines", ""]
    else:
        out += [f"**Justified Line Ratio:** {m['jlr_percent']}% · dead weight: {m['dead_weight_lines']} lines "
                f"in {m['dead_weight_units']} units ({m['per_1000_lines']} per 1,000 lines) · "
                f"{res.files} Python files, {res.lines:,} lines", ""]
    if res.unparsed:
        out += [f"_{len(res.unparsed)} Python file(s) could not be parsed and were not audited: "
                + ", ".join(f"`{u}`" for u in res.unparsed[:10]) + ("…" if len(res.unparsed) > 10 else "") + "_", ""]
    out += _languages(res)
    if p:
        out += [f"**Proof:** `{p['command']}` — {p.get('passed', 0)} of {p.get('candidates', 0)} removals "
                f"pass the tests ({p.get('batch')}).", ""]
    out += ["### Who wrote it", "", _attr_line(m), ""]
    if remove:
        out += ["### Remove", "", "| Where | What | Why | Proof | Written by |", "|---|---|---|---|---|"]
        for f in remove[:ROWS]:
            why = (f.judgement or {}).get("justify", {}).get("reason") or f.reason
            out.append(f"| `{f.file}:{f.line}` | {f.kind} `{f.name}` | {why} | {f.proof} | "
                       f"{WHO.get(f.authored_by, f.authored_by)} |")
        out += _more(remove) + [""]
    if unproved:
        out += ["### Found — not proved, because the tests could not run" if blind else "### Found, not proved", "",
                "| Where | What | Why it looks unused | Why it stays | Written by |", "|---|---|---|---|---|"]
        for f in unproved[:ROWS]:
            stays = str(f.proof).removeprefix("not provable").strip(" ()")
            out.append(f"| `{f.file}:{f.line}` | {f.kind} `{f.name}` | {f.reason} | {stays} | "
                       f"{WHO.get(f.authored_by, f.authored_by)} |")
        out += _more(unproved) + [""]
    if simplify:
        out += ["### Simplify", ""] + [f"- `{f.file}:{f.line}` `{f.name}`{'()' if f.file.endswith('.py') else ''} — "
                                       f"{f.reason}" for f in simplify[:ROWS]] + _more(simplify) + [""]
    judged_keep = [f for f in kept if f.judgement and f.judgement.get("justify")]
    if judged_keep:
        out += ["### Kept after review", ""]
        out += [f"- `{f.file}:{f.line}` {f.kind} `{f.name}` — "
                f"{(f.judgement.get('challenge') or {}).get('reason') or f.judgement['justify'].get('reason')}"
                for f in judged_keep[:20]] + [""]
    out += ["---", "_Nothing here was changed automatically. A person approves every removal._"]
    return "\n".join(out)
