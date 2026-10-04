"""
C and C++.

  function   a function defined in a source file (.c .cc .cpp .cxx). `static`, or anywhere in an
             unnamed namespace, it has internal linkage: only its translation unit can call it
             (scope file). Otherwise any object file linked with it could — judged, never removed
             from here. `main` and the other functions a runtime calls by name are not units.
  variable   a `static` variable at file scope (or one in an unnamed namespace) that declares a
             single name.

Headers are not judged: a static or inline function in one belongs to every file that includes
it. #include lines are never units — what an include brings in is the preprocessor's to say.
C++ class members are not judged yet, in a class or defined outside it.

A translation unit is more than its file, so the search widens wherever the preprocessor reaches:

  * a source file that another file #includes (a unity build, a test that includes the .c to
    reach its statics) shares its statics with the includer — they are searched for in the whole
    repository;
  * a name mentioned in a repository file this one includes (a header's macro that calls it, an
    X-macro list) is used;
  * a macro that pastes tokens (`cmd_##name`, or a library's G_DEFINE_TYPE) builds names the text
    never spells out, so a name containing a word handed to one is judged, never removed.

Kept for judgement, whatever the count says: `__attribute__((used / constructor / destructor /
unused / section …))`, `[[maybe_unused]]`, `__declspec(dllexport)`, EXPORT / API / UNUSED macros
before the name, an @(#) or $Id$ string, C++ functions found by argument-dependent lookup (begin,
swap, get …), and C++ variables whose construction may run code.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict, deque

from . import IDENT, Pack, Source, Unit, walk, whole_lines

SOURCE = (".c", ".cc", ".cpp", ".cxx")
ENTRY_POINTS = {"main", "wmain", "WinMain", "wWinMain", "DllMain", "_start",
                "LLVMFuzzerTestOneInput", "LLVMFuzzerInitialize"}
# C++ calls these without naming them: range-for (begin/end), swap in algorithms, structured
# bindings (get), and the customisation points libraries look up by argument (to_json, PrintTo …)
ADL = {"begin", "end", "cbegin", "cend", "rbegin", "rend", "size", "data", "empty", "swap", "get",
       "hash_value", "to_json", "from_json", "serialize", "save", "load", "tag_invoke", "format_as",
       "PrintTo", "AbslHashValue", "AbslStringify"}
PREPROC_BLOCKS = {"preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "preproc_elifdef"}
DECLARATORS = {"pointer_declarator", "reference_declarator", "array_declarator", "function_declarator",
               "parenthesized_declarator", "attributed_declarator"}
TYPE_BODIES = {"struct_specifier", "union_specifier", "enum_specifier", "class_specifier"}

INCLUDE = re.compile(r'^[ \t]*#[ \t]*include(?:_next)?[ \t]*[<"]([^>"\n]+)[>"]', re.M)
COMPUTED_INCLUDE = re.compile(r"^[ \t]*#[ \t]*include[ \t]+[A-Za-z_]", re.M)
DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(\()?((?:[^\n]*\\\r?\n)*[^\n]*)", re.M)
FILE_SCOPE_MACRO = re.compile(r"^([A-Z][A-Z0-9_]*)[ \t]*\(", re.M)
# files the preprocessor reads, which Justify may not parse: one of them can #include a source file
PREPROCESSED = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".c++", ".h++", ".inc", ".inl", ".ipp",
                ".tpp", ".tcc", ".def", ".x", ".tbl", ".y", ".yy", ".ypp", ".l", ".ll", ".lex", ".cu", ".cuh",
                ".m", ".mm", ".ino", ".pde", ".s", ".sx", ".rc", ".pc", ".ec", ".cl", ".metal"}

ATTRIBUTE = re.compile(r"__attribute__\s*\(\((.*?)\)\)|\[\[(.*?)\]\]|__declspec\s*\(\s*(dllexport)", re.S)
KEEP_ATTRIBUTES = {"used", "unused", "maybe_unused", "constructor", "destructor", "section", "alias", "ifunc",
                   "weak", "weakref", "externally_visible", "visibility", "retain", "dllexport"}
KEEP_MACRO_LOWER = {"__unused", "_unused", "__maybe_unused", "__used", "__visible", "asmlinkage"}
VERSION_STRING = re.compile(r'"\s*(@\(#\)|\$(Id|Header|Revision)\b)')
LICENCE = re.compile(r"(?i)copyright|licen[sc]e|SPDX")
PLAIN_VALUES = {"lambda_expression", "number_literal", "string_literal", "char_literal", "raw_string_literal",
                "concatenated_string", "true", "false", "null", "nullptr"}


class CFamilyPack(Pack):
    langs = ("C", "C++", "C/C++ header")
    implemented = True

    def __init__(self):
        self._repo_cache: tuple[object, _Repo] | None = None

    def units(self, src: Source) -> list[Unit]:
        if not src.rel.lower().endswith(SOURCE):
            return []                                    # a header belongs to whoever includes it
        cpp = not src.rel.lower().endswith(".c")
        found = []
        for node, internal in _file_scope(src.tree.root_node, False):
            if node.type in ("function_definition", "template_declaration"):
                u = _function(src, node, internal, cpp)
            elif node.type == "declaration":
                u = _variable(src, node, internal, cpp)
            else:
                u = None
            if u is not None:
                found.append(u)
        if src.index is None:                            # re-finding units to cut them: no search needed
            return found
        return self._in_translation_unit(src, found)

    def _in_translation_unit(self, src: Source, found: list[Unit]) -> list[Unit]:
        """What the preprocessor adds to a file: its includes, its includers, and pasted names."""
        repo = self._repo(src)
        tu = [r for r in repo.closure(src.rel) if r != src.rel]
        shared = repo.is_included(src.rel)
        words = repo.pasted_words([src.rel, *tu])
        computed = bool(COMPUTED_INCLUDE.search(src.text))
        out = []
        for u in found:
            if u.scope == "file":
                if any(src.index.count(rel, u.name) for rel in tu):
                    continue                             # a header's macro or an included list names it
                if shared:
                    u.scope = "repo"
                if not u.keep and computed:
                    u.keep = "the file #includes a file named by a macro, which may use it"
                if not u.keep:
                    w = _pasted(u.name, words)
                    if w:
                        u.keep = f"a macro that pastes names is handed '{w}' here, and may build this one"
            out.append(u)
        return out

    def _repo(self, src: Source) -> _Repo:
        if self._repo_cache is None or self._repo_cache[0] is not src.sources:
            self._repo_cache = (src.sources, _Repo(src.index, src.sources))
        return self._repo_cache[1]


# ---------------------------------------------------------------- units

def _file_scope(node, internal: bool):
    """(declaration, in an unnamed namespace) at file scope — through namespaces, extern "C"
    blocks and every branch of an #if, but never into a class or a function."""
    for child in node.named_children:
        t = child.type
        if t in PREPROC_BLOCKS:
            yield from _file_scope(child, internal)
        elif t == "namespace_definition":
            body = child.child_by_field_name("body")
            if body is not None:
                yield from _file_scope(body, internal or child.child_by_field_name("name") is None)
        elif t == "linkage_specification":
            body = child.child_by_field_name("body")
            if body is not None and body.type == "declaration_list":
                yield from _file_scope(body, internal)
            elif body is not None:
                yield body, internal
        else:
            yield child, internal


