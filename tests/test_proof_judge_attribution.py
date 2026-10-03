"""Stages 4-6 and attribution: proof isolates the one removal that was needed, the
model can veto but never force, invented evidence is rejected, and authorship is
read from the git history."""

import subprocess
import sys

from conftest import git
from justify.engine import run
from justify.llm import Model
from justify.proof import edit_source


# ---------------------------------------------------------------- stage 6: proof

def test_edit_keeps_multiline_import_valid():
    src = "from os import (\n    path,\n    sep,\n    getcwd,\n)\nprint(path, getcwd)\n"
    from justify.model import Finding
    out = edit_source(src, [Finding(kind="import", file="x.py", line=1, end_line=5, name="sep",
                                    verdict="REMOVE", reason="")])
    assert "sep" not in out and "path" in out and "getcwd" in out
    compile(out, "x", "exec")


def test_edit_leaves_pass_in_an_emptied_block():
    src = "if True:\n    import io\nx = 1\n"
    from justify.model import Finding
    out = edit_source(src, [Finding(kind="import", file="x.py", line=2, end_line=2, name="io",
                                    verdict="REMOVE", reason="")])
    assert "pass" in out
    compile(out, "x", "exec")


def test_proof_passes_and_never_touches_the_original(make_repo):
    root = make_repo({"app.py": "import io\nimport csv\nprint(csv)\n",
                      "check.py": "import app\n"})
    res = run(root, prove_command=f"{sys.executable} check.py", record=False)
    io = next(f for f in res.findings if f.name == "io")
    assert io.proof == "passed" and io.final == "REMOVE"
    assert "import io" in (root / "app.py").read_text()


def test_proof_isolates_the_removal_that_was_needed(make_repo):
    # helper() looks unused to the graph: its name is only ever built at runtime, which no
    # static tool can see. The tests can — so it must be kept while io still goes.
    root = make_repo({
        "app.py": "import io\ndef helper():\n    return 42\n",
        "check.py": "import importlib\nm = importlib.import_module('app')\nassert getattr(m, 'hel' + 'per')() == 42\n",
    })
    res = run(root, prove_command=f"{sys.executable} check.py", record=False)
    by = {f.name: f for f in res.findings}
    assert by["io"].proof == "passed" and by["io"].final == "REMOVE"
    assert by["helper"].proof.startswith("failed") and by["helper"].final == "KEEP"


# ---------------------------------------------------------------- stages 4-5: judgement

class Scripted(Model):
    name = "scripted"

    def __init__(self, justify, challenge):
        self.justify, self.challenge, self.calls = justify, challenge, []

    def ask(self, system, user):
        self.calls.append(system[:12])
        return self.challenge if "stage 5" in system else self.justify


def test_model_can_veto_a_static_removal(make_repo):
    root = make_repo({"app.py": "import io\n"})
    res = run(root, model=Scripted({"verdict": "keep", "reason": "x", "evidence": [], "confidence": 0.9}, {}),
              record=False)
    assert next(f for f in res.findings if f.name == "io").final == "KEEP"


def test_challenge_can_refute_a_removal(make_repo):
    root = make_repo({"app.py": "import io\n"})
    m = Scripted({"verdict": "remove", "reason": "unused", "evidence": [], "confidence": 0.95},
                 {"refuted": True, "reason": "used by a plugin", "evidence": []})
    res = run(root, model=m, record=False)
    assert next(f for f in res.findings if f.name == "io").final == "KEEP"
    assert len(m.calls) == 2


def test_removal_needs_confidence(make_repo):
    root = make_repo({"app.py": "import io\n"})
    m = Scripted({"verdict": "remove", "reason": "unused", "evidence": [], "confidence": 0.4},
                 {"refuted": False})
    res = run(root, model=m, record=False)
    assert next(f for f in res.findings if f.name == "io").final == "KEEP"


