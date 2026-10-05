"""
CSS and SCSS: style rules and custom properties nothing in the repository asks for.

  style      a rule whose selector is one compound naming a class — `.btn`, `.btn:hover`,
             `button.btn.primary`, `.card::before` — searched for by its first class; one naming
             an id and no class (`#main`); and `@keyframes spin`, searched for by its name, which
             an `animation` in a stylesheet, an inline style or a script must say.
  variable   a custom property declaration, `--gap: 4px;`, searched for by its name.

All are scope repo: markup, scripts, templates and docs anywhere here can name them, and a name
found nowhere is a removal candidate. Kept for judgement, however unused:

  - classes scripts toggle at run time (active, show, is-*, …), transition classes
    (fade-enter-active), and classes a library or platform adds from code that is not here
    (hljs-*, modal-backdrop, wp-*, …);
  - a name a script may build: its start up to a '-' or '_' ("btn-" + kind, `btn-${kind}`) or
    its tail (`${size}-lg`, '%s-lg' % size) stands alone in a script, template or page; or a
    script glues parts with the name's separator (['icon', n].join('-'), `${block}__${elem}`) and
    holds its start as a string a few lines away — anywhere in a file using a BEM helper;
  - a rule holding an @include or an at-rule other than @media-like ones and @apply: a mixin may
    @at-root rules out of it; a CSS module's classes when a script reads it with a computed key
    or hands the whole module on (<Child classes={styles}>);
  - the classes of a stylesheet whose markup is mostly elsewhere: no file names it, or fewer than
    half the classes it defines appear in any markup or script here — a theme's, a CMS's or a
    framework's markup, most likely;
  - custom properties a framework reads (--bs-*, --ifm-*, …), any when a script builds their
    names or a page loads a stylesheet from elsewhere, and a file's when most of them are read
    nowhere here; keyframes, too, when a stylesheet from elsewhere is loaded (it may name them,
    or a rule here may override its own);
  - everything on a platform that renders markup of its own (WordPress, Drupal, Ghost, Shopify,
    Sphinx, MkDocs);
  - a published package's stylesheets are its API: public, judged and never removed.

Not units: selector lists, combinators, `&` and nested rules, rules holding @keyframes or
@at-root (those reach past the rule), %placeholders, mixin bodies,
escaped or interpolated names, classes inside :not() and other pseudo-class arguments; files
under vendor/, minified files, and a stylesheet that announces itself as a released library.
"""

from __future__ import annotations

import bisect
import posixpath
import re

from . import CSS_TOKEN, IDENT, Pack, Source, Unit, walk

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
VAR = re.compile(r"--[A-Za-z_][A-Za-z0-9_-]*")
SHEETS = (".css", ".scss", ".sass", ".less", ".styl", ".pcss", ".postcss")
SIMPLE = {"class_selector", "id_selector", "pseudo_class_selector", "pseudo_element_selector",
          "attribute_selector"}
GROUPS = {"media_statement", "supports_statement"}
AT_GROUPS = {b"@layer", b"@container"}
# at-rules inside a rule that only style that rule's elements
INNER_AT = {b"@apply", b"@screen", b"@container", b"@layer", b"@media", b"@supports", b"@extend", b"@debug",
            b"@warn", b"@error", b"@variants", b"@responsive"}

STATE = {"active", "show", "open", "hidden", "hide", "disabled", "selected", "current", "visible",
         "invisible", "fade", "in", "out", "collapse", "collapsed", "collapsing", "expanded", "loading",
         "loaded", "error", "success", "focus", "hover", "dark", "light", "sticky", "fixed", "no-js",
         "closed", "checked", "valid", "invalid", "scrolled", "dragging", "animating", "ready",
         "showing", "hiding", "focus-visible", "js", "touch", "no-touch", "pending", "transitioning", "rtl", "ltr"}
