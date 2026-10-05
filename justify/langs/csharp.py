"""
C#.

  import   `using X = Some.Type;` — an alias nothing in the file uses. Namespace usings
           (`using System.Linq;`), `global using` and `using static` are never touched: which of
           their names a file uses only the compiler knows.
  method / field / class
           a private member of a class, struct or record: `private`, or no access modifier at all
           (C#'s default there). Methods, fields (each variable; only a field that declares one
           variable is cut), properties (as fields) and nested types. Only the type's own text can
           use it, so only its file is searched — unless the type is `partial`: then its other
           parts (a WinForms Designer file, the code-behind of XAML or Razor markup) can, and the
           whole repository is searched. Interface members are public and never named.

Kept for judgement, whatever the count says: anything with an attribute; extern, partial,
virtual, override and abstract members and explicit interface implementations; names a runtime
calls by convention (Main, Dispose, Unity messages such as Update or OnTriggerEnter, editor and
asset-processor messages, On<Action> for an action in a .inputactions file, ShouldSerializeX/ResetX,
ASP.NET's Page_Load, CreateHostBuilder, PropertyChanged.Fody's On<Property>Changed); names the compiler
calls by pattern without spelling them (GetEnumerator, Deconstruct, GetAwaiter, a sealed record's
PrintMembers; Add, Count or Select where an initializer, an index or a query in the file calls them);
an alias `using XAttribute = ...` that `[X]` uses; a struct's instance fields (its memory layout) and
the fields of a type whose attribute may read them ([Serializable], [StructLayout]); fields whose
initializer does work (`new Timer(Tick, ...)`); nested types that derive from something or hold
attributed members (assembly scanning finds them); every private member of a file that looks up
non-public members at run time (BindingFlags.NonPublic, SendMessage); any name that a file using
[UnsafeAccessor] also names; and any name that another
file could reach by name — a string in other C#, or any mention in markup, scenes and configs.
Generated files (*.Designer.cs, *.g.cs) are not judged.
"""

from __future__ import annotations

import re
import weakref
from collections import Counter

from . import IDENT, Pack, Source, Unit, walk

TYPES = {"class_declaration", "struct_declaration", "record_declaration", "interface_declaration",
         "enum_declaration", "delegate_declaration"}
BODIED = {"class_declaration", "struct_declaration", "record_declaration"}     # members default to private
SCANNABLE = BODIED | {"interface_declaration"}
KINDS = {"method_declaration": "method", "field_declaration": "field", "property_declaration": "field",
         **{t: "class" for t in TYPES}}
ACCESS = {"public", "private", "protected", "internal", "file"}
PREPROC = {"preproc_if", "preproc_elif", "preproc_else"}
NAMESPACES = {"namespace_declaration", "file_scoped_namespace_declaration", "declaration_list"} | PREPROC
STRINGS = {"string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression"}
NO_RUN = {"lambda_expression", "anonymous_method_expression", "local_function_statement"}
GENERATED_FILE = re.compile(r"(\.(designer|g|g\.i|generated)\.cs|(^|/)AssemblyInfo\.cs)$", re.I)
PROSE = (".md", ".markdown", ".txt", ".rst", ".adoc")

SPECIAL = {"extern": "extern: its body is native code, bound by name",
           "partial": "partial: another part of the type, perhaps generated, declares or calls it",
           "override": "override: the runtime calls it through its base type",
           "virtual": "virtual: the runtime calls it through its base type",
           "abstract": "abstract: the runtime calls it through its base type"}