def test_invented_evidence_is_rejected(make_repo):
    root = make_repo({"app.py": "import io\nratio = 1\n",
                      "other.py": "import io\nio.StringIO\n"})
    m = Scripted({"verdict": "keep", "reason": "used",
                  "evidence": ["app.py:2", "ghost.py:9", "app.py:1", "other.py:2"], "confidence": 0.9}, {})
    res = run(root, model=m, record=False)
    j = next(f for f in res.findings if f.name == "io" and f.file == "app.py").judgement
    rejected = j["evidence_rejected"]
    assert "ghost.py:9" in rejected                      # invented file
    assert "app.py:1" in rejected                        # the import's own line proves nothing
    assert "app.py:2" in rejected                        # "ratio" contains "io" but is not io
    assert "other.py:2" in rejected                      # another file's io is another import


def test_no_model_means_ambiguous_stays(make_repo):
    root = make_repo({"app.py": "import readline\n"})
    res = run(root, record=False)
    assert next(f for f in res.findings if f.name == "readline").final == "KEEP"


# ---------------------------------------------------------------- attribution

def test_dead_weight_is_attributed_to_ai_or_human(make_repo, git_repo):
    root = git_repo(make_repo({"human.py": "import os\nprint(1)\n"}))
    git(root, "add", "human.py")
    git(root, "commit", "-qm", "human work")
    (root / "ai.py").write_text("import io\nimport sys\nimport json\nprint(2)\n")
    git(root, "add", "ai.py")
    git(root, "commit", "-qm", "assistant work\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    res = run(root, record=False)
    by = {f.name: f.authored_by for f in res.findings}
    assert by["os"] == "human"
    assert by["io"] == "ai" and by["sys"] == "ai" and by["json"] == "ai"
    a = res.metrics["attribution"]
    assert a["ai_commits"] == 1 and a["commits"] == 2
    assert a["ai_dead_per_1000"] > a["human_dead_per_1000"]
    assert a["ai_to_human_ratio"] is None              # a handful of lines is too few for a ratio
    assert a["ratio_needs_lines"] == 500


def test_folder_inside_another_repo_is_not_attributed(make_repo, git_repo, tmp_path):
    outer = git_repo(tmp_path)
    inner = make_repo({"app.py": "import io\n"}, name="inner")
    res = run(inner, record=False)
    assert res.metrics["attribution"] is None


# ---------------------------------------------------------------- ledger + report

def test_ledger_records_runs_and_spots_changed_files(make_repo):
    from justify.ledger import Ledger
    root = make_repo({"app.py": "import io\n"})
    first = run(root)
    (root / "app.py").write_text("import io\nimport sys\n")
    second = run(root)
    led = Ledger()
    assert [h["id"] for h in led.history(str(root.resolve()))] == [first.run_id, second.run_id]
    assert led.changed_since_last(str(root.resolve()), {"app.py": "different"}) == ["app.py"]


def test_report_and_dashboard_render(make_repo):
    from justify.dashboard import render
    from justify.report import markdown
    res = run(make_repo({"app.py": "import io\nx = 1\n"}), record=False)
    md = markdown(res)
    assert "Justified Line Ratio" in md and "`app.py:1`" in md
    assert "<html" in render(res, [])


def test_unchanged_files_reuse_last_judgement_without_model_calls(make_repo):
    root = make_repo({"app.py": "import io\n", "other.py": "import sys\n"})
    m = Scripted({"verdict": "keep", "reason": "x", "evidence": [], "confidence": 0.9}, {})
    run(root, model=m)
    first_calls = len(m.calls)
    (root / "other.py").write_text("import sys\nimport json\n")
    res = run(root, model=m)
    assert res.judging["reused_from_last_run"] == 1          # app.py did not change
    assert len(m.calls) - first_calls == 2                   # only other.py's two imports were asked


def test_rework_counts_lines_later_rewritten(make_repo, git_repo):
    root = git_repo(make_repo({"base.py": "x = 0\n"}))
    git(root, "add", "base.py")
    git(root, "commit", "-qm", "human start")
    (root / "ai.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    git(root, "add", "ai.py")
    git(root, "commit", "-qm", "assistant\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    (root / "ai.py").write_text("a = 1\nb = 20\n")          # a person rewrites half of it
    (root / "human.py").write_text("h = 1\nk = 2\n")
    git(root, "add", "ai.py", "human.py")
    git(root, "commit", "-qm", "fix the assistant's code")
    rw = run(root, record=False).metrics["attribution"]["rework"]
    assert rw["ai"] == {"added": 4, "surviving": 1, "rewritten_percent": 75.0}
    # the human start commit lands in the same second as the assistant's, so it is inside the window too
    assert rw["human"]["added"] in (3, 4) and rw["human"]["rewritten_percent"] == 0.0


def test_judging_reports_progress_and_stops_after_two_failures(make_repo):
    from justify.llm import ModelError

    class Down(Model):
        name = "down"
        calls = 0

        def ask(self, system, user):
            Down.calls += 1
            raise ModelError("not signed in")

    root = make_repo({"app.py": "import io\nimport csv\nimport json\nimport sys\nx = 1\n"})
    lines = []
    res = run(root, model=Down(), record=False, progress=lines.append)
    assert Down.calls == 2                                      # gave up after two failures in a row
    assert lines[0].startswith("judging 1/4: app.py:")
    assert all(f.final == "REMOVE" for f in res.findings)       # a failed model never vetoes


def test_cli_progress_version_and_friendly_errors(make_repo, capsys):
    import json as _json
    from justify.cli import main
    root = make_repo({"app.py": "import io\nx = 1\n"})
    assert main(["scan", str(root), "--json", "--no-record", "--progress"]) == 0
    out, err = capsys.readouterr()
    stages = [_json.loads(line)["stage"] for line in err.strip().splitlines()]
    assert stages[:3] == ["ingest", "facts", "candidates"] and stages[-1] == "metrics"
    assert _json.loads(out)["files"] == 1                         # stdout is only the JSON document
    assert main(["scan", str(root / "missing"), "--no-record"]) == 2
    assert "Not a folder" in capsys.readouterr().err
    try:
        main(["--version"])
    except SystemExit:
        pass
    assert "justify 1.0.0" in capsys.readouterr().out


# ---------------------------------------------------------------- the jury

def _jury(*answers, challenge=None):
    from justify.llm import Jury

    class Fixed(Model):
        def __init__(self, name, answer):
            self.name, self.answer, self.calls = name, answer, 0

        def ask(self, system, user):
            self.calls += 1
            if "stage 5" in system:
                return challenge or {"refuted": False}
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    members = [Fixed(f"m{i}", a) for i, a in enumerate(answers)]
    return Jury(members, members[0]), members


REMOVE_VOTE = {"verdict": "remove", "reason": "unused", "evidence": [], "confidence": 0.9}
KEEP_VOTE = {"verdict": "keep", "reason": "unsure", "evidence": [], "confidence": 0.6}


def test_jury_removes_only_with_near_agreement(make_repo):
    root = make_repo({"app.py": "import io\nx = 1\n"})
    jury, _ = _jury(REMOVE_VOTE, REMOVE_VOTE, REMOVE_VOTE, KEEP_VOTE)          # 3 of 4: enough
    f = next(f for f in run(root, model=jury, record=False).findings if f.name == "io")
    assert f.final == "REMOVE" and f.judgement["remove_votes"] == 3 and f.judgement["need"] == 3
    jury, _ = _jury(REMOVE_VOTE, REMOVE_VOTE, KEEP_VOTE, KEEP_VOTE)            # 2 of 4: a split
    f = next(f for f in run(root, model=jury, record=False).findings if f.name == "io")
    assert f.final == "KEEP" and f.judgement["split"]


def test_one_juror_with_real_evidence_keeps_the_code(make_repo):
    # io is reached through globals(): the graph cannot see it, one juror can, and cites the line
    root = make_repo({"app.py": "import io\nbuf = globals()['io']\n"})
    evidence_keep = {"verdict": "keep", "reason": "used", "evidence": ["app.py:2"], "confidence": 0.8}
    jury, _ = _jury(REMOVE_VOTE, REMOVE_VOTE, REMOVE_VOTE, evidence_keep)
    f = next(f for f in run(root, model=jury, record=False).findings if f.name == "io")
    assert f.final == "KEEP" and "cited evidence" in f.judgement["decision"]


def test_challenger_and_failed_jurors(make_repo):
    from justify.llm import ModelError
    root = make_repo({"app.py": "import io\nx = 1\n"})
    jury, _ = _jury(REMOVE_VOTE, REMOVE_VOTE, REMOVE_VOTE, challenge={"refuted": True, "reason": "plugin"})
    f = next(f for f in run(root, model=jury, record=False).findings if f.name == "io")
    assert f.final == "KEEP" and f.judgement["challenge"]["refuted"]
    jury, _ = _jury(REMOVE_VOTE, ModelError("down"), ModelError("down"))       # one answer: cannot sit
    f = next(f for f in run(root, model=jury, record=False).findings if f.name == "io")
    assert f.final == "KEEP" and "could not sit" in f.judgement["decision"]


def test_the_tests_grade_the_jury(make_repo):
    root = make_repo({"app.py": "import io\nx = 1\n", "check.py": "import app\n"})
    jury, _ = _jury(REMOVE_VOTE, REMOVE_VOTE, KEEP_VOTE)
    res = run(root, model=jury, record=False, prove_command=f"{sys.executable} check.py")
    board = res.judging["scoreboard"]
    assert board["m0"] == {"right": 1, "wrong": 0} and board["m2"] == {"right": 0, "wrong": 1}


def test_a_jury_keep_is_graded_but_still_stands(make_repo):
    root = make_repo({"app.py": "import io\nx = 1\n", "check.py": "import app\n"})
    jury, _ = _jury(REMOVE_VOTE, KEEP_VOTE, KEEP_VOTE)                         # the jury keeps io
    res = run(root, model=jury, record=False, prove_command=f"{sys.executable} check.py")
    io = next(f for f in res.findings if f.name == "io")
    assert io.final == "KEEP" and io.proof == "passed"                       # proved removable, still kept
    assert res.judging["scoreboard"]["m1"] == {"right": 0, "wrong": 1}        # and the keep voters lose a point
    assert res.proof["proved_for_grading_only"] == 1


def test_a_word_inside_a_string_is_not_evidence(make_repo):
    # the trap a text search falls for: "date" in a string is not a use of the date import
    root = make_repo({"app.py": "from datetime import date\ndef day(args):\n    return args.get('date')  # date\nday({})\n"})
    m = Scripted({"verdict": "keep", "reason": "used", "evidence": ["app.py:3"], "confidence": 0.9}, {})
    j = next(f for f in run(root, model=m, record=False).findings if f.name == "date").judgement
    assert "app.py:3" in j["evidence_rejected"] and not j["evidence_checked"]


def test_report_never_passes_off_tests_that_cannot_run_as_a_clean_result(make_repo):
    """The first real run of the GitHub Action without the project's test tools installed: the
    baseline fails, so nothing is proved — and the report must say that, not 'JLR 100%'."""
    from justify.report import markdown
    root = make_repo({"app.py": "import io\n\n\ndef unused():\n    return 1\n", "check.py": "import app\n"})
    res = run(root, prove_command=f"{sys.executable} -c 'import no_such_module_here'", record=False)
    md = markdown(res)
    assert res.proof["batch"] == "baseline failed"
    assert "nothing proved — the tests fail before any change" in md
    assert "This is not a clean result" in md and "no_such_module_here" in md     # the real error is shown
    assert "100.0%" not in md and "not measured" in md
    assert "`app.py:1`" in md and "`app.py:4`" in md                             # found, listed, not dropped
    assert "None" not in md.replace("No ", "")


def test_report_lists_removals_the_tests_cannot_reach(make_repo):
    from justify.report import markdown
    # the tests load app.py but never lonely.py: its unused import is found, but not provable
    root = make_repo({"app.py": "import io\nx = 1\n", "lonely.py": "import json\ny = 2\n",
                      "check.py": "import app\n"})
    res = run(root, prove_command=f"{sys.executable} check.py", record=False)
    md = markdown(res)
    assert "### Found, not proved" in md and "`lonely.py:1`" in md
    assert "### Remove" in md and "`app.py:1`" in md
    assert "the tests never load this file" in md
    assert "whole repository at this commit" in md


def test_report_flags_a_shallow_clone(make_repo, git_repo, tmp_path):
    from justify.report import markdown
    src = git_repo(make_repo({"app.py": "import io\n"}))
    git(src, "add", "app.py")
    git(src, "commit", "-qm", "one")
    (src / "app.py").write_text("import io\nx = 2\n")
    git(src, "commit", "-qam", "two")
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src}", str(clone)], check=True)
    md = markdown(run(clone, record=False))
    assert "shallow clone" in md and "fetch-depth: 0" in md
