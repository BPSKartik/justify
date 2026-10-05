"""
HTML pages: the stylesheets and scripts written into them. A page itself is never a unit — it is
where classes are used, and the core reads every page, template and script for their names.

  <style>     its rules and custom properties, exactly as in a .css file (see css.py), with the
              page outside its <style> blocks counting as markup.
  <script>    an inline script's top-level functions and classes. In a module script
              (type="module") they belong to the page: scope file. In a classic script they are
              globals any other script or an onclick="…" can call: public, judged, never removed.
              A script with src=, or of a type that is not JavaScript (a template, JSON), is not read.
"""

from __future__ import annotations

from . import Pack, Source, Unit, error_count
from .css import judge, rules

JS_TYPES = {"", "text/javascript", "application/javascript", "application/ecmascript", "text/ecmascript",
            "module"}
_PARSERS: dict = {}
# an inline script's declarations, as js.py reads them in a file (copied: packs stand alone)
DECL = {"function_declaration": "function", "generator_function_declaration": "function",
        "class_declaration": "class"}
FUNC_VALUES = {"arrow_function": "function", "function_expression": "function", "function": "function",
               "generator_function": "function", "class": "class"}
ASI_STARTS = tuple(b"([`+-/")
ENDS_CLEANLY = {"import_statement", "function_declaration", "generator_function_declaration", "class_declaration",
                "if_statement", "for_statement", "for_in_statement", "while_statement", "do_statement",
                "try_statement", "switch_statement", "statement_block"}


def _parse(grammar: str, data: bytes):
    if grammar not in _PARSERS:
        from tree_sitter_language_pack import get_parser
        _PARSERS[grammar] = get_parser(grammar)
    return _PARSERS[grammar].parse(data)


class HtmlPack(Pack):
    langs = ("HTML",)
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        styles: list[Unit] = []
        sheets: list[tuple[int, int]] = []
        scripts: list[Unit] = []
        for el in _elements(src.tree.root_node):
            body = next((c for c in el.children if c.type == "raw_text"), None)
            if body is None:
                continue
            attrs = _attributes(src, el)
            data = src.data[body.start_byte:body.end_byte]
            if el.type == "style_element":
                if attrs.get("type", "text/css").lower() != "text/css":
                    continue
                tree = _parse("css", data)
                if error_count(tree):
                    continue                             # a template tag in the CSS, say: not judged
                sheets.append((body.start_byte, body.end_byte))
                styles += rules(data, tree.root_node, base=body.start_byte)
            elif "src" not in attrs and attrs.get("type", "").lower() in JS_TYPES:
                scripts += _script(src, data, body.start_byte, module=attrs.get("type", "").lower() == "module")
        return judge(src, styles, sheets) + scripts


def _elements(root):
    """Every <style> and <script> element, wherever it sits in the page."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in ("style_element", "script_element"):
            yield n
        else:
            stack.extend(reversed(n.children))


def _attributes(src: Source, el) -> dict[str, str]:
    tag = next((c for c in el.children if c.type == "start_tag"), None)
    out = {}
    for a in (tag.named_children if tag is not None else ()):
        if a.type != "attribute":
            continue
        name = next((c for c in a.named_children if c.type == "attribute_name"), None)
        value = next((c for c in a.named_children if c.type in ("attribute_value", "quoted_attribute_value")), None)
        if value is not None and value.type == "quoted_attribute_value":
            value = next((c for c in value.named_children if c.type == "attribute_value"), None)
        if name is not None:
            key = src.data[name.start_byte:name.end_byte].decode("utf-8", "replace").lower()
            out[key] = src.data[value.start_byte:value.end_byte].decode("utf-8", "replace").strip() if value else ""
    return out


def _script(src: Source, data: bytes, base: int, module: bool) -> list[Unit]:
    """Top-level declarations of an inline script, found the way js.py finds them in a file."""
    tree = _parse("javascript", data)
    if error_count(tree):
        return []
    part = Source(rel=src.rel, lang="JavaScript", data=data, text=data.decode("utf-8", "replace"))
    out: list[Unit] = []
    for stmt in tree.root_node.named_children:
        if stmt.type in DECL or stmt.type in ("lexical_declaration", "variable_declaration"):
            out += _declaration(part, stmt, scope="file" if module else "public")
    for u in out:
        u.start, u.end = u.start + base, u.end + base
        u.cut = (u.cut[0] + base, u.cut[1] + base) if u.cut else None
        if not module:
            u.keep = "a classic script's top-level names are globals: another script or an inline handler may call it"
    return out


PACK = HtmlPack()


def _declaration(src: Source, node, scope: str) -> list[Unit]:
    """A top-level function or class: `function f() {}`, `class C {}`, `const f = () => …`.
    A `const` holding any other value may do something when it runs, and is never a unit."""
    if node.type in DECL:
        name = node.child_by_field_name("name")
        if name is None:
            return []
        kind, body = DECL[node.type], node
    else:
        declarators = [d for d in node.named_children if d.type == "variable_declarator"]
        if len(declarators) != 1:
            return []                                    # `const a = …, b = …` is cut together or not at all
        name = declarators[0].child_by_field_name("name")
        body = declarators[0].child_by_field_name("value")
        if name is None or body is None or name.type != "identifier":
            return []
        kind = FUNC_VALUES.get(body.type)
        if kind is None:
            return []
    cut, keep = _statement(src, node) if scope != "public" else ((node.start_byte, node.end_byte), "")
    if kind == "class":
        keep = _class_keep(body) or keep
    word = src.data[name.start_byte:name.end_byte].decode("utf-8", "replace")
    return [Unit(kind=kind, name=word, start=node.start_byte, end=node.end_byte, scope=scope, cut=cut, keep=keep)]


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
