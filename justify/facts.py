"""
Stage 2 — static facts.

Turns source text into facts about who uses what, by reading syntax trees and
never by searching text. A text search for "time." matches inside "datetime."
and makes a dead import look alive; the tree knows the difference.

Everything here leans towards "it is used". A name counts as referenced if it
appears anywhere it could be reached from: as a name, an attribute, an imported
name, a function parameter (pytest injects fixtures by parameter name), inside a
string (getattr(obj, "name"), "pkg.mod:func" entry points), or in a non-Python
file such as pyproject.toml, a Dockerfile or a Procfile. Calling yourself does
not count, or a dead recursive function would look alive. A false "unused" can
delete working code; a false "used" only leaves some weight behind.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import pathlib
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from .ingest import SourceFile, inside, is_skipped, other_files

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DYNAMIC_CALLS = {"getattr", "globals", "locals", "vars", "__import__", "eval", "exec",
                 "import_module", "setattr", "hasattr"}
DUPLICATE_MIN_NODES = 30


@dataclass
class ImportFact:
    bound: str
    module: str
    alias: ast.alias
    node: ast.AST
    lineno: int
    end_lineno: int
    in_try: bool
    top_level: bool
    noqa: bool


@dataclass
class DefFact:
    name: str
    kind: str                # function / class
    node: ast.AST
    start: int               # first decorator line, or the def line
    end: int
    decorated: bool
    decorators: list[str]
    body_hash: str | None = None
    nodes: int = 0
    bases: list[str] = field(default_factory=list)     # each base as written: "Model", "migrations.Migration"
    rel: str = ""
    metaclass: str | None = None
    init_subclass: bool = False


@dataclass
class FileFacts:
    src: SourceFile
    imports: list[ImportFact] = field(default_factory=list)
    defs: list[DefFact] = field(default_factory=list)
    used: set[str] = field(default_factory=set)        # names this file uses (for its imports)
    dunder_all: set[str] = field(default_factory=set)
    chains: set[str] = field(default_factory=set)      # "mod.attr.attr" read through a name
    dotted: list[tuple[str, int]] = field(default_factory=list)   # "pkg.mod.name" / "pkg.mod:name" strings
    star_imports: list[tuple[str, int]] = field(default_factory=list)
    params: set[str] = field(default_factory=set)
    attrs: set[str] = field(default_factory=set)       # every ".name" read in this file
    dynamic_globals: bool = False     # globals() / vars() / locals() / eval / exec / sys.modules[__name__]
    all_unknown: bool = False         # __all__ is built in a way we cannot read
    only_imports: bool = False        # the module is nothing but imports: a shim that exposes names
    prefixes: set[str] = field(default_factory=set)    # "handle_" + name, f"cmd_{x}": computed lookups
    dyn_targets: list[str] = field(default_factory=list)   # modules/packages reached by computed name
    subclass_scans: set[str] = field(default_factory=set)  # X in X.__subclasses__()
    module_vars: set[str] = field(default_factory=set)     # names assigned at module level
    dynamic: bool = False
    is_init: bool = False
    is_test: bool = False


@dataclass
class RepoFacts:
    root: pathlib.Path
    files: dict[str, FileFacts]
    refs: dict[str, list[tuple[str, int]]]       # name -> every place it is referenced
    imported_roots: set[str]                     # top-level modules imported anywhere
    other: list[tuple[str, str]]                 # non-Python files (path, text)
    is_library: bool
    total_lines: int
    # rel -> name -> where another file reads that name from this module
    exported: dict[str, dict[str, str]] = field(default_factory=dict)
    # rel -> files that do "from <rel> import *"
    star_importers: dict[str, list[str]] = field(default_factory=dict)
    # rel -> config file that names it (mkdocs hooks, plugin lists): its functions may be called by name
    config_loaded: dict[str, str] = field(default_factory=dict)
    test_params: set[str] = field(default_factory=set)
    modules: dict[str, set[str]] = field(default_factory=dict)   # dotted name -> files
    library_roots: list[str] = field(default_factory=list)       # folders holding a packaged library
    dynamic_files: set[str] = field(default_factory=set)         # reached by getattr/pkgutil from elsewhere
    unparsed_names: set[str] = field(default_factory=set)        # identifiers in files we could not parse
    classes: dict[str, list["DefFact"]] = field(default_factory=dict)
    subclass_scans: set[str] = field(default_factory=set)

    def in_library(self, rel: str) -> bool:
        return any(not r or rel.startswith(r + "/") for r in self.library_roots)

    def resolve(self, module: str, importer: str) -> set[str]:
        return _resolve(module, importer, self.modules, self.files.keys())


# ------------------------------------------------------------------ helpers

def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (name.startswith("test") or name.endswith(("_test.py", "_tests.py")) or name == "conftest.py"
            or any(p in ("tests", "test", "testing") for p in parts[:-1]))


def _inside_try_import_error(parents: list[ast.AST]) -> bool:
    for p in parents:
        if isinstance(p, ast.Try):
            for h in p.handlers:
                names = []
                if isinstance(h.type, ast.Name):
                    names = [h.type.id]
                elif isinstance(h.type, ast.Tuple):
                    names = [e.id for e in h.type.elts if isinstance(e, ast.Name)]
                elif h.type is None:
                    names = ["*"]
                if {"ImportError", "ModuleNotFoundError", "Exception", "*"} & set(names):
                    return True
    return False


_CODEISH = re.compile(r"[\w.,|\[\]:]+")
DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
ENTRY_POINT = re.compile(r"^\s*[\w.-]+\s*=\s*([\w.]+:[\w.]+)")      # "name = pkg.mod:func" in setup.py
PREFIX = re.compile(r"[A-Za-z]\w{1,}_")
DOCTEST = re.compile(r"^\s*(?:>>>|\.\.\.)\s?(.*)$", re.M)
GLOBALS_CALLS = {"globals", "locals", "vars", "eval", "exec"}
MODULE_SCANS = {"getattr", "hasattr", "vars", "dir", "getmembers"}
TYPING_CALLS = {"cast", "TypeVar", "NewType", "ForwardRef", "TypeAliasType", "assert_type"}


def _string_idents(value: str) -> list[str]:
    """Identifiers inside a string that looks like code: "Foo", "pkg.mod:main",
    "Optional[Foo, Bar]". Prose — docstrings, messages — has spaces between words
    and is ignored, or every "time" in a sentence would keep an unused import alive."""
    stripped = value.strip()
    if not stripped or len(stripped) > 120:
        return []
    squeezed = re.sub(r"\s*([,|\[\]:])\s*", r"\1", stripped)
    if not _CODEISH.fullmatch(squeezed):
        return []
    return IDENT.findall(squeezed)


GENERIC_BUILTINS = {"list", "dict", "set", "frozenset", "tuple", "type"}


def _generic(node: ast.AST) -> bool:
    """Optional[...], list[...], typing.Union[...] — a type, not row["date"] or CONFIG["key"]."""
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return name in GENERIC_BUILTINS or (name[:1].isupper() and not name.isupper())


def _type_string_names(value: str) -> list[str]:
    """Names in a type written as a string: "Literal['a', 'b']", "Annotated[int, Field(gt=0)]"."""
    try:
        tree = ast.parse(value.strip(), mode="eval")
    except SyntaxError:
        return _string_idents(value)
    return [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]


def _decorator_text(d: ast.AST) -> str:
    try:
        return ast.unparse(d)
    except Exception:
        return "<decorator>"


class _Normaliser(ast.NodeTransformer):
    """Renames a function's own locals and parameters so two bodies can be compared by shape.
    Names it does not own — the functions it calls, globals — keep their spelling, so two
    functions that call different helpers are not mistaken for duplicates."""

    def __init__(self, owned: set[str]):
        self.owned = owned
        self.map: dict[str, str] = {}

    def _name(self, n: str) -> str:
        if n not in self.owned:
            return n
        return self.map.setdefault(n, f"v{len(self.map)}")

    def visit_Name(self, node):
        node.id = self._name(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._name(node.arg)
        node.annotation = None
        return node


def _body_hash(fn: ast.AST) -> tuple[str | None, int]:
    fn = copy.deepcopy(fn)
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return None, 0
    owned = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
    for n in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            owned.add(n.id)
    mod = ast.Module(body=body, type_ignores=[])
    mod = _Normaliser(owned).visit(mod)
    nodes = sum(1 for _ in ast.walk(mod))
    dumped = ast.dump(mod, annotate_fields=False, include_attributes=False)
    return hashlib.sha256(dumped.encode()).hexdigest()[:16], nodes


# --------------------------------------------------------------- per file

def file_facts(src: SourceFile, refs: dict[str, list[tuple[str, int]]]) -> FileFacts:
    ff = FileFacts(src=src, is_init=src.rel.endswith("__init__.py"), is_test=_is_test_path(src.rel))
    tree = src.tree
    if tree is None:
        return ff

    noqa_lines = {i + 1 for i, line in enumerate(src.source_lines) if "# noqa" in line or "#noqa" in line}

    # parent chain, so we know whether an import sits inside a try / function / if
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def chain(node):
        out = []
        while node in parents:
            node = parents[node]
            out.append(node)
        return out

    def add_ref(name: str, line: int):
        refs[name].append((src.rel, line))

    # strings count as a use of an import only inside a type annotation (def f(x: "Foo")).
    # Elsewhere — request.args.get("date") — a string is data, not a use of the date import.
    annotation_nodes: set[int] = set()
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            anns += [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs if x.annotation]
            anns += [x.annotation for x in (a.vararg, a.kwarg) if x is not None and x.annotation]
            if node.returns:
                anns.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            anns.append(node.annotation)
        for ann in anns:
            annotation_nodes.update(id(n) for n in ast.walk(ann))
        # cast("Foo", x), TypeVar("T", bound="Foo"), Optional["Foo"] in an alias: types written as strings
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if fname in TYPING_CALLS:
                annotation_nodes.update(id(n) for n in ast.walk(node))
        elif isinstance(node, ast.Subscript) and _generic(node.value):
            annotation_nodes.update(id(n) for n in ast.walk(node.slice))

    for node in ast.walk(tree):
        # ---- imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            ups = chain(node)
            top = all(isinstance(p, ast.Module) for p in ups)
            in_try = _inside_try_import_error(ups)
            for alias in node.names:
                if alias.name == "*":
                    ff.star_imports.append((("." * node.level) + (node.module or ""), node.lineno))
                    continue
                if isinstance(node, ast.Import):
                    bound = alias.asname or alias.name.split(".")[0]
                    module = alias.name
                else:
                    bound = alias.asname or alias.name
                    module = ("." * node.level) + (node.module or "")
                    # "from mod import func" is a use of func, wherever func is defined
                    add_ref(alias.name, node.lineno)
                ff.imports.append(ImportFact(bound=bound, module=module, alias=alias, node=node,
                                             lineno=node.lineno, end_lineno=node.end_lineno or node.lineno,
                                             in_try=in_try, top_level=top, noqa=node.lineno in noqa_lines))

        # ---- every way a name can be referenced
        elif isinstance(node, ast.Name):
            ff.used.add(node.id)
            add_ref(node.id, node.lineno)
        elif isinstance(node, ast.Attribute):
            add_ref(node.attr, node.lineno)
            root, parts = node, []
            while isinstance(root, ast.Attribute):
                parts.append(root.attr)
                root = root.value
            ff.attrs.add(node.attr)
            if isinstance(root, ast.Name):
                ff.used.add(root.id)
                ff.chains.add(".".join([root.id] + parts[::-1]))
            if node.attr == "__subclasses__" and isinstance(node.value, ast.Name):
                ff.subclass_scans.add(node.value.id)
            elif node.attr == "__subclasses__":
                ff.subclass_scans.add("*")
        elif isinstance(node, ast.arg):
            add_ref(node.arg, getattr(node, "lineno", 0))
            ff.params.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            add_ref(node.arg, getattr(node, "lineno", 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # a string is a use of an import only where it is a type: an annotation, cast(),
            # TypeVar(bound=), a subscript, or text with type syntax ("Foo[Bar]", "A | B")
            typed = id(node) in annotation_nodes or "[" in node.value or "|" in node.value
            value = re.sub(r"\(.*\)$", "", node.value.strip())          # "app:create_app()"
            ep = ENTRY_POINT.match(node.value)
            if ep:
                value = ep.group(1)
            if DOTTED.fullmatch(value):
                ff.dotted.append((value, getattr(node, "lineno", 0)))
                for ident in IDENT.findall(value):
                    add_ref(ident, getattr(node, "lineno", 0))
            if ">>>" in node.value:      # doctest examples run with the module's globals
                for code in DOCTEST.findall(node.value):
                    for ident in IDENT.findall(code):
                        ff.used.add(ident)
                        add_ref(ident, getattr(node, "lineno", 0))
            parent = parents.get(node)
            if PREFIX.fullmatch(node.value) and isinstance(parent, (ast.BinOp, ast.JoinedStr, ast.Call)):
                ff.prefixes.add(node.value)
            if typed and id(node) in annotation_nodes:
                for ident in _type_string_names(node.value):
                    ff.used.add(ident)
            for ident in _string_idents(node.value):
                if typed:
                    ff.used.add(ident)
                add_ref(ident, getattr(node, "lineno", 0))   # still protects functions named in strings
        elif isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if fname in DYNAMIC_CALLS:
                ff.dynamic = True
            if fname in GLOBALS_CALLS and not (fname == "vars" and node.args):
                ff.dynamic_globals = True
            computed = any(not isinstance(a, ast.Constant) for a in node.args[1:2])
            # getattr(module, computed) / inspect.getmembers(module) reach into another module
            if fname in MODULE_SCANS and node.args and isinstance(node.args[0], ast.Name) \
                    and (computed or fname in ("dir", "vars", "getmembers")):
                ff.dyn_targets.append(node.args[0].id)
            if fname in MODULE_SCANS and node.args and isinstance(node.args[0], ast.Subscript):
                ff.dynamic_globals = True          # getattr(sys.modules[__name__], ...), getmembers(...)
                ff.dynamic = True
            # pkgutil.iter_modules(pkg.__path__): every module in that package is loaded by name
            if fname in ("iter_modules", "walk_packages"):
                for a in ast.walk(node):
                    if isinstance(a, ast.Attribute) and a.attr == "__path__" and isinstance(a.value, ast.Name):
                        ff.dyn_targets.append(a.value.id + ".*")
            # import_module(f"plugins.{name}"): every module under plugins/
            if fname in ("import_module", "__import__") and node.args and not isinstance(node.args[0], ast.Constant):
                first = node.args[0]
                lead = None
                if isinstance(first, ast.JoinedStr) and first.values and isinstance(first.values[0], ast.Constant):
                    lead = first.values[0].value
                elif isinstance(first, ast.BinOp) and isinstance(first.left, ast.Constant):
                    lead = first.left.value
                if isinstance(lead, str) and "." in lead:
                    ff.dyn_targets.append("pkg:" + lead.rsplit(".", 1)[0])

        # ---- __all__
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                value = node.value
                if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                    for e in value.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            ff.dunder_all.add(e.value)
                        else:
                            ff.all_unknown = True
                elif value is not None:
                    ff.all_unknown = True          # __all__ = base.__all__ + [...]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "__all__":
            for a in node.args:                    # __all__.extend([...]) / .append("x")
                elts = a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a]
                for e in elts:
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        ff.dunder_all.add(e.value)
                    else:
                        ff.all_unknown = True

    # ---- module-level definitions (methods are left alone: they are called through
    # objects and frameworks in ways a static graph cannot see)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decos = [_decorator_text(d) for d in node.decorator_list]
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            df = DefFact(name=node.name, kind="class" if isinstance(node, ast.ClassDef) else "function",
                         node=node, start=start, end=node.end_lineno or node.lineno,
                         decorated=bool(node.decorator_list), decorators=decos)
            if df.kind == "function":
                try:
                    df.body_hash, df.nodes = _body_hash(node)
                except (RecursionError, MemoryError):
                    df.body_hash, df.nodes = None, 0      # too deep to compare; never a duplicate
            else:
                df.bases = [_decorator_text(b).split("[")[0] for b in node.bases]
                df.rel = src.rel
                for kw in node.keywords:
                    if kw.arg == "metaclass":
                        df.metaclass = _decorator_text(kw.value).split(".")[-1]
                df.init_subclass = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                       and n.name == "__init_subclass__" for n in node.body)
            ff.defs.append(df)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    ff.module_vars.add(t.id)
    body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    ff.only_imports = bool(body) and all(
        isinstance(n, (ast.Import, ast.ImportFrom)) or
        (isinstance(n, (ast.Assign, ast.AugAssign)) and "__all__" in ast.unparse(n)) for n in body) \
        and any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in body) and not ff.is_init
    return ff


# ---------------------------------------------------------------- per repo

def _library_roots(root: pathlib.Path) -> list[str]:
    """Every folder that packages a library (a monorepo can hold several). Their public
    names may be used by code outside this repository."""
    out = []
    for marker in ("setup.py", "setup.cfg", "pyproject.toml"):
        for path in root.rglob(marker):
            rel_parts = path.relative_to(root).parts
            if is_skipped(rel_parts[:-1]) or not inside(root, path):
                continue
            if _looks_like_library(path.parent):
                out.append("/".join(rel_parts[:-1]))
    return sorted(set(out))


def _looks_like_library(root: pathlib.Path) -> bool:
    if (root / "setup.py").exists() or (root / "setup.cfg").exists():
        return True
    pp = root / "pyproject.toml"
    if pp.exists() and not pp.is_symlink():
        text = pp.read_text(encoding="utf-8", errors="replace")
        return "[project]" in text or "[tool.poetry]" in text
    return False


def _module_index(rels) -> dict[str, set[str]]:
    """Every dotted name a file could be imported as, whatever folder is on sys.path:
    src/pkg/mod.py answers to src.pkg.mod, pkg.mod and mod."""
    idx: dict[str, set[str]] = defaultdict(set)
    for rel in rels:
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for i in range(len(parts)):
            idx[".".join(parts[i:])].add(rel)
    return idx


def _join(module: str, name: str) -> str:
    return module + name if not module.strip(".") else f"{module}.{name}"


def _resolve(module: str, importer: str, idx, rels) -> set[str]:
    level = len(module) - len(module.lstrip("."))
    name = module.lstrip(".")
    if level == 0:
        if name.split(".")[0] in sys.stdlib_module_names:
            return set()          # "import types" is the standard library, not shared/types.py
        hits = set()
        for rel in idx.get(name, set()):
            # the folder above the match must be a sys.path root, not a package:
            # pkg/shared/types.py answers to "shared.types" only if pkg/ is not itself a package
            depth = name.count(".") + (2 if rel.endswith("/__init__.py") else 1)
            folder = "/".join(rel.split("/")[:-depth])
            if not folder or f"{folder}/__init__.py" not in rels:
                hits.add(rel)
        return hits
    base = importer.split("/")[:-1]
    if level > 1:
        base = base[: max(0, len(base) - (level - 1))]
    path = "/".join(base + (name.split(".") if name else []))
    return {r for r in (f"{path}.py", f"{path}/__init__.py") if r in rels}


def _exports(files: dict[str, FileFacts], others) -> tuple[dict, dict]:
    """Which names other files read out of each module: "from mod import name",
    mod.name through an imported module, "pkg.mod.name" strings (mock.patch, entry
    points, settings), and star imports. This is what "not re-exported" checks."""
    rels = set(files)
    idx = _module_index(rels)
    exported: dict[str, dict[str, str]] = defaultdict(dict)
    star_importers: dict[str, list[str]] = defaultdict(list)

    def mark(targets, name, where):
        for t in targets:
            exported[t].setdefault(name, where)

    def mark_path(full: str, importer: str, where: str):
        """full = "a.b.c.name": the longest prefix that is a module gives the name read from it."""
        level = len(full) - len(full.lstrip("."))
        segs = full.lstrip(".").split(".")
        for k in range(len(segs) - 1, 0, -1):
            targets = _resolve("." * level + ".".join(segs[:k]), importer, idx, rels)
            if targets:
                mark(targets, segs[k], where)
                return

    for rel, ff in files.items():
        aliases: dict[str, str] = {}
        for imp in ff.imports:
            where = f"{rel}:{imp.lineno}"
            if isinstance(imp.node, ast.ImportFrom):
                mark(_resolve(imp.module, rel, idx, rels), imp.alias.name, where)
                aliases[imp.bound] = _join(imp.module, imp.alias.name)     # may be a submodule
            else:
                aliases[imp.bound] = imp.alias.name if imp.alias.asname else imp.bound
        for chain in ff.chains:
            head, _, rest = chain.partition(".")
            if head in aliases and rest:
                mark_path(f"{aliases[head]}.{rest}", rel, f"{rel}: {chain}")
        for text, line in ff.dotted:
            module, _, attr = text.partition(":")
            mark_path(f"{module}.{attr}" if attr else module, rel, f"{rel}:{line}")
        for module, line in ff.star_imports:
            for t in _resolve(module, rel, idx, rels):
                star_importers[t].append(rel)
                for name in ff.used:
                    exported[t].setdefault(name, f"{rel}:{line} (import *)")
    for orel, text in others:
        for lineno, line in enumerate(text.split("\n"), 1):
            for m in DOTTED.finditer(line):
                module, _, attr = m.group(0).partition(":")
                mark_path(f"{module}.{attr}" if attr else module, "", f"{orel}:{lineno}")
    return dict(exported), dict(star_importers)


PROSE_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".markdown")
# in web code only string literals can name a Python function ("handler.process" in a CDK stack,
# url_for('index') in a template); its own variables — {% for b in batches %} — cannot
WEB_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".html", ".htm",
                ".jinja", ".jinja2", ".j2", ".css", ".scss", ".less")
STRING_LIT = re.compile(r"""(["'`])((?:\\.|(?!\1).)*)\1""")


# A YAML key whose list names Python files that a tool loads and calls by function name:
# mkdocs "hooks:", plugin lists. Paths elsewhere — a CI step running "python x.py", a
# linter's per-file ignores, a coverage omit list — run or configure a file; they do not.
PLUGIN_KEYS = {"hooks", "plugins", "extensions"}
PY_PATH = re.compile(r"[\w./-]+\.py\b")
TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


def _config_loaded(rels, others) -> dict[str, str]:
    by_base: dict[str, list[str]] = defaultdict(list)
    for rel in rels:
        by_base[rel.rsplit("/", 1)[-1]].append(rel)
    out = {}
    for orel, text in others:
        if not orel.endswith((".yml", ".yaml")):
            continue
        key = None
        for line in text.split("\n"):
            m = TOP_KEY.match(line)
            if m:
                key = m.group(1)
            if key not in PLUGIN_KEYS:
                continue
            for pm in PY_PATH.finditer(line):
                path = pm.group(0).lstrip("./")
                for rel in by_base.get(path.rsplit("/", 1)[-1], []):
                    if rel == path or rel.endswith("/" + path):
                        out.setdefault(rel, orel)
    return out


def repo_facts(root: pathlib.Path, sources: list[SourceFile]) -> RepoFacts:
    refs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    files = {}
    for s in sources:
        try:
            files[s.rel] = file_facts(s, refs)
        except (RecursionError, MemoryError) as exc:
            # one pathological file (a 250-term expression chain) must not take the scan down:
            # it is recorded as unparsed, and its words still protect the names it mentions
            s.error = f"{exc.__class__.__name__}: too deeply nested to analyse"
            s.tree = None
            files[s.rel] = file_facts(s, refs)

    imported_roots: set[str] = set()
    for ff in files.values():
        for imp in ff.imports:
            mod = imp.module.lstrip(".")
            if mod and not imp.module.startswith("."):
                imported_roots.add(mod.split(".")[0])

    others = other_files(root)
    for rel, text in others:
        prose = rel.lower().endswith(PROSE_SUFFIXES) or "." not in rel.rsplit("/", 1)[-1] and \
            rel.rsplit("/", 1)[-1].upper().startswith(("README", "LICENSE", "CHANGELOG", "AUTHORS", "NOTICE"))
        for lineno, line in enumerate(text.split("\n"), 1):
            # in docs, only code counts: `inline code`, doctest lines, dotted names — not every
            # English word, or "upgrade" in a README would keep every upgrade() alive
            if prose:
                chunks = re.findall(r"`([^`]+)`", line) + DOCTEST.findall(line) \
                    + [m.group(0) for m in DOTTED.finditer(line)]
            elif rel.lower().endswith(WEB_SUFFIXES):
                chunks = [m.group(2) for m in STRING_LIT.finditer(line)]
            else:
                chunks = [line]
            for chunk in chunks:
                for ident in IDENT.findall(chunk):
                    refs[ident].append((rel, lineno))

    exported, star_importers = _exports(files, others)
    modules = _module_index(files)

    # getattr(mod, name) / pkgutil.iter_modules(pkg.__path__) / import_module(f"pkg.{x}") in one
    # file make the target module's definitions reachable by name
    dynamic_files: set[str] = set()
    for rel, ff in files.items():
        aliases = {imp.bound: (_join(imp.module, imp.alias.name) if isinstance(imp.node, ast.ImportFrom)
                               else imp.alias.name) for imp in ff.imports}
        for target in ff.dyn_targets:
            if target.startswith("pkg:"):
                pkg = target[4:]
                hits = _resolve(pkg, rel, modules, files)
            else:
                name, _, star = target.partition(".")
                if name not in aliases:
                    continue
                hits = _resolve(aliases[name], rel, modules, files)
                if not star:
                    dynamic_files |= hits
                    continue
            for h in hits:      # every module in that package
                folder = h.rsplit("/", 1)[0] + "/"
                dynamic_files |= {r for r in files if r.startswith(folder)}

    unparsed_names: set[str] = set()
    for s in sources:
        if s.error:
            unparsed_names.update(IDENT.findall(s.text))
            for lineno, line in enumerate(s.text.split("\n"), 1):
                for ident in IDENT.findall(line):
                    refs[ident].append((s.rel, lineno))

    classes: dict[str, list[DefFact]] = defaultdict(list)
    scans: set[str] = set()
    for ff in files.values():
        scans |= ff.subclass_scans
        for d in ff.defs:
            if d.kind == "class":
                classes[d.name].append(d)

    return RepoFacts(root=root, files=files, refs=dict(refs), imported_roots=imported_roots,
                     other=others, is_library=_looks_like_library(root),
                     total_lines=sum(s.lines for s in sources),
                     exported=exported, star_importers=star_importers,
                     config_loaded=_config_loaded(set(files), others),
                     test_params=set().union(*[ff.params for ff in files.values() if ff.is_test]),
                     modules=modules, library_roots=_library_roots(root), dynamic_files=dynamic_files,
                     unparsed_names=unparsed_names, classes=dict(classes), subclass_scans=scans)
