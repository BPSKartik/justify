"""CSS, SCSS and HTML: style rules, keyframes and custom properties nothing asks for, and the
<style> and module <script> blocks of a page. Each trap sits beside dead code that must still be
found, so a trap that passes is not just a stylesheet that was never judged."""

import pytest

pytest.importorskip("tree_sitter_language_pack")

from justify.engine import run  # noqa: E402
from justify.langs import edit_source, error_count, parser_for  # noqa: E402

R, A = "REMOVE", "AMBIGUOUS"
SHEET_EXT = (".css", ".scss", ".html")

PAGE = """<!doctype html>
<html><head><link rel="stylesheet" href="style.css"></head>
<body>
  <main class="shell"><div class="card"><p class="note">{body}</p></div></main>
  <nav class="bar"><a class="link">a</a><i class="icon"></i><b class="badge"></b><em class="hint"></em></nav>
  <script src="app.js"></script>
</body></html>
"""
BASE = """.shell { display: grid; }
.card { padding: 1rem; }
.note { color: gray; }
.bar { margin: 0; }
.link { margin: 0; }
.icon { margin: 0; }
.badge { margin: 0; }
.hint { color: gray; }
"""


def audit(root) -> dict[tuple[str, str], tuple[str, str]]:
    """(file, name) → (kind, verdict) for every stylesheet and page finding."""
    res = run(root, record=False)
    return {(f.file, f.name): (f.kind, f.verdict) for f in res.findings
            if f.file.endswith(SHEET_EXT) and f.kind != "duplicate"}


def site(make_repo, css="", body="", extra=None, sheet="style.css"):
    """A page that loads one stylesheet whose base classes it uses, so the sheet is judged."""
    page = PAGE.replace("style.css", sheet).replace("{body}", body)
    return make_repo({"index.html": page, sheet: BASE + css, "app.js": "export {};\n", **(extra or {})})


def verdicts(root, rel="style.css"):
    return {name: v for (f, name), (_, v) in audit(root).items() if f == rel}


def parses(lang, rel, text) -> bool:
    return error_count(parser_for(lang, rel).parse(text.encode())) == 0


# ---------------------------------------------------------------- each rule finds dead code

def test_a_class_an_id_keyframes_and_a_variable_nothing_names_are_removed(make_repo):
    root = site(make_repo, """
.dead-box { margin: 0; }
#dead-id { margin: 0; }
@keyframes deadspin { to { transform: rotate(1turn); } }
:root { --dead-gap: 4px; --live-gap: 2px; }
.card { gap: var(--live-gap); }
""")
    v = verdicts(root)
    assert v == {".dead-box": R, "#dead-id": R, "@keyframes deadspin": R, "--dead-gap": R}


def test_compound_selectors_are_found_by_their_first_class(make_repo):
    root = site(make_repo, """
button.ghost-btn.wide:hover { color: red; }
.ghost-tip::before { content: ''; }
@media (min-width: 40em) { .ghost-wide { width: 100%; } }
""")
    assert verdicts(root) == {".ghost-btn": R, ".ghost-tip": R, ".ghost-wide": R}


# ---------------------------------------------------------------- uses in markup and scripts

