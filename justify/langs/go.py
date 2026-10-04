"""
Go.

  function     an unexported top-level function (lowercase first letter). Go lets only its own
               package see it, and a package is a directory: every file beside it — tests of
               either package name, other build tags, and the assembly and C files a package
               links against by name — is searched (scope package).
  variable     an unexported package-level `const` or `var` spec that names one thing.
  type         an unexported type.

Never units: methods (an interface can need one without naming it), `init`, `main` and `_`,
and imports (the compiler already refuses an unused one). Kept for judgement, whatever the
count says: a function under a cgo `//export` or a `//go:linkname`, one with no body (it is
written in assembly or linked by name), one another package pulls in with `//go:linkname`, and
a `var` whose value is computed by a call — the call may register something. A const group that
counts with `iota` or repeats an expression is left whole: cutting one line renumbers the rest.
"""

from __future__ import annotations

import pathlib
import re

from . import Pack, Source, Unit, walk, whole_lines

try:
    from tree_sitter_language_pack import get_parser as _get_parser
except Exception:                                        # noqa: BLE001 — the core says when it is missing
    _get_parser = None

SKIP_NAMES = {"init", "main", "_"}
COMMENTS = {"comment"}
# a call in a var's value runs when the package loads; these only build a value
PURE_CALLS = {"errors.New", "fmt.Errorf", "fmt.Sprintf", "fmt.Sprint", "regexp.MustCompile", "strings.NewReplacer",
              "time.Duration", "big.NewInt", "reflect.TypeOf", "template.HTML", "filepath.Join", "path.Join"}
BUILTINS = {"make", "new", "len", "cap", "append", "complex", "real", "imag", "min", "max", "string", "byte", "rune",
            "bool", "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16", "uint32", "uint64",
            "uintptr", "float32", "float64", "complex64", "complex128", "any", "error"}
DIRECTIVE = re.compile(r"^//(export\s+\w+|go:linkname\b|go:generate\b)")
DIRECTIVE_REASON = {"//export": "a cgo `//export` above it: C code calls it by name",
                    "//go:linkname": "a `//go:linkname` above it: linked by name, not called",
                    "//go:generate": "a `//go:generate` directive sits on it"}


class GoPack(Pack):
    langs = ("Go",)
    implemented = True

    def package(self, src: Source, sources: list[Source]) -> list[Source]:
        """Every file in the same directory, not only parsed Go: generated files, other build
        tags, and the .s and .c files that call into the package by name are all in it."""
        here = _dir(src.rel)
        rels = set(src.index.ident) if src.index is not None else {s.rel for s in sources}
        return [Source(rel=r, lang="", data=b"", text="") for r in sorted(rels) if _dir(r) == here]

    def units(self, src: Source) -> list[Unit]:
        out: list[Unit] = []
        for decl in src.tree.root_node.named_children:
            if decl.type == "function_declaration":
                out += self._function(src, decl)
            elif decl.type in ("const_declaration", "var_declaration"):
                out += self._values(src, decl)
            elif decl.type == "type_declaration":
                out += self._types(src, decl)
        return out

    def _function(self, src: Source, decl) -> list[Unit]:
        name = _name(src, decl.child_by_field_name("name"))
        if not _unexported(name):
            return []
        start = leading(src.data, decl, COMMENTS)
        u = Unit(kind="function", name=name, start=start, end=trailing(src.data, decl, COMMENTS), scope="package")
        directive = _directive(src, start, decl.start_byte)
        if directive:
            u.keep = DIRECTIVE_REASON[directive.split()[0]]
        elif decl.child_by_field_name("body") is None:
            u.keep = "it has no body: written in assembly or linked by name"
        else:
            u.keep = _linked_from_elsewhere(src, name)
        return [u]

    def _values(self, src: Source, decl) -> list[Unit]:
        kind = "const" if decl.type == "const_declaration" else "var"
        holder = next((c for c in decl.named_children if c.type == "var_spec_list"), decl)
        specs = [c for c in holder.named_children if c.type in ("const_spec", "var_spec")]
        grouped = any(c.type == "(" for c in holder.children)
        if kind == "const" and grouped and any(_counts(src, s) for s in specs):
            return []                          # iota or a repeated expression: lines depend on their place
        out = []
        for spec in specs:
            names = [c for i, c in enumerate(spec.children) if spec.field_name_for_child(i) == "name"]
            if len(names) != 1:
                continue                       # `var a, b = f()` is cut together or not at all
            name = _name(src, names[0])
            if not _unexported(name):
                continue
            node = spec if grouped else decl
            start = leading(src.data, node, COMMENTS)
            u = Unit(kind="variable", name=name, start=start, end=trailing(src.data, node, COMMENTS),
                     scope="package")
            directive = _directive(src, start, node.start_byte)
            if directive:
                u.keep = DIRECTIVE_REASON[directive.split()[0]]
            elif kind == "var" and _runs_code(src, spec.child_by_field_name("value")):
                u.keep = "its value is computed by a call when the package loads, which may do something"
            else:
                u.keep = _linked_from_elsewhere(src, name)
            out.append(u)
        return out

    def _types(self, src: Source, decl) -> list[Unit]:
        specs = [c for c in decl.named_children if c.type in ("type_spec", "type_alias")]
        grouped = any(c.type == "(" for c in decl.children)
        out = []
        for spec in specs:
            name = _name(src, spec.child_by_field_name("name"))
            if not _unexported(name):
                continue
            node = spec if grouped else decl
            start = leading(src.data, node, COMMENTS)
            out.append(Unit(kind="type", name=name, start=start, end=trailing(src.data, node, COMMENTS),
                            scope="package"))
        return out

    def tidy(self, data: bytes) -> bytes:
        """Imports the cut left unused, and groups it left empty. Go refuses to compile an unused
        import, so dropping one never changes what a program does. Only imports whose name is
        certain are touched: an explicit alias, or a standard-library path."""
        if _get_parser is None:
            return data
        tree = _get_parser("go").parse(data)
        root = tree.root_node
        used = {data[n.start_byte:n.end_byte] for n in walk(root)
                if n.type in ("identifier", "package_identifier", "type_identifier")
                and not _within(n, "import_declaration")}
        spans = []
        for decl in root.named_children:
            if decl.type == "import_declaration":
                specs = [n for n in walk(decl) if n.type == "import_spec"]
                dead = [s for s in specs if _import_unused(data, s, used)]
                if dead and len(dead) == len(specs):
                    spans.append((decl.start_byte, decl.end_byte))
                else:
                    spans += [(s.start_byte, s.end_byte) for s in dead]
            elif decl.type in ("const_declaration", "var_declaration", "type_declaration"):
                if not any(n.type in ("const_spec", "var_spec", "type_spec", "type_alias") for n in walk(decl)):
                    spans.append((decl.start_byte, decl.end_byte))      # `var ()` left by a cut
        for a, b in sorted(spans, reverse=True):
            a, b = _line_span(data, a, b)
            data = data[:a] + data[b:]
        return data


