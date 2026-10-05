"""Command line: `justify scan PATH`, `justify history PATH`, `justify mcp`."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import NAME, TAGLINE, __version__
from .model import REMOVE, SIMPLIFY


def _n(value) -> str:
    return "n/a" if value is None else str(value)


def _print_summary(res) -> None:
    m = res.metrics
    print(f"\n{NAME}  ·  {res.root}")
    if res.languages:
        print("  Made of   " + ", ".join(f"{l['name']} {l['lines']:,}" for l in res.languages[:6]) + " lines")
    print(f"  Stage 1  {res.files} Python files, {res.lines:,} lines, each hashed"
          + (f" ({len(res.unparsed)} could not be parsed)" if res.unparsed else ""))
    au = m.get("audited") or {}
    others = {k: v for k, v in (au.get("lines_by_language") or {}).items() if k != "Python"}
    if others:
        print("  Also     " + ", ".join(f"{k} {v:,}" for k, v in others.items()) + " lines audited for dead code")
    print("  Stage 2  syntax trees parsed; imports, definitions and references collected")
    counts = {}
    for f in res.findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    print("  Stage 3  " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) if counts else "  Stage 3  nothing to report")
    if res.judging:
        j = res.judging
        print(f"  Stage 4-5  {j['model']}: judged {j['judged']}, {j['calls']} calls, "
              f"vetoed {j['vetoed_static_removals']} static removal(s)" + (f", errors: {j['errors'][0]}" if j["errors"] else ""))
        for name, u in (j.get("usage") or {}).items():
            if u.get("input_tokens") or u.get("output_tokens"):
                cached = u.get("cache_read_tokens", 0) + u.get("cache_write_tokens", 0)
                print(f"  Tokens     {name}: {u['calls']} calls, {u.get('input_tokens', 0):,} in"
                      + (f" (+{cached:,} cached)" if cached else "") + f", {u.get('output_tokens', 0):,} out")
        board = j.get("scoreboard")
        if board:
            print("  Scoreboard  the tests graded each juror: " + "   ".join(
                f"{name} {row['right']}/{row['right'] + row['wrong']}" for name, row in board.items()))
    else:
        print("  Stage 4-5  no model (pass --judge to use one) — undecided units stay")
    if res.proof:
        p = res.proof
        others = {}
        for f in res.findings:
            if f.verdict == REMOVE and f.proof not in ("passed", "not run"):
                key = f.proof.split(":")[0] if f.proof.startswith(("failed", "compile")) else f.proof
                others[key] = others.get(key, 0) + 1
        extra = "".join(f" · {n} {why}" for why, n in others.items())
        print(f"  Stage 6  {p.get('passed', 0)} of {p.get('candidates', 0)} removals proved by the tests{extra}")
    print()
    for f in res.findings:
        if f.final in (REMOVE, SIMPLIFY) or f.judgement:
            loc = f"{f.file}:{f.line}"
            print(f"  {f.final:<8} {f.kind:<10} {loc:<34} {f.name:<22} {f.reason[:70]}")
            jury = (f.judgement or {}).get("jury")
            if jury:
                marks = "  ".join(f"{v['model']} {'—' if 'error' in v else ('remove' if v['verdict'] == 'remove' else 'keep')}"
                                  for v in jury)
                print(f"  {'':<8} jury       {marks}")
                print(f"  {'':<8}            {f.judgement.get('decision', '')[:100]}")
        elif f.verdict == REMOVE and res.proof:      # kept because the proof did not pass: say why
            loc = f"{f.file}:{f.line}"
            print(f"  {'KEEP':<8} {f.kind:<10} {loc:<34} {f.name:<22} proof: {f.proof[:64]}")
    print()
    if m["jlr_percent"] is None:
        print("  Justified Line Ratio  n/a — nothing here in a language Justify audits; copies checked")
    else:
        print(f"  Justified Line Ratio  {m['jlr_percent']}%   ·   dead weight {m['dead_weight_lines']} lines "
              f"({m['per_1000_lines']} per 1,000)   ·   duplicate lines {m['duplicate_lines']}")
        if others and m.get("python_jlr_percent") is not None:
            print(f"  Python alone          {m['python_jlr_percent']}%")
    a = m.get("attribution")
    if a:
        code = a.get("all_code") or {"ai_lines": a["ai_lines"], "human_lines": a["human_lines"]}
        print(f"  Written by            AI-signed commits {code['ai_lines']:,} lines ({a['ai_commits']} of {a['commits']} "
              f"commits), no AI trace {code['human_lines']:,} lines")
        if a.get("tools"):
            print("  Assistants            " + ", ".join(f"{k} {v}" for k, v in a["tools"].items()))
        h = a.get("history") or {}
        if h.get("thin"):
            print(f"  History               thin: {h['commits']} commit(s); one wrote {h['largest_commit_percent']}% of "
                  "today's lines — it cannot show how this code was written")
        if m["jlr_percent"] is not None:
            print(f"  Dead weight / 1,000   AI-assisted {_n(a['ai_dead_per_1000'])}   ·   human {_n(a['human_dead_per_1000'])}"
                  + (f"   ·   ratio {a['ai_to_human_ratio']}×" if a["ai_to_human_ratio"] is not None else ""))
        if a.get("ai_dup_per_1000") is not None or a.get("human_dup_per_1000") is not None:
            print(f"  Duplicates / 1,000    AI-assisted {_n(a.get('ai_dup_per_1000'))}   ·   human {_n(a.get('human_dup_per_1000'))}")
        rw = a.get("rework")
        if rw and rw["ai"]["rewritten_percent"] is not None:
            human = rw["human"]["rewritten_percent"]
            print(f"  Rework since {rw['since']}  AI-assisted {rw['ai']['rewritten_percent']}% of added lines later "
                  f"rewritten or deleted" + (f"   ·   human {human}%" if human is not None else ""))
    else:
        print("  Written by            not a git repository — authorship not attributed")
    if res.run_id:
        print(f"  Ledger                run #{res.run_id} recorded")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="justify", description=f"{NAME} — {TAGLINE}")
    ap.add_argument("--version", action="version", version=f"justify {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="scan a repository")
    s.add_argument("path", type=pathlib.Path)
    s.add_argument("--prove", metavar="TEST_COMMAND", help="prove removals by running this command in a copy")
    s.add_argument("--judge", action="store_true", help="run stages 4-5 with the model from the environment")
    s.add_argument("--judge-limit", type=int, default=40)
    s.add_argument("--json", action="store_true", help="print the full result as JSON")
    s.add_argument("--report", type=pathlib.Path, help="write a markdown pull-request report here")
    s.add_argument("--dashboard", type=pathlib.Path, help="write an HTML dashboard here")
    s.add_argument("--no-record", action="store_true", help="do not write this run to the ledger")
    s.add_argument("--progress", action="store_true",
                   help="write one JSON line per finished stage to stderr; stdout is unchanged")

    h = sub.add_parser("history", help="show the ledger for a repository")
    h.add_argument("path", type=pathlib.Path)

    m = sub.add_parser("mcp", help="run as an MCP server (stdio, or --http for URL-based clients)")
    m.add_argument("--http", action="store_true", help="serve streamable HTTP at http://HOST:PORT/mcp")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8765)

    v = sub.add_parser("serve", help="run the hosted service: web page, JSON API and public MCP endpoint")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)

    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    if args.cmd == "mcp":
        try:
            from .mcp_server import main as mcp_main
        except ImportError:
            print('The MCP server needs the mcp extra:  pip install "justify-code[mcp]"', file=sys.stderr)
            return 2
        mcp_main(http=args.http, host=args.host, port=args.port)
        return 0

    if args.cmd == "serve":
        try:
            from .hosted.app import main as serve_main
        except ImportError:
            print('The hosted service needs the hosted extra:  pip install "justify-code[hosted]"', file=sys.stderr)
            return 2
        serve_main(host=args.host, port=args.port)
        return 0

    if args.cmd == "history":
        from .ledger import Ledger
        for r in Ledger().history(str(args.path.expanduser().resolve())):
            print(f"  #{r['id']:<4} {r['started']}  JLR {r['jlr']}%  dead {r['dead_lines']} lines  "
                  f"AI {_n(r['ai_dead_per_1000'])} / human {_n(r['human_dead_per_1000'])} per 1,000")
        return 0

    if args.cmd != "scan":
        ap.print_help()
        return 1

    from .engine import run
    model = None
    if args.judge:
        from .llm import from_environment
        try:
            model = from_environment()
        except ValueError as exc:
            print(f"  --judge: {exc}")
            return 2
        if model is None:
            print("  --judge: no model configured (see justify/llm.py); continuing without stages 4-5")
    if model is not None and not args.json:
        print(f"  Stages 4-5 ask {model.name} about each undecided unit — a few seconds each", file=sys.stderr)
    def on_stage(name, message, **numbers):
        print(json.dumps({"stage": name, "message": message, **numbers}), file=sys.stderr, flush=True)

    try:
        res = run(args.path, model=model, prove_command=args.prove, record=not args.no_record,
                  judge_limit=args.judge_limit,
                  progress=None if args.json else (lambda msg: print(f"  {msg}", file=sys.stderr, flush=True)),
                  on_stage=on_stage if args.progress else None)
    except ValueError as exc:
        print(f"justify: {exc}", file=sys.stderr)
        return 2

    if args.report:
        from .report import markdown
        args.report.write_text(markdown(res), encoding="utf-8")
    if args.dashboard:
        from .dashboard import render
        from .ledger import Ledger
        args.dashboard.write_text(render(res, Ledger().history(res.root)), encoding="utf-8")
    if args.json:
        print(json.dumps(res.as_dict(), indent=2, default=str))
    else:
        _print_summary(res)
        if args.report:
            print(f"  Report                {args.report}")
        if args.dashboard:
            print(f"  Dashboard             {args.dashboard}")
    failed_proof = res.proof and res.proof.get("failed")
    return 0 if not failed_proof else 2


if __name__ == "__main__":
    sys.exit(main())
