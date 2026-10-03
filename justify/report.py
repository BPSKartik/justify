"""Stage 7 — the report: one reviewable change, with the reason next to every line."""

from __future__ import annotations

from . import NAME
from .model import KEEP, REMOVE, SIMPLIFY


def _attr_line(m: dict) -> str:
    a = m.get("attribution")
    if not a:
        return "_Not a git repository — authorship could not be attributed._"
    parts = [f"AI-assisted lines: **{a['ai_lines']:,}** · human lines: **{a['human_lines']:,}**"]
    if a["ai_dead_per_1000"] is not None or a["human_dead_per_1000"] is not None:
        parts.append(f"Dead weight per 1,000 lines — AI-assisted: **{a['ai_dead_per_1000']}**, "
                     f"human: **{a['human_dead_per_1000']}**")
    if a["ai_to_human_ratio"] is not None:
        parts.append(f"AI-assisted code carries **{a['ai_to_human_ratio']}×** the dead weight of human code here.")
    parts.append(f"_{a['note']}_")
    return "\n\n".join(parts)


def markdown(res) -> str:
    m = res.metrics
    remove = [f for f in res.findings if f.final == REMOVE]
    simplify = [f for f in res.findings if f.final == SIMPLIFY]
    kept = [f for f in res.findings if f.final == KEEP]
    out = [f"## {NAME}: {len(remove)} unit(s) to remove, {len(simplify)} to simplify",
           "",
           f"**Justified Line Ratio:** {m['jlr_percent']}% · dead weight: {m['dead_weight_lines']} lines "
           f"in {m['dead_weight_units']} units ({m['per_1000_lines']} per 1,000 lines) · "
           f"{res.files} files, {res.lines:,} lines",
           ""]
    if res.proof:
        p = res.proof
        out += [f"**Proof:** `{p['command']}` — {p.get('passed', 0)} of {p.get('candidates', 0)} removals "
                f"pass the tests ({p.get('batch')}).", ""]
    out += ["### Who wrote the dead weight", "", _attr_line(m), ""]
    if remove:
        out += ["### Remove", "", "| Where | What | Why | Proof | Written by |", "|---|---|---|---|---|"]
        for f in remove:
            why = (f.judgement or {}).get("justify", {}).get("reason") or f.reason
            out.append(f"| `{f.file}:{f.line}` | {f.kind} `{f.name}` | {why} | {f.proof} | {f.authored_by} |")
        out.append("")
    if simplify:
        out += ["### Simplify", ""] + [f"- `{f.file}:{f.line}` `{f.name}()` — {f.reason}" for f in simplify] + [""]
    judged_keep = [f for f in kept if f.judgement and f.judgement.get("justify")]
    if judged_keep:
        out += ["### Kept after review", ""]
        out += [f"- `{f.file}:{f.line}` {f.kind} `{f.name}` — "
                f"{(f.judgement.get('challenge') or {}).get('reason') or f.judgement['justify'].get('reason')}"
                for f in judged_keep[:20]] + [""]
    out += ["---", f"_Nothing here was changed automatically. A person approves every removal._"]
    return "\n".join(out)
