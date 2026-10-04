"""
JavaScript and TypeScript, JSX and TSX (React).

  import       a binding nothing in the file uses — `import x`, `import {a}`, `import * as ns`,
               `const x = require(...)`. A bare `import './styles.css'` binds nothing and is
               never touched: it is there for its side effect.
  function / class / variable / type
               a top-level declaration. Not exported: only the file can use it (scope file).
               Exported: anything could — judged, never removed from here.

Kept, whatever the count says: `React` (and a `@jsx` pragma's name) in a file with JSX, since
the classic JSX transform needs it in scope without naming it; default exports; every export of
a file a framework or tool calls by its place — pages, routes, configs, tests, stories, entry
files. `.d.ts` files only describe other code and are not judged.
"""

from __future__ import annotations

import re

from . import Pack, Source, Unit, walk

JSX = {"jsx_element", "jsx_self_closing_element", "jsx_fragment"}
DECL = {"function_declaration": "function", "generator_function_declaration": "function",
        "class_declaration": "class", "abstract_class_declaration": "class",
        "interface_declaration": "type", "type_alias_declaration": "type", "enum_declaration": "type"}
FUNC_VALUES = {"arrow_function": "function", "function_expression": "function", "function": "function",
               "generator_function": "function", "class": "class", "class_expression": "class"}
ENTRY = re.compile(r"(^|/)(pages|app|routes|api|bin|scripts|e2e|cypress|playwright|__tests__|__mocks__|test|tests)/"
                   r"|\.(config|conf|test|spec|stories|story|setup|d)\.[cm]?[jt]sx?$"
                   r"|(^|/)(index|main|server|app|cli|worker|sw|service-worker|vite-env|setupTests)\.[cm]?[jt]sx?$")
PRAGMA = re.compile(r"@jsx(?:Frag)?\s+([A-Za-z_$][\w$]*)")
COMMONJS = re.compile(r"\brequire\s*\(|\bmodule\.exports\b|\bexports\.\w")
MODULE_SUFFIX = (".mjs", ".cjs", ".mts", ".cts")