def _function(src: Source, node, internal: bool, cpp: bool) -> Unit | None:
    fn = node
    if node.type == "template_declaration":
        fn = next((c for c in node.named_children if c.type == "function_definition"), None)
        if fn is None:
            return None
    if fn.child_by_field_name("type") is None or _defines_type(fn):
        return None                                      # `TEST(a, b) { … }` is a macro, not a definition
    ident, is_function = _declared(fn.child_by_field_name("declarator"))
    if ident is None or not is_function:
        return None
    name = _txt(src, ident)
    if name in ENTRY_POINTS:
        return None
    static = _storage(src, fn) == "static"
    u = _unit(src, "function", name, node, "file" if static or internal else "public")
    body = fn.child_by_field_name("body")
    u.keep = _kept(_between(src, node.start_byte, ident.start_byte),
                   _between(src, ident.end_byte, body.start_byte if body is not None else fn.end_byte))
    if not u.keep and cpp and name in ADL:
        u.keep = "C++ finds it by argument-dependent lookup (range-for, swap, structured bindings …) without naming it"
    return u


def _variable(src: Source, node, internal: bool, cpp: bool) -> Unit | None:
    storage = _storage(src, node)
    if not (storage == "static" or (internal and storage is None)) or _defines_type(node):
        return None
    declarators = node.children_by_field_name("declarator")
    if len(declarators) != 1:
        return None                                      # `static int a, b;` is cut together or not at all
    d = declarators[0]
    value = d.child_by_field_name("value") if d.type == "init_declarator" else None
    ident, is_function = _declared(d.child_by_field_name("declarator") if d.type == "init_declarator" else d)
    if ident is None or is_function:
        return None                                      # a prototype, not a variable
    name = _txt(src, ident)
    u = _unit(src, "variable", name, node, "file")
    u.keep = _kept(_between(src, node.start_byte, ident.start_byte),
                   _between(src, ident.end_byte, value.start_byte if value is not None else node.end_byte))
    if not u.keep and value is not None and VERSION_STRING.match(_txt(src, value)):
        u.keep = "an @(#) or $Id$ string is kept in the binary for `what` and `ident` to find"
    if not u.keep and cpp and _runs_code(node, d, value):
        u.keep = "constructing it may run code (a constructor or a call) that nothing needs to name"
    return u


