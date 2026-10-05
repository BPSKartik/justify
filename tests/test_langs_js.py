"""JavaScript and TypeScript: imports, requires and top-level declarations, found dead only where
nothing — JSX, decorators, CommonJS exports, the page's globals, a framework's file layout, the
code that runs when a class is defined — could still reach them. Each trap sits beside dead code
that must still be found, so a trap that passes is not just a file that failed to parse."""

import re

import pytest

pytest.importorskip("tree_sitter_language_pack")

from justify.engine import run  # noqa: E402
from justify.langs import edit_source, error_count, parser_for  # noqa: E402

R, A = "REMOVE", "AMBIGUOUS"
SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")


def audit(root) -> dict[tuple[str, str], tuple[str, str]]:
    """(file, name) → (kind, verdict) for every JavaScript or TypeScript finding."""
    res = run(root, record=False)
    assert not res.metrics["audited"]["unparsed"], res.metrics["audited"]["unparsed"]
    return {(f.file, f.name): (f.kind, f.verdict) for f in res.findings
            if f.file.endswith(SUFFIXES) and f.kind != "duplicate"}


def removals(root, rel):
    return [f for f in run(root, record=False).findings if f.file == rel and f.verdict == R]


def parses(rel, text) -> bool:
    lang = "TypeScript" if rel.endswith((".ts", ".tsx")) else "JavaScript"
    return error_count(parser_for(lang, rel).parse(text.encode())) == 0


LIB_TS = "export const a = 1\n"


# ---------------------------------------------------------------- imports: uses the word index sees

def test_imports_used_as_types_generics_jsx_decorators_and_reexports_stay(make_repo):
    root = make_repo({
        "src/view.tsx": """
            import { TypeOnly, Gen, Dec, Reexp, Foo, Ns } from './lib'
            import * as All from './lib'
            import { DeadOne } from './lib'
            export * from './lib'
            export { Reexp }
            let v: TypeOnly
            const g = new Map<string, Gen>()
            @Dec
            export class Widget {}
            export const View = () => <><Foo /><Ns.Item /></>
            console.log(v, g)
        """,
        "src/lib.ts": LIB_TS,
    })
    assert audit(root) == {
        ("src/view.tsx", "All"): ("import", R),
        ("src/view.tsx", "DeadOne"): ("import", R),
        ("src/view.tsx", "Widget"): ("class", A),          # exported: judged, never removed
        ("src/view.tsx", "View"): ("function", A),
    }


def test_react_classic_transform_pragma_side_effects_css_modules_and_dynamic_import(make_repo):
    root = make_repo({
        "src/App.jsx": """
            import React from 'react'
            import styles from './App.module.css'
            import './global.css'
            import { gone } from './util'
            const load = () => import('./Page')
            export default function App() {
              load()
              return <div className={styles.app} />
            }
        """,
        "src/hello.jsx": """
            /** @jsx h */
            import { h } from 'preact'
            import { unused } from 'preact'
            export default () => <p>hi</p>
        """,
        "src/util.js": "export const gone = 1\n",
    })
    assert audit(root) == {
        ("src/App.jsx", "React"): ("import", A),           # the transform calls React.createElement
        ("src/App.jsx", "gone"): ("import", R),
        ("src/hello.jsx", "unused"): ("import", R),
    }


def test_a_jsx_factory_named_by_a_config_keeps_its_whole_import(make_repo):
    root = make_repo({
        ".babelrc": '{"plugins": [["@babel/plugin-transform-react-jsx", {"pragma": "jsx"}]]}\n',
        "src/c.tsx": """
            import { jsx, other } from '@emotion/react'
            import { o1, o2 } from './d'
            import React, { r1 } from 'react'
            export const C = () => <div />
        """,
        "src/d.ts": "export const o1 = 1, o2 = 2\n",
    })
    got = audit(root)
    assert got[("src/c.tsx", "jsx")] == ("import", A)
    assert got[("src/c.tsx", "other")] == ("import", A)   # cutting it would take jsx along
    assert got[("src/c.tsx", "React")] == ("import", A)
    assert {k: v for k, v in got.items() if v[1] == R} == {
        ("src/c.tsx", "o1"): ("import", R), ("src/c.tsx", "o2"): ("import", R), ("src/c.tsx", "r1"): ("import", R)}


