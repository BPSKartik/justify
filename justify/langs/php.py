"""
PHP.

  import       a `use` clause at file or namespace level — `use A\\B\\C;`, `use A\\B as D;`,
               `use function a\\b;`, `use A\\{B, C as D};` — whose name nothing in the file uses.
               A `use Trait;` inside a class is not an import and is never touched.
  method / field
               a private method or property of a class, trait or enum. Private, but not to the
               file: a trait's private members belong to every class that uses it, and tests
               reach private members by reflection, naming them in a string — so each is
               searched for across the repository (scope repo).

PHP names ignore case — `new user` builds a `User`, `$this->Save()` calls `save` — so a name
seen in the file only in another case is kept for judgement. Magic methods (`__*`) are never
units: the engine calls them. Kept for judgement as well: a member with an attribute (`#[Route]`,
`#[ORM\\Column]`, `#[Required]`) or a docblock annotation (`@Route`, `@ORM\\Column`, `@Groups`) —
frameworks call and fill these by reflection; any method of a class that has `__call` or
`__callStatic`, or that calls methods by computed name (`$this->$m()`, `[$this, $m]`); any
property of a class with `__get`/`__set` or that reads properties by computed name.
"""

from __future__ import annotations

import re
from collections import defaultdict

from . import Pack, Source, Unit, walk
from .go import leading, trailing

CLASSES = {"class_declaration", "trait_declaration", "enum_declaration", "anonymous_class"}
CALLS = {"member_call_expression", "nullsafe_member_call_expression", "scoped_call_expression"}
ACCESS = {"member_access_expression", "nullsafe_member_access_expression"}
CALL_BY_NAME = {"__call", "__callstatic"}
READ_BY_NAME = {"__get", "__set", "__isset", "__unset"}
REFLECTIVE = re.compile(r"\b(get_object_vars|property_exists|get_class_vars)\s*\(|\(array\)\s*\$this\b"
                        r"|foreach\s*\(\s*\$this\s+as\b", re.I)
TAG = re.compile(r"(?<![\w.@])@([A-Za-z_\\][\w\\-]*)")
# phpDocumentor, PHPStan and Psalm tags: they describe code, they do not wire it to anything
DOC_TAGS = {"api", "author", "category", "copyright", "deprecated", "example", "filesource", "global", "ignore",
            "inheritdoc", "internal", "license", "link", "method", "package", "param", "property",
            "property-read", "property-write", "return", "see", "since", "source", "subpackage", "throws",
            "todo", "uses", "used-by", "var", "version", "access", "static", "final", "abstract", "type",
            "template", "template-covariant", "template-contravariant", "extends", "implements", "mixin",
            "readonly", "pure", "immutable", "override", "noinspection", "codecoverageignore", "suppress",
            "throw", "returns", "params", "const", "private", "protected", "public", "noreturn"}
DOC_PREFIXES = ("psalm-", "phpstan-", "phan-")


class PhpPack(Pack):
    langs = ("PHP",)
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        root = src.tree.root_node
        spellings = _spellings(src)
        out: list[Unit] = []
        holders = [root] + [n.child_by_field_name("body") for n in root.named_children
                            if n.type == "namespace_definition" and n.child_by_field_name("body") is not None]
        for holder in holders:
            for stmt in holder.named_children:
                if stmt.type == "namespace_use_declaration":
                    out += self._imports(src, stmt, spellings)
        for node in walk(root):
            if node.type in CLASSES and node.child_by_field_name("body") is not None:
                out += self._members(src, node.child_by_field_name("body"), spellings)
        return out

    # ---------------------------------------------------------------- imports
    def _imports(self, src: Source, stmt, spellings: dict) -> list[Unit]:
        group = stmt.child_by_field_name("body")
        clauses = [c for c in (group or stmt).named_children if c.type == "namespace_use_clause"]
        kinds = {_text(src, n.child_by_field_name("type")) for n in [stmt, *clauses]}
        out = []
        for clause in clauses:
            target = clause.child_by_field_name("alias") or next(
                (c for c in reversed(clause.named_children) if c.type in ("qualified_name", "name")), None)
            if target is None:
                continue
            name = _text(src, target).rsplit("\\", 1)[-1]
            end = trailing(src.data, stmt, {"comment"})
            u = Unit(kind="import", name=name, start=stmt.start_byte, end=end, scope="file",
                     cut=(stmt.start_byte, end) if len(clauses) == 1 else _with_comma(clause))
            if "const" not in kinds:                      # constants are the one name that keeps its case
                u.keep = _other_case(spellings, name, stmt.start_byte, end, ("name", "string"))
            out.append(u)
        return out

    # ---------------------------------------------------------------- members
    def _members(self, src: Source, body, spellings: dict) -> list[Unit]:
        members = list(body.named_children)
        methods = {_text(src, m.child_by_field_name("name")).lower() for m in members
                   if m.type == "method_declaration"}
        out = []
        for m in members:
            if m.type not in ("method_declaration", "property_declaration") or not _private(src, m):
                continue
            start, end = leading(src.data, m, {"comment"}), trailing(src.data, m, {"comment"})
            if m.type == "method_declaration":
                name = _text(src, m.child_by_field_name("name"))
                if not name or name.startswith("__"):
                    continue
                u = Unit(kind="method", name=name, start=start, end=end, scope="repo")
                u.keep = (_wired(src, m, start)
                          or ("its class has __call or __callStatic, which take calls by name"
                              if methods & CALL_BY_NAME else "")
                          or ("its class calls methods by computed name" if _dynamic(src, body, CALLS) else "")
                          or _other_case(spellings, name, start, end, ("member", "string")))
            else:
                elems = [c for c in m.named_children if c.type == "property_element"]
                if len(elems) != 1:
                    continue                             # `private $a, $b;` is cut together or not at all
                name = _text(src, elems[0].child_by_field_name("name")).lstrip("$")
                if any(c.type == "static_modifier" for c in m.children) and \
                        _read_as_variable(src, name, start, end):
                    continue                             # `self::$name` is a use under the word `$name`
                u = Unit(kind="field", name=name, start=start, end=end, scope="repo")
                u.keep = (_wired(src, m, start)
                          or ("its class has __get or __set, which reach properties by name"
                              if methods & READ_BY_NAME else "")
                          or ("its class reads properties by computed name"
                              if _dynamic(src, body, ACCESS) or REFLECTIVE.search(_text(src, body)) else ""))
            out.append(u)
        return out