STATE_PREFIX = ("is-", "has-", "js-", "ng-", "v-", "router-link-", "swiper-", "slick-")
TRANSITION = re.compile(r".-(enter|leave|exit|appear|move)(-(active|from|to|done))?$")
# classes and ids that libraries and platforms add from their own code, rarely in the repository
LIBRARY = re.compile(
    r"(hljs|token$|language-|lang-|line-numbers|highlight|chroma|codehilite|linenos|shiki|footnote|"
    r"reversefootnote|task-list|contains-task-list|headerlink|header-anchor|admonition|katex|mjx-|MathJax|"
    r"modal-backdrop|modal-open|modal-static|offcanvas-backdrop|tooltip|popover|bs-|carousel-item-|"
    r"dropdown-backdrop|was-validated|ui-|select2|choices|flatpickr|leaflet|mapboxgl|maplibregl|gm-|pac-|"
    r"swal2|toast|tippy|aos-|owl-|fancybox|mfp-|lb-|glightbox|gslide|pswp|plyr|vjs-|mejs|lg-|nprogress|pace-|"
    r"cm-|CodeMirror|ace_|monaco|ql-|tox-|mce|ck-|ProseMirror|fc-|dataTables|dt-|dz-|noUi-|irs-|simplebar|"
    r"ps__|headroom|sortable-|gu-|introjs|shepherd|driver-|typed-cursor|tagify|lazyload|flickity|glide|"
    r"splide|tns-|twitter-|fb_|fb-|instagram-|g-recaptcha|grecaptcha|cc-|cky-|ot-|onetrust|Cybot|hs-|"
    r"intercom|goog-|skiptranslate|gsc-|adsbygoogle|ais-|DocSearch|docsearch|algolia|StripeElement|"
    r"paypal|rc-|react-|Mui|ant-|el-|mat-|mdc-|cdk-|ion-|chakra-|mantine-|radix|headlessui|wp-|wp_|"
    r"align(left|right|center|none|wide|full)$|screen-reader-text|bypostauthor|gallery-|menu-item|"
    r"current-menu-|current_page_|page_item|sub-menu|kg-|shopify|gatsby-|__next|__nuxt|nuxt-|astro-|"
    r"svelte-|turbo-|turbolinks|livewire|animate__|animated$|wow$|d?mermaid|anchor$|hash-link|heading-anchor|"
    r"table-of-contents|markdown-body|theme-|navbar__|menu__|__docusaurus|wpadminbar|hubspot|crisp-|tidio|"
    r"beacon-|credential_picker|google_translate|cbox|colorbox|drift-|webWidget|tawk|olark|_hj|hotjar|"
    r"field_with_errors|errorlist|errornote|helptext|nonfield|asteriskField|ember-|tiptap|xterm|recharts-|"
    r"apexcharts|highcharts-|chartjs-|rdp|sonner|Toastify|medium-zoom|lucide|feather|iconify|tabler-icon|"
    r"phx-|htmx-|up-|sourceCode|docutils|toctree|pointer-event$|svg-inline)")
# custom properties a framework, a theme or a widget reads from code that is rarely in the repository
LIBRARY_VAR = re.compile(r"--(bs|tw|ifm|vp|vt|md|docsearch|swiper|plyr|toastify|rdp|mdc|mat|ion|chakra|"
                         r"mantine|sl|fa|pst|pico|bulma|nc|wp|radix|shiki|astro|mui|joy|ant|el|van|"
                         r"sd|rsbs|rt|toastify|reach|aa|ck|tox|fc|leaflet|maplibre|mapbox)-")
PLATFORM = {"wp_head": "WordPress", "wp_footer": "WordPress", "wp_enqueue_style": "WordPress",
            "get_template_part": "WordPress", "Drupal": "Drupal", "core_version_requirement": "Drupal",
            "ghost_head": "Ghost", "content_for_header": "Shopify", "content_for_layout": "Shopify",
            "html_static_path": "Sphinx", "html_css_files": "Sphinx", "extra_css": "MkDocs"}
BANNER = re.compile(rb"\A(?:\s*@charset[^;]*;)?\s*/\*.{0,800}?\*/", re.S)
RELEASED = re.compile(rb"\bv?\d+\.\d+(?:\.\d+)?\b")
LICENSE = re.compile(rb"licen[cs]e|copyright|\(c\)|\xc2\xa9", re.I)
MODULE = re.compile(r"\.module\.(css|scss|sass|less)$")
BUILDS_VARS = re.compile(r"""(['"`])--(\1|\$\{)""")
LINK = re.compile(r"<link\b[^>]*>", re.I)
HREF = re.compile(r"""\bhref\s*=\s*["']?([^"'\s>]+)""", re.I)
SHEET_IMPORT = re.compile(r"""@(?:import|use|forward)\s+(?:url\(\s*)?["']([^"']+)["']""")
SCRIPT_CSS = re.compile(r"""(?:\bimport\s+|\brequire\s*\(\s*)["']([^"'./][^"']*\.(?:css|scss))["']""")
# a script that glues class names together from parts: ['icon', name].join('-'), a + '__' + b,
# `${block}--${mod}`, '%s-%s' % (a, b) — or hands that job to a BEM helper library
JOINER = re.compile(r"""\.join\(\s*(['"`])(?:-|_|__|--)\1\s*\)|\+\s*(['"`])(?:-|_|__|--)\2\s*\+|"""
                    r"""\}(?:-|_|__|--)\$\{|%s(?:-|_|__|--)%s|\{\}(?:-|_|__|--)\{\}""")
