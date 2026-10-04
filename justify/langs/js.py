"""
JavaScript and TypeScript, JSX and TSX (React).

  import       a binding nothing in the file uses — `import x`, `import {a}`, `import * as ns`,
               `const x = require(...)`, `const {a, b} = require(...)`. A bare `import './styles.css'`
               binds nothing and is never touched: it is there for its side effect. For the same
               reason, cutting the last binding of a JavaScript import or require keeps the load
               (`import 'x'`, `require('x')`) unless the module is one of Node's own; TypeScript drops
               an import nobody uses when it compiles, so there the whole statement goes.
  function / class / type
               a top-level declaration. Exported: anything could use it — judged, never removed.

How far a top-level name reaches depends on what the file is:

  a module (import or export, .mjs/.cjs/.mts/.cts, CommonJS exports)      its file
  a script that calls require() and exports nothing                       the repository: it may be
               a page script with Node built in (Electron, NW.js), whose names are the page's globals
  a plain script (no import, export or require), UMD or AMD               anywhere: a global, judged

Kept, whatever the count says: the JSX factory in a file with JSX — `React`, `h`, a `@jsx` pragma's
name, or a name a Babel, TypeScript or bundler config gives as its pragma — since the transform calls
it without the file naming it; a decorated class (the decorator may register it) or one with a static
block (it runs when the class is defined); every name in a file that calls eval; a statement whose
removal would join the lines around it under automatic semicolon insertion; default exports; every
export of a file a framework or tool calls by its place — pages, routes, configs, tests, stories,
entry files. `.d.ts` files only describe other code and are not judged. A name the word index cannot
see (non-ASCII) is never named.
"""

from __future__ import annotations

import re

from . import IDENT, Pack, Source, Unit, walk, whole_lines

JSX = {"jsx_element", "jsx_self_closing_element", "jsx_fragment"}
DECL = {"function_declaration": "function", "generator_function_declaration": "function",
        "class_declaration": "class", "abstract_class_declaration": "class",
        "interface_declaration": "type", "type_alias_declaration": "type", "enum_declaration": "type"}
FUNC_VALUES = {"arrow_function": "function", "function_expression": "function", "function": "function",
               "generator_function": "function", "class": "class", "class_expression": "class"}
ENTRY = re.compile(r"(^|/)(pages|app|routes|api|bin|scripts|e2e|cypress|playwright|__tests__|__mocks__|test|tests)/"
                   r"|\.(config|conf|test|spec|stories|story|setup|d)\.[cm]?[jt]sx?$"
                   r"|(^|/)(index|main|server|app|cli|worker|sw|service-worker|vite-env|setupTests)\.[cm]?[jt]sx?$")
DECLARATION_FILE = re.compile(r"\.d\.[cm]?ts$")
PRAGMA = re.compile(r"@jsx(?:Frag)?\s+([A-Za-z_$][\w$]*)")
# words a Babel, SWC, esbuild or TypeScript config names its JSX factory with
FACTORY_WORDS = ("pragma", "pragmaFrag", "jsxPragma", "jsxPragmaFrag", "jsxFactory", "jsxFragmentFactory",
                 "jsxFragment", "jsxInject")
MODULE_SUFFIX = (".mjs", ".cjs", ".mts", ".cts")
UMD = re.compile(r"\btypeof\s+(?:module|exports|define|require)\b|\bdefine\.amd\b|^\s*define\s*\(", re.M)
CJS_EXPORT = re.compile(r"^\s*(?:module\.exports|exports)\s*[.\[=]|\bObject\.(?:defineProperty|assign)\(\s*(?:module\.)?exports\b")
NODE_BUILTINS = frozenset(
    "assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns "
    "domain events fs http http2 https inspector module net os path perf_hooks process punycode querystring "
    "readline repl stream string_decoder sys timers tls trace_events tty url util v8 vm wasi worker_threads "
    "zlib".split())
