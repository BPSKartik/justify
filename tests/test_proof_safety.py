"""Stage 6 under attack: every way a proof could "pass" without meaning anything,
found by red-teaming the proof stage, and what it must say instead."""

import os
import sys

from justify.engine import run
from justify.model import Finding
from justify.proof import SharedLine, edit_source

PY_CMD = f"{sys.executable} -B"


def by_name(res):
    return {(f.file, f.name): f for f in res.findings}


def test_removals_that_pass_alone_but_fail_together_are_not_certified(make_repo):
    # two copies of the same registration: either one alone keeps the command, both together lose it
    root = make_repo({
        "registry.py": "COMMANDS = {}\n",
        "handlers.py": "from registry import COMMANDS\nCOMMANDS['greet'] = lambda: 'hi'\n",
        "web.py": "import handlers\nfrom registry import COMMANDS\nprint(COMMANDS)\n",
        "worker.py": "import handlers\nfrom registry import COMMANDS\nprint(COMMANDS)\n",
        "check.py": "import web, worker\nfrom registry import COMMANDS\nassert COMMANDS['greet']() == 'hi'\n",
    })
    from justify.engine import run as _run
    res = _run(root, record=False)
    # force both side-effect imports through the proof, as a reckless rule would
    for f in res.findings:
        if f.name == "handlers":
            f.verdict, f.final, f.proof = "REMOVE", "REMOVE", "not run"
    from justify.proof import prove
    summary = prove(root, res.findings, f"{PY_CMD} check.py")
    handlers = [f for f in res.findings if f.name == "handlers"]
    assert [f.proof for f in handlers].count("passed") == 1
    assert any(f.proof.startswith("failed together") for f in handlers)
    assert summary["passed"] == 1


def test_symlinked_file_is_never_written(make_repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "production.py").write_text("import time\nMODE = 'live'\n")
    root = make_repo({"app.py": "import settings\nprint(settings.MODE)\n"})
    (root / "settings.py").symlink_to(outside / "production.py")
    res = run(root, prove_command=f"{PY_CMD} app.py", record=False)
    t = by_name(res)[("settings.py", "time")]
    assert t.proof.startswith("not provable") and t.final == "KEEP"
    assert (outside / "production.py").read_text() == "import time\nMODE = 'live'\n"


def test_file_the_tests_never_load_is_not_provable(make_repo):
    root = make_repo({"app.py": "import io\nx = 1\n", "lonely.py": "import json\ny = 2\n",
                      "check.py": "import app\n"})
    res = run(root, prove_command=f"{PY_CMD} check.py", record=False)
    by = by_name(res)
    assert by[("app.py", "io")].proof == "passed"
    assert by[("lonely.py", "json")].proof == "not provable (the tests never load this file)"
    assert by[("lonely.py", "json")].final == "KEEP"


def test_baseline_must_pass(make_repo):
    root = make_repo({"app.py": "import io\nx = 1\n", "check.py": "import app\nraise SystemExit(1)\n"})
    res = run(root, prove_command=f"{PY_CMD} check.py", record=False)
    assert res.proof["batch"] == "baseline failed"
    assert by_name(res)[("app.py", "io")].final == "KEEP"


def test_fewer_tests_passing_is_a_failure(make_repo):
    # removing the import silently skips a test; exit code stays 0, the count drops
    root = make_repo({
        "app.py": "import io\nimport json\nx = 1\n",
        "tests/test_app.py": "import pytest\nimport app\n"
                             "def test_one():\n    assert app.x == 1\n"
                             "@pytest.mark.skipif(not hasattr(app, 'json'), reason='no json')\n"
                             "def test_two():\n    assert True\n",
    })
    res = run(root, prove_command=f"{sys.executable} -m pytest -q -p no:cacheprovider tests", record=False)
    by = by_name(res)
    assert by[("app.py", "io")].proof == "passed"
    assert by[("app.py", "json")].final == "KEEP"
    assert "skipped" in by[("app.py", "json")].proof or "fewer" in by[("app.py", "json")].proof


def test_dotenv_and_build_files_are_copied(make_repo):
    root = make_repo({"app.py": "import io\nx = 1\n",
                      "check.py": "import os, app\nassert open('.env').read().strip() == 'DB=1'\n"
                                  "assert os.path.isfile('build')\n"})
    (root / ".env").write_text("DB=1\n")
    (root / "build").write_text("not a folder\n")
    res = run(root, prove_command=f"{PY_CMD} check.py", record=False)
    assert by_name(res)[("app.py", "io")].proof == "passed"


def test_isolation_limit_leaves_untested_removals_kept(make_repo):
    files = {f"m{i:02d}.py": "import json\nx = 1\n" for i in range(4)}
    files["check.py"] = "import m00, m01, m02, m03\nimport m03 as t\nassert hasattr(t, 'json')\n"
    root = make_repo(files)
    from justify.engine import run as _run
    import justify.proof as proof
    orig = proof.prove
    res = _run(root, record=False)
    proof.prove(root, res.findings, f"{PY_CMD} check.py", isolate_limit=2)
    proofs = sorted(f.proof for f in res.findings if f.name == "json")
    assert proofs.count("not run (isolation limit)") == 2
    assert proof.prove is orig


def test_semicolon_line_is_not_edited():
    src = "import numpy as np; import sys\nx = 1\n"
    try:
        edit_source(src, [Finding(kind="import", file="x.py", line=1, end_line=1, name="sys",
                                  verdict="REMOVE", reason="")])
        assert False, "should refuse"
    except SharedLine:
        pass


def test_nested_spans_do_not_eat_following_code():
    src = "def gone():\n    import io\n    return 1\nKEEP = 2\n"
    out = edit_source(src, [Finding(kind="function", file="x.py", line=1, end_line=3, name="gone",
                                    verdict="REMOVE", reason=""),
                            Finding(kind="import", file="x.py", line=2, end_line=2, name="io",
                                    verdict="REMOVE", reason="")])
    assert out.strip() == "KEEP = 2"


def test_bom_is_kept_when_editing(make_repo):
    root = make_repo({"check.py": "import app\n"})
    (root / "app.py").write_bytes("﻿import io\nx = 1\n".encode("utf-8"))
    res = run(root, prove_command=f"{PY_CMD} check.py", record=False)
    assert by_name(res)[("app.py", "io")].proof == "passed"
    assert (root / "app.py").read_bytes().startswith(b"\xef\xbb\xbf")     # the original is untouched