@pytest.mark.parametrize("rel,text", [
    ("App.jsx", 'export const A = () => <div className="jsx-only">x</div>;\n'),
    ("App.tsx", 'export const A = (on: boolean) => <i className={on ? "tsx-on" : ""} />;\n'),
    ("Card.vue", '<template><div :class="{ \'vue-lit\': on }"></div></template>\n'),
    ("card.component.html", '<div [class.ng-lit]="on" [ngClass]="{\'ang-lit\': on}"></div>\n'),
    ("Views/Home.cshtml", '<div class="@(Model.On ? "razor-lit" : "")"></div>\n'),
    ("page.php", "<div class=\"<?php echo $on ? 'php-lit' : ''; ?>\"></div>\n"),
    ("docs/usage.md", 'Wrap it: `<span class="md-badge">new</span>`\n'),
    ("toggle.js", "document.body.classList.add('lit-open');\nel.classList.toggle('lit-closed');\n"),
])
def test_a_class_used_only_in_markup_or_scripts_elsewhere_is_kept(make_repo, rel, text):
    names = ["jsx-only", "tsx-on", "vue-lit", "ng-lit", "ang-lit", "razor-lit", "php-lit", "md-badge",
             "lit-open", "lit-closed"]
    used = [n for n in names if n in text]
    css = "".join(f".{n} {{ color: red; }}\n" for n in used) + ".dead-one { color: red; }\n"
    root = site(make_repo, css, extra={rel: text})
    v = verdicts(root)
    assert used
    for n in used:
        assert v.get("." + n) != R, (n, v)
    assert v[".dead-one"] == R


def test_template_literals_and_concatenation_keep_the_classes_they_build(make_repo):
    root = site(make_repo, """
.btn-primary { color: blue; }
.btn-danger { color: red; }
.tone-x-lg { font-size: 2em; }
.chip--on { color: red; }
.lonely-prefix { color: red; }
""", extra={"app.js": "export const b = (v, s) => ['btn-' + v, `${s}-lg`, `chip--${v}`];\n"})
    v = verdicts(root)
    assert v[".btn-primary"] == A and v[".btn-danger"] == A
    assert v.get(".tone-x-lg") != R and v.get(".chip--on") != R
    assert v[".lonely-prefix"] == R


def test_state_classes_scripts_toggle_are_never_removed(make_repo):
    root = site(make_repo, """
.show { display: block; }
.active { font-weight: bold; }
.collapsing { transition: height .3s; }
.is-open { display: block; }
.never-toggled { color: red; }
""")
    v = verdicts(root)
    assert v == {".show": A, ".active": A, ".collapsing": A, ".is-open": A, ".never-toggled": R}


CDN_PAGE = PAGE.replace("{body}", "") + '<link rel="stylesheet" href="https://cdn.example.com/fa.css">\n'


@pytest.mark.parametrize("css,extra", [
    (".icon-home { color: red; }\n",                                      # ['icon', name].join('-')
     {"app.js": "export const c = (name) => ['icon', name].join('-');\n"}),
    (".button__icon { color: red; }\n.button_size_l { color: red; }\n",   # a BEM helper builds them
     {"app.jsx": "import block from 'bem-cn';\nconst b = block('button');\n"
                 "export const B = () => <i className={b('icon') + b({ size: 'l' })} />;\n"}),
    (".x-lg { font-size: 2em; }\n", {"views.py": "def size(s):\n    return '%s-lg' % s\n"}),
    ("#dmermaid { display: none; }\n", {"app.js": "import mermaid from 'mermaid';\nmermaid.run();\n"}),
    (".showing { opacity: 1; }\n.hiding { opacity: 0; }\n", {}),           # Bootstrap 5 toasts, offcanvas
    (".field_with_errors { color: red; }\n.errorlist { color: red; }\n", {}),   # Rails and Django add them
    ("@keyframes fa-spin { to { opacity: 0; } }\n", {"index.html": CDN_PAGE}),   # overrides a CDN sheet's
])
def test_names_code_not_here_or_not_literal_may_add_are_kept(make_repo, css, extra):
    root = site(make_repo, css + ".dead-x { color: red; }\n", extra=extra)
    v = verdicts(root)
    assert v.pop(".dead-x") == R
    assert v and R not in v.values(), v


def test_glue_keeps_only_names_whose_block_sits_beside_it_with_the_same_separator(make_repo):
    app = ("const block = 'panel';\nexport const el = (e) => `${block}__${e}`;\n"
           "export const file = (n, d) => `${n}_${d}.` + 'txt';\n" + "// filler\n" * 40 + "export const w = 'far';\n")
    root = site(make_repo, ".panel__title { a: b; }\n.txt-xs { a: b; }\n.far-away { a: b; }\n", extra={"app.js": app})
    assert verdicts(root) == {".panel__title": A, ".txt-xs": R, ".far-away": R}