ASI_STARTS = tuple(b"([`+-/")
# statements after which a line starting with ( or [ always begins a new statement
ENDS_CLEANLY = {"import_statement", "function_declaration", "generator_function_declaration", "class_declaration",
                "abstract_class_declaration", "interface_declaration", "enum_declaration", "if_statement",
                "for_statement", "for_in_statement", "while_statement", "do_statement", "try_statement",
                "switch_statement", "statement_block", "function_signature"}


class JsPack(Pack):
    langs = ("JavaScript", "TypeScript")
    implemented = True

    def entry(self, rel: str) -> bool:
        return bool(ENTRY.search(rel))

    def units(self, src: Source) -> list[Unit]:
        if DECLARATION_FILE.search(src.rel):
            return []
        root = src.tree.root_node
        seen = _seen(src, root)
        reach = _reach(src, root, seen)
        out: list[Unit] = []
        for stmt in root.named_children:
            if stmt.type == "import_statement":
                out += self._imports(src, stmt, seen)
            elif stmt.type in ("lexical_declaration", "variable_declaration"):
                out += self._requires(src, stmt, reach)
                out += self._declarations(src, stmt, reach, cut_node=stmt)
            elif stmt.type in DECL:
                out += self._declarations(src, stmt, reach, cut_node=stmt)
            elif stmt.type == "export_statement":
                if any(c.type == "default" for c in stmt.children):
                    continue                                 # a default export is used by any name
                decl = stmt.child_by_field_name("declaration")
                if decl is not None:
                    out += self._declarations(src, decl, "public", cut_node=stmt)
        out = [u for u in out if IDENT.fullmatch(u.name)]    # the word index cannot see any other name
        if "eval" in seen:
            for u in out:
                u.keep = u.keep or "the file calls eval, which can reach any name in it"
        return out

    # ---------------------------------------------------------------- imports
    def _imports(self, src: Source, stmt, seen: set[str]) -> list[Unit]:
        clause = next((c for c in stmt.named_children if c.type == "import_clause"), None)
        if clause is None:
            return []                                    # `import 'x'`: a side effect, never a binding
        bindings = []                                    # (name node, the cut that removes it from the list)
        for c in clause.named_children:
            if c.type == "identifier":                   # default import
                bindings.append((c, _item(src, c)))
            elif c.type == "namespace_import":
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is not None:
                    bindings.append((ident, _item(src, c)))
            elif c.type == "named_imports":
                for spec in c.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                    if name is not None:
                        bindings.append((name, _item(src, spec)))
        source = stmt.child_by_field_name("source")
        type_only = any(c.type == "type" for c in stmt.children)
        out = []
        for name_node, cut in bindings:
            name = _txt(src, name_node)
            keep = ""
            if len(bindings) == 1:
                if src.lang == "TypeScript" or type_only or source is None or _builtin(_module(src, source)):
                    cut, keep = _statement(src, stmt)
                else:
                    cut = (clause.start_byte, source.start_byte)      # `import x from 'y'` → `import 'y'`
            u = Unit(kind="import", name=name, start=stmt.start_byte, end=stmt.end_byte, scope="file",
                     cut=cut, keep=keep)
            if _factory(src, name, seen):
                u.keep = "JSX in this file may need it in scope without naming it (the classic JSX transform)"
            out.append(u)
        return out

    def _requires(self, src: Source, stmt, reach: str) -> list[Unit]:
        """`const x = require('y')` and `const {a, b: c, d = 1} = require('y')`."""
        declarators = [d for d in stmt.named_children if d.type == "variable_declarator"]
        if len(declarators) != 1:
            return []
        d = declarators[0]
        value = d.child_by_field_name("value")
        if value is None or value.type != "call_expression":
            return []
        fn = value.child_by_field_name("function")
        if fn is None or src.data[fn.start_byte:fn.end_byte] != b"require":
            return []
        target = d.child_by_field_name("name")
        if target is None:
            return []

        def lone() -> tuple[tuple[int, int], str]:
            args = value.child_by_field_name("arguments")
            spec = next((a for a in (args.named_children if args is not None else []) if a.type == "string"), None)
            if spec is not None and _builtin(_module(src, spec)):
                return _statement(src, stmt)
            return (stmt.start_byte, value.start_byte), ""        # `const x = require('y')` → `require('y')`

        out = []
        if target.type == "identifier":
            cut, keep = lone()
            out.append(Unit(kind="import", name=_txt(src, target), start=stmt.start_byte, end=stmt.end_byte,
                            scope=reach, cut=cut, keep=keep))
        elif target.type == "object_pattern":
            items = [p for p in target.named_children if p.type != "comment"]
            for p in items:
                ident = _pattern_name(p)
                if ident is None:
                    continue
                cut, keep = lone() if len(items) == 1 else (_item(src, p), "")
                out.append(Unit(kind="import", name=_txt(src, ident), start=stmt.start_byte, end=stmt.end_byte,
                                scope=reach, cut=cut, keep=keep))
        return out

    # ---------------------------------------------------------------- declarations
    def _declarations(self, src: Source, node, scope: str, cut_node) -> list[Unit]:
        if node.type in DECL:
            name = node.child_by_field_name("name")
            if name is None:
                return []
            kind, body = DECL[node.type], node
        elif node.type in ("lexical_declaration", "variable_declaration"):
            declarators = [d for d in node.named_children if d.type == "variable_declarator"]
            if len(declarators) != 1:
                return []                                # `const a = …, b = …` is cut together or not at all
            d = declarators[0]
            name = d.child_by_field_name("name")
            body = d.child_by_field_name("value")
            if name is None or body is None or name.type != "identifier":
                return []
            kind = FUNC_VALUES.get(body.type)
            if kind is None:
                return []                                # a value with side effects is never a candidate
        else:
            return []
        cut, keep = _statement(src, cut_node) if scope != "public" else ((cut_node.start_byte, cut_node.end_byte), "")
        if kind == "class":
            keep = _class_keep(body) or keep
        return [Unit(kind=kind, name=_txt(src, name), start=cut_node.start_byte, end=cut_node.end_byte,
                     scope=scope, cut=cut, keep=keep)]