# ---------------------------------------------------------------- helpers shared by the packs

INNER = (b"//!", b"/*!")                # a Rust doc comment for the enclosing module, not the next item


def leading(data: bytes, node, kinds: set[str]) -> int:
    """Where `node` starts once the comments (or attributes) stacked directly on it are counted
    as its own: each on a line of its own, no blank line in between. A doc comment names what
    it documents; it is part of the unit, not a use of it."""
    start = node.start_byte
    prev = node.prev_sibling
    while prev is not None and prev.type in kinds and data[prev.start_byte:prev.start_byte + 3] not in INNER:
        end = prev.end_byte - (1 if data[prev.end_byte - 1:prev.end_byte] == b"\n" else 0)
        if data[end:start].count(b"\n") > 1 or data[data.rfind(b"\n", 0, prev.start_byte) + 1:prev.start_byte].strip():
            break
        start = prev.start_byte
        prev = prev.prev_sibling
    return start


def trailing(data: bytes, node, kinds: set[str]) -> int:
    """Where `node` ends once a comment after it on the same line is counted as its own."""
    nxt = node.next_sibling
    if nxt is not None and nxt.type in kinds and b"\n" not in data[node.end_byte:nxt.start_byte]:
        return nxt.end_byte - (1 if data[nxt.end_byte - 1:nxt.end_byte] == b"\n" else 0)
    return node.end_byte


def _dir(rel: str) -> str:
    return str(pathlib.PurePosixPath(rel).parent)


def _name(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace") if node is not None else ""


def _unexported(name: str) -> bool:
    return bool(name) and name not in SKIP_NAMES and not name[0].isupper()


def _directive(src: Source, start: int, end: int) -> str:
    for line in src.data[start:end].decode("utf-8", "replace").splitlines():
        m = DIRECTIVE.match(line.strip())
        if m:
            return m.group(0)
    return ""


def _counts(src: Source, spec) -> bool:
    """A const spec that only makes sense in its place: no value (it repeats the line above),
    or a value that uses iota."""
    value = spec.child_by_field_name("value")
    return value is None or any(n.type == "iota" or _name(src, n) == "iota" for n in walk(value))


def _runs_code(src: Source, value) -> bool:
    """True when evaluating `value` calls something that is not plainly a value builder.
    A function literal's body only runs when called, so it is not looked into."""
    if value is None:
        return False
    stack = [value]
    while stack:
        n = stack.pop()
        if n.type == "func_literal":
            continue
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is None or _name(src, fn) not in PURE_CALLS | BUILTINS:
                return True
        stack.extend(n.children)
    return False


LINKNAME = r"//go:linkname\s+\S+\s+\S*[./]{name}\b"


def _linked_from_elsewhere(src: Source, name: str) -> str:
    """Another package can pull an unexported function in by name with `//go:linkname local
    path.name`. That file is outside the package, so the package search never sees it."""
    if src.index is None:
        return ""
    pattern = re.compile(LINKNAME.format(name=re.escape(name)))
    holders = src.index.files_with(name)
    if any(s.rel in holders and pattern.search(s.text) for s in src.sources if s.rel.endswith(".go")):
        return "a package elsewhere links to it by name (`//go:linkname`)"
    return ""


def _within(node, kind: str) -> bool:
    p = node.parent
    while p is not None:
        if p.type == kind:
            return True
        p = p.parent
    return False


STD_PATH = re.compile(r"^[a-z][a-z0-9]*(/[a-z][a-z0-9]*)*$")


def _import_unused(data: bytes, spec, used: set[bytes]) -> bool:
    name = spec.child_by_field_name("name")
    path = spec.child_by_field_name("path")
    if path is None:
        return False
    p = data[path.start_byte:path.end_byte].strip(b"\"`").decode("utf-8", "replace")
    if name is not None:
        if name.type != "package_identifier":
            return False                        # `_` and `.` imports are there for their effect
        return data[name.start_byte:name.end_byte] not in used
    if p == "C" or not STD_PATH.match(p) or "." in p.split("/")[0]:
        return False                            # not the standard library: its name is a guess
    last = p.rsplit("/", 1)[-1]
    names = {last}
    if re.fullmatch(r"v\d+", last) and "/" in p:
        names.add(p.rsplit("/", 2)[-2])         # math/rand/v2 is package rand
    return not any(n.encode() in used for n in names)


def _line_span(data: bytes, a: int, b: int) -> tuple[int, int]:
    return whole_lines(data, a, b) or (a, b)


PACK = GoPack()