class JsPack(Pack):
    langs = ("JavaScript", "TypeScript")
    implemented = True

    def entry(self, rel: str) -> bool:
        return bool(ENTRY.search(rel))

    def units(self, src: Source) -> list[Unit]:
        if src.rel.endswith(".d.ts"):
            return []
        root = src.tree.root_node
        has_jsx = any(n.type in JSX for n in walk(root))
        jsx_names = {"React"} | set(PRAGMA.findall(src.text)) if has_jsx else set(PRAGMA.findall(src.text))
        # In a module, top-level names belong to the file. In a plain script (no import, export or
        # require) they are globals that any other script or page can call — so they are searched
        # for everywhere, like exports.
        module = (src.rel.endswith(MODULE_SUFFIX) or bool(COMMONJS.search(src.text))
                  or any(n.type in ("import_statement", "export_statement") for n in root.named_children))
        local = not module
        out: list[Unit] = []
        for stmt in root.named_children:
            if stmt.type == "import_statement":
                out += self._imports(src, stmt, jsx_names)
            elif stmt.type in ("lexical_declaration", "variable_declaration"):
                out += self._requires(src, stmt)
                out += self._declarations(src, stmt, exported=local, cut_node=stmt)
            elif stmt.type in DECL:
                out += self._declarations(src, stmt, exported=local, cut_node=stmt)
            elif stmt.type == "export_statement":
                if any(c.type == "default" for c in stmt.children):
                    continue                                 # a default export is used by any name
                decl = stmt.child_by_field_name("declaration")
                if decl is not None:
                    out += self._declarations(src, decl, exported=True, cut_node=stmt)
        return out

    # ---------------------------------------------------------------- imports
    def _imports(self, src: Source, stmt, jsx_names: set[str]) -> list[Unit]:
        clause = next((c for c in stmt.named_children if c.type == "import_clause"), None)
        if clause is None:
            return []                                    # `import 'x'`: a side effect, never a binding
        bindings = []                                    # (name node, span node, cut)
        for c in clause.named_children:
            if c.type == "identifier":                   # default import
                bindings.append((c, c, _with_comma(c)))
            elif c.type == "namespace_import":
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is not None:
                    bindings.append((ident, c, _with_comma(c)))
            elif c.type == "named_imports":
                for spec in c.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                    if name is not None:
                        bindings.append((name, spec, _with_comma(spec)))
        out = []
        group = (stmt.start_byte, stmt.end_byte)
        for name_node, span_node, cut in bindings:
            name = src.data[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
            u = Unit(kind="import", name=name, start=stmt.start_byte, end=stmt.end_byte, scope="file",
                     cut=group if len(bindings) == 1 else cut)
            if name in jsx_names:
                u.keep = "JSX in this file may need it in scope without naming it (the classic React transform)"
            out.append(u)
        return out

    def _requires(self, src: Source, stmt) -> list[Unit]:
        """`const x = require('y')` and `const {a, b} = require('y')`."""
        out = []
        declarators = [d for d in stmt.named_children if d.type == "variable_declarator"]
        if len(declarators) != 1:
            return out
        d = declarators[0]
        value = d.child_by_field_name("value")
        if value is None or value.type != "call_expression":
            return out
        fn = value.child_by_field_name("function")
        if fn is None or src.data[fn.start_byte:fn.end_byte] != b"require":
            return out
        target = d.child_by_field_name("name")
        if target is None:
            return out
        if target.type == "identifier":
            name = src.data[target.start_byte:target.end_byte].decode("utf-8", "replace")
            out.append(Unit(kind="import", name=name, start=stmt.start_byte, end=stmt.end_byte, scope="file",
                            cut=(stmt.start_byte, stmt.end_byte)))
        elif target.type == "object_pattern":
            props = [p for p in target.named_children
                     if p.type in ("shorthand_property_identifier_pattern", "pair_pattern")]
            for p in props:
                ident = p if p.type == "shorthand_property_identifier_pattern" else p.child_by_field_name("value")
                if ident is None or ident.type not in ("identifier", "shorthand_property_identifier_pattern"):
                    continue
                name = src.data[ident.start_byte:ident.end_byte].decode("utf-8", "replace")
                out.append(Unit(kind="import", name=name, start=stmt.start_byte, end=stmt.end_byte, scope="file",
                                cut=(stmt.start_byte, stmt.end_byte) if len(props) == 1 else _with_comma(p)))
        return out

    # ---------------------------------------------------------------- declarations
    def _declarations(self, src: Source, node, exported: bool, cut_node) -> list[Unit]:
        scope = "public" if exported else "file"
        cut = (cut_node.start_byte, cut_node.end_byte)
        if node.type in DECL:
            name = node.child_by_field_name("name")
            if name is None:
                return []
            kind = DECL[node.type]
            return [Unit(kind=kind, name=_txt(src, name), start=cut_node.start_byte, end=cut_node.end_byte,
                         scope=scope, cut=cut)]
        if node.type not in ("lexical_declaration", "variable_declaration"):
            return []
        declarators = [d for d in node.named_children if d.type == "variable_declarator"]
        if len(declarators) != 1:
            return []                                    # `const a = …, b = …` is cut together or not at all
        d = declarators[0]
        name = d.child_by_field_name("name")
        value = d.child_by_field_name("value")
        if name is None or value is None or name.type != "identifier":
            return []
        kind = FUNC_VALUES.get(value.type)
        if kind is None:
            return []                                    # a value with side effects is never a candidate
        return [Unit(kind=kind, name=_txt(src, name), start=cut_node.start_byte, end=cut_node.end_byte,
                     scope=scope, cut=cut)]


def _txt(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _with_comma(node) -> tuple[int, int]:
    """The node and the comma that joins it to a neighbour, so cutting it leaves a valid list."""
    nxt = node.next_sibling
    if nxt is not None and nxt.type == ",":
        after = nxt.next_sibling
        return node.start_byte, (after.start_byte if after is not None else nxt.end_byte)
    prev = node.prev_sibling
    if prev is not None and prev.type == ",":
        return prev.start_byte, node.end_byte
    return node.start_byte, node.end_byte


EMPTY_AFTER_DEFAULT = re.compile(rb",\s*\{\s*\}(\s*from\b)")
EMPTY_IMPORT = re.compile(rb"\bimport\s+(?:type\s+)?\{\s*\}\s*from\s*(['\"][^'\"]+['\"])")


def _tidy(data: bytes) -> bytes:
    """`import React, {} from 'x'` → `import React from 'x'`; `import {} from 'x'` → `import 'x'`
    (the module still loads, so any side effect it has is kept)."""
    data = EMPTY_AFTER_DEFAULT.sub(rb"\1", data)
    return EMPTY_IMPORT.sub(rb"import \1", data)


JsPack.tidy = staticmethod(_tidy)
PACK = JsPack()