def _seen(src: Source, root) -> set[str]:
    """What the file does anywhere in it that changes how its names are judged: JSX, eval, require."""
    out = set()
    for n in walk(root):
        if n.type in JSX:
            out.add("jsx")
        elif n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" and src.data[fn.start_byte:fn.end_byte] in (b"eval", b"require"):
                out.add(_txt(src, fn))
    return out


def _reach(src: Source, root, seen: set[str]) -> str:
    """How far the file's top-level names can be seen from — see the module docstring."""
    if src.rel.endswith(MODULE_SUFFIX) or any(n.type in ("import_statement", "export_statement")
                                               for n in root.named_children):
        return "file"
    if UMD.search(src.text):
        return "public"                                  # in a browser, a UMD or AMD file is a plain script
    if any(n.type == "expression_statement" and CJS_EXPORT.search(_txt(src, n)) for n in root.named_children):
        return "file"                                    # CommonJS: Node wraps the file in a function
    if "require" in seen:
        return "repo"
    return "public"


def _factory(src: Source, name: str, seen: set[str]) -> bool:
    """Whether the JSX transform may call this name without the file naming it."""
    if name in PRAGMA.findall(src.text):
        return True
    if "jsx" not in seen:
        return False
    if name in ("React", "h"):
        return True
    index = src.index
    if index is None:
        return False
    configs = set().union(*(index.files_with(w) for w in FACTORY_WORDS)) - {src.rel}
    return any(index.count(f, name) for f in configs)


def _class_keep(node) -> str:
    """A reason a class is more than its name: something runs when it is defined."""
    if any(c.type == "decorator" for c in node.children):
        return "a decorator may register it — frameworks find classes that way"
    body = node.child_by_field_name("body")
    if body is not None and any(c.type == "class_static_block" for c in body.named_children):
        return "its static block runs when the class is defined"
    return ""


