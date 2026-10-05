"""
Swift.

  function / method
               a `private` or `fileprivate` func — at the top of the file, or in a type or an
               extension (a method). Members of a `private extension` are file-private too.
  variable / field
               a `private` or `fileprivate` property naming one thing, at the top of the file
               (variable) or in a type (field).

In Swift, private reaches no further than the file — a type's private members are seen by its
extensions in the same file — so each is searched for in its file (scope file). Never units:
`init`, `deinit`, subscripts, operators, `override`s and imports.

Kept for judgement, whatever the count says:
  - anything with an attribute (`@objc`, `@IBAction`, `@IBOutlet`, `@available`, `@MainActor`,
    `@discardableResult`, property wrappers such as `@State`) or `dynamic`, and every member of
    an `@objc` or `@objcMembers` type: Objective-C, Interface Builder and SwiftUI reach these by
    name or by reflection;
  - a stored property of a type whose conformances may be synthesized from its stored
    properties (`Codable`, `Equatable`, `Hashable` — any for a struct, the coding ones for a
    class): the compiler reads every one of them without a name in sight;
  - a stored property whose value is made by a call (`let token = center.addObserver(...)`, a
    Combine `.sink`): holding the result may be the whole point.
"""

from __future__ import annotations

from . import Pack, Source, Unit, walk
from .go import leading, trailing

COMMENTS = {"comment", "multiline_comment"}
BODIES = {"class_body", "enum_class_body"}
PRIVATE = {"private", "fileprivate"}
ACCESS = {"private", "fileprivate", "internal", "package", "public", "open", "private(set)", "fileprivate(set)",
          "internal(set)", "package(set)", "public(set)"}
# struct conformances that synthesize nothing from stored properties
INERT = {"View", "App", "Scene", "PreviewProvider", "ViewModifier", "Shape", "Identifiable", "Sendable",
         "Error", "LocalizedError", "CustomStringConvertible", "CustomDebugStringConvertible"}
CODING = ("Codable", "Encodable", "Decodable", "NSCoding", "NSSecureCoding")


class SwiftPack(Pack):
    langs = ("Swift",)
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        root = src.tree.root_node
        conforms = _conformances(src, root)
        out: list[Unit] = []
        stack = [(root, None)]
        while stack:
            holder, owner = stack.pop()
            for node in holder.named_children:
                if node.type == "class_declaration":
                    body = node.child_by_field_name("body")
                    if body is not None and body.type in BODIES:
                        stack.append((body, node))
                elif node.type in ("function_declaration", "property_declaration"):
                    u = self._unit(src, node, owner, conforms)
                    if u is not None:
                        out.append(u)
        return out

    def _unit(self, src: Source, node, owner, conforms: dict[str, set[str]]) -> Unit | None:
        mods = _mods(node)
        words = _modifiers(src, mods)
        inherited = (not words & ACCESS and _is_extension(src, owner)
                     and bool(_modifiers(src, _mods(owner)) & PRIVATE))     # a member of `private extension`
        if not (words & PRIVATE or inherited) or "override" in words:
            return None
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.type != "simple_identifier":
                return None                              # an operator
            kind = "method" if owner is not None else "function"
        else:
            names = [c for i, c in enumerate(node.children) if node.field_name_for_child(i) == "name"]
            bound = names[0].child_by_field_name("bound_identifier") if len(names) == 1 else None
            if bound is None:
                return None                              # `let (a, b) = ...`, `var a = 1, b = 2`
            name_node = bound
            kind = "field" if owner is not None else "variable"
        name = _text(src, name_node).strip("`")
        start = leading(src.data, node, COMMENTS)
        u = Unit(kind=kind, name=name, start=start, end=trailing(src.data, node, COMMENTS), scope="file")
        u.keep = _attribute(src, mods) or ("it is `dynamic`: dispatched through the Objective-C runtime"
                                           if "dynamic" in words else "")
        if not u.keep and owner is not None and _attribute(src, _mods(owner), {"objc", "objcMembers"}):
            u.keep = "its type is @objc: Objective-C can call it by selector"
        if not u.keep and node.type == "property_declaration" and _stored(node, words):
            u.keep = _stored_reason(src, node, owner, conforms)
        return u


def _text(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace") if node is not None else ""


def _mods(node):
    if node is None:
        return None
    return next((c for c in node.children if c.type == "modifiers"), None)


def _modifiers(src: Source, mods) -> set[str]:
    """The modifier words, `private(set)` kept whole: it makes only the setter private."""
    if mods is None:
        return set()
    return {_text(src, c).replace(" ", "") for c in mods.named_children if c.type != "attribute"}


def _attribute(src: Source, mods, only: set[str] | None = None) -> str:
    for c in (mods.named_children if mods is not None else ()):
        if c.type == "attribute":
            word = _text(src, c).lstrip("@").split("(")[0].strip()
            if only is None or word in only:
                return f"it carries @{word}, which may mean it is reached by name or by reflection"
    return ""


def _is_extension(src: Source, owner) -> bool:
    kind = owner.child_by_field_name("declaration_kind") if owner is not None else None
    return kind is not None and _text(src, kind) == "extension"


def _stored(node, words: set[str]) -> bool:
    """A stored instance property: initialised with every instance, so read by whatever reads
    them all. Static and lazy ones are made only when first used."""
    computed = any(c.type == "computed_property" for c in node.children)
    return not computed and not words & {"static", "class", "lazy"}


def _stored_reason(src: Source, node, owner, conforms: dict[str, set[str]]) -> str:
    value = node.child_by_field_name("value")
    if value is not None and _calls(value):
        if owner is not None or src.rel.endswith("main.swift"):
            return "its value is made by a call when it is created; holding the result may be the point"
    if owner is None:
        return ""
    kind = _text(src, owner.child_by_field_name("declaration_kind"))
    names = conforms.get(_text(src, owner.child_by_field_name("name")), set())
    if kind == "struct" and names - INERT:
        return f"{', '.join(sorted(names - INERT))} may be synthesized from every stored property"
    if kind in ("class", "actor") and any(n.endswith(CODING) for n in names):
        return "Codable may be synthesized from every stored property"
    return ""


def _calls(value) -> bool:
    """A call outside any closure: a closure's body runs only when it is called."""
    stack = [value]
    while stack:
        n = stack.pop()
        if n.type == "lambda_literal":
            continue
        if n.type == "call_expression":
            return True
        stack.extend(n.children)
    return False


def _conformances(src: Source, root) -> dict[str, set[str]]:
    """Every type's declared conformances in this file — on the type and on its extensions,
    where a synthesized one must also be declared (it cannot be in another file)."""
    out: dict[str, set[str]] = {}
    for n in walk(root):
        if n.type != "class_declaration":
            continue
        name = _text(src, n.child_by_field_name("name")).split("<")[0].split(".")[-1].strip()
        for c in n.children:
            if c.type == "inheritance_specifier":
                out.setdefault(name, set()).add(_text(src, c).split("<")[0].split(".")[-1].strip())
    return out


PACK = SwiftPack()