# ---------------------------------------------------------------- CommonJS

def test_commonjs_requires_exports_and_names_in_strings(make_repo):
    root = make_repo({
        "lib/cjs.js": """
            const { used, gone: alias, withDefault = 1 } = require('./dep')
            const { r1, ...rest } = require('./dep')
            const { d = make() } = require('./dep')
            const whole = require('./dep')
            const fs = require('fs')
            function helper() {}
            function viaExports() {}
            function onlyInString() {}
            function deadCjs() {}
            exports.x = function () {}
            exports.viaExports = viaExports
            module.exports = { helper, rest, used, withDefault }
            run('onlyInString')
        """,
        "lib/dep.js": "module.exports = {}\n",
    })
    assert audit(root) == {
        ("lib/cjs.js", "alias"): ("import", R),
        ("lib/cjs.js", "r1"): ("import", A),               # ...rest would collect it instead
        ("lib/cjs.js", "d"): ("import", A),                # its default value calls make()
        ("lib/cjs.js", "whole"): ("import", R),
        ("lib/cjs.js", "fs"): ("import", R),
        ("lib/cjs.js", "deadCjs"): ("function", R),
    }


# ---------------------------------------------------------------- scripts and what a file is

def test_a_plain_browser_script_has_globals_and_a_node_script_reaches_the_repository(make_repo):
    root = make_repo({
        "public/site.js": "function onLoad() {}\nconst handler = () => {}\nfunction fromHtml() {}\n",
        "public/page.html": '<button onclick="fromHtml()">go</button>\n',
        "tools/build.js": "const path = require('path')\nfunction unusedHelper() {}\nfunction main() {}\nmain()\n",
        "src/global.d.ts": "interface Unused {}\ndeclare function nobody(): void\n",
    })
    assert audit(root) == {
        ("public/site.js", "onLoad"): ("function", A),     # a global: any page may call it
        ("public/site.js", "handler"): ("function", A),
        ("tools/build.js", "path"): ("import", R),
        ("tools/build.js", "unusedHelper"): ("function", R),
    }


def test_files_a_framework_or_tool_calls_by_place_keep_their_exports(make_repo):
    root = make_repo({
        "pages/index.tsx": """
            export async function getServerSideProps() { return { props: {} } }
            export default function Page() { return null }
            function pageDead() {}
        """,
        "app/blog/page.tsx": """
            export async function generateMetadata() { return {} }
            export const revalidate = 60
            export default function P() { return null }
        """,
        "vite.config.ts": "export function plugin() {}\nfunction configDead() {}\nexport default {}\n",
        "src/Button.stories.tsx": "export const Primary = () => null\nexport default {}\n",
        "src/util.test.ts": "import { x } from './util'\nexport function fixture() {}\ntest('x', () => x)\n",
        "src/util.ts": "export const x = 1\n",
    })
    assert audit(root) == {
        ("pages/index.tsx", "pageDead"): ("function", R),
        ("vite.config.ts", "configDead"): ("function", R),
    }


def test_eval_reaches_every_name(make_repo):
    root = make_repo({"src/a.js": "import { x } from './b'\nfunction hidden() {}\neval(code)\nexport {}\n",
                      "src/b.js": "export const x = 1\n"})
    assert audit(root) == {("src/a.js", "x"): ("import", A), ("src/a.js", "hidden"): ("function", A)}


def test_exported_through_a_clause_or_named_only_in_a_string(make_repo):
    root = make_repo({"src/a.js": """
        function shared() {}
        function renamed() {}
        function legacy() {}
        function gone() {}
        el.setAttribute('onclick', 'legacy()')
        export { shared, renamed as other }
    """})
    assert audit(root) == {("src/a.js", "gone"): ("function", R)}


# ---------------------------------------------------------------- TypeScript declarations