UNITY = {"Awake", "Start", "Update", "FixedUpdate", "LateUpdate", "OnEnable", "OnDisable", "OnDestroy", "OnGUI",
         "OnValidate", "Reset", "OnApplicationQuit", "OnApplicationPause", "OnApplicationFocus", "OnBecameVisible",
         "OnBecameInvisible", "OnDrawGizmos", "OnDrawGizmosSelected", "OnAnimatorIK", "OnAnimatorMove",
         "OnAudioFilterRead", "OnControllerColliderHit", "OnJointBreak", "OnJointBreak2D", "OnParticleCollision",
         "OnParticleTrigger", "OnParticleSystemStopped", "OnParticleUpdateJobScheduled", "OnPreCull",
         "OnPreRender", "OnPostRender", "OnRenderImage", "OnRenderObject", "OnWillRenderObject",
         "OnTransformChildrenChanged", "OnTransformParentChanged", "OnBeforeTransformParentChanged",
         "OnRectTransformDimensionsChange", "OnCanvasGroupChanged", "OnCanvasHierarchyChanged",
         "OnDidApplyAnimationProperties", "OnServerInitialized", "OnConnectedToServer", "OnDisconnectedFromServer",
         "OnPlayerConnected", "OnPlayerDisconnected", "OnFailedToConnect", "OnLevelWasLoaded",
         "OnNetworkInstantiate", "OnSerializeNetworkView", "OnSceneGUI", "OnInspectorUpdate", "OnHierarchyChange",
         "OnProjectChange", "OnSelectionChange", "OnFocus", "OnLostFocus", "OnWizardCreate", "OnWizardUpdate",
         "OnWizardOtherButton", "CreateGUI", "OnPreviewGUI", "OnPreviewSettings", "OnHeaderGUI",
         # editor windows and editors, and AssetModificationProcessor's static messages
         "ShowButton", "OnAddedAsTab", "OnBeforeRemovedAsTab", "OnTabDetached", "OnMainWindowMove",
         "OnBackingScaleFactorChanged", "OnDidOpenScene", "ModifierKeysChanged", "HasFrameBounds",
         "OnGetFrameBounds", "OnSceneDrag", "OnPreSceneGUI", "IsOpenForEdit", "CanOpenForEdit", "MakeEditable",
         "FileModeChanged", "OnStatusUpdated"}
UNITY_FAMILY = re.compile(r"^(On(Trigger|Collision)(Enter|Stay|Exit)(2D)?|OnMouse\w*|On(Pre|Post)process\w*"
                          r"|OnAssign\w*|OnWill\w*|OnGenerated\w*|OnPreGenerating\w*)$")
# members the compiler calls by pattern, never spelling the name where it calls them: foreach, await,
# deconstruction, `fixed`, `await using`, and a sealed record's ToString and Equals
PATTERN = {"GetEnumerator", "GetAsyncEnumerator", "MoveNext", "MoveNextAsync", "Current", "Deconstruct",
           "GetAwaiter", "GetResult", "IsCompleted", "OnCompleted", "UnsafeOnCompleted", "GetPinnableReference",
           "DisposeAsync", "PrintMembers", "EqualityContract"}
# ... and these where the file holds the syntax that calls them: collection initializers and
# expressions call Add, `^i` and `a..b` call Length or Count and Slice, query expressions call LINQ's names
PATTERN_SYNTAX = {"Add": ("initializer_expression", "collection_expression"),
                  **dict.fromkeys(("Length", "Count", "Slice"), ("range_expression", "^")),
                  **dict.fromkeys(("Select", "SelectMany", "Where", "Join", "GroupJoin", "OrderBy",
                                   "OrderByDescending", "ThenBy", "ThenByDescending", "GroupBy", "Cast"),
                                  ("query_expression",))}
ASPNET = re.compile(r"^(Page|Application|Session)_[A-Z]\w*$")       # AutoEventWireup and Global.asax
# found on the Program class by name, non-public too, by EF Core's and ASP.NET's design-time tools
HOST_BUILDERS = {"CreateHostBuilder", "CreateWebHostBuilder", "BuildWebHost"}
# PropertyChanged.Fody weaves calls to On{Property}Changed into the property's setter
ON_CHANGED = re.compile(r"^On(\w+?)Chang(ed|ing)$")
# words that show a file looking members up by name at run time, private ones included
REFLECTION = ("NonPublic", "DeclaredMethods", "DeclaredFields", "DeclaredProperties", "DeclaredMembers",
              "DeclaredNestedTypes", "GetDeclaredMethod", "GetDeclaredMethods", "GetDeclaredField",
              "GetDeclaredProperty", "GetDeclaredNestedType", "GetRuntimeMethods", "GetRuntimeMethod",
              "GetRuntimeFields", "GetRuntimeField", "GetRuntimeProperties", "GetRuntimeProperty", "SendMessage",
              "SendMessageUpwards", "BroadcastMessage", "InvokeRepeating", "UnsafeAccessor")
# type attributes known not to read the type's fields
HARMLESS = {"Obsolete", "ApiController", "Route", "Authorize", "AllowAnonymous", "Area", "Produces", "Consumes",
            "TestClass", "TestFixture", "Collection", "Trait", "Category", "ExcludeFromCodeCoverage",
            "DebuggerDisplay", "DebuggerStepThrough", "DebuggerNonUserCode", "DebuggerTypeProxy", "EditorBrowsable",
            "Description", "DisplayName", "CLSCompliant", "SuppressMessage", "RequireComponent",
            "DisallowMultipleComponent", "AddComponentMenu", "ExecuteInEditMode", "ExecuteAlways",
            "CreateAssetMenu", "CustomEditor", "HelpURL", "DefaultExecutionOrder", "SelectionBase", "Icon"}

