"""
Dead code beyond Python: the same three questions, asked in every language Justify can parse.

Each language gets a small pack that reads a file's syntax tree (tree-sitter) and names its
*units* — imports, functions, classes, methods, fields, style rules — together with the one
thing the language itself guarantees about each: how far it can be seen.

    file      only this file can use it: an import, a private method, a static C function,
              a JavaScript function nobody exports
    package   only files next to it can: an unexported Go function
    repo      anything in this repository could, nothing outside it: a CSS class, a private
              Rust function — searched for everywhere, and a removal candidate if never found
    public    anything could: an exported function, a public class — another project may
              call it, so it is never removed from here, only flagged for judgement

The core then looks for each name, as a whole word, everywhere it could be used — in code,
comments, strings, markup and config — outside the unit itself. A unit is a removal candidate
only when it is seen nowhere it could be seen from, and its language says nothing else can
reach it. Anything a framework or the runtime calls by name (annotations, decorators,
lifecycle methods, exports) is kept or sent for judgement. Doubt means keep.

Removals are proved the way Python's are: in a temporary copy, cut out of the source, the file
parsed again — a cut that breaks the syntax is refused — and the project's tests run.

Needs tree-sitter (`pip install "justify-code[languages]"`); without it, other languages keep
their census and copy check.
"""

from __future__ import annotations

import os
import pathlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..model import AMBIGUOUS, KEEP, REMOVE, Finding

try:                                                     # optional: the core has no dependencies
    from tree_sitter_language_pack import get_parser as _get_parser
    AVAILABLE = True
except Exception:                                        # noqa: BLE001 — any import failure means "not installed"
    _get_parser = None
    AVAILABLE = False

# Justify's language names (polyglot.LANGS) → tree-sitter grammars; a file's suffix can refine it
GRAMMARS = {"JavaScript": "javascript", "TypeScript": "typescript", "Java": "java", "Kotlin": "kotlin",
            "C#": "csharp", "C": "c", "C/C++ header": "cpp", "C++": "cpp", "CSS": "css", "SCSS": "scss",
            "HTML": "html", "Go": "go", "Rust": "rust", "PHP": "php", "Swift": "swift"}
SUFFIX_GRAMMAR = {".tsx": "tsx", ".jsx": "javascript"}
MAX_PARSE_BYTES = 400_000

IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
CSS_TOKEN = re.compile(r"-?-?[A-Za-z_][A-Za-z0-9_-]*")


@dataclass
class Unit:
    """Something a pack found that could be dead. Byte offsets are into the file's UTF-8 bytes."""
    kind: str                      # import, function, class, method, field, variable, type, style
    name: str                      # the word searched for
    start: int                     # the unit's own text, which never counts as a use of it
    end: int
    scope: str                     # file, package, repo or public
    cut: tuple[int, int] | None = None   # what a removal deletes, when it is not (start, end)
    keep: str = ""                 # a reason it must not be removed even if unused → judgement
    reason: str = ""               # overrides the core's reason
    tokens: str = "ident"          # ident, or css (names with hyphens)
    show: str = ""                 # the name as shown, when it differs (".btn", "--gap", "@keyframes spin")


@dataclass
class Source:
    rel: str
    lang: str
    data: bytes
    text: str
    tree: object = None
    errors: int = 0
    units: list[Unit] = field(default_factory=list)
    pack: object = None
    index: object = None            # the repository's word index, for packs that must look wider
    sources: list = field(default_factory=list)    # every parsed file, same reason


def parser_for(lang: str, rel: str):
    grammar = SUFFIX_GRAMMAR.get(pathlib.PurePosixPath(rel).suffix.lower()) or GRAMMARS.get(lang)
    return _get_parser(grammar) if grammar and AVAILABLE else None


def walk(node):
    """Every node under `node`, depth first, without recursion."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def error_count(tree) -> int:
    return sum(1 for n in walk(tree.root_node) if n.type == "ERROR" or n.is_missing)


def text_of(src: Source, node) -> str:
    return src.data[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def line_of(src: Source, byte: int) -> int:
    return src.data.count(b"\n", 0, byte) + 1


def whole_lines(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """(start, end) widened to whole lines when the span owns them — only whitespace before it
    on its first line and after it on its last — else None."""
    ls = data.rfind(b"\n", 0, start) + 1
    le = data.find(b"\n", end)
    le = len(data) if le < 0 else le + 1
    if data[ls:start].strip() or data[end:le].strip():
        return None
    return ls, le


class Pack:
    implemented = False             # a placeholder pack names no units and does not count as an audit

    """A language pack. `units` names what could be dead; `entry` says a file is called from
    outside (its public units are then used by definition); `package` lists the files that can
    see a package-scope unit; `tokens_from` says which files' words count as uses."""
    langs: tuple[str, ...] = ()

    def units(self, src: Source) -> list[Unit]:
        return []

    def entry(self, rel: str) -> bool:
        return False

    def package(self, src: Source, sources: list[Source]) -> list[Source]:
        return [src]

    def tidy(self, data: bytes) -> bytes:
        """Clean-up after cuts — an import list left empty, say. Must keep the code's meaning."""
        return data