BEM_HELPER = re.compile(r"""['"](?:bem-cn(?:-lite)?|@bem-react/classname|react-bem-helper|bem-classname|easy-bem|"""
                        r"""bem-css-modules|classnames-bem|bem-names|b_|bemit|@?[\w./-]*/bem)['"]|\bwithNaming\s*\(""")
NEAR = 300                    # characters around the glue a part's string may sit
QUOTED = re.compile(r"""(['"`])([A-Za-z_][A-Za-z0-9_-]*)\1""")
FONT_HOSTS = ("fonts.googleapis.com", "fonts.bunny.net", "use.typekit.net", "fonts.cdnfonts.com",
              "api.fontshare.com")


class CssPack(Pack):
    langs = ("CSS", "SCSS")
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        if "vendor" in src.rel.split("/")[:-1] or src.rel.endswith(".min.css") or _released(src.data):
            return []                                    # someone else's code
        return judge(src, rules(src.data, src.tree.root_node), [(0, len(src.data))])


# ---------------------------------------------------------------- what a stylesheet defines

def rules(data: bytes, root, base: int = 0) -> list[Unit]:
    """The units of a stylesheet. Offsets are into `data`, plus `base` (where a <style> block
    starts in its page)."""
    out: list[Unit] = []
    for node in _statements(root):
        span = (base + node.start_byte, base + node.end_byte)
        if node.type == "keyframes_statement":
            name = next((c for c in node.named_children if c.type == "keyframes_name"), None)
            word = _txt(data, name) if name is not None else ""
            if NAME.fullmatch(word):
                out.append(Unit(kind="style", name=word, start=span[0], end=span[1], scope="repo", tokens="css",
                                show=f"@keyframes {word}", cut=span))
            continue
        unit = _rule(data, node, span)
        if unit is not None:
            out.append(unit)
            continue                                     # its own declarations go with it
        block = next((c for c in node.named_children if c.type == "block"), None)
        for decl in (block.named_children if block is not None else ()):
            prop = decl.named_children[0] if decl.type == "declaration" and decl.named_children else None
            word = _txt(data, prop) if prop is not None and prop.type == "property_name" else ""
            if VAR.fullmatch(word):
                cut = (base + decl.start_byte, base + decl.end_byte)    # the declaration and its ';'
                out.append(Unit(kind="variable", name=word, start=cut[0], end=cut[1], scope="repo",
                                tokens="css", cut=cut))
    return out


def _statements(node):
    """Rule sets and keyframes at the top level or in a conditional group (@media, @supports,
    @layer, @container) — never inside another rule, a mixin, an include or a loop."""
    for n in node.named_children:
        if n.type in ("rule_set", "keyframes_statement"):
            yield n
        elif n.type in GROUPS or (n.type == "at_rule" and n.named_children
                                  and n.named_children[0].type == "at_keyword"
                                  and n.named_children[0].text in AT_GROUPS):
            block = next((c for c in n.named_children if c.type == "block"), None)
            if block is not None:
                yield from _statements(block)


def _rule(data: bytes, node, span: tuple[int, int]) -> Unit | None:
    """A rule set whose selector is one compound with a class (or, failing that, an id)."""
    sel = node.named_children[0] if node.named_children else None
    block = next((c for c in node.named_children if c.type == "block"), None)
    if sel is None or sel.type != "selectors" or len(sel.children) != 1 or block is None:
        return None
    keep = ""
    for n in walk(block):
        if n.type in ("rule_set", "nesting_selector", "ERROR", "keyframes_statement", "at_root_statement"):
            return None                                  # nested rules style other elements; keyframes are global
        if n.type == "include_statement" or (n.type == "at_rule" and n.named_children
                                             and n.named_children[0].text not in INNER_AT):
            keep = "a mixin or at-rule inside it may emit rules beyond it (@at-root, @font-face, @keyframes)"
    parts = _compound(data, sel.children[0]) or []
    classes = [w for k, w in parts if k == "class"]
    ids = [w for k, w in parts if k == "id"]
    word, show = (classes[0], "." + classes[0]) if classes else (ids[0], "#" + ids[0]) if ids else ("", "")
    if not NAME.fullmatch(word):
        return None
    return Unit(kind="style", name=word, start=span[0], end=span[1], scope="repo", tokens="css", show=show,
                cut=span, keep=keep)


