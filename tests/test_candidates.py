"""
Stage 2-3: what counts as unused, and — more importantly — every trap where a
static graph would be wrong and must say AMBIGUOUS or stay silent instead of REMOVE.
"""

from justify.engine import run


def verdicts(root):
    res = run(root, record=False)
    return {(f.kind, f.name): f.verdict for f in res.findings}, res


# ---------------------------------------------------------------- the plain cases

def test_unused_import_is_found(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        import io
        import csv
        rows = csv.reader([])
    """}))
    assert v.get(("import", "io")) == "REMOVE"
    assert ("import", "csv") not in v


def test_text_search_trap_time_inside_datetime(make_repo):
    # a text search for "time." matches inside "datetime." — the tree must not
    v, _ = verdicts(make_repo({"app.py": """
        import time
        import datetime
        x = datetime.datetime.now()
    """}))
    assert v.get(("import", "time")) == "REMOVE"
    assert ("import", "datetime") not in v


def test_string_data_does_not_keep_an_import_alive(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        from datetime import date
        def handler(args):
            return args.get("date")
        handler({})
    """}))
    assert v.get(("import", "date")) == "REMOVE"


def test_string_annotation_keeps_an_import_alive(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        from decimal import Decimal
        def price(x: "Decimal") -> "Decimal":
            return x
        price(1)
    """}))
    assert ("import", "Decimal") not in v


def test_import_used_only_in_annotation_is_used(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        from __future__ import annotations
        from pathlib import Path
        def f(p: Path) -> None:
            pass
        f(None)
    """}))
    assert ("import", "Path") not in v


def test_unused_function_is_found_used_one_is_not(make_repo):
    v, _ = verdicts(make_repo({
        "a.py": """
            def used():
                return 1
            def unused():
                return 2
        """,
        "b.py": """
            from a import used
            print(used())
        """}))
    assert v.get(("function", "unused")) == "REMOVE"
    assert ("function", "used") not in v


def test_recursion_does_not_count_as_a_use(make_repo):
    v, _ = verdicts(make_repo({"a.py": """
        def countdown(n):
            return countdown(n - 1) if n else 0
    """}))
    assert v.get(("function", "countdown")) == "REMOVE"


# ---------------------------------------------------------------- the traps

def test_trap_import_inside_try_import_error(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        try:
            import ujson
        except ImportError:
            ujson = None
    """}))
    assert v.get(("import", "ujson")) in (None, "AMBIGUOUS")


def test_trap_noqa_import_is_left_alone(make_repo):
    v, _ = verdicts(make_repo({"app.py": "import readline  # noqa: F401\n"}))
    assert ("import", "readline") not in v


def test_trap_side_effect_module(make_repo):
    v, _ = verdicts(make_repo({"app.py": "import readline\n"}))
    assert v.get(("import", "readline")) == "AMBIGUOUS"


def test_trap_init_reexport(make_repo):
    v, _ = verdicts(make_repo({"pkg/__init__.py": "from .core import thing\n", "pkg/core.py": "thing = 1\n"}))
    assert v.get(("import", "thing")) == "AMBIGUOUS"


def test_trap_dunder_all(make_repo):
    v, _ = verdicts(make_repo({"mod.py": """
        from os import path
        __all__ = ["path"]
    """}))
    assert ("import", "path") not in v


def test_trap_framework_route_is_an_entry_point(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        from flask import Flask
        app = Flask(__name__)
        @app.route("/")
        def index():
            return "hi"
    """}))
    assert ("function", "index") not in v


def test_trap_unknown_decorator_is_ambiguous(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        def registry(fn):
            return fn
        @registry
        def plugin():
            return 1
    """}))
    assert v.get(("function", "plugin")) == "AMBIGUOUS"


def test_trap_called_by_name_through_getattr(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        import sys
        def handle_start():
            return 1
        def dispatch(cmd):
            return getattr(sys.modules[__name__], "handle_" + cmd)()
        dispatch("start")
    """}))
    assert v.get(("function", "handle_start")) in (None, "AMBIGUOUS")