def full_languages() -> set[str]:
    """Languages Justify audits for dead code here, besides Python."""
    if not AVAILABLE:
        return set()
    return {lang for lang, pack in _packs().items() if pack.implemented}


def _packs() -> dict[str, Pack]:
    from . import cfamily, csharp, css, go, html, java, js, kotlin, php, rust, swift
    out: dict[str, Pack] = {}
    for mod in (js, css, html, java, kotlin, csharp, cfamily, go, rust, php, swift):
        pack = mod.PACK
        for lang in pack.langs:
            out[lang] = pack
    return out


class Index:
    """How often each word appears in each file — the one search every unit's question uses."""

    def __init__(self):
        self.ident: dict[str, Counter] = {}
        self.css: dict[str, Counter] = {}
        self.where: dict[str, set[str]] = defaultdict(set)      # word → files that hold it

    def add(self, rel: str, text: str) -> None:
        c = Counter(IDENT.findall(text))
        self.ident[rel] = c
        for w in c:
            self.where[w].add(rel)
        cc = Counter(m.lstrip("-") for m in CSS_TOKEN.findall(text) if "-" in m)
        self.css[rel] = cc
        for w in cc:
            self.where["css:" + w].add(rel)

    def count(self, rel: str, word: str, kind: str = "ident") -> int:
        table = self.css if kind == "css" and "-" in word else self.ident
        return table.get(rel, Counter()).get(word.lstrip("-") if table is self.css else word, 0)

    def files_with(self, word: str, kind: str = "ident") -> set[str]:
        if kind == "css" and "-" in word:
            return self.where.get("css:" + word.lstrip("-"), set())
        return self.where.get(word, set())


def _inside(src: Source, unit: Unit, word: str, kind: str) -> int:
    """How many times the word appears inside the unit's own text (its definition)."""
    chunk = src.data[unit.start:unit.end].decode("utf-8", errors="replace")
    if kind == "css" and "-" in word:
        return sum(1 for m in CSS_TOKEN.findall(chunk) if m.lstrip("-") == word.lstrip("-"))
    return sum(1 for m in IDENT.findall(chunk) if m == word)


def audit(root: pathlib.Path, census: list, texts: dict[str, str] | None = None) -> tuple[list[Finding], dict]:
    """Findings for every non-Python file a pack can read. `census` is polyglot.census(root);
    every text file in it — code, markup, config, docs — counts as a place a name can be used."""
    note = {"available": AVAILABLE, "files": 0, "lines": 0, "languages": [], "unparsed": []}
    if not AVAILABLE:
        return [], note
    packs = _packs()
    # every syntax tree stays in memory until the audit ends (about 0.5 KB a line), so a server
    # sets a ceiling; past it the other languages are only checked for copies, and the audit says so
    cap = int(os.environ.get("JUSTIFY_LANG_MAX_LINES") or 0)
    if cap:
        size = sum(c.lines for c in census
                   if not c.generated and getattr(packs.get(c.lang), "implemented", False))
        if size > cap:
            note["skipped"] = size
            return [], note
    index = Index()
    sources: list[Source] = []
    for c in census:
        text = (texts or {}).get(c.rel)
        if text is None:
            try:
                raw = (root / c.rel).read_bytes()
            except OSError:
                continue
            text = raw.decode("utf-8", errors="replace")
        index.add(c.rel, text)
        pack = packs.get(c.lang)
        if pack is None or not pack.implemented or c.generated:
            continue
        data = text.encode("utf-8")
        if len(data) > MAX_PARSE_BYTES:
            continue
        p = parser_for(c.lang, c.rel)
        if p is None:
            continue
        src = Source(rel=c.rel, lang=c.lang, data=data, text=text, pack=pack)
        src.tree = p.parse(data)
        src.errors = error_count(src.tree)
        sources.append(src)
    for rel_text in (texts or {}):                       # files outside the census still hold words
        if rel_text not in index.ident:
            index.add(rel_text, texts[rel_text])
    _index_the_rest(root, index)                         # templates, Razor, XAML, configs: uses too

    findings: list[Finding] = []
    langs: Counter = Counter()
    for src in sources:
        langs[src.lang] += src.data.count(b"\n") + (0 if src.data.endswith(b"\n") or not src.data else 1)
        if src.errors:
            note["unparsed"].append(src.rel)       # a file the grammar cannot read cleanly is not judged
            continue
        src.index, src.sources = index, sources
        try:
            src.units = src.pack.units(src)
        except Exception as exc:                     # noqa: BLE001 — one odd file never stops an audit
            note.setdefault("pack_errors", []).append(f"{src.rel}: {type(exc).__name__}")
            continue
        for u in src.units:
            f = _decide(src, u, sources, index)
            if f is not None:
                findings.append(f)
    note["files"] = len(sources)
    note["lines"] = sum(langs.values())
    note["languages"] = sorted(langs)
    note["lines_by_language"] = dict(langs)
    return findings, note


