"""
Stage 3 — candidates.

Decides what needs judgement. Most code is settled by the graph alone: used and
unique means KEEP, with the use as its recorded reason. Only what the graph
cannot settle goes on — and nothing is ever marked REMOVE here without also
having to survive the challenge and the proof later.

The rules that send something to AMBIGUOUS instead of REMOVE are the traps
where a static graph is wrong:

  * an import inside try/except ImportError      optional-dependency checks
  * an import marked "# noqa"                     a developer said keep it
  * a module known to work by side effect         readline, gevent monkey-patching …
  * anything in __init__.py                       may be a public re-export
  * a decorated function                          frameworks call it: @app.route
  * a module that uses getattr / importlib        may be called by name
  * a public function in a library                its users live outside the repo
  * a dependency with no import                   proving it needs a clean install
  * a class a framework finds by type            TestCase, Model, Command, a registry base
  * a function a tool calls by name              pytest_*, Alembic upgrade(), gunicorn hooks
  * code in tests                                the tests cannot prove changes to themselves
"""

from __future__ import annotations

import builtins
import re
from collections import defaultdict

from .facts import DUPLICATE_MIN_NODES, RepoFacts
from .model import AMBIGUOUS, REMOVE, SIMPLIFY, Finding

SIDE_EFFECT_MODULES = {
    "readline", "rlcompleter", "gevent.monkey", "eventlet", "django_extensions", "pytest_asyncio",
    "matplotlib.pyplot", "tensorflow_addons", "dotenv", "nest_asyncio", "this", "antigravity",
    "faulthandler", "tracemalloc", "sitecustomize", "usercustomize", "colorama", "coloredlogs",
}

# distribution name -> the module name it is imported as, where they differ
DIST_TO_MODULE = {
    "opencv-python": "cv2", "opencv-python-headless": "cv2", "opencv-contrib-python": "cv2",
    "pillow": "PIL", "python-dotenv": "dotenv", "scikit-learn": "sklearn", "scikit-image": "skimage",
    "beautifulsoup4": "bs4", "pyyaml": "yaml", "python-dateutil": "dateutil", "pyjwt": "jwt",
    "psycopg2-binary": "psycopg2", "psycopg-binary": "psycopg", "mysqlclient": "MySQLdb",
    "python-multipart": "multipart", "google-cloud-storage": "google", "protobuf": "google",
    "pymupdf": "fitz", "python-pptx": "pptx", "python-docx": "docx", "attrs": "attr",
    "msgpack-python": "msgpack", "pyserial": "serial", "pycryptodome": "Crypto",
    "faiss-cpu": "faiss", "faiss-gpu": "faiss", "onnxruntime-gpu": "onnxruntime",
    "typing-extensions": "typing_extensions", "email-validator": "email_validator",
}

# packages that are run, not imported: servers, linters, test runners
RUNTIME_TOOLS = {"gunicorn", "uvicorn", "waitress", "hypercorn", "daphne", "pytest", "black", "ruff",
                 "flake8", "mypy", "isort", "pre-commit", "coverage", "tox", "nox", "wheel",
                 "setuptools", "pip", "twine", "build", "pylint", "ipython", "jupyter", "notebook"}


# A function wearing one of these is called by its framework — a route, a fixture, a CLI
# command, an MCP tool, a signal receiver. That is its recorded reason to exist.
FRAMEWORK_DECORATOR = re.compile(
    r"(^|\.)(route|get|post|put|patch|delete|head|options|websocket|api_route|errorhandler|"
    r"before_request|after_request|before_app_request|before_first_request|teardown_request|"
    r"teardown_appcontext|context_processor|template_filter|template_global|template_test|"
    r"url_value_preprocessor|user_loader|request_loader|command|group|fixture|tool|resource|prompt|"
    r"task|shared_task|periodic_task|receiver|on_event|middleware|exception_handler|listens_for|"
    r"validator|field_validator|model_validator|root_validator|hookimpl|subscriber|callback|"
    r"handler|message_handler|event|on|cli|app|lifespan|dependency|setup|teardown)\b(\(|$)")


def _framework_entry(decorators: list[str]) -> str | None:
    for d in decorators:
        if FRAMEWORK_DECORATOR.search(d.strip()):
            return d
    return None


def _evidence(rf: RepoFacts, name: str, exclude_file: str | None = None,
              exclude_span: tuple[int, int] | None = None, limit: int = 5) -> list[str]:
    out = []
    for f, line in rf.refs.get(name, []):
        if exclude_file and f == exclude_file and exclude_span and exclude_span[0] <= line <= exclude_span[1]:
            continue
        out.append(f"{f}:{line}")
        if len(out) >= limit:
            break
    return out


def _local_roots(rf: RepoFacts) -> set[str]:
    return {rel.split("/")[0].removesuffix(".py") for rel in rf.files}


