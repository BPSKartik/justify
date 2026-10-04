"""
Kotlin.

  import   an import whose name — its `as` alias when it has one — nothing in the file mentions.
           `import a.b.*` names nothing and is never touched.
  function / method / variable / field
           a private function or property at the top of a file (function, variable) or in a
           class, object or enum body (method, field). Only its file can name it (scope file).

Uses the core's word search cannot see are looked for here first: Kotlin's "$name" string
template is a use of `name` (the core reads `$name` as one word), so a name used that way is
never a unit at all.

Kept for judgement, whatever the count says:
  - an import named like an operator or delegate convention (`getValue`, `plus`, `invoke`,
    `component1`, ...): `by`, `a + b` or a destructuring call it without spelling it;
  - an annotated function or property (@JvmStatic, @Inject, @Test, @JvmField...), and a property
    of an annotated class (@Serializable, @Entity, @Parcelize generate code from its fields);
  - an `external` function, implemented outside Kotlin;
  - a property whose initializer runs code (`= scope.launch { }`): dropping the property would
    drop the call — `by lazy { }` and the plain collection builders do not count;
  - the reflection and configuration cases the Java pack keeps: an instance property in a
    repository that serializes objects by reflection, a name in another file's string or in a file
    Justify does not parse.

Never units: `override` and `operator` functions, `main`, `expect`/`actual` declarations (the
other half lives in another source set), backticked names, and Java serialization's own names.
"""

from __future__ import annotations

from . import Pack, Source, Unit
from .java import (NAME, PURE_CALLS, PURE_TYPES, SERIAL, Repo, Words, doc_start, member_cut, outside_keep,
                   repo_of, runs_code)

BODIES = {"class_body", "enum_class_body"}
DECLS = {"function_declaration", "property_declaration"}
# Functions the compiler calls by convention, never by the name an import brings in.
OPERATORS = {"getValue", "setValue", "provideDelegate", "invoke", "get", "set", "contains", "iterator", "next",
             "hasNext", "compareTo", "equals", "rangeTo", "rangeUntil", "plus", "minus", "times", "div", "rem",
             "mod", "unaryPlus", "unaryMinus", "not", "inc", "dec", "plusAssign", "minusAssign", "timesAssign",
             "divAssign", "remAssign", "modAssign"} | {f"component{i}" for i in range(1, 33)}
PURE = PURE_CALLS | PURE_TYPES | {
    "lazy", "lazyOf", "listOf", "listOfNotNull", "mutableListOf", "arrayListOf", "setOf", "mutableSetOf",
    "hashSetOf", "linkedSetOf", "sortedSetOf", "mapOf", "mutableMapOf", "hashMapOf", "linkedMapOf", "sortedMapOf",
    "emptyArray", "arrayOf", "arrayOfNulls", "intArrayOf", "longArrayOf", "byteArrayOf", "charArrayOf",
    "shortArrayOf", "floatArrayOf", "doubleArrayOf", "booleanArrayOf", "Regex", "toRegex", "Pair", "Triple",
    "Any", "Mutex", "MutableStateFlow", "MutableSharedFlow", "MutableLiveData", "Channel", "SupervisorJob", "Job",
    "logger", "notNull", "observable", "vetoable"}
NOT_RUN = {"lambda_literal", "anonymous_function"}


class KotlinPack(Pack):
    langs = ("Kotlin",)
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        words = Words(src.data)
        repo = repo_of(src)
        root = src.tree.root_node
        out: list[Unit] = []
        for lst in root.named_children:
            if lst.type == "import_list":
                out += [u for h in lst.named_children if h.type == "import_header" for u in _import(src, h, words)]
        stack = [root]
        while stack:
            node = stack.pop()
            for child in node.named_children:
                found = _declaration(src, child, node, words, repo) \
                    if child.type in DECLS and node.type in BODIES | {"source_file"} else []
                out += found
                if not found:                    # an unused declaration's insides go with it
                    stack.append(child)
        return out


def _import(src: Source, node, words: Words) -> list[Unit]:
    if any(c.type == "wildcard_import" for c in node.named_children):
        return []
    path = next((c for c in node.named_children if c.type == "identifier"), None)
    alias = next((c for c in node.named_children if c.type == "import_alias"), None)
    if path is None or not path.named_children:
        return []
    imported = _txt(src, path.named_children[-1])
    name = _txt(src, alias.named_children[-1]) if alias is not None and alias.named_children else imported
    if not NAME.match(name) or words.used(name, node.start_byte, node.end_byte):
        return []
    keep = ""
    if {name, imported} & OPERATORS:
        keep = "an operator or delegate convention: `by`, an operator or destructuring calls it without its name"
    return [Unit(kind="import", name=name, start=node.start_byte, end=node.end_byte, scope="file",
                 cut=(node.start_byte, node.end_byte), keep=keep)]


def _declaration(src: Source, node, parent, words: Words, repo) -> list[Unit]:
    mods = next((c for c in node.named_children if c.type == "modifiers"), None)
    if mods is None:
        return []
    annotated = any(c.type == "annotation" for c in mods.named_children)
    flags = {_txt(src, c) for c in mods.named_children if c.type != "annotation"}
    if "private" not in flags or flags & {"override", "operator", "expect", "actual"}:
        return []
    member = parent.type in BODIES
    if node.type == "function_declaration":
        ident = next((c for c in node.named_children if c.type == "simple_identifier"), None)
        kind = "method" if member else "function"
    else:
        var = next((c for c in node.named_children if c.type == "variable_declaration"), None)
        ident = next((c for c in var.named_children if c.type == "simple_identifier"), None) if var else None
        kind = "field" if member else "variable"
    name = _txt(src, ident) if ident is not None else ""
    start = doc_start(src.data, node, ("multiline_comment",))
    if not NAME.match(name) or name in SERIAL or name == "main" or words.used(name, start, node.end_byte):
        return []
    owner = parent.parent if member else None
    keep = ""
    if annotated:
        keep = "it is annotated: a framework or the compiler may call or read it by its name"
    elif "external" in flags:
        keep = "it is external: implemented outside Kotlin and bound by its name"
    elif kind == "field" and owner is not None and _annotated(src, owner):
        keep = "its class is annotated: a plugin or serializer may generate code from its properties or read them"
    elif node.type == "property_declaration":
        value = _initializer(node)
        if value is not None and runs_code(value, PURE, NOT_RUN):
            keep = "its initializer runs code, which still runs when nothing reads the property"
    instance = kind == "field" and owner is not None and owner.type == "class_declaration"
    keep = keep or outside_keep(src, repo, name, instance_field=instance)
    return [Unit(kind=kind, name=name, start=start, end=node.end_byte, scope="file",
                 cut=member_cut(src.data, start, node.end_byte), keep=keep)]


def _initializer(prop):
    """What a property runs when it is created: the expression after `=`, or its delegate."""
    seen_eq = False
    for c in prop.children:
        if c.type == "property_delegate":
            return c
        if seen_eq and c.is_named:
            return c
        seen_eq = seen_eq or c.type == "="
    return None


def _annotated(src: Source, owner) -> bool:
    mods = next((c for c in owner.named_children if c.type == "modifiers"), None)
    return mods is not None and any(c.type == "annotation" for c in mods.named_children)


def _txt(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace")


PACK = KotlinPack()