def _unit(src: Source, kind: str, name: str, node, scope: str) -> Unit:
    start, end = _with_comments(src, node)
    return Unit(kind=kind, name=name, start=start, end=end, scope=scope, cut=_cut(src.data, start, end))


def _declared(node):
    """The identifier a declarator names, and whether it names a function: the declarator
    nearest the name decides (`*f(void)` is a function, `(*f)(void)` a pointer to one)."""
    nearest = None
    while node is not None and node.type != "identifier":
        if node.type not in DECLARATORS:
            return None, False                           # Foo::bar, operator==, ~Foo, f<int>: not ours
        if node.type not in ("parenthesized_declarator", "attributed_declarator"):
            nearest = node.type
        node = node.child_by_field_name("declarator") or next(
            (c for c in node.named_children if c.type in DECLARATORS or c.type == "identifier"), None)
    return node, nearest == "function_declarator"


def _storage(src: Source, node) -> str | None:
    for c in node.children:
        if c.type == "storage_class_specifier":
            word = _txt(src, c)
            if word in ("static", "extern"):
                return word
    return None


def _defines_type(node) -> bool:
    """`static struct s { … } x;` — cutting the variable would take the type with it."""
    t = node.child_by_field_name("type")
    return t is not None and t.type in TYPE_BODIES and t.child_by_field_name("body") is not None


def _kept(before: str, after: str = "") -> str:
    """A reason the compiler or linker keeps it though no code names it: an attribute anywhere
    in its declaration, or an export / keep macro before its name."""
    for m in ATTRIBUTE.finditer(before + " " + after):
        said = " ".join(g for g in m.groups() if g)
        hit = {w.strip("_").split("::")[-1] for w in re.findall(r"[\w:]+", said)} & KEEP_ATTRIBUTES
        if hit:
            return f"marked {sorted(hit)[0]}, which keeps it though nothing names it"
    for w in IDENT.findall(before):
        caps = re.fullmatch(r"[A-Z0-9_]+", w) is not None
        if w in KEEP_MACRO_LOWER or caps and ("EXPORT" in w or "UNUSED" in w or "KEEPALIVE" in w
                                              or {"API", "USED"} & set(w.split("_"))):
            return f"marked {w}, which exports or keeps it though nothing here names it"
    return ""


def _runs_code(decl, declarator, value) -> bool:
    """In C++ a variable of class type has a constructor, `auto` can hide one, and any variable
    can be initialised by a call — a registration that works without the name ever being used."""
    t = decl.child_by_field_name("type")
    inner = declarator.child_by_field_name("declarator") if declarator.type == "init_declarator" else declarator
    pointer = any(n.type == "pointer_declarator" for n in walk(inner))
    if t is not None and t.type == "placeholder_type_specifier" and not pointer:
        return value is None or value.type not in PLAIN_VALUES
    if not pointer and (t is None or t.type not in ("primitive_type", "sized_type_specifier")):
        return True
    stack = [value] if value is not None else []
    while stack:
        n = stack.pop()
        if n.type in ("call_expression", "new_expression", "compound_literal_expression", "user_defined_literal"):
            return True
        if n.type != "lambda_expression":                # a lambda's body runs only when called
            stack.extend(n.children)
    return False


def _with_comments(src: Source, node) -> tuple[int, int]:
    """The node with the comments directly above it (no blank line between) and one trailing on
    its last line: a doc comment that names the function is part of it, not a use of it. A
    file's opening comment, or one that holds a licence, is never taken."""
    top, end = node, node.end_byte
    prev = node.prev_sibling
    while (prev is not None and prev.type == "comment" and prev.end_point[0] + 1 == top.start_point[0]
           and whole_lines(src.data, prev.start_byte, prev.end_byte) is not None
           and not (prev.prev_sibling is None and prev.parent.type == "translation_unit")
           and not LICENCE.search(_txt(src, prev))):
        top, prev = prev, prev.prev_sibling
    nxt = node.next_sibling
    if nxt is not None and nxt.type == "comment" and nxt.start_point[0] == node.end_point[0]:
        end = nxt.end_byte
    return top.start_byte, end