def test_declaration_merging_enums_as_types_overloads_and_hoisting(make_repo):
    root = make_repo({
        "src/types.ts": """
            import { api } from './api'
            interface Merged { a: string }
            interface Merged { b: string }
            function decorate() {}
            namespace decorate { export const level = 1 }
            enum Color { Red }
            const enum Size { S }
            function over(a: string): void
            function over(a: number): void
            function over(a: any) {}
            hoisted()
            function hoisted() {}
            type Dead = string
            interface DeadShape {}
            enum DeadEnum { A }
            function deadFn() {}
            export function paint(c: Color, s: Size, m: Merged) { return api(c, s, m, over) }
        """,
        "src/api.ts": "export const api = (...a: unknown[]) => a\n",
    })
    assert audit(root) == {
        ("src/types.ts", "Dead"): ("type", R),
        ("src/types.ts", "DeadShape"): ("type", R),
        ("src/types.ts", "DeadEnum"): ("type", R),
        ("src/types.ts", "deadFn"): ("function", R),
        ("src/types.ts", "paint"): ("function", A),
    }


def test_a_class_or_enum_that_runs_code_when_defined_is_kept(make_repo):
    root = make_repo({
        "src/reg.ts": """
            import { registry, mixin, Base, Input, boot, sideEffect, Dec } from './lib'
            @Dec
            class Decorated {}
            class Registered { static self = registry.add(Registered) }
            class Mixed extends mixin(Base) {}
            class Computed { [sideEffect()]() {} }
            class MemberDecorated { @Input() name = '' }
            class WithBlock { static { boot() } }
            enum Booted { A = boot() }
            class Plain extends Base { static label = 'x'; static make = () => boot(); run() { boot() } }
            export {}
        """,
        "src/reg.js": """
            import { register, boot } from './lib'
            class Reg { static x = register() }
            const Klass = class { static y = new Map() }
            const Quiet = class { static z = [1, 2] }
            export { boot }
        """,
        "src/lib.ts": LIB_TS,
    })
    got = audit(root)
    for name in ("Decorated", "Registered", "Mixed", "Computed", "MemberDecorated", "WithBlock"):
        assert got[("src/reg.ts", name)] == ("class", A), name
    assert got[("src/reg.ts", "Booted")] == ("type", A)
    assert got[("src/reg.ts", "Plain")] == ("class", R)
    assert got[("src/reg.js", "Reg")] == ("class", A)
    assert got[("src/reg.js", "Klass")] == ("class", A)
    assert got[("src/reg.js", "Quiet")] == ("class", R)


# ---------------------------------------------------------------- imports whose every binding is unused

def test_an_import_that_is_the_files_last_tie_to_being_a_module_is_kept(make_repo):
    root = make_repo({
        "src/only.ts": "import { gone } from './lib'\nfunction f() {}\nf()\n",
        "src/aug.ts": "import type { App } from './lib'\ndeclare module './lib' { interface Extra {} }\n",
        "src/exported.ts": "import { gone } from './lib'\nexport const z = 1\n",
        "src/bare.ts": "import './polyfill'\nimport { gone } from './lib'\n",
        "src/lib.ts": LIB_TS,
    })
    assert audit(root) == {
        ("src/only.ts", "gone"): ("import", A),            # without it the file is a script: f is global
        ("src/aug.ts", "App"): ("import", A),              # ... and `declare module` stops augmenting
        ("src/exported.ts", "gone"): ("import", R),
        ("src/bare.ts", "gone"): ("import", R),
    }


def test_verbatim_module_syntax_keeps_an_imports_load(make_repo):
    root = make_repo({
        "tsconfig.json": '{"compilerOptions": {"verbatimModuleSyntax": true}}\n',
        "src/a.ts": "import { gone } from './lib'\nimport type { T } from './lib'\nimport { readFile } from 'fs'\n"
                    "import { used, alsoGone } from './lib'\nexport const z = used\n",
        "src/lib.ts": LIB_TS,
    })
    assert audit(root) == {
        ("src/a.ts", "gone"): ("import", A),
        ("src/a.ts", "T"): ("import", R),
        ("src/a.ts", "readFile"): ("import", R),
        ("src/a.ts", "alsoGone"): ("import", R),           # `used` keeps the load
    }