def test_a_class_that_is_a_prefix_of_a_used_class_is_still_removed(make_repo):
    """A known true case: .refined-shadow is dead though .refined-shadow-hover is used."""
    root = site(make_repo, """
.refined-shadow { box-shadow: 0 1px 2px #0003; }
.refined-shadow-hover { box-shadow: 0 2px 4px #0003; }
""", body='<span class="refined-shadow-hover">x</span>',
                extra={"app.js": "export const c = (n) => ['shadow', n].join('-');\n"})
    v = verdicts(root)
    assert v == {".refined-shadow": R}


# ---------------------------------------------------------------- SCSS and modern CSS

def test_scss_nesting_bem_extend_and_placeholders(make_repo):
    root = make_repo({
        "index.html": PAGE.replace("style.css", "main.css").replace("{body}", '<b class="block__elem"></b>'),
        "app.js": "",
        "main.scss": """
@use 'parts';
.shell { display: grid; }
.card { padding: 1rem; }
.note { color: gray; }
.block { color: red; &__elem { color: blue; } &--mod { color: green; } }
.base-btn { padding: 0; }
%quiet { color: gray; }
.cta { @extend .base-btn; }
.dead-scss { color: red; }
""",
        "_parts.scss": ".from-part { @extend .shared-base; }\n",
        "_base.scss": ".shared-base { margin: 0; }\n",
    })
    assert not run(root, record=False).metrics["audited"]["unparsed"]
    v = verdicts(root, "main.scss")
    assert v.get(".block") != R and v.get(".base-btn") != R and "%quiet" not in v
    assert v[".dead-scss"] == R
    assert verdicts(root, "_base.scss").get(".shared-base") != R


def test_a_rule_whose_body_reaches_beyond_it_is_never_removed(make_repo):
    """@keyframes inside a rule are global; a mixin may @at-root rules out of it."""
    root = make_repo({
        "index.html": PAGE.replace("style.css", "main.css").replace("{body}", ""),
        "main.scss": BASE + """
.spin-host { animation: spin 1s; @keyframes spin { to { opacity: 0; } } }
.mixed { @include clearfix; }
.fonted { @font-face { font-family: x; } }
.plain-dead { color: red; }
""",
    })
    assert not run(root, record=False).metrics["audited"]["unparsed"]
    assert verdicts(root, "main.scss") == {".mixed": A, ".fonted": A, ".plain-dead": R}


def test_native_nesting_with_ampersand_is_not_a_unit(make_repo):
    root = site(make_repo, """
.nest-parent { color: red; &:hover { color: blue; } .nest-child { color: green; } }
.flat-dead { color: red; }
""", body='<a class="nest-child"></a>')
    v = verdicts(root)
    assert not run(root, record=False).metrics["audited"]["unparsed"]
    assert ".nest-parent" not in v
    assert v[".flat-dead"] == R


def test_not_and_attribute_selectors_are_not_units(make_repo):
    root = site(make_repo, """
.card:not(.flagged) { opacity: 1; }
.flagged { outline: 1px solid red; }
p:not(.lead) { margin: 0; }
[class*="col-"] { padding: 0 8px; }
div[class^="span"] { float: left; }
.col-dead { width: 1px; }
""")
    v = verdicts(root)
    assert v.get(".flagged") != R and ".lead" not in v
    assert v.get(".col-dead") == R
    assert not [k for k in v if "col-" in k and k != ".col-dead"]


def test_tailwind_apply_and_layers(make_repo):
    root = make_repo({
        "index.html": PAGE.replace("{body}", '<b class="btn-brand"></b>'),
        "app.js": "",
        "style.css": BASE + """
@tailwind base;
@tailwind components;
@layer components {
  .btn-brand { @apply px-4 py-2 btn-core; }
  .btn-core { @apply rounded; }
  .layer-dead { @apply mt-2; }
}
""",
    })
    v = verdicts(root)
    assert v.get(".btn-core") != R and v.get(".btn-brand") != R
    assert v[".layer-dead"] == R