# functions a tool calls by name — nothing in the repository ever writes the call
PYTEST_XUNIT = {"setup_module", "teardown_module", "setup_function", "teardown_function", "setUpModule",
                "tearDownModule", "load_tests", "setup_package", "teardown_package", "setup", "teardown"}
CONVENTION_FUNCS = {"includeme", "create_app", "make_app", "lambda_handler", "handler", "application",
                    "app_factory", "get_wsgi_application", "get_asgi_application"}
GUNICORN_FILE = re.compile(r"(^|/)[^/]*gunicorn[^/]*\.py$")

# bases that never discover their subclasses: builtins, exceptions, plain typing and enum helpers
SAFE_BASES = {n for n, v in vars(builtins).items() if isinstance(v, type)} | {
    "NamedTuple", "TypedDict", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "Protocol", "ABC",
    "Generic", "dataclass", "SimpleNamespace", "UserDict", "UserList", "UserString", "OrderedDict",
    "defaultdict", "Counter", "deque"}
SAFE_METACLASSES = {"ABCMeta", "EnumMeta", "EnumType"}

# third-party modules that work by being imported (accessors, plugins, monkey-patches)
SIDE_EFFECT_MODULES |= {
    "sklearn.experimental", "tensorflow_text", "hvplot", "janitor", "rioxarray", "cf_xarray", "pint_xarray",
    "pandas_flavor", "mpl_toolkits.mplot3d", "encodings", "pillow_heif", "pillow_avif", "ydata_profiling",
    "swifter", "geopandas", "seaborn", "tqdm.auto", "plotly.io", "django.contrib.gis"}


def _side_effect(module: str) -> bool:
    parts = module.split(".")
    return any(".".join(parts[:i]) in SIDE_EFFECT_MODULES for i in range(1, len(parts) + 1))


def _imports(rf: RepoFacts) -> list[Finding]:
    import ast as _ast
    local = _local_roots(rf)
    out = []
    for rel, ff in rf.files.items():
        statement_unused: dict[int, bool] = {}
        for imp in ff.imports:
            used = imp.bound in ff.used or imp.bound in ff.dunder_all
            statement_unused[id(imp.node)] = statement_unused.get(id(imp.node), True) and not used
        statements_per_module: dict[str, set[int]] = defaultdict(set)
        for i in ff.imports:
            statements_per_module[i.module].add(id(i.node))
        for imp in ff.imports:
            if imp.bound in ff.used or imp.bound in ff.dunder_all:
                continue
            if imp.bound in rf.exported.get(rel, {}):
                continue      # another file reads it from here: a re-export, and that is its use
            if imp.alias.asname and imp.alias.asname == imp.alias.name.split(".")[-1]:
                continue      # "import x as x": the typing convention for an explicit re-export
            if ff.is_test and imp.bound in rf.test_params:
                continue      # a pytest fixture imported so tests can take it as a parameter
            last = imp.alias.name.split(".")[-1]
            if (isinstance(imp.node, _ast.Import) and "." in imp.alias.name and last in ff.attrs) or \
                    (isinstance(imp.node, _ast.ImportFrom) and imp.alias.name in ff.attrs):
                continue      # loads a submodule that is used as an attribute: log.handlers, xml.dom
            alias_line = getattr(imp.alias, "lineno", imp.lineno)
            if imp.noqa or any("noqa" in ff.src.source_lines[n - 1]
                               for n in {alias_line, imp.end_lineno} if 0 < n <= len(ff.src.source_lines)):
                continue      # a developer marked it; that is a recorded reason to keep
            base = dict(kind="import", file=rel, line=imp.lineno, end_line=imp.end_lineno,
                        name=imp.bound, lines=1)
            module = imp.module.lstrip(".")
            star = [g for g in rf.star_importers.get(rel, [])
                    if rf.files[g].is_init or g in rf.star_importers]
            is_from = isinstance(imp.node, _ast.ImportFrom)
            local_target = rf.resolve(imp.module, rel) if is_from else set()
            submodule = is_from and rf.resolve(
                imp.module + imp.alias.name if not imp.module.strip(".") else f"{imp.module}.{imp.alias.name}", rel)

            def amb(reason):
                out.append(Finding(verdict=AMBIGUOUS, reason=reason, **base))

            if imp.in_try:
                amb("inside try/except ImportError — probably checks whether an optional package is installed")
            elif ff.is_init:
                amb("in __init__.py — may be a public re-export used by code outside this repository")
            elif star and not imp.bound.startswith("_"):
                amb(f"re-exported by 'from … import *' in {star[0]} — its users may be anywhere")
            elif ff.all_unknown:
                amb("this module builds __all__ in a way that cannot be read — it may export this name")
            elif ff.only_imports:
                amb("this file is nothing but imports — it exists to expose these names (a shim like wsgi.py)")
            elif ff.dynamic_globals:
                amb("this file reads its own names through globals()/eval/vars — it may use this one")
            elif imp.bound in rf.unparsed_names:
                amb("a file that could not be parsed mentions this name")
            elif _side_effect(module) or imp.alias.name.startswith("enable_"):
                amb(f"'{module}' is known to work by its side effect on import")
            elif module.split(".")[0] in ff.attrs:
                amb(f"'{module.split('.')[0]}' appears as an attribute here — the import may register an accessor")
            elif not is_from and module.split(".")[0] in local:
                amb("imports a module from this repository — running that module's code (registration, a "
                    "smoke test) may be the point")
            elif submodule:
                amb(f"'{imp.alias.name}' is a module of this repository — importing it may register something")
            elif local_target and statement_unused[id(imp.node)] and len(statements_per_module[imp.module]) == 1:
                amb(f"nothing from this statement is used, so removing it stops '{module}' being imported "
                    "here — that import may register routes, signals or models")
            elif not is_from and "." in imp.alias.name and not imp.alias.asname:
                amb(f"'import {imp.alias.name}' loads a submodule — that may be its purpose")
            elif not imp.top_level:
                amb("imported inside a function or block — may be deliberate lazy loading")
            else:
                out.append(Finding(verdict=REMOVE, reason="no use anywhere in the file, not re-exported", **base))
    return out