def _text(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace") if node is not None else ""


def _private(src: Source, member) -> bool:
    return any(c.type == "visibility_modifier" and _text(src, c).lower() == "private" for c in member.children)


def _wired(src: Source, member, start: int) -> str:
    """An attribute or a docblock annotation: a framework reads it, and may call or fill the
    member by reflection without its name ever being written."""
    if any(c.type == "attribute_list" for c in member.children):
        return "it has an attribute, which a framework may use to call or fill it"
    doc = src.data[start:member.start_byte].decode("utf-8", "replace")
    for tag in TAG.findall(doc):
        t = tag.lower()
        if t not in DOC_TAGS and not t.startswith(DOC_PREFIXES):
            return f"its docblock has @{tag}, an annotation a framework may act on"
    return ""


def _dynamic(src: Source, body, kinds: set[str]) -> bool:
    """A call or property access whose member name is computed, not written: `$this->$m()`,
    `$this->{$m}`, or (for calls) a callable `[$this, $m]`."""
    for n in walk(body):
        if n.type in kinds:
            name = n.child_by_field_name("name")
            if name is not None and name.type != "name":
                return True
        elif n.type == "array_creation_expression" and kinds is CALLS and _computed_callable(src, n):
            return True
    return False


def _computed_callable(src: Source, array) -> bool:
    items = [c.named_children for c in array.named_children if c.type == "array_element_initializer"]
    if len(items) != 2 or len(items[0]) != 1 or len(items[1]) != 1:
        return False
    target, method = items[0][0], items[1][0]
    return (_text(src, target).lower() in ("$this", "self::class", "static::class")
            and method.type not in ("string", "encapsed_string"))


def _spellings(src: Source) -> dict[str, list[tuple[str, int, str]]]:
    """Every name and string in the file by its lowercase: (as written, where, and what stands
    there — a member as in `$x->name`, another name, or a string). Variables and namespace
    names are left out: `$user` and `namespace App\\User` never refer to a class `User`."""
    out: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for n in walk(src.tree.root_node):
        if n.type not in ("name", "string_content"):
            continue
        p = n.parent
        if p is None or p.type == "variable_name" or (p.type == "namespace_name" and p.parent is not None
                                                      and p.parent.type == "namespace_definition"):
            continue
        name_field = p.child_by_field_name("name")
        place = ("string" if n.type == "string_content" else
                 "member" if p.type in CALLS | ACCESS and name_field is not None and name_field.id == n.id else
                 "name")
        text = _text(src, n)
        out[text.lower()].append((text, n.start_byte, place))
    return out


def _other_case(spellings: dict, name: str, start: int, end: int, places: tuple[str, ...]) -> str:
    """PHP class, function and method names ignore case: `new user` builds a `User`, and
    `$this->Save()` calls `save`. Only the places such a name can stand are looked at."""
    for text, at, place in spellings.get(name.lower(), ()):
        if text != name and place in places and not start <= at < end:
            return f"'{text}' appears in the file, and PHP names ignore case"
    return ""


def _read_as_variable(src: Source, name: str, start: int, end: int) -> bool:
    """A static property is read as `self::$name`, whose word is `$name`, not `name`."""
    word = ("$" + name).encode()
    at = src.data.find(word)
    while at >= 0:
        after = src.data[at + len(word):at + len(word) + 1]
        if not (start <= at < end) and not (after.isalnum() or after in (b"_", b"$") or after >= b"\x80"):
            return True
        at = src.data.find(word, at + 1)
    return False


PACK = PhpPack()


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

