"""Every language gets a census and a copy check; assistants are recognised by their signatures,
and only by their signatures."""

import pathlib

from justify import polyglot
from justify.attribution import classify
from justify.engine import run


# ---------------------------------------------------------------- who signed the commit

def test_assistant_signatures_are_recognised():
    assert classify("fix\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>") == ("ai", "Claude")
    assert classify("feat\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)") == ("ai", "Claude")
    assert classify("x", "copilot-swe-agent[bot] <198982749+Copilot@users.noreply.github.com>")[1] == "GitHub Copilot"
    assert classify("add parser", "Ada Lovelace (aider) <ada@example.com>") == ("ai", "Aider")
    assert classify("x", "google-labs-jules[bot] <x@users.noreply.github.com>")[1] == "Gemini / Jules"
    assert classify("x\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>")[1] == "Cursor"
    assert classify("x\n\nAssisted-by: some-new-tool")[0] == "ai"


def test_mentions_and_people_are_not_signatures():
    assert classify("Fix the cursor jumping in the editor") == ("human", None)
    assert classify("Use Copilot-style naming in docs") == ("human", None)       # prose, not a trailer
    assert classify("x\n\nCo-authored-by: Claude Monet <claude@paint.fr>") == ("human", None)
    assert classify("x\n\nCo-authored-by: Devin Shah <devin@gmail.com>") == ("human", None)
    assert classify("x", "Jules Verne <jules@nautilus.fr>") == ("human", None)


# ---------------------------------------------------------------- census and copies

BODY = """function total(items) {
  let sum = 0;
  for (const item of items) {
    if (item.price > 0 && item.qty > 0) {
      sum += item.price * item.qty;
    }
  }
  const tax = sum * 0.18;
  return Math.round((sum + tax) * 100) / 100;
}
"""


def _write(root: pathlib.Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def test_a_copied_block_is_reported_once_at_the_later_place(tmp_path):
    _write(tmp_path, {
        "src/a.js": "import x from 'y';\n" + BODY,
        # the same code, re-indented and with a comment in it, is still the same code
        "src/b.js": "// helpers\n" + BODY.replace("  ", "    ").replace("let sum = 0;", "let sum = 0; /* start */"),
        "src/c.js": "export const one = 1;\n",
        "node_modules/lib/d.js": BODY,           # someone else's code is never audited
        "README.md": "# demo\n",
    })
    files = polyglot.census(tmp_path)
    found, note = polyglot.copies(files)
    assert [(f.file, f.evidence[0]) for f in found] == [("src/b.js", "src/a.js:2")]
    assert found[0].final == "SIMPLIFY" and found[0].lines >= 6
    langs = {l["name"]: l for l in polyglot.summary(files)}
    from justify import langs as packs
    assert langs["JavaScript"]["files"] == 3
    assert langs["JavaScript"]["audit"] == ("full" if packs.AVAILABLE else "copies")
    assert langs["Markdown"]["audit"] == "prose" and note["files"] == 3


def test_short_or_trivial_repeats_are_not_copies(tmp_path):
    trivial = "}\n".join(["case 1: break;\n"] * 10)
    _write(tmp_path, {"a.c": "int a = 1;\nint b = 2;\n" + trivial, "b.c": "int a = 1;\nint b = 2;\n" + trivial})
    found, _ = polyglot.copies(polyglot.census(tmp_path))
    assert found == []


def test_a_repository_without_python_still_gets_an_audit(tmp_path):
    _write(tmp_path, {"lib/a.ts": BODY, "lib/b.ts": BODY, "notes.md": "hi\n"})
    res = run(tmp_path, record=False)
    from justify import langs as packs
    # with the language packs, TypeScript is audited too: its shared function is used in the other file
    assert res.files == 0
    assert res.metrics["jlr_percent"] == (100.0 if packs.AVAILABLE else None)
    assert res.metrics["duplicate_lines"] > 0
    assert [l["name"] for l in res.languages] == ["TypeScript", "Markdown"]
    assert {r["path"] for r in res.files_detail} == {"lib/a.ts", "lib/b.ts"}
    assert next(r for r in res.files_detail if r["path"] == "lib/b.ts")["dup"] > 0


def test_a_history_of_one_commit_is_called_thin_not_human(tmp_path):
    import subprocess
    env = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a",
           "PATH": __import__("os").environ["PATH"]}
    _write(tmp_path, {"train.py": "import json\nprint(json.dumps({}))\n", "predict.py": "print(1)\n"})
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "everything"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, env=env)
    a = run(tmp_path, record=False).metrics["attribution"]
    assert a["history"]["thin"] and a["history"]["largest_commit_percent"] == 100.0
    assert a["tools"] == {} and a["all_code"] == {"ai_lines": 0, "human_lines": 3}


def test_history_that_is_not_utf8_does_not_stop_an_audit(tmp_path):
    """A commit written in Latin-1 (old projects have them) once crashed `git blame` decoding."""
    import subprocess
    env = {"GIT_AUTHOR_NAME": "J\xf6rg", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a",
           "PATH": __import__("os").environ["PATH"]}
    (tmp_path / "app.py").write_bytes(b"# caf\xe9 \xb0C\nimport json\nprint(1)\n")      # Latin-1 bytes in the file itself
    for args in (["init", "-q"], ["config", "i18n.commitEncoding", "latin1"], ["add", "-A"],
                 ["commit", "-qm", "temp \xb0C".encode("latin-1").decode("latin-1")]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, env=env)
    res = run(tmp_path, record=False)
    assert any(f.name == "json" for f in res.findings)


def test_every_package_is_installed():
    """A sub-package missing from pyproject's list works from a checkout and breaks every real install
    (the GitHub Action, the hosted image) — it happened once with justify.langs."""
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    listed = set(re.findall(r'"(justify(?:\.\w+)*)"', re.search(r"packages = \[([^\]]*)\]",
                                                               (root / "pyproject.toml").read_text()).group(1)))
    found = {".".join(p.parent.relative_to(root).parts) for p in (root / "justify").rglob("__init__.py")}
    assert found <= listed, f"not in pyproject packages: {sorted(found - listed)}"
