"""
Java.

  import   a single-type or static import whose name nothing in the file mentions — a Javadoc
           {@link} or @see counts, since the comment needs the import too. `import a.b.*` names
           nothing and is never touched.
  method / field / class
           a private member of a class, enum, record or interface (and of the bodies of anonymous
           classes and enum constants). Only its file can name it (scope file).

Kept for judgement, whatever the count says:
  - an annotated member: Spring, JUnit, Jackson, JPA and Lombok call or read it by reflection;
  - a field of an annotated class: Lombok @Data/@Getter/@Value, @Entity, Jackson and the like
    generate accessors from private fields or read them directly;
  - a native method, implemented outside Java;
  - a field whose initializer runs code (`= startServer()`, `= counter++`): dropping the field
    would drop the call;
  - an instance field of a class that implements Serializable (its serialized form), or of any
    class in a repository that serializes objects by reflection — Gson, Jackson, JPA, Moshi and
    the rest read private fields nobody names;
  - a name that appears in another file's string literal (getDeclaredMethod("x"),
    ReflectionTestUtils.setField(o, "x", v)), or in a file Justify does not parse — XML, YAML,
    ProGuard rules, JNI C code, Groovy tests — any of which can reach a private member by name.

Never units: constructors, and the names Java serialization calls by itself (serialVersionUID,
readObject, writeReplace and the rest).

A member inside another member that is itself unused waits for the next run: removing the outer
one removes it anyway, and two overlapping cuts cannot be applied together.
"""

from __future__ import annotations

import re
import weakref
from collections import Counter

from . import Pack, Source, Unit, whole_lines

# A file's words, split at `$` as well: Kotlin's "$name" template uses `name`, though the core's
# word search reads `$name` as one word. Splitting more never finds fewer uses.
WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")      # names the core's word search finds whole

SERIAL = {"serialVersionUID", "serialPersistentFields", "readObject", "writeObject", "readObjectNoData",
          "readResolve", "writeReplace"}
BODIES = {"class_body", "interface_body", "enum_body_declarations"}
MEMBERS = {"method_declaration": "method", "field_declaration": "field", "class_declaration": "class",
           "enum_declaration": "class", "record_declaration": "class", "interface_declaration": "class",
           "annotation_type_declaration": "class"}
ANNOTATIONS = {"marker_annotation", "annotation"}

# What an initializer may call or construct and still do nothing but build its value.
PURE_CALLS = {"getLogger", "getLog", "getAnonymousLogger", "compile", "quote", "asList", "of", "ofEntries",
              "copyOf", "valueOf", "emptyList", "emptyMap", "emptySet", "singletonList", "singleton",
              "singletonMap", "unmodifiableList", "unmodifiableMap", "unmodifiableSet",
              "unmodifiableCollection", "synchronizedList", "synchronizedMap", "ofPattern", "ofNanos",
              "ofMillis", "ofSeconds", "ofMinutes", "ofHours", "ofDays"}
PURE_TYPES = {"Object", "String", "StringBuilder", "StringBuffer", "ArrayList", "LinkedList", "HashMap",
              "LinkedHashMap", "TreeMap", "HashSet", "LinkedHashSet", "TreeSet", "ArrayDeque", "PriorityQueue",
              "ConcurrentHashMap", "ConcurrentLinkedQueue", "ConcurrentLinkedDeque", "ConcurrentSkipListMap",
              "ConcurrentSkipListSet", "CopyOnWriteArrayList", "CopyOnWriteArraySet", "LinkedBlockingQueue",
              "ArrayBlockingQueue", "EnumMap", "IdentityHashMap", "WeakHashMap", "Vector", "Hashtable",
              "Properties", "AtomicInteger", "AtomicLong", "AtomicBoolean", "AtomicReference",
              "AtomicIntegerArray", "AtomicLongArray", "LongAdder", "ReentrantLock", "ReentrantReadWriteLock",
              "CountDownLatch", "Semaphore", "ThreadLocal", "BigDecimal", "BigInteger", "Random",
              "SimpleDateFormat", "DecimalFormat", "Date"}
CALLS = {"method_invocation", "object_creation_expression"}

# Libraries that read or write an object's private fields without being told their names.
REFLECTIVE = re.compile(
    rb"^[ \t]*import[ \t]+(?:static[ \t]+)?(com\.google\.gson|com\.fasterxml\.jackson|org\.codehaus\.jackson"
    rb"|com\.squareup\.moshi|com\.alibaba\.fastjson|javax\.persistence|jakarta\.persistence|org\.hibernate"
    rb"|javax\.xml\.bind|jakarta\.xml\.bind|javax\.json\.bind|jakarta\.json\.bind|org\.simpleframework\.xml"
    rb"|com\.thoughtworks\.xstream|org\.mongodb|dev\.morphia|org\.springframework\.data|com\.google\.firebase"
    rb"|com\.google\.cloud\.firestore|androidx\.room|android\.arch\.persistence|io\.realm|com\.esotericsoftware"
    rb"|org\.apache\.commons\.lang3?\.builder|kotlinx\.serialization|org\.yaml\.snakeyaml|org\.modelmapper"
    rb"|ma\.glasnost|org\.dozer|com\.github\.dozermapper|org\.apache\.avro|io\.protostuff|com\.jsoniter"
    rb"|com\.owlike\.genson|flexjson|net\.sf\.json)\b"
    rb"|\b(getDeclaredFields|declaredFields|declaredMemberProperties|memberProperties)\b", re.M)