# per repository index: where each parsed file sits, and the words in each one's strings
_STRING_WORDS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class CSharpPack(Pack):
    langs = ("C#",)
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        if GENERATED_FILE.search(src.rel):
            return []
        f = _File(src)
        out: list[Unit] = []
        for node in _top(src.tree.root_node):
            if node.type == "using_directive":
                out += _alias(f, node)
            else:
                self._members(f, node, out)
        return out

    def _members(self, f: _File, node, out: list[Unit]) -> None:
        """The private members of a class, struct or record — and, inside it, of its nested types."""
        if node.type not in BODIED:
            return
        body = node.child_by_field_name("body")
        if body is None:
            return
        t = _Type(f.src, node)
        for m in _declarations(body):
            if m.type not in KINDS:
                continue
            mods = _modifiers(f.src, m)
            private = ("private" in mods and "protected" not in mods) or not (mods & ACCESS)
            if m.type in TYPES:
                u = self._nested(f, t, m) if private else None
                if u is not None:
                    out.append(u)
                if u is None or not f.dead(u):         # a dead type's members go with it: no nested cuts
                    self._members(f, m, out)
            elif not private:
                continue
            elif m.type == "field_declaration":
                out += self._fields(f, t, m, mods)
            else:
                u = (self._property if m.type == "property_declaration" else self._method)(f, t, m, mods)
                if u is not None:
                    out.append(u)

    def _nested(self, f: _File, t: _Type, m) -> Unit | None:
        name = _name(f.src, m.child_by_field_name("name"))
        if name is None:
            return None
        u = Unit(kind="class", name=name, start=_start(f.src, m), end=m.end_byte, scope=t.scope)
        attrs = _attributes(f.src, m)
        base = next((c for c in m.named_children if c.type == "base_list"), None)
        if attrs:
            u.keep = f"[{attrs[0]}] may have a framework find it by reflection"
        elif base is not None and m.type in SCANNABLE:
            u.keep = (f"it derives from {_txt(f.src, base).lstrip(':').strip()[:60]}: a framework may find it "
                      f"by scanning the assembly")
        elif any(n.type == "attribute_list" for n in walk(m)):
            u.keep = "it holds attributed members, which a framework may find by reflection"
        return f.finish(u)

    def _method(self, f: _File, t: _Type, m, mods: set[str]) -> Unit | None:
        name = _name(f.src, m.child_by_field_name("name"))
        if name is None:
            return None
        u = Unit(kind="method", name=name, start=_start(f.src, m), end=m.end_byte, scope=t.scope)
        u.keep = _member_keep(f, m, mods) or _called_by_name(f, name) or _by_pattern(f, name)
        return f.finish(u)

    def _property(self, f: _File, t: _Type, m, mods: set[str]) -> Unit | None:
        name = _name(f.src, m.child_by_field_name("name"))
        if name is None:
            return None
        u = Unit(kind="field", name=name, start=_start(f.src, m), end=m.end_byte, scope=t.scope)
        value = m.child_by_field_name("value")
        initial = value if value is not None and value.type != "arrow_expression_clause" else None
        accessors = m.child_by_field_name("accessors")
        stores = initial is not None or (accessors is not None and (
            any(a.type == "accessor_declaration" and a.child_by_field_name("body") is None
                for a in accessors.named_children) or "field" in IDENT.findall(_txt(f.src, accessors))))
        u.keep = (_member_keep(f, m, mods) or (stores and "static" not in mods and t.layout)
                  or (_works(initial) if initial is not None else "") or _by_pattern(f, name))
        return f.finish(u)

    def _fields(self, f: _File, t: _Type, m, mods: set[str]) -> list[Unit]:
        decl = next((c for c in m.named_children if c.type == "variable_declaration"), None)
        if decl is None:
            return []
        declarators = [d for d in decl.named_children if d.type == "variable_declarator"]
        keep = _member_keep(f, m, mods) or ("static" not in mods and "const" not in mods and t.layout)
        out = []
        for d in declarators:
            name = _name(f.src, d.child_by_field_name("name"))
            if name is None:
                continue
            # one variable: the whole declaration goes; several: each names only itself, which never
            # owns its line, so the proof leaves it alone
            start, end = (_start(f.src, m), m.end_byte) if len(declarators) == 1 else (d.start_byte, d.end_byte)
            u = Unit(kind="field", name=name, start=start, end=end, scope=t.scope)
            initial = [c for c in d.named_children if c.start_byte > d.child_by_field_name("name").start_byte]
            u.keep = keep or ("" if "const" in mods else next(filter(None, map(_works, initial)), ""))
            out.append(f.finish(u))
        return out