TEXT_LIMIT = 8_000_000         # bytes per file read only for its words (Unity scenes and prefabs run large)
TEXT_FILES_MAX = 20_000
# folders never read for words: tools' caches and other people's installed code. Build output and
# vendored code ARE read — markup there can be the only place a class is used.
INDEX_SKIP = {".git", ".hg", ".svn", "node_modules", "bower_components", ".venv", "venv", "env", "site-packages",
              "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".eggs", ".justify",
              ".next", ".nuxt", ".svelte-kit", ".turbo", ".cache", ".parcel-cache", ".gradle", "Pods", "DerivedData",
              "target", "coverage"}


def _index_the_rest(root: pathlib.Path, index: Index) -> None:
    """Every other text file in the repository — templates, Razor and XAML views, configs, docs —
    is a place a name can be used, even if Justify does not audit its language."""
    from ..ingest import BINARY_SUFFIXES, inside
    seen = 0
    for path in root.rglob("*"):
        if seen >= TEXT_FILES_MAX:
            break
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        rel = "/".join(parts)
        if rel in index.ident or any(part in INDEX_SKIP or part.endswith(".egg-info") for part in parts[:-1]) \
                or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if not path.is_file() or not inside(root, path) or path.stat().st_size > TEXT_LIMIT:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        index.add(rel, raw.decode("utf-8", errors="replace"))
        seen += 1


def _decide(src: Source, u: Unit, sources: list[Source], index: Index) -> Finding | None:
    word = u.name
    own = index.count(src.rel, word, u.tokens) - _inside(src, u, word, u.tokens)
    if u.scope == "file":
        if own > 0:
            return None
        where = "anywhere in its file"
    elif u.scope == "package":
        if own > 0:
            return None
        mates = [s.rel for s in src.pack.package(src, sources) if s.rel != src.rel]
        if any(index.count(r, word, u.tokens) for r in mates):
            return None
        where = "anywhere in its package"
    elif u.scope == "repo":                          # anything here could use it, nothing outside
        if own > 0 or (index.files_with(word, u.tokens) - {src.rel}):
            return None
        where = "anywhere in the repository"
    else:                                            # public: any file could use it
        if src.pack.entry(src.rel):
            return None
        if own > 0 or (index.files_with(word, u.tokens) - {src.rel}):
            return None
        where = "anywhere in the repository"
    line, end_line = line_of(src, u.start), line_of(src, max(u.start, u.end - 1))
    shown = u.show or word
    reason = u.reason or (f"no use of '{shown}' {where}")
    if u.scope == "public" and not u.keep:
        u.keep = "nothing here uses it, but it is public — another project may"
    verdict = AMBIGUOUS if u.keep else REMOVE
    if u.keep:
        reason = f"{reason}; {u.keep}"
    return Finding(kind=u.kind, file=src.rel, line=line, end_line=end_line, name=shown, verdict=verdict,
                   reason=reason, lines=end_line - line + 1, evidence=[])


# ---------------------------------------------------------------- proving a removal

def handles(rel: str) -> bool:
    from .. import polyglot
    lang = polyglot.language_of(rel)
    return bool(AVAILABLE and lang and lang != "Python" and lang in _packs())


def edit_source(rel: str, text: str, items: list[Finding]) -> str:
    """The file with the given findings cut out. Raises SyntaxError when the result no longer
    parses as cleanly as the original, and SharedLine-style ValueError when a unit cannot be cut
    on its own (the caller treats both as 'not provable')."""
    from .. import polyglot
    from ..proof import SharedLine
    lang = polyglot.language_of(rel)
    pack = _packs()[lang]
    data = text.encode("utf-8")
    p = parser_for(lang, rel)
    src = Source(rel=rel, lang=lang, data=data, text=text, pack=pack)
    src.tree = p.parse(data)
    before = error_count(src.tree)
    units = pack.units(src)
    spans: list[tuple[int, int]] = []
    for f in items:
        match = [u for u in units if u.kind == f.kind and (u.show or u.name) == f.name
                 and line_of(src, u.start) == f.line]
        if not match:
            raise SharedLine(f"{f.kind} {f.name} at line {f.line} was not found again")
        u = match[0]
        a, b = u.cut or (u.start, u.end)
        wide = whole_lines(data, a, b)
        if wide is None and u.cut is None:
            raise SharedLine(f"{f.name} shares its line with other code")
        spans.append(wide or (a, b))
    out = data
    for a, b in sorted(set(spans), reverse=True):
        out = out[:a] + out[b:]
    out = pack.tidy(out)
    after = error_count(p.parse(out))
    if after > before:
        raise SyntaxError(f"removing it leaves {rel} unparsable")
    return out.decode("utf-8")


__all__ = ["AVAILABLE", "Pack", "Source", "Unit", "audit", "edit_source", "handles", "line_of", "text_of",
           "walk", "whole_lines", "KEEP"]