# Short string literals with no spaces in them: where a name handed to reflection lives.
STRING = re.compile(rb"\"([^\"\s\\]{1,200})\"|'([^'\s\\]{2,200})'")
# Files whose words reach no code: prose, licences, build and CI settings (ProGuard rules do).
NOT_A_USE = re.compile(r"\.(md|markdown|mdx|rst|adoc|asciidoc|txt)$|(^|/)(pom\.xml|gradle\.properties|[^/]*\.gradle"
                       r"|LICEN[CS]E[^/]*|NOTICE[^/]*|CHANGELOG[^/]*|CHANGES[^/]*|AUTHORS[^/]*|CONTRIBUTORS[^/]*)$"
                       r"|(^|/)\.github/", re.I)


class Words:
    """How often each word appears in one file — split finer than the core splits it, so a pack
    sees every use the core sees and the ones it reads as other words."""

    def __init__(self, data: bytes):
        self.data = data
        self.count = Counter(WORD.findall(data))

    def used(self, name: str, start: int, end: int) -> bool:
        word = name.encode()
        inside = sum(1 for w in WORD.findall(self.data, start, end) if w == word)
        return self.count[word] > inside


class Repo:
    """What the rest of the repository says about private names; built once per audit."""

    def __init__(self, sources: list[Source]):
        self.parsed = {s.rel for s in sources}
        self.strings: dict[bytes, set[str]] = {}
        self.reflective = ""
        for s in sources:
            for m in STRING.finditer(s.data):
                for w in WORD.findall(m.group(1) or m.group(2)):
                    self.strings.setdefault(w, set()).add(s.rel)
            if not self.reflective and REFLECTIVE.search(s.data):
                self.reflective = s.rel

    def elsewhere(self, src: Source, name: str) -> str:
        """Another file that could reach `name` by spelling it: a string literal in parsed code, or
        any mention in a file nobody parsed (XML, YAML, ProGuard rules, C, Groovy...)."""
        found = sorted(self.strings.get(name.encode(), set()) - {src.rel})
        if found:
            return found[0]
        return next((rel for rel in sorted(src.index.files_with(name))
                     if rel != src.rel and rel not in self.parsed
                     and ("proguard" in rel.lower() or not NOT_A_USE.search(rel))), "")


_REPOS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()      # an audit's index → its Repo


def repo_of(src: Source) -> Repo | None:
    """The repository view for this audit, or None when a file is read alone (to prove a cut)."""
    if src.index is None:
        return None
    repo = _REPOS.get(src.index)
    if repo is None:
        repo = _REPOS[src.index] = Repo(src.sources)
    return repo


def outside_keep(src: Source, repo: Repo | None, name: str, instance_field: bool) -> str:
    """Why something outside the file may reach this private name, or ''."""
    if repo is None:
        return ""
    if instance_field and repo.reflective:
        return f"{repo.reflective} uses a library that serializes objects by reflection, reading private fields"
    where = repo.elsewhere(src, name)
    return f"'{name}' appears in {where}: reflection or configuration may reach it by name" if where else ""


def doc_start(data: bytes, node, comment_types: tuple[str, ...]) -> int:
    """Where a declaration starts once its own doc comment (`/** ... */`) is counted in."""
    prev = node.prev_sibling
    if prev is not None and prev.type in comment_types and data.startswith(b"/**", prev.start_byte) \
            and not data[prev.end_byte:node.start_byte].strip():
        return prev.start_byte
    return node.start_byte


def member_cut(data: bytes, start: int, end: int) -> tuple[int, int]:
    """A member's whole lines, plus the blank line after them when there is one before them too,
    so the gap does not double. Only ever reaches forward: reaching back could overlap the cut of
    the member above."""
    wide = whole_lines(data, start, end)
    if wide is None:
        return start, end
    a, b = wide
    before = data[data.rfind(b"\n", 0, max(a - 1, 0)) + 1:a] if a else b"{"
    nl = data.find(b"\n", b)
    if nl >= 0 and not data[b:nl].strip() and (not before.strip() or before.rstrip().endswith(b"{")):
        b = nl + 1
    return a, b