def _compound(data: bytes, node) -> list[tuple[str, str]] | None:
    """The classes and ids of one compound selector, left to right; None for anything else —
    a combinator, `&`, a placeholder. A class inside :not(…) or :is(…) is an argument, not
    part of the compound: `p:not(.lead)` styles every other paragraph."""
    parts: list[tuple[str, str]] = []
    while node.type in SIMPLE:
        if node.type == "class_selector":
            parts.append(("class", _txt(data, node.children[-1])))
        elif node.type == "id_selector":
            parts.append(("id", _txt(data, node.children[-1])))
        first = node.children[0]
        if not first.is_named:                           # nothing to its left
            return parts[::-1]
        node = first
    return parts[::-1] if node.type in ("tag_name", "universal_selector") else None


def _released(data: bytes) -> bool:
    """A licence banner with a version — `/*! Bootstrap v5.3.0 … Licensed under MIT */` — marks a
    library copied in, not the project's own styles."""
    m = BANNER.match(data[:2000])
    return bool(m and RELEASED.search(m.group()) and LICENSE.search(m.group()))


def _txt(data: bytes, node) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", "replace")


# ---------------------------------------------------------------- what keeps an unused name

def judge(src: Source, units: list[Unit], sheets: list[tuple[int, int]]) -> list[Unit]:
    """Mark the units a search for their name cannot settle. `sheets` are the stylesheet spans
    of `src` — the whole file, or a page's <style> blocks; what is outside them is markup."""
    if src.index is None or not units:                   # found again for an edit: names and spans only
        return units
    repo = _facts(src)
    published = _published(src)
    unnamed = _named_nowhere(src)
    names = {u.name for u in units if u.kind == "style" and not u.show.startswith("@")}
    seen = {n for n in names if _in_markup(src, n, sheets) or _module_name(src, n)}
    elsewhere = len(seen) * 2 < len(names)
    variables = [u for u in units if u.kind == "variable"]
    var_names = {u.name for u in variables}
    unread = len([n for n in var_names if _read(src, n, variables)]) * 2 < len(var_names)
    for u in units:
        if published:
            u.scope = "public"
            u.keep = f"a stylesheet of the package {published} describes, which other projects install"
        elif u.kind == "variable":
            u.keep = _keep_var(src, u.name, repo, unread)
        elif u.show.startswith("@"):
            u.keep = (_keep_built(src, u.name)
                      or (repo["external"] and f"this project loads a stylesheet from elsewhere ({repo['external']}), "
                                               "which may name these keyframes or have them overridden here"))
        else:
            u.keep = (u.keep or _keep_name(u.name) or _keep_built(src, u.name) or _keep_joined(src, u.name, repo)
                      or _keep_module(src, u.name)
                      or (repo["platform"] and f"{repo['platform']} renders markup from code that is not here")
                      or (unnamed and "no file here names this stylesheet; what loads it, and the markup "
                                      "it styles, is elsewhere")
                      or (elsewhere and "most classes in this stylesheet appear in no markup or script here; "
                                        "its markup probably lives elsewhere (a theme, a CMS, a framework)"))
    return units


def _keep_name(name: str) -> str:
    if name in STATE or name.startswith(STATE_PREFIX):
        return "scripts commonly toggle this class at run time"
    if TRANSITION.search(name):
        return "a transition component adds it from its name (name-enter-active)"
    if LIBRARY.match(name):
        return "a library or platform adds this name from code that is usually not in the repository"
    return ""


def _keep_built(src: Source, name: str) -> str:
    piece, where = _built(src, name)
    return f"a script may build it: '{piece}' stands alone in {where}" if piece else ""


def _keep_joined(src: Source, name: str, repo: dict) -> str:
    """A script that glues names from parts (.join('-'), a + '__' + b, `${a}-${b}`) and holds this
    name's block as a string a few lines from the glue — 'icon' for icon-home — may build it; with a
    BEM helper (block('button')('icon') → button__icon) the string can be anywhere in the file."""
    for i in range(1, len(name)):
        if name[i] not in "-_" or name[i - 1] in "-_":
            continue
        sep = name[i:i + 2] if name[i:i + 2] in ("__", "--") else name[i]
        rel = repo["glued"].get(sep, {}).get(name[:i]) or repo["bem"].get(name[:i])
        if rel:
            return f"{rel} builds class names from parts, and '{name[:i]}' is a string beside the glue"
    return ""