def test_trap_named_in_a_string(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        def on_save():
            return 1
        HOOKS = {"save": "on_save"}
    """}))
    assert ("function", "on_save") not in v


def test_trap_pytest_fixture_used_as_a_parameter(make_repo):
    v, _ = verdicts(make_repo({"tests/conftest.py": """
        import pytest
        @pytest.fixture
        def db():
            return {}
    """, "tests/test_x.py": """
        def test_it(db):
            assert db == {}
    """}))
    assert ("function", "db") not in v
    assert ("function", "test_it") not in v


def test_trap_entry_point_in_pyproject(make_repo):
    v, _ = verdicts(make_repo({"pkg/cli.py": """
        def main():
            return 0
    """, "pyproject.toml": """
        [project]
        name = "pkg"
        [project.scripts]
        pkg = "pkg.cli:main"
    """}))
    assert ("function", "main") not in v


def test_trap_public_function_in_a_library(make_repo):
    v, _ = verdicts(make_repo({"lib/api.py": """
        def public_helper():
            return 1
        def _private_helper():
            return 2
    """, "pyproject.toml": "[project]\nname = \"lib\"\n"}))
    assert v.get(("function", "public_helper")) == "AMBIGUOUS"
    assert v.get(("function", "_private_helper")) == "REMOVE"


def test_trap_test_functions_are_found_by_name(make_repo):
    v, _ = verdicts(make_repo({"tests/test_a.py": "def test_one():\n    assert True\n"}))
    assert v == {}


# ---------------------------------------------------------------- dependencies and duplicates

def test_unused_dependency_is_ambiguous_never_remove(make_repo):
    v, res = verdicts(make_repo({
        "app.py": "import requests\nrequests.get\n",
        "requirements.txt": "requests==2.31\nleftpad==1.0\ngunicorn\n",
    }))
    assert v.get(("dependency", "leftpad")) == "AMBIGUOUS"
    assert ("dependency", "requests") not in v
    assert ("dependency", "gunicorn") not in v


def test_dependency_with_a_different_import_name(make_repo):
    v, _ = verdicts(make_repo({"app.py": "import cv2\ncv2.imread\n", "requirements.txt": "opencv-python\n"}))
    assert ("dependency", "opencv-python") not in v


def test_dependency_used_outside_python(make_repo):
    v, _ = verdicts(make_repo({"app.py": "x = 1\n", "requirements.txt": "celery\n",
                               "Procfile": "worker: celery -A app worker\n"}))
    assert ("dependency", "celery") not in v


def test_duplicate_helpers_are_simplify(make_repo):
    body = """
        def {name}(items):
            total = 0
            for item in items:
                if item is not None and item > 0:
                    total += item * 2
            return total
    """
    v, _ = verdicts(make_repo({"a.py": body.format(name="score"), "b.py": body.format(name="tally"),
                               "c.py": "from a import score\nfrom b import tally\nscore([]); tally([])\n"}))
    assert v.get(("duplicate", "tally")) == "SIMPLIFY"


def test_functions_calling_different_helpers_are_not_duplicates(make_repo):
    v, _ = verdicts(make_repo({"a.py": """
        def first(items):
            total = 0
            for item in items:
                if item is not None and item > 0:
                    total += helper_a(item)
            return total
        def second(items):
            total = 0
            for item in items:
                if item is not None and item > 0:
                    total += helper_b(item)
            return total
        def helper_a(x): return x
        def helper_b(x): return x
        first([]); second([])
    """}))
    assert not any(k[0] == "duplicate" for k in v)


def test_trap_importing_a_local_module_may_be_for_its_side_effect(make_repo):
    v, _ = verdicts(make_repo({"signals.py": "REGISTERED = True\n",
                               "app.py": "import signals\nfrom signals import REGISTERED\nfrom signals import X\nprint(X)\n"}))
    assert v.get(("import", "signals")) == "AMBIGUOUS"
    assert v.get(("import", "REGISTERED")) == "REMOVE"


def test_test_code_helpers_are_never_proved_by_the_tests(make_repo):
    res = run(make_repo({"tests/test_a.py": "import io\ndef helper():\n    return 1\ndef test_x():\n    assert True\n"}),
              record=False)
    by = {f.name: f for f in res.findings}
    assert by["io"].verdict == "REMOVE" and by["io"].proof == "not run"      # running the suite proves an import
    assert by["helper"].verdict == "AMBIGUOUS" and by["helper"].proof == "not provable (test code)"


# ---------------------------------------------------------------- re-exports (found on real repositories)

def test_import_read_by_another_file_is_a_reexport(make_repo):
    v, _ = verdicts(make_repo({"pkg/models.py": "from pkg.base import Order\n",
                               "pkg/base.py": "class Order:\n    pass\n",
                               "app.py": "from pkg.models import Order\nprint(Order)\n"}))
    assert ("import", "Order") not in v


def test_relative_reexport(make_repo):
    v, _ = verdicts(make_repo({"pkg/__init__.py": "", "pkg/a.py": "from .b import thing\n",
                               "pkg/b.py": "thing = 1\n", "pkg/c.py": "from .a import thing\nprint(thing)\n"}))
    assert ("import", "thing") not in v


def test_reexport_read_through_module_attribute(make_repo):
    v, _ = verdicts(make_repo({"lib/helpers.py": "from json import dumps\n",
                               "app.py": "from lib import helpers\nhelpers.dumps({})\n"}))
    assert ("import", "dumps") not in v


def test_explicit_as_reexport_is_kept(make_repo):
    v, _ = verdicts(make_repo({"mod.py": "from os.path import join as join\nimport json as json\n"}))
    assert v == {}


def test_mock_patch_string_keeps_the_import(make_repo):
    v, _ = verdicts(make_repo({"app.py": "import requests\n",
                               "tests/test_app.py": "from unittest import mock\n"
                                                    "with mock.patch('app.requests'):\n    pass\n"}))
    assert ("import", "requests") not in v


def test_star_import_uses_count(make_repo):
    v, _ = verdicts(make_repo({"common.py": "from dataclasses import dataclass\nimport io\nVERSION = 1\n",
                               "order.py": "from common import *\n@dataclass\nclass Order:\n    pass\nprint(Order)\n"}))
    assert ("import", "dataclass") not in v
    assert v.get(("import", "io")) == "REMOVE"


def test_star_reexport_through_init_is_ambiguous(make_repo):
    v, _ = verdicts(make_repo({"pkg/__init__.py": "from .core import *\n",
                               "pkg/core.py": "from os.path import join\n"}))
    assert v.get(("import", "join")) == "AMBIGUOUS"


def test_type_strings_outside_annotations(make_repo):
    v, _ = verdicts(make_repo({"app.py": """
        from typing import TypeVar, cast
        from base import Transport, Middleware, Handler
        T = TypeVar("T", bound="Transport")
        m = cast("Middleware[int]", None)
        Alias = list["Handler"]
    """, "base.py": "Transport = Middleware = Handler = object\n"}))
    assert not any(k[0] == "import" for k in v)


def test_fixture_imported_into_conftest(make_repo):
    v, _ = verdicts(make_repo({"tests/fixtures.py": "import pytest\n@pytest.fixture\ndef db():\n    return {}\n",
                               "tests/conftest.py": "from tests.fixtures import db\n",
                               "tests/test_x.py": "def test_it(db):\n    assert db == {}\n"}))
    assert ("import", "db") not in v


def test_hook_file_named_in_config_is_ambiguous(make_repo):
    v, _ = verdicts(make_repo({"hooks/build.py": "def on_post_build(config):\n    return None\n",
                               "mkdocs.yml": "hooks:\n  - hooks/build.py\n"}))
    assert v.get(("function", "on_post_build")) == "AMBIGUOUS"


def test_script_run_by_a_dockerfile_is_still_checked(make_repo):
    v, _ = verdicts(make_repo({"serve.py": "def unused():\n    return 1\n",
                               "Dockerfile": "CMD python serve.py\n"}))
    assert v.get(("function", "unused")) == "REMOVE"


def test_script_run_by_ci_or_listed_by_a_linter_is_still_checked(make_repo):
    v, _ = verdicts(make_repo({
        "scripts/build.py": "def unused():\n    return 1\n",
        ".github/workflows/ci.yml": "jobs:\n  b:\n    steps:\n      - run: python scripts/build.py\n",
        "pyproject.toml": "[tool.ruff.lint.per-file-ignores]\n\"scripts/build.py\" = [\"E501\"]\n"}))
    assert v.get(("function", "unused")) == "REMOVE"


def test_dict_key_string_does_not_keep_an_import_alive(make_repo):
    v, _ = verdicts(make_repo({"app.py": "import time\nrow = {}\nCONFIG = {}\nprint(row['time'], CONFIG['time'])\n"}))
    assert v.get(("import", "time")) == "REMOVE"


# ---------------------------------------------------------------- red-team traps: frameworks and conventions

def test_registry_subclasses_are_ambiguous(make_repo):
    v, _ = verdicts(make_repo({"base.py": """
        class Exporter:
            registry = {}
            def __init_subclass__(cls, **kw):
                super().__init_subclass__(**kw)
                Exporter.registry[cls.__name__] = cls
        class Rule(metaclass=type):
            pass
        class Validator:
            pass
        def run():
            return [c() for c in Validator.__subclasses__()]
        run()
    """, "plugins.py": """
        from base import Exporter, Rule, Validator
        class Csv(Exporter):
            pass
        class NoTabs(Rule):
            pass
        class NotEmpty(Validator):
            pass
        class Plain:
            pass
    """}))
    assert v.get(("class", "Csv")) == "AMBIGUOUS"
    assert v.get(("class", "NoTabs")) == "AMBIGUOUS"
    assert v.get(("class", "NotEmpty")) == "AMBIGUOUS"
    assert v.get(("class", "Plain")) == "REMOVE"


def test_framework_base_classes_are_ambiguous(make_repo):
    v, _ = verdicts(make_repo({
        "polls/migrations/0001_initial.py": "from django.db import migrations\nclass Migration(migrations.Migration):\n    pass\n",
        "polls/management/commands/close.py": "from django.core.management.base import BaseCommand\nclass Command(BaseCommand):\n    pass\n",
        "inventory/tests.py": "import unittest\nclass StoreChecks(unittest.TestCase):\n    def test_a(self):\n        pass\n",
        "errors.py": "class Unused(ValueError):\n    pass\n"}))
    assert v.get(("class", "Migration")) == "AMBIGUOUS"
    assert v.get(("class", "Command")) == "AMBIGUOUS"
    assert v.get(("class", "StoreChecks")) == "AMBIGUOUS"
    assert v.get(("class", "Unused")) == "REMOVE"


def test_functions_tools_call_by_name(make_repo):
    v, _ = verdicts(make_repo({
        "conftest.py": "def pytest_addoption(parser):\n    pass\n",
        "tests/test_p.py": "def setup_module(m):\n    pass\ndef test_a():\n    pass\n",
        "migrations/versions/001.py": "revision = 'a'\ndown_revision = None\ndef upgrade():\n    pass\ndef downgrade():\n    pass\n",
        "gunicorn.conf.py": "def post_fork(server, worker):\n    pass\n",
        "docs/conf.py": "def setup(app):\n    pass\n",
        "dodo.py": "def task_docs():\n    return {}\n",
        "src/handler.py": "def lambda_handler(event, ctx):\n    return 1\n"}))
    for name in ("pytest_addoption", "setup_module", "upgrade", "downgrade", "post_fork", "setup",
                 "task_docs", "lambda_handler"):
        assert v.get(("function", name)) in (None, "AMBIGUOUS"), name


def test_name_in_terraform_counts(make_repo):
    v, _ = verdicts(make_repo({"src/handler.py": "def process(event, ctx):\n    return 1\n",
                               "infra/main.tf": 'resource "aws_lambda_function" "f" {\n  handler = "handler.process"\n}\n'}))
    assert ("function", "process") not in v


def test_english_word_in_readme_does_not_count_but_code_does(make_repo):
    v, _ = verdicts(make_repo({"app.py": "def upgrade():\n    pass\ndef migrate_all():\n    pass\n",
                               "README.md": "To upgrade, run `migrate_all()` first.\n"}))
    assert v.get(("function", "upgrade")) == "REMOVE"
    assert ("function", "migrate_all") not in v


def test_doctest_use_counts(make_repo):
    v, _ = verdicts(make_repo({"pricing.py": '''
        import json
        def _sample():
            return [1]
        def total(xs):
            """
            >>> total(_sample())
            1
            >>> json.dumps(1)
            '1'
            """
            return sum(xs)
        total([])
    '''}))
    assert ("function", "_sample") not in v and ("import", "json") not in v


def test_computed_lookups_reach_their_targets(make_repo):
    v, _ = verdicts(make_repo({
        "handlers.py": "def handle_push():\n    return 1\n",
        "webhook.py": "import handlers\ndef route(kind):\n    return getattr(handlers, f'handle_{kind}')()\nroute('push')\n",
        "convert.py": "import inspect, sys\ndef to_kelvin(f):\n    return f\n"
                      "C = {n: f for n, f in inspect.getmembers(sys.modules[__name__]) if n.startswith('to_')}\n",
        "commands/__init__.py": "", "commands/text.py": "def cmd_upper(s):\n    return s.upper()\n",
        "shell.py": "import pkgutil, importlib, commands\nfor m in pkgutil.iter_modules(commands.__path__):\n"
                    "    importlib.import_module('commands.' + m.name)\n"}))
    assert v.get(("function", "handle_push")) in (None, "AMBIGUOUS")
    assert v.get(("function", "to_kelvin")) in (None, "AMBIGUOUS")
    assert v.get(("function", "cmd_upper")) in (None, "AMBIGUOUS")


def test_setup_py_entry_point(make_repo):
    v, _ = verdicts(make_repo({
        "services/worker/setup.py": "from setuptools import setup\nsetup(entry_points={'console_scripts': "
                                    "['work = worker.cli:run_worker']})\n",
        "services/worker/worker/__init__.py": "",
        "services/worker/worker/cli.py": "def run_worker():\n    return 0\n",
        "services/worker/worker/sinks.py": "class S3Sink:\n    pass\n"}))
    assert ("function", "run_worker") not in v
    assert v.get(("class", "S3Sink")) == "AMBIGUOUS"         # public API of a packaged library


# ---------------------------------------------------------------- red-team traps: imports

def test_shim_module_imports_are_ambiguous(make_repo):
    v, _ = verdicts(make_repo({"myapp/__init__.py": "", "myapp/server.py": "app = object()\n",
                               "wsgi.py": '"""gunicorn wsgi:app"""\nfrom myapp.server import app\n',
                               "Procfile": "web: gunicorn wsgi:app\n"}))
    assert ("import", "app") not in v                    # Procfile reads wsgi:app — a re-export


def test_local_side_effect_from_import_is_ambiguous(make_repo):
    v, _ = verdicts(make_repo({"bot/__init__.py": "", "bot/handlers.py": "REG = []\n",
                               "bot/main.py": "from . import handlers\nfrom bot.handlers import REG as R2, REG\nprint(R2)\n",
                               "app/__init__.py": "", "app/views.py": "x = 1\n",
                               "app/wsgi.py": "from app.views import x\nAPP = 1\n"}))
    assert v.get(("import", "handlers")) == "AMBIGUOUS"   # a submodule of this repository
    assert v.get(("import", "x")) == "AMBIGUOUS"          # the whole statement is the only import of app.views
    assert v.get(("import", "REG")) == "REMOVE"           # its statement still imports the module


def test_globals_and_eval_make_imports_ambiguous(make_repo):
    v, _ = verdicts(make_repo({"calc.py": "from math import floor, pi\n"
                                          "def ev(f):\n    return eval(f, globals())\nev('1')\n"}))
    assert v.get(("import", "floor")) == "AMBIGUOUS" and v.get(("import", "pi")) == "AMBIGUOUS"


def test_dunder_all_extend_and_noqa_on_a_later_line(make_repo):
    v, _ = verdicts(make_repo({"core.py": "from os import sep, getcwd\n__all__ = []\n__all__.extend(['sep'])\nx = 1\n",
                               "schema.py": "from os.path import (\n    join,\n    split,  # noqa: F401\n)\nx = 1\n"}))
    assert ("import", "sep") not in v and v.get(("import", "getcwd")) == "REMOVE"
    assert ("import", "split") not in v and v.get(("import", "join")) == "REMOVE"


def test_submodule_import_used_through_another_alias(make_repo):
    v, _ = verdicts(make_repo({"applog.py": "import logging as log\nimport logging.handlers\n"
                                            "h = log.handlers.RotatingFileHandler\n",
                               "xmlio.py": "import xml\nfrom xml import dom\nfrom xml.dom import minidom\n"
                                           "p = xml.dom.minidom.parseString\n"}))
    assert not any(k[0] == "import" for k in v)


def test_side_effect_import_heuristics(make_repo):
    v, _ = verdicts(make_repo({"impute.py": "from sklearn.experimental import enable_iterative_imputer\nx = 1\n",
                               "plot3d.py": "from mpl_toolkits.mplot3d import Axes3D\nx = 1\n",
                               "dash.py": "import hvplot.pandas\ndef chart(df):\n    return df.hvplot()\nchart\n"}))
    assert v.get(("import", "enable_iterative_imputer")) == "AMBIGUOUS"
    assert v.get(("import", "Axes3D")) == "AMBIGUOUS"
    assert v.get(("import", "hvplot")) == "AMBIGUOUS"


def test_string_annotations_with_quotes_and_calls(make_repo):
    v, _ = verdicts(make_repo({"api.py": "from typing import Annotated, Literal\nfrom pydantic import Field\n"
                                         "def f(a: \"Literal['x', 'y']\", b: \"Annotated[int, Field(gt=0)]\"):\n"
                                         "    return a\nf\n"}))
    assert not any(k[0] == "import" for k in v)


def test_bom_file_is_parsed(make_repo, tmp_path):
    root = make_repo({"pricing.py": "def fmt(x):\n    return x\n"})
    (root / "main.py").write_bytes("﻿import pricing\nprint(pricing.fmt(1))\n".encode("utf-8"))
    res = run(root, record=False)
    assert res.unparsed == [] and not any(f.name == "fmt" for f in res.findings)


def test_stdlib_name_is_not_a_local_module(make_repo):
    v, _ = verdicts(make_repo({"shared/__init__.py": "", "shared/types.py": "X = 1\n",
                               "tests/test_a.py": "from types import SimpleNamespace\ndef test_a():\n    pass\n"}))
    assert v.get(("import", "SimpleNamespace")) == "REMOVE"


def test_template_variables_are_not_references_but_strings_are(make_repo):
    v, _ = verdicts(make_repo({"roster.py": "def batches():\n    return []\ndef handler_name():\n    return 1\n",
                               "templates/a.html": "{% for b in batches %}{{ b }}{% endfor %}\n",
                               "infra/stack.ts": "new Function(this, 'f', { handler: 'roster.handler_name' })\n"}))
    assert v.get(("function", "batches")) == "REMOVE"
    assert ("function", "handler_name") not in v
