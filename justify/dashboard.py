"""A self-contained HTML dashboard: one file, no server, no external assets."""

from __future__ import annotations

import html
import json

from . import NAME, TAGLINE
from .model import REMOVE, SIMPLIFY


def _e(x) -> str:
    return html.escape(str(x))


def render(res, history: list[dict] | None = None) -> str:
    m = res.metrics
    a = m.get("attribution") or {}
    rows = []
    for f in res.findings:
        if f.final not in (REMOVE, SIMPLIFY) and not (f.judgement or {}).get("justify"):
            continue
        why = (f.judgement or {}).get("justify", {}).get("reason") or f.reason
        rows.append(f"<tr><td><code>{_e(f.file)}:{f.line}</code></td><td>{_e(f.kind)}</td><td><code>{_e(f.name)}</code></td>"
                    f"<td class='v {_e(f.final.lower())}'>{_e(f.final)}</td><td>{_e(why)}</td>"
                    f"<td>{_e(f.proof)}</td><td>{_e(f.authored_by)}</td></tr>")
    hist = history or []
    spark = json.dumps([h.get("jlr") for h in hist if h.get("jlr") is not None])
    ai_rate, human_rate = a.get("ai_dead_per_1000"), a.get("human_dead_per_1000")
    top = max([x for x in (ai_rate, human_rate) if x] or [1])

    def bar(label, val, cls):
        w = 0 if not val else max(2, round(100 * val / top))
        return (f"<div class='bar'><span>{label}</span><div class='track'><div class='fill {cls}' style='width:{w}%'></div>"
                f"</div><b>{'—' if val is None else val}</b></div>")

    attr_html = ("<p class='muted'>Not a git repository — authorship could not be attributed.</p>" if not a else
                 bar("AI-assisted", ai_rate, "ai") + bar("Human", human_rate, "human") +
                 (f"<p class='big2'>{a['ai_to_human_ratio']}× the dead weight</p>" if a.get("ai_to_human_ratio") else "") +
                 f"<p class='muted'>{_e(a['note'])}</p>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(NAME)} dashboard</title>
<style>
:root {{ --navy:#12233F; --blue:#0078D4; --tint:#E8F2FC; --rule:#D8E1EC; --muted:#5A6B80; --paper:#F4F7FB;
        --red:#B42318; --green:#1A7F37; --amber:#9A5B00; }}
* {{ box-sizing:border-box }} body {{ margin:0; font-family:-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
  background:var(--paper); color:var(--navy) }}
header {{ background:var(--navy); color:#fff; padding:22px 28px }} header h1 {{ margin:0; font-size:22px }}
header p {{ margin:4px 0 0; color:#9DBCE0 }} main {{ padding:22px 28px; max-width:1200px; margin:auto }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:14px; margin-bottom:18px }}
.card {{ background:#fff; border:1px solid var(--rule); border-radius:10px; padding:16px 18px }}
.label {{ font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--blue); font-weight:700 }}
.big {{ font-size:40px; font-weight:800; margin:6px 0 2px }} .big2 {{ font-size:22px; font-weight:800; color:var(--red); margin:10px 0 4px }}
.muted {{ color:var(--muted); font-size:13px }} .bar {{ display:flex; align-items:center; gap:10px; margin:8px 0 }}
.bar span {{ width:90px; font-size:13px }} .track {{ flex:1; height:12px; background:var(--tint); border-radius:6px }}
.fill {{ height:12px; border-radius:6px }} .fill.ai {{ background:var(--red) }} .fill.human {{ background:var(--blue) }}
table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--rule); border-radius:10px; overflow:hidden; font-size:13px }}
th {{ background:var(--navy); color:#fff; text-align:left; padding:9px }} td {{ padding:8px 9px; border-top:1px solid var(--rule); vertical-align:top }}
.v {{ font-weight:700 }} .v.remove {{ color:var(--red) }} .v.simplify {{ color:var(--amber) }} .v.keep {{ color:var(--green) }}
canvas {{ width:100%; height:70px }} code {{ font-size:12px }}
</style></head><body>
<header><h1>{_e(NAME)} · {_e(res.root.split('/')[-1])}</h1><p>{_e(TAGLINE)} · scanned {_e(res.started)}</p></header>
<main>
<div class="grid">
  <div class="card"><div class="label">Justified Line Ratio</div><div class="big">{_e(m['jlr_percent'])}%</div>
    <div class="muted">{res.lines:,} lines in {res.files} files</div><canvas id="spark"></canvas></div>
  <div class="card"><div class="label">Dead weight</div><div class="big">{m['dead_weight_lines']}</div>
    <div class="muted">lines in {m['dead_weight_units']} units · {m['per_1000_lines']} per 1,000 lines ·
    {m['proved_removals']} proved by tests</div></div>
  <div class="card"><div class="label">Dead weight per 1,000 lines</div>{attr_html}</div>
</div>
<table><tr><th>Where</th><th>Kind</th><th>Name</th><th>Verdict</th><th>Why</th><th>Proof</th><th>Written by</th></tr>
{''.join(rows) or '<tr><td colspan=7>Nothing to remove — every line justified.</td></tr>'}</table>
<p class="muted">Nothing here was changed automatically. A person approves every removal.</p>
</main>
<script>
const d = {spark}; const c = document.getElementById('spark');
if (c && d.length > 1) {{ const x = c.getContext('2d'); const w = c.width = c.offsetWidth * 2, h = c.height = 140;
  const lo = Math.min(...d) - 1, hi = Math.max(...d) + 1; x.strokeStyle = '#0078D4'; x.lineWidth = 4; x.beginPath();
  d.forEach((v, i) => {{ const px = i * (w / (d.length - 1)), py = h - (v - lo) / (hi - lo) * h;
    i ? x.lineTo(px, py) : x.moveTo(px, py); }}); x.stroke(); }}
</script></body></html>"""