def _keep_var(src: Source, name: str, repo: dict, unread: bool) -> str:
    if LIBRARY_VAR.match(name):
        return "a framework or widget reads custom properties named like this"
    if repo["builds_vars"]:
        return f"a script builds custom property names ({repo['builds_vars']})"
    if repo["external"]:
        return f"this project loads a stylesheet from elsewhere ({repo['external']}), which may read it"
    if unread:
        return "most custom properties in this file are read nowhere here; something elsewhere probably reads them"
    return _keep_built(src, name[2:])


def _module_name(src: Source, name: str) -> str:
    """A CSS module's class as scripts see it: `styles.fooBar` for .foo-bar."""
    if not MODULE.search(src.rel):
        return ""
    camel = re.sub(r"[-_]+([A-Za-z0-9])", lambda m: m.group(1).upper(), name)
    return camel if camel != name and src.index.files_with(camel) else ""


def _keep_module(src: Source, name: str) -> str:
    """A CSS module's classes reach scripts as properties — `styles.fooBar`, or `styles[kind]`,
    which can be any of them."""
    camel = _module_name(src, name)
    if camel:
        return f"a CSS module exposes it to scripts as '{camel}'"
    if not MODULE.search(src.rel):
        return ""
    seen = _facts(src).setdefault("modules", {})
    if src.rel not in seen:
        seen[src.rel] = _module_read_whole(src)
    return seen[src.rel]


def _module_read_whole(src: Source) -> str:
    """A script that reads a CSS module by a computed key or hands the whole object on."""
    base = re.escape(posixpath.basename(src.rel))
    binding = re.compile(r"import\s+(?:\*\s+as\s+)?(\w+)\s+from\s+['\"][^'\"]*" + base
                         + r"|(\w+)\s*=\s*require\(\s*['\"][^'\"]*" + base)
    for s in src.sources:
        for m in binding.finditer(s.text):
            b = m.group(1) or m.group(2)
            rest = s.text[:m.start()] + s.text[m.end():]                # the import itself aside
            for use in re.finditer(rf"(?<![\w$.]){re.escape(b)}(?![\w$])", rest):
                after = rest[use.end():use.end() + 80]
                if re.match(r"\s*(?:\?\.)?\s*\[", after):
                    return f"{s.rel} reads this CSS module with a computed key ({b}[…])"
                if re.match(r"\s*\??\.\s*[A-Za-z_$]", after) or re.match(r"\s*=[^=>]", after):
                    continue                                            # styles.foo, or a name being set
                return f"{s.rel} hands the whole CSS module on ({b}), where any of its classes may be read"
    return ""


def _built(src: Source, name: str) -> tuple[str, str]:
    """The piece of `name` a script, template or page holds on its own, and where: 'btn-' for
    btn-primary ("btn-" + kind, `btn-${kind}`, btn-{{ kind }}), or '-primary' (`${base}-primary`)."""
    pieces = []
    for i in range(1, len(name)):
        if name[i] in "-_":
            pieces.append(name[:i + 1])
        if name[i] == "-" or (name[i:i + 2] == "__" and name[i - 1] != "_"):
            pieces.append(name[i:])
    for piece in pieces:
        if "-" in piece:
            files = src.index.files_with(piece, "css")
        else:
            files = src.index.files_with(piece) | src.index.files_with(piece + "$")   # `card__${elem}`
        if piece[0] in "-_":
            files = files | src.index.files_with("s" + piece, "css") | src.index.files_with("d" + piece, "css")
            # '%s-lg' % size, printf("%d-col")
        where = next((f for f in sorted(files) if not f.endswith(SHEETS)), "")
        if where:
            return piece, where
    return "", ""


def _in_markup(src: Source, name: str, sheets: list[tuple[int, int]]) -> bool:
    """`name` appears outside stylesheets: in another file, or in this page outside its <style>s."""
    if any(not f.endswith(SHEETS) for f in src.index.files_with(name, "css") - {src.rel}):
        return True
    if src.rel.endswith(SHEETS):
        return False
    inside = sum(_count(src.data[a:b].decode("utf-8", "replace"), name) for a, b in sheets)
    return src.index.count(src.rel, name, "css") > inside