class _File:
    """What every unit of one file is judged against."""

    def __init__(self, src: Source):
        self.src = src
        self.words = Counter(IDENT.findall(src.text))
        self.reflects = next((w for w in REFLECTION if self.words[w]), "")
        self._syntax: set[str] | None = None

    def has(self, kind: str) -> bool:
        """Whether the file holds a node of this type (or `^`, an index from the end)."""
        if self._syntax is None:
            self._syntax = set()
            for n in walk(self.src.tree.root_node):
                self._syntax.add(n.type)
                if n.type == "prefix_unary_expression" and n.child_count and n.children[0].type == "^":
                    self._syntax.add("^")
        return kind in self._syntax

    def own(self, u: Unit) -> int:
        chunk = self.src.data[u.start:u.end].decode("utf-8", errors="replace")
        return self.words[u.name] - IDENT.findall(chunk).count(u.name)

    def dead(self, u: Unit) -> bool:
        """Whether the core will call this unit a removal — only knowable with the repository's index."""
        index = self.src.index
        if index is None or u.keep or self.own(u) > 0:
            return False
        return not (u.scope == "repo" and index.files_with(u.name) - {self.src.rel})

    def finish(self, u: Unit) -> Unit:
        if not u.keep and self.reflects:
            u.keep = f"this file looks up non-public members at run time ({self.reflects}), and may find it"
        if not u.keep and u.scope == "file":
            u.keep = self._named_elsewhere(u.name)
        return u

    def _named_elsewhere(self, name: str) -> str:
        """A private member is out of other files' reach — except by name, through reflection, a
        string in another C# file, or markup, a scene or a config that names it."""
        index = self.src.index
        if index is None:
            return ""
        for rel in sorted(index.files_with(name) - {self.src.rel}):
            low = rel.lower()
            if low.endswith(PROSE):
                continue
            if not low.endswith(".cs"):
                return f"{rel} names it, and markup, scenes and configs can reach a member by name"
            if index.count(rel, "UnsafeAccessor"):
                return f"{rel} uses [UnsafeAccessor], which reaches a private member by its name"
            words = _strings_of(self.src, rel)
            if words is None:
                return f"{rel}, a file Justify does not parse, names it"
            if name in words:
                return f"a string in {rel} names it, and reflection can reach a private member by name"
        return ""


class _Type:
    """The containing type, as far as its members' fate depends on it."""

    def __init__(self, src: Source, node):
        mods = _modifiers(src, node)
        self.scope = "repo" if "partial" in mods else "file"
        struct = node.type == "struct_declaration" or (node.type == "record_declaration"
                                                       and any(c.type == "struct" for c in node.children))
        attrs = [a for a in _attributes(src, node) if a not in HARMLESS]
        self.layout = ("a struct's instance fields are its memory layout, which native or unsafe code may rely on"
                       if struct else
                       f"the type's [{attrs[0]}] may read its fields by reflection or layout" if attrs else "")


def _member_keep(f: _File, m, mods: set[str]) -> str:
    attrs = _attributes(f.src, m)
    if attrs:
        return f"[{attrs[0]}] may have a framework reach it by reflection"
    special = next((SPECIAL[s] for s in SPECIAL if s in mods), "")
    if special:
        return special
    iface = next((c for c in m.named_children if c.type == "explicit_interface_specifier"), None)
    if iface is not None:
        return f"it implements {_txt(f.src, iface).rstrip('.')}, and callers reach it through the interface"
    return ""


def _called_by_name(f: _File, name: str) -> str:
    if name in UNITY or UNITY_FAMILY.match(name):
        return f"Unity calls {name} by name"
    index = f.src.index
    if name.startswith("On") and len(name) > 2 and index is not None and any(
            rel.lower().endswith(".inputactions") for rel in index.files_with(name[2:])):
        return f"Unity's PlayerInput sends On{name[2:]} for the {name[2:]} input action, by name"
    if name == "Main":
        return "it may be the program's entry point"
    if name == "Dispose":
        return "Dispose is called through the IDisposable pattern"
    if name.startswith("ShouldSerialize") or (name.startswith("Reset") and f.words[name[5:]]):
        return "the WinForms designer and Json.NET call ShouldSerializeX and ResetX by name"
    if ASPNET.match(name):
        return f"ASP.NET wires {name} up by name"
    if name in HOST_BUILDERS:
        return f"design-time tools find {name} on the Program class by name"
    changed = ON_CHANGED.match(name)
    if changed and f.words[changed.group(1)]:
        return f"PropertyChanged.Fody calls {name} by name when {changed.group(1)} changes"
    return ""