def runs_code(node, pure: set[str], skip: set[str]) -> bool:
    """Whether evaluating an initializer could do more than build its value: a call or construction
    beyond the known-pure ones, an assignment, an increment. A lambda's body is a value, not run."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in skip:
            continue
        if n.type in ("assignment_expression", "assignment", "update_expression", "object_literal"):
            return True
        if n.type in ("postfix_expression", "prefix_expression") and any(c.type in ("++", "--") for c in n.children):
            return True
        if (n.type in CALLS or n.type == "call_expression") and callee(n) not in pure:
            return True
        stack.extend(n.children)
    return False


def callee(node) -> str:
    """The simple name a call or a construction invokes (Java or Kotlin)."""
    if node.type == "method_invocation":
        target = node.child_by_field_name("name")
    elif node.type == "object_creation_expression":
        target = node.child_by_field_name("type")
    else:                                                # Kotlin call_expression: what is called
        target = node.named_children[0] if node.named_children else None
        if target is not None and target.type == "navigation_expression":
            target = target.named_children[-1]
    if target is None:
        return ""
    names = WORD.findall(target.text.split(b"<")[0])
    return names[-1].decode() if names else ""


class JavaPack(Pack):
    langs = ("Java",)
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        words = Words(src.data)
        repo = repo_of(src)
        root = src.tree.root_node
        out: list[Unit] = []
        for node in root.named_children:
            if node.type == "import_declaration":
                out += _import(src, node, words)
        stack = [root]
        while stack:
            node = stack.pop()
            for child in node.named_children:
                found = _member(src, child, node, words, repo) \
                    if node.type in BODIES and child.type in MEMBERS else []
                out += found
                if not found:                    # an unused member's insides go with it: not units yet
                    stack.append(child)
        return out


def _import(src: Source, node, words: Words) -> list[Unit]:
    if any(c.type == "asterisk" for c in node.children):
        return []                                        # `import a.b.*` binds no one name
    path = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
    if path is None:
        return []
    ident = path.child_by_field_name("name") if path.type == "scoped_identifier" else path
    name = _txt(src, ident)
    if not NAME.match(name) or words.used(name, node.start_byte, node.end_byte):
        return []
    return [Unit(kind="import", name=name, start=node.start_byte, end=node.end_byte, scope="file",
                 cut=(node.start_byte, node.end_byte))]


def _member(src: Source, node, body, words: Words, repo: Repo | None) -> list[Unit]:
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    flags = {c.type for c in mods.children} if mods is not None else set()
    if "private" not in flags:
        return []
    kind = MEMBERS[node.type]
    start = doc_start(src.data, node, ("block_comment",))
    keep = ""
    if flags & ANNOTATIONS:
        keep = "it is annotated: a framework may call or read it by reflection"
    elif "native" in flags:
        keep = "it is native: implemented outside Java and bound by its name"
    if kind != "field":
        ident = node.child_by_field_name("name")
        name = _txt(src, ident) if ident is not None else ""
        if not NAME.match(name) or name in SERIAL or words.used(name, start, node.end_byte):
            return []
        return [Unit(kind=kind, name=name, start=start, end=node.end_byte, scope="file",
                     cut=member_cut(src.data, start, node.end_byte),
                     keep=keep or outside_keep(src, repo, name, instance_field=False))]

    owner = body.parent.parent if body.type == "enum_body_declarations" else body.parent
    instance = "static" not in flags and "transient" not in flags
    if not keep and _annotated(owner):
        keep = "its class is annotated: Lombok, JPA or a serializer may generate code from its fields or read them"
    if not keep and instance and owner.type == "class_declaration" and _serializable(src, owner):
        keep = "an instance field of a Serializable class is part of its serialized form"
    declarators = node.children_by_field_name("declarator")
    out = []
    for d in declarators:
        ident = d.child_by_field_name("name")
        name = _txt(src, ident) if ident is not None else ""
        if not NAME.match(name) or name in SERIAL:
            continue
        alone = len(declarators) == 1
        span = (start, node.end_byte) if alone else (d.start_byte, d.end_byte)
        if words.used(name, *span):
            continue
        why = keep
        value = d.child_by_field_name("value")
        if not why and value is not None and runs_code(value, PURE_CALLS | PURE_TYPES, {"lambda_expression"}):
            why = "its initializer runs code, which still runs when nothing reads the field"
        why = why or outside_keep(src, repo, name, instance_field=instance)
        if not alone:
            why = why or "it is declared together with other fields in one statement"
        out.append(Unit(kind="field", name=name, start=span[0], end=span[1], scope="file",
                        cut=member_cut(src.data, *span) if alone else None, keep=why))
    return out


def _annotated(owner) -> bool:
    mods = next((c for c in owner.children if c.type == "modifiers"), None)
    return mods is not None and any(c.type in ANNOTATIONS for c in mods.children)


def _serializable(src: Source, owner) -> bool:
    sup = owner.child_by_field_name("interfaces")
    return sup is not None and bool(re.search(rb"\b(Serializable|Externalizable)\b",
                                              src.data[sup.start_byte:sup.end_byte]))


def _txt(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace")


PACK = JavaPack()