def test_css_modules_keep_classes_scripts_read_as_properties(make_repo):
    root = make_repo({
        "Button.jsx": ("import styles from './Button.module.css';\n"
                       "export const B = () => <b className={styles.primary + styles.fooBar}/>;\n"),
        "Button.module.css": ".primary { color: blue; }\n.foo-bar { color: red; }\n.mod-dead { color: red; }\n",
        "Dyn.jsx": "import s from './Dyn.module.css';\nexport const D = (k) => <b className={s[k]}/>;\n",
        "Dyn.module.css": ".dyn-a { color: red; }\n.dyn-b { color: blue; }\n",
    })
    v = verdicts(root, "Button.module.css")
    assert v.get(".primary") != R and v.get(".foo-bar") != R
    assert v[".mod-dead"] == R
    assert R not in verdicts(root, "Dyn.module.css").values()


def test_a_css_module_handed_on_whole_or_read_with_an_optional_computed_key_is_kept(make_repo):
    root = make_repo({
        "Card.module.css": ".primary { color: blue; }\n.secondary { color: red; }\n.passed-on { color: red; }\n",
        "Card.jsx": ("import styles from './Card.module.css';\nimport Inner from './Inner';\n"
                     "export const C = () => <Inner classes={styles} c={styles.primary + styles.secondary} />;\n"),
        "Inner.jsx": "export default ({ classes, kind }) => <b className={classes[kind]} />;\n",
        "Opt.module.css": ".primary { color: blue; }\n.secondary { color: red; }\n.opt-read { color: red; }\n",
        "Opt.jsx": ("import s from './Opt.module.css';\n"
                    "export const O = (k) => <b className={s?.[k] + s.primary + s.secondary} />;\n"),
        "Plain.module.css": ".primary { color: blue; }\n.secondary { color: red; }\n.plain-dead { color: red; }\n",
        "Plain.jsx": ("import st from './Plain.module.css';\n"
                      "export const P = ({ styles }) => <b styles={styles} className={st.primary + st.secondary} />;\n"),
    })
    assert verdicts(root, "Card.module.css") == {".passed-on": A}
    assert verdicts(root, "Opt.module.css") == {".opt-read": A}
    assert verdicts(root, "Plain.module.css") == {".plain-dead": R}


# ---------------------------------------------------------------- keyframes and custom properties

def test_keyframes_named_in_a_script_or_another_sheet_are_kept(make_repo):
    root = site(make_repo, """
@keyframes spin { to { transform: rotate(1turn); } }
@keyframes pulse { 50% { opacity: .5; } }
@keyframes fade-up { from { opacity: 0; } }
@keyframes unused-kf { from { opacity: 0; } }
""", extra={"app.js": "el.style.animation = 'spin 1s linear';\nel.style.animationName = 'pulse';\n",
            "more.css": ".note { animation: fade-up .2s; }\n"})
    v = verdicts(root)
    assert v == {"@keyframes unused-kf": R}


def test_custom_properties_set_from_scripts_fallbacks_and_overrides(make_repo):
    root = site(make_repo, """
:root { --gap: 8px; --fallback: red; --themed: blue; --dead-var: 1px; }
.card { margin: var(--outer, var(--fallback)); color: var(--themed); }
""", extra={"app.js": "document.body.style.setProperty('--gap', '4px');\n",
            "dark.css": ":root { --themed: black; }\n.note { padding: var(--gap); }\n"})
    v = verdicts(root)
    assert v == {"--dead-var": R}
    assert "--themed" not in verdicts(root, "dark.css")