def _by_pattern(f: _File, name: str) -> str:
    if name in PATTERN:
        return f"the compiler calls {name} by pattern, without naming it"
    if any(f.has(k) for k in PATTERN_SYNTAX.get(name, ())):
        return f"the compiler calls {name} by pattern (an initializer, an index or range, a query) without naming it"
    return ""


def _works(node) -> str:
    """Why an initializer may be there for what it does, not the value it leaves behind."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in NO_RUN:
            continue
        if n.type in ("invocation_expression", "await_expression", "assignment_expression") or (
                n.type in ("object_creation_expression", "implicit_object_creation_expression")
                and any(a.type == "argument_list" and a.named_child_count for a in n.named_children)):
            return "its initializer does work (a call, or an object built with arguments) that may be the point"
        stack.extend(n.named_children)
    return ""


def _alias(f: _File, node) -> list[Unit]:
    """`using X = Some.Type;` binds a name for this file alone."""
    src = f.src
    if any(c.type in ("global", "static") for c in node.children) or not any(c.type == "=" for c in node.children):
        return []
    name = _name(src, node.child_by_field_name("name"))
    if name is None:
        return []
    u = Unit(kind="import", name=name, start=node.start_byte, end=node.end_byte, scope="file")
    if name.endswith("Attribute") and len(name) > 9 and f.words[name[:-9]]:
        u.keep = f"[{name[:-9]}] names the {name} alias without its Attribute suffix"
    return [u]


def _top(node):
    """Using directives and type declarations outside any type: in namespaces and #if blocks too."""
    for c in node.named_children:
        if c.type == "using_directive" or c.type in TYPES:
            yield c
        elif c.type in NAMESPACES:
            yield from _top(c)


def _declarations(body):
    """A type's members, including those inside #if / #elif / #else."""
    for c in body.named_children:
        if c.type in PREPROC:
            yield from _declarations(c)
        else:
            yield c


def _modifiers(src: Source, node) -> set[str]:
    return {_txt(src, c) for c in node.children if c.type == "modifier"}


def _attributes(src: Source, node) -> list[str]:
    """Names of the attributes on a declaration itself, `Attribute` suffix and namespace dropped."""
    out = []
    for lst in node.children:
        if lst.type != "attribute_list":
            continue
        for a in lst.named_children:
            if a.type == "attribute":
                n = a.child_by_field_name("name")
                word = _txt(src, n if n is not None else a).split("<")[0].split(".")[-1].strip()
                out.append(word[:-9] if word.endswith("Attribute") and len(word) > 9 else word)
    return out


def _name(src: Source, node) -> str | None:
    """The name as the word index sees it: `@class` is searched as `class`; a name the index cannot
    see whole (non-ASCII letters, escapes) is never a unit — it would look unused."""
    if node is None:
        return None
    name = _txt(src, node).lstrip("@")
    return name if IDENT.fullmatch(name) else None


def _start(src: Source, node) -> int:
    """Where a member begins, counting the `///` documentation lines directly above it."""
    start = node.start_byte
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment" and src.data.startswith(b"///", prev.start_byte):
        gap = src.data[prev.end_byte:start]
        line_start = src.data.rfind(b"\n", 0, prev.start_byte) + 1
        if gap.strip() or gap.count(b"\n") > 1 or src.data[line_start:prev.start_byte].strip():
            break
        start = prev.start_byte
        prev = prev.prev_sibling
    return start


def _strings_of(src: Source, rel: str) -> frozenset[str] | None:
    """Every word inside a string literal of another C# file; None when that file was not parsed."""
    entry = _STRING_WORDS.get(src.index)
    if entry is None:
        entry = _STRING_WORDS[src.index] = ({s.rel: i for i, s in enumerate(src.sources)}, {})
    where, cache = entry
    if rel not in cache:
        other = src.sources[where[rel]] if rel in where else None
        if other is None or other.tree is None:
            cache[rel] = None
        else:
            cache[rel] = frozenset(w for n in walk(other.tree.root_node) if n.type in STRINGS
                                   for w in IDENT.findall(_txt(other, n)))
    return cache[rel]


def _txt(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", "replace")


PACK = CSharpPack()
