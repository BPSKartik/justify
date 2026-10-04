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
from .js import DECL
from .js import PACK as JS

JS_TYPES = {"", "text/javascript", "application/javascript", "application/ecmascript", "text/ecmascript",
            "module"}
_PARSERS: dict = {}


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
            out += JS._declarations(part, stmt, scope="file" if module else "public", cut_node=stmt)
    for u in out:
        u.start, u.end = u.start + base, u.end + base
        u.cut = (u.cut[0] + base, u.cut[1] + base) if u.cut else None
        if not module:
            u.keep = "a classic script's top-level names are globals: another script or an inline handler may call it"
    return out


PACK = HtmlPack()