def _base_origin(rf: RepoFacts, rel: str, base: str) -> str:
    """'external' when the base comes from an imported package, else 'local'/'unknown'."""
    root = base.split(".")[0]
    for imp in rf.files[rel].imports:
        if imp.bound == root:
            if imp.module.startswith(".") or imp.module.split(".")[0] in _local_roots(rf):
                return "local"
            return "external"
    return "local" if base.split(".")[-1] in rf.classes else "unknown"


def _registered_class(rf: RepoFacts, d, seen: set[int] | None = None) -> str | None:
    """Why a class may be found without its name being written: a framework base it inherits
    from, a registering metaclass, __init_subclass__, or a __subclasses__() scan."""
    seen = seen if seen is not None else set()
    seen.add(id(d))
    if d.metaclass and d.metaclass not in SAFE_METACLASSES:
        return f"its metaclass {d.metaclass} may register it"
    for written in d.bases:
        b = written.split(".")[-1]
        if b == "object":
            continue
        if b in rf.subclass_scans or ("*" in rf.subclass_scans and b in rf.classes):
            return f"{b}.__subclasses__() is scanned, which finds it"
        origin = _base_origin(rf, d.rel, written) if d.rel in rf.files else "unknown"
        parents = [c for c in rf.classes.get(b, []) if id(c) not in seen] if origin != "external" else []
        if parents:
            for parent in parents:
                if parent.init_subclass:
                    return f"{b} defines __init_subclass__, which registers subclasses"
                why = _registered_class(rf, parent, seen)
                if why:
                    return why
        elif b not in SAFE_BASES:
            return f"inherits from {written}, which a framework may discover by type (TestCase, Model, Command …)"
    return None


def _convention(rel: str, ff, d) -> str | None:
    name, base = d.name, rel.rsplit("/", 1)[-1]
    if d.kind != "function":
        return None
    if name.startswith("pytest_"):
        return "a pytest hook, called by name"
    if ff.is_test and name in PYTEST_XUNIT:
        return "a test-module hook, called by name"
    if {"revision", "down_revision"} <= ff.module_vars and name.split("_")[0] in ("upgrade", "downgrade"):
        return "an Alembic migration step, called by name"
    if GUNICORN_FILE.search(rel):
        return "gunicorn calls the hooks in its config file by name"
    if base == "conf.py" and name == "setup":
        return "Sphinx calls setup() in conf.py"
    if base == "dodo.py" and name.startswith("task_"):
        return "doit finds task_* functions by name"
    if base == "fabfile.py" and not name.startswith("_"):
        return "Fabric runs the public functions in fabfile.py"
    if base in ("noxfile.py", "tasks.py", "conftest.py"):
        return f"{base} is loaded by its tool, which calls its functions"
    if name in CONVENTION_FUNCS:
        return f"'{name}' is a name servers and platforms look up"
    return None