def _statement(src: Source, stmt) -> tuple[tuple[int, int], str]:
    """The cut for a whole statement, and a reason to keep it when cutting would join its neighbours:
    with no semicolon before it, a next line starting with ( [ ` + - / would continue the line above."""
    cut = (stmt.start_byte, stmt.end_byte)
    prev, nxt = stmt.prev_named_sibling, stmt.next_named_sibling
    while prev is not None and prev.type == "comment":
        prev = prev.prev_named_sibling
    while nxt is not None and nxt.type == "comment":
        nxt = nxt.next_named_sibling
    if prev is None or nxt is None or src.data[nxt.start_byte] not in ASI_STARTS:
        return cut, ""
    if prev.type in ENDS_CLEANLY or src.data[prev.start_byte:prev.end_byte].rstrip().endswith(b";"):
        return cut, ""
    return cut, "cutting it would join the lines around it, which no semicolon separates"


def _pattern_name(p):
    """The binding a destructuring item makes: `a`, `b: a`, `a = 1`."""
    if p.type == "shorthand_property_identifier_pattern":
        return p
    if p.type == "pair_pattern":
        v = p.child_by_field_name("value")
        return v if v is not None and v.type == "identifier" else None
    if p.type == "object_assignment_pattern":
        left = p.child_by_field_name("left")
        return left if left is not None and left.type in ("shorthand_property_identifier_pattern", "identifier") else None
    return None


def _module(src: Source, string_node) -> str:
    return _txt(src, string_node).strip("'\"`")


def _builtin(spec: str) -> bool:
    """One of Node's own modules: loading it does nothing a program can see."""
    return spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS


def _txt(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _item(src: Source, node) -> tuple[int, int]:
    """A list item and the comma after it, so cutting it leaves a valid list. The last item has no comma
    of its own and goes alone, leaving a trailing comma (valid, and tidied), so that two neighbours cut
    together never both claim the comma between them."""
    nxt = node.next_sibling
    if nxt is None or nxt.type != ",":
        return node.start_byte, node.end_byte
    if whole_lines(src.data, node.start_byte, nxt.end_byte):
        return node.start_byte, nxt.end_byte             # an item on its own line: the core takes the line
    after = nxt.next_sibling
    return node.start_byte, (after.start_byte if after is not None else nxt.end_byte)


EMPTY_TYPE_IMPORT = re.compile(rb"^[ \t]*import\s+type\s*\{\s*\}\s*from\s*(['\"])[^'\"]*\1[ \t]*;?[ \t]*\r?\n?", re.M)
EMPTY_AFTER_DEFAULT = re.compile(rb",\s*\{\s*\}(\s*from\b)")
LONE_COMMA = re.compile(rb"(\bimport\s+[\w$]+)\s*,\s*(from\s*['\"])")
EMPTY_IMPORT = re.compile(rb"\bimport\s+(?:\{\s*\}\s*)?from\s*(['\"][^'\"]+['\"])")
TRAILING_COMMA = re.compile(rb"(\bimport\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?\{[^{}]*?),[ \t]*\}"
                            rb"|(\b(?:const|let|var)\s*\{[^{}]*?),[ \t]*\}(?=\s*(?::[^=]*)?=\s*require\b)")
EMPTY_REQUIRE = re.compile(rb"\b(?:const|let|var)\s*\{\s*\}\s*=\s*(require\s*\()")


def _tidy(data: bytes) -> bytes:
    """What cuts leave behind, made tidy without changing what the code does:
    `import React, {} from 'x'` → `import React from 'x'`; `import {} from 'x'` → `import 'x'` and
    `const {} = require('x')` → `require('x')` (the module still loads, so any side effect it has is
    kept); `import type {} from 'x'` goes (it never loads anything); `{ a, }` → `{ a }`."""
    data = EMPTY_TYPE_IMPORT.sub(b"", data)
    data = TRAILING_COMMA.sub(lambda m: (m.group(1) or m.group(2)) + b" }", data)
    data = EMPTY_AFTER_DEFAULT.sub(rb"\1", data)
    data = LONE_COMMA.sub(rb"\1 \2", data)
    data = EMPTY_IMPORT.sub(rb"import \1", data)
    return EMPTY_REQUIRE.sub(rb"\1", data)


JsPack.tidy = staticmethod(_tidy)
PACK = JsPack()