def _cut(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Its whole lines and, when a blank line is both before and after them, the one after — so
    no double gap is left. None (the core's own whole-line cut) otherwise."""
    lines = whole_lines(data, start, end)
    if lines is None:
        return None                                      # it shares a line: the core refuses the cut
    a, b = lines
    blank_before = a == 0 or not data[data.rfind(b"\n", 0, a - 1) + 1:a].strip()
    nl = data.find(b"\n", b)
    if blank_before and nl >= 0 and not data[b:nl].strip():
        return a, nl
    return None


def _txt(src: Source, node) -> str:
    return _between(src, node.start_byte, node.end_byte)


def _between(src: Source, a: int, b: int) -> str:
    return src.data[a:b].decode("utf-8", "replace")


# ---------------------------------------------------------------- the translation unit

class _Repo:
    """How the repository's files include one another, and which macros paste tokens — worked
    out once per audit, since every source file's question needs it."""

    def __init__(self, index, sources: list[Source]):
        self.index = index
        self.texts = {s.rel: s.text for s in sources if s.lang in CFamilyPack.langs}
        self.by_base: dict[str, list[str]] = defaultdict(list)
        for rel in index.ident:
            self.by_base[posixpath.basename(rel)].append(rel)
        self.includes = {rel: [t for inc in INCLUDE.findall(text) for t in self._resolve(inc)]
                         for rel, text in self.texts.items()}
        self.included = {t for rel, ts in self.includes.items() for t in ts if t != rel}
        defines = [(m.group(1), m.group(2) is not None, m.group(3), m.span())
                   for text in self.texts.values() for m in DEFINE.finditer(text)]
        self.macros = {name for name, *_ in defines}
        self.pasting = {name for name, fn, body, _ in defines if fn and "##" in body}
        grew = True
        while grew:                                      # a macro that calls a pasting macro pastes too
            more = {name for name, fn, body, _ in defines
                    if fn and name not in self.pasting and set(IDENT.findall(body)) & self.pasting}
            self.pasting |= more
            grew = bool(more)
        self._words: dict[str, set[str]] = {}

    def _resolve(self, include: str) -> list[str]:
        """Every repository file an #include could mean — any directory on the include path."""
        parts = [p for p in include.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        if not parts:
            return []
        tail = "/".join(parts)
        return [r for r in self.by_base.get(parts[-1], []) if r == tail or r.endswith("/" + tail)]

    def closure(self, rel: str) -> list[str]:
        seen, todo = {rel}, deque([rel])
        while todo:
            for t in self.includes.get(todo.popleft(), []):
                if t not in seen:
                    seen.add(t)
                    todo.append(t)
        return sorted(seen)

    def is_included(self, rel: str) -> bool:
        """Another file #includes this one. Files Justify does not parse (.inc, .y, .cu …) can
        too: only their words are known, so one holding `include` and the file's name counts."""
        if rel in self.included:
            return True
        maybe = set(self.index.files_with("include"))
        for w in IDENT.findall(posixpath.basename(rel)):
            maybe &= self.index.files_with(w)
        return any(r not in self.texts and r != rel and posixpath.splitext(r)[1].lower() in PREPROCESSED
                   for r in maybe)

    def pasted_words(self, rels: list[str]) -> list[str]:
        """Words handed to a pasting macro anywhere in these files, longest first: the repository's
        own (`##` in the body) and a library's, called at the start of a line (G_DEFINE_TYPE)."""
        out: set[str] = set()
        for rel in rels:
            if rel not in self._words:
                self._words[rel] = self._words_in(rel)
            out |= self._words[rel]
        return sorted((w for w in out if len(w) >= 2), key=lambda w: (-len(w), w))

    def _words_in(self, rel: str) -> set[str]:
        text = self.texts.get(rel)
        if text is None:                                 # an X-macro list, say: its words are all we know
            return set(self.index.ident.get(rel, ())) if self.pasting else set()
        bodies = [m.span() for m in DEFINE.finditer(text) if m.group(2)]
        calls = []
        if self.pasting:
            names = "|".join(sorted(map(re.escape, self.pasting)))
            calls += list(re.finditer(rf"\b({names})\s*\(", text))
        calls += [m for m in FILE_SCOPE_MACRO.finditer(text) if m.group(1) not in self.macros]
        out: set[str] = set()
        for m in calls:
            if any(a <= m.start() < b for a, b in bodies):
                continue                                 # inside a macro's own body: its words are parameters
            out |= set(IDENT.findall(_arguments(text, m.end())))
        return out


def _arguments(text: str, at: int, limit: int = 4000) -> str:
    """The text between the parenthesis just before `at` and its match."""
    depth = 1
    for i in range(at, min(len(text), at + limit)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[at:i]
    return text[at:at + limit]


def _pasted(name: str, words: list[str]) -> str:
    """A word handed to a pasting macro that this name contains, if any."""
    return next((w for w in words if w != name and w in name), "")


PACK = CFamilyPack()