def test_a_cut_that_would_join_two_lines_is_not_made(make_repo):
    root = make_repo({"src/a.js": """
        const x = a
        function dead() {}
        (b || c).run()
        const y = a;
        function dead2() {}
        (b || c).run()
        export { x, y }
    """})
    assert audit(root) == {("src/a.js", "dead"): ("function", A), ("src/a.js", "dead2"): ("function", R)}


# ---------------------------------------------------------------- proving a removal: the cuts

TS_SOURCE = """\
import { DeadA, DeadB } from './lib'
import Def, { Named } from './lib'
import type { T1, T2 } from './lib'
import { kept, gone } from './lib'
import {
  multi1,
  stays,
} from './lib'
import * as ns from './lib'
import { readFile } from 'fs'
import style from './a.module.css'
function deadFn() {}
type DeadType = string
interface DeadShape { a: number }
enum DeadEnum { A }
class DeadClass {}
const deadArrow = () => 1
abstract class DeadBase {}
export const v = [kept, stays]
"""

JS_SOURCE = """\
import { a, b } from './lib'
import Def, * as ns from './lib'
import { kept, gone } from './lib'
import lone from './lib'
import { readFile } from 'fs'
const { r1, r2 } = require('./lib')
const { r3, keep3 } = require('./lib')
const one = require('./lib')
const os = require('os')
function deadFn() {}
function* deadGen() {}
class DeadClass {}
const deadExpr = function () {}
export const v = [kept, keep3]
"""


def test_every_removal_kind_cuts_cleanly_alone_and_together(make_repo):
    root = make_repo({"src/a.ts": TS_SOURCE, "src/b.js": JS_SOURCE, "src/lib.ts": LIB_TS})
    ts, js = removals(root, "src/a.ts"), removals(root, "src/b.js")
    assert {f.name for f in ts} == {"DeadA", "DeadB", "Def", "Named", "T1", "T2", "gone", "multi1", "ns",
                                    "readFile", "style", "deadFn", "DeadType", "DeadShape", "DeadEnum",
                                    "DeadClass", "deadArrow", "DeadBase"}
    assert {f.name for f in js} == {"a", "b", "Def", "ns", "gone", "lone", "readFile", "r1", "r2", "r3",
                                    "one", "os", "deadFn", "deadGen", "DeadClass", "deadExpr"}
    for rel, text, found in (("src/a.ts", TS_SOURCE, ts), ("src/b.js", JS_SOURCE, js)):
        for f in found:
            out = edit_source(rel, text, [f])
            assert parses(rel, out), (f.name, out)
            assert "export const v = [kept, " in out
        out = edit_source(rel, text, found)
        assert parses(rel, out), out
        for f in found:
            assert not re.search(rf"\b{f.name}\b", out), (f.name, out)

    # TypeScript drops an import nobody uses, so the whole statement goes: nothing new is loaded
    assert edit_source("src/a.ts", TS_SOURCE, ts) == (
        "import { kept } from './lib'\nimport {\n  stays,\n} from './lib'\nexport const v = [kept, stays]\n")
    # JavaScript loads the module whatever it binds, so the load stays (Node's own modules excepted)
    assert edit_source("src/b.js", JS_SOURCE, js) == (
        "import './lib'\nimport './lib'\nimport { kept } from './lib'\nimport './lib'\n"
        "require('./lib')\nconst { keep3 } = require('./lib')\nrequire('./lib')\n"
        "export const v = [kept, keep3]\n")


def test_one_binding_of_several_is_cut_from_its_list(make_repo):
    root = make_repo({"src/a.ts": TS_SOURCE, "src/lib.ts": LIB_TS})
    [gone] = [f for f in removals(root, "src/a.ts") if f.name == "gone"]
    assert "import { kept } from './lib'\n" in edit_source("src/a.ts", TS_SOURCE, [gone])
    [dead_a] = [f for f in removals(root, "src/a.ts") if f.name == "DeadA"]
    out = edit_source("src/a.ts", TS_SOURCE, [dead_a])      # its neighbour is as dead: the import goes
    assert not re.search(r"\bDead[AB]\b", out) and "import './lib'" not in out and parses("src/a.ts", out)