def _defs(rf: RepoFacts) -> list[Finding]:
    prefixes = set().union(*[ff.prefixes for ff in rf.files.values()]) if rf.files else set()
    out = []
    for rel, ff in rf.files.items():
        for d in ff.defs:
            used_elsewhere = _evidence(rf, d.name, exclude_file=rel, exclude_span=(d.start, d.end))
            if used_elsewhere:
                continue
            base = dict(kind=d.kind, file=rel, line=d.start, end_line=d.end, name=d.name,
                        lines=d.end - d.start + 1)
            if d.name.startswith("__") and d.name.endswith("__"):
                continue
            if d.name in ff.dunder_all:
                continue
            if ff.is_test and (d.name.startswith("test") or d.name.startswith("Test")):
                continue      # pytest finds these by name
            if d.decorated and _framework_entry(d.decorators):
                continue      # called by its framework: that is the reason it exists

            def amb(reason):
                out.append(Finding(verdict=AMBIGUOUS, reason=reason, **base))

            convention = _convention(rel, ff, d)
            registered = _registered_class(rf, d) if d.kind == "class" else None
            prefix = next((p for p in prefixes if d.name.startswith(p) and d.name != p), None)
            if convention:
                amb(convention)
            elif registered:
                amb(registered)
            elif d.decorated:
                amb(f"decorated ({', '.join(d.decorators)[:80]}) — a framework may call it")
            elif ff.is_init:
                amb("defined in __init__.py — may be public API")
            elif rel in rf.config_loaded:
                amb(f"this file is named in {rf.config_loaded[rel]} — the tool that loads it may call its "
                    "functions by name")
            elif ff.dynamic or rel in rf.dynamic_files:
                amb("this module is reached through getattr/importlib — it may be called by name")
            elif prefix:
                amb(f"a name is built from '{prefix}' + something elsewhere — this may be looked up that way")
            elif ff.is_test:
                amb("in test code — the tests cannot prove that removing their own code is safe")
            elif rf.in_library(rel) and not d.name.startswith("_"):
                amb("public name in a library — its users may live outside this repository")
            else:
                out.append(Finding(verdict=REMOVE, reason=f"no reference to '{d.name}' anywhere in the "
                                   "repository", **base))
    return out


def _requirements(rf: RepoFacts) -> list[tuple[str, str, int]]:
    """(distribution, file, line) from requirements*.txt and pyproject dependencies."""
    deps = []
    root = rf.root
    for path in sorted(list(root.glob("requirements*.txt")) + list(root.glob("requirements/*.txt"))):
        for i, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
            line = raw.split("#")[0].strip()
            if not line or line.startswith(("-", "git+", "http")):
                continue
            name = re.split(r"[<>=!~\[;@ ]", line, maxsplit=1)[0].strip()
            if name:
                deps.append((name, str(path.relative_to(root)), i))
    pp = root / "pyproject.toml"
    if pp.exists():
        text = pp.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
        if m:
            start_line = text[: m.start()].count("\n") + 1
            for j, item in enumerate(re.findall(r"[\"']([^\"']+)[\"']", m.group(1))):
                name = re.split(r"[<>=!~\[;@ ]", item.strip(), maxsplit=1)[0]
                if name:
                    deps.append((name, "pyproject.toml", start_line + j + 1))
    return deps


def _dependencies(rf: RepoFacts) -> list[Finding]:
    out = []
    for dist, rel, line in _requirements(rf):
        norm = dist.lower().replace("_", "-")
        if norm in RUNTIME_TOOLS:
            continue
        module = DIST_TO_MODULE.get(norm, norm.replace("-", "_"))
        if module in rf.imported_roots or module.lower() in {r.lower() for r in rf.imported_roots}:
            continue
        mentions = [f"{f}:{n}" for f, n in rf.refs.get(dist, []) + rf.refs.get(module, [])
                    if not f.startswith("requirements") and f != "pyproject.toml"]
        if mentions:
            continue
        out.append(Finding(kind="dependency", file=rel, line=line, end_line=line, name=dist,
                           verdict=AMBIGUOUS, lines=1, proof="not provable",
                           reason=f"no file imports '{module}' and nothing else mentions it — "
                                  "removing a dependency can only be proved with a clean install"))
    return out


def _duplicates(rf: RepoFacts) -> list[Finding]:
    groups: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for rel, ff in rf.files.items():
        for d in ff.defs:
            if d.kind == "function" and d.body_hash and d.nodes >= DUPLICATE_MIN_NODES:
                groups[d.body_hash].append((rel, d))
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        first_rel, first = members[0]
        for rel, d in members[1:]:
            out.append(Finding(kind="duplicate", file=rel, line=d.start, end_line=d.end, name=d.name,
                               verdict=SIMPLIFY, lines=d.end - d.start + 1,
                               evidence=[f"{first_rel}:{first.start}"],
                               reason=f"same body as {first.name}() in {first_rel}:{first.start} — "
                                      "merge into one", proof="not provable"))
    return out


def find_candidates(rf: RepoFacts) -> list[Finding]:
    found = _imports(rf) + _defs(rf) + _dependencies(rf) + _duplicates(rf)
    found.sort(key=lambda f: (f.file, f.line, f.name))
    return found