def test_a_variable_read_only_in_another_files_root_is_kept(make_repo):
    root = site(make_repo, ":root { --accent: red; --dead-accent: red; }\n",
                extra={"theme.css": ":root { --accent: blue; }\n.shell { color: var(--accent); }\n"})
    assert verdicts(root) == {"--dead-accent": R}
    assert verdicts(root, "theme.css") == {}


# ---------------------------------------------------------------- HTML pages

PAGE_INLINE = """<!doctype html>
<html><head>
<style>
.inline-used { color: red; }
.inline-dead { color: blue; }
</style>
<style type="text/x-template">.not-css { </style>
</head>
<body>
<p class="inline-used">hi</p>
<script type=module>
function usedHelper() { return 1; }
function deadHelper() { return 2; }
usedHelper();
</script>
<script>
function globalHelper() { return 3; }
</script>
</body></html>
"""


def test_inline_style_and_module_script(make_repo):
    root = make_repo({"index.html": PAGE_INLINE})
    v = verdicts(root, "index.html")
    assert v[".inline-dead"] == R and v["deadHelper"] == R
    assert ".inline-used" not in v and "usedHelper" not in v
    assert v.get("globalHelper") == A


# ---------------------------------------------------------------- cuts that still parse

def _cut_all(root, rel, lang):
    found = [f for f in run(root, record=False).findings if f.file == rel and f.verdict == R]
    text = (root / rel).read_text()
    for f in found:                                      # one at a time
        out = edit_source(rel, text, [f])
        assert parses(lang, rel, out), f.name
    out = edit_source(rel, text, found)                  # all together
    assert parses(lang, rel, out)
    return found, out


def test_every_cut_in_a_stylesheet_parses_and_keeps_the_rest(make_repo):
    root = site(make_repo, """
.dead-box { margin: 0; }
#dead-id { margin: 0; }
@keyframes deadspin { to { transform: rotate(1turn); } }
:root { --dead-gap: 4px; --live-gap: 2px; }
@media (min-width: 40em) { .dead-wide { width: 100%; } .card { width: 50%; } }
.card { gap: var(--live-gap); }
""")
    found, out = _cut_all(root, "style.css", "CSS")
    assert {f.name for f in found} == {".dead-box", "#dead-id", "@keyframes deadspin", "--dead-gap", ".dead-wide"}
    for gone in ("dead-box", "dead-id", "deadspin", "dead-gap", "dead-wide"):
        assert gone not in out
    for kept in (".shell", ".note", "--live-gap: 2px", ".card { width: 50%; }", "@media"):
        assert kept in out


def test_every_cut_in_scss_parses(make_repo):
    root = make_repo({
        "index.html": PAGE.replace("style.css", "main.css").replace("{body}", ""),
        "main.scss": BASE + "$pad: 4px;\n.dead-s { padding: $pad; }\n:root { --gone-var: 1; --kept-var: 2; }\n.note { order: var(--kept-var); }\n",
    })
    found, out = _cut_all(root, "main.scss", "SCSS")
    assert {f.name for f in found} == {".dead-s", "--gone-var"}
    assert "$pad: 4px;" in out and ".note" in out


def test_every_cut_in_a_page_parses_and_keeps_the_rest(make_repo):
    root = make_repo({"index.html": PAGE_INLINE})
    found, out = _cut_all(root, "index.html", "HTML")
    assert {f.name for f in found} == {".inline-dead", "deadHelper"}
    for kept in (".inline-used", "usedHelper", "globalHelper", "not-css"):
        assert kept in out


def test_a_class_used_only_in_built_or_vendored_markup_is_not_dead(make_repo):
    root = site(make_repo, css=".only-in-dist { color: red; }\n.really-dead { color: blue; }\n",
                extra={"dist/index.html": '<div class="only-in-dist"></div>\n',
                       "vendor/widget/tpl.html": '<p class="only-in-dist"></p>\n'})
    found = {f.name: f.verdict for f in run(root, record=False).findings}
    assert ".only-in-dist" not in found and found.get(".really-dead") == "REMOVE"