def _read(src: Source, name: str, variables: list[Unit]) -> bool:
    """A custom property is named somewhere besides its own declarations here."""
    if src.index.files_with(name, "css") - {src.rel}:
        return True
    own = sum(_count(src.data[u.start:u.end].decode("utf-8", "replace"), name) for u in variables if u.name == name)
    return src.index.count(src.rel, name, "css") > own


def _count(text: str, word: str) -> int:
    if "-" in word:
        return sum(1 for m in CSS_TOKEN.findall(text) if m.lstrip("-") == word.lstrip("-"))
    return sum(1 for m in IDENT.findall(text) if m == word)


def _stem(rel: str) -> str:
    return posixpath.basename(rel).split(".")[0].lstrip("_")


def _named_nowhere(src: Source) -> bool:
    """No other file names this stylesheet (`style` for css/style.css, `buttons` for
    _buttons.scss): whatever loads it — and the markup it styles — is not here."""
    if not src.rel.endswith(SHEETS):
        return False
    stem = _stem(src.rel)
    return bool(stem) and not (src.index.files_with(stem, "css") - {src.rel})


def _published(src: Source) -> str:
    """The nearest package.json above the stylesheet, when it describes a package other projects
    install — not private, and it lists its files or its stylesheet — or a bower.json."""
    words = src.index.ident
    d = posixpath.dirname(src.rel)
    while True:
        prefix = d + "/" if d else ""
        if prefix + "bower.json" in words:
            return prefix + "bower.json"
        pkg = prefix + "package.json"
        if pkg in words:
            c = words[pkg]
            if not c.get("private") and (any(c.get(k) for k in ("style", "sass", "files", "unpkg", "jsdelivr"))
                                         or prefix + ".npmignore" in words):
                return pkg
            return ""
        if not d:
            return ""
        d = posixpath.dirname(d)


_FACTS: dict = {"index": None, "facts": None}


def _facts(src: Source) -> dict:
    """What holds for the whole repository, worked out once per audit."""
    if _FACTS["index"] is src.index:
        return _FACTS["facts"]
    platform = next((p for w, p in PLATFORM.items() if src.index.files_with(w)), "")
    stems = {_stem(rel) for rel in src.index.ident if rel.endswith(SHEETS)}
    builds = external = ""
    glued: dict[str, dict[str, str]] = {}               # separator → string beside it → file
    bem: dict[str, str] = {}                            # any string in a file using a BEM helper → file
    for s in src.sources:
        if not s.rel.endswith(SHEETS):
            m = BUILDS_VARS.search(s.text)
            builds = builds or (s.rel if m else "")
            _glue(s, glued, bem)
        found = [h.group(1) for tag in LINK.findall(s.text) if "stylesheet" in tag.lower()
                 for h in HREF.finditer(tag)]
        found = [u for u in found if u.startswith(("http:", "https:", "//"))]
        found += [u for u in SCRIPT_CSS.findall(s.text)]                 # import 'bootstrap/dist/css/…'
        for u in SHEET_IMPORT.findall(s.text) if s.rel.endswith(SHEETS) else ():
            if u.startswith(("http:", "https:", "//")) or not (u.startswith("sass:") or _stem(u) in stems):
                found.append(u)                                          # not a stylesheet of this repo
        found = [u for u in found if not any(h in u for h in FONT_HOSTS)]
        external = external or (found[0] if found else "")
    facts = {"platform": platform, "builds_vars": builds, "external": external, "glued": glued, "bem": bem}
    _FACTS.update(index=src.index, facts=facts)
    return facts


def _glue(s: Source, glued: dict, bem: dict) -> None:
    """The strings a script holds a few lines from where it glues parts with a separator."""
    if BEM_HELPER.search(s.text):
        for m in QUOTED.finditer(s.text):
            bem.setdefault(m.group(2), s.rel)
        return
    starts: dict[str, list[int]] = {}
    for m in JOINER.finditer(s.text):
        starts.setdefault(re.search(r"[-_]+", m.group()).group(), []).append(m.start())
    if not starts:
        return
    for m in QUOTED.finditer(s.text):
        for sep, at in starts.items():
            k = bisect.bisect_right(at, m.start() + NEAR)
            if k and at[k - 1] >= m.start() - NEAR - 20:
                glued.setdefault(sep, {}).setdefault(m.group(2), s.rel)


PACK = CssPack()
