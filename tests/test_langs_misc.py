"""Go, Rust, PHP and Swift: each rule finds genuinely dead code, and each trap — a use the word
search could miss, or a caller no search can see — stays out of REMOVE. Every trap sits beside
dead code that must still be found, so a trap that passes is not just a file that failed to parse.
Every REMOVE is also cut for real, and what is left must still parse with the rest intact."""

import pytest

pytest.importorskip("tree_sitter_language_pack")

from justify.engine import run  # noqa: E402
from justify.langs import edit_source, error_count, parser_for  # noqa: E402

R, A = "REMOVE", "AMBIGUOUS"
LANG = {".go": "Go", ".rs": "Rust", ".php": "PHP", ".swift": "Swift"}


def audit(root, suffix) -> dict[tuple[str, str], tuple[str, str]]:
    """(file, name) → (kind, verdict) for every finding in files with this suffix."""
    res = run(root, record=False)
    assert not res.metrics["audited"]["unparsed"], res.metrics["audited"]["unparsed"]
    return {(f.file, f.name): (f.kind, f.verdict) for f in res.findings
            if f.file.endswith(suffix) and f.kind != "duplicate"}


def removes(root, suffix):
    return [f for f in run(root, record=False).findings
            if f.file.endswith(suffix) and f.kind != "duplicate" and f.verdict == R]


def parses(rel, text) -> bool:
    lang = LANG["." + rel.rsplit(".", 1)[-1]]
    return error_count(parser_for(lang, rel).parse(text.encode())) == 0


def cut_each(root, suffix, survivors: dict[str, list[str]]):
    """Cut every REMOVE alone, then all of a file's together: what is left parses, the unit's
    definition is gone, and the code named in `survivors` for that file is still there."""
    found = removes(root, suffix)
    assert found
    by_file: dict[str, list] = {}
    for f in found:
        by_file.setdefault(f.file, []).append(f)
    for rel, items in by_file.items():
        text = (root / rel).read_text()
        for f in items + [None]:
            out = edit_source(rel, text, items if f is None else [f])
            assert parses(rel, out), out
            for word in survivors.get(rel, []):
                assert word in out, (word, out)
            assert len(out) < len(text)
        out = edit_source(rel, text, items)
        for f in items:
            assert out.count(f.name) < text.count(f.name), (f.name, out)


# ---------------------------------------------------------------- Go

GO = {
    "go.mod": "module example.com/m\n",
    "a/a.go": """
        package a

        import (
        \t"fmt"
        \t"reflect"
        \t_ "unsafe"
        )

        // deadConst is never read.
        const deadConst = 1

        var (
        \tdeadVar  = "x"
        \tkeptVar  = 2
        )

        type deadType struct{}

        func deadFunc() {
        \tfmt.Println("gone")
        }

        func usedInOtherFile() {}

        func usedInTest() int { return 1 }

        func viaMap() {}

        var handlers = map[string]func(){"x": viaMap}

        // Handlers is the package's door.
        func Handlers() map[string]func() { return handlers }

        //go:linkname runtimeNano runtime.nanotime
        func runtimeNano() int64

        //export goCallback
        func goCallback() {}

        //go:wasmexport wasmHook
        func wasmHook() {}

        func pulledByOther() {}

        type config struct{ Name string }

        type server struct{}

        func (s *server) handle() {}

        func (s *server) Handle() {}

        // Serve hands out a method value.
        func Serve() func() { s := &server{}; return s.handle }

        // Reflect reaches methods and types by reflection.
        func Reflect() {
        \treflect.ValueOf(&server{}).MethodByName("Handle")
        \t_ = reflect.TypeOf(config{})
        \tfmt.Println(keptVar)
        }
    """,
    "a/b.go": """
        package a

        func B() { usedInOtherFile() }
    """,
    "a/a_test.go": """
        package a

        import "testing"

        func TestX(t *testing.T) {
        \tif usedInTest() != 1 {
        \t\tt.Fatal()
        \t}
        }
    """,
    "other/o.go": """
        package other

        import _ "unsafe"

        //go:linkname pulled example.com/m/a.pulledByOther
        func pulled()

        func Use() { pulled() }
    """,
}


def test_go_dead_code_and_traps(make_repo):
    root = make_repo(GO)
    got = audit(root, ".go")
    assert {k: v for k, v in got.items() if v[1] == R} == {
        ("a/a.go", "deadConst"): ("variable", R),
        ("a/a.go", "deadVar"): ("variable", R),
        ("a/a.go", "deadType"): ("type", R),
        ("a/a.go", "deadFunc"): ("function", R),
    }
    for name in ("runtimeNano", "goCallback", "wasmHook", "pulledByOther"):
        assert got[("a/a.go", name)] == ("function", A), name
    for name in ("usedInOtherFile", "usedInTest", "viaMap", "handlers", "handle", "config", "server", "keptVar"):
        assert ("a/a.go", name) not in got, name


def test_go_cuts_parse_and_leave_the_rest(make_repo):
    root = make_repo(GO)
    cut_each(root, ".go", {"a/a.go": ["keptVar", "usedInTest", "func Handlers", "reflect", "\"fmt\""]})
    out = edit_source("a/a.go", (root / "a/a.go").read_text(), removes(root, ".go"))
    assert "deadConst is never read" not in out


# ---------------------------------------------------------------- Rust

RUST = {
    "Cargo.toml": "[package]\nname = \"m\"\nversion = \"0.1.0\"\n",
    "src/lib.rs": """
        //! The crate.
        mod util;

        /// Never called.
        fn dead_fn() -> u32 {
            1
        }

        const DEAD_CONST: u32 = 1;
        static DEAD_STATIC: u32 = 2;

        fn only_in_tests() -> u32 {
            1
        }

        fn in_macro() {}

        macro_rules! call_it {
            () => {
                in_macro()
            };
        }

        fn as_pointer() {}

        pub fn table() -> fn() {
            call_it!();
            as_pointer
        }

        #[no_mangle]
        extern "C" fn exported_c() {}

        #[no_mangle]
        pub extern "C" fn exported_pub() {}

        fn helper_for_child() {}

        pub struct S;

        impl S {
            fn private_method(&self) {}
        }

        #[cfg(test)]
        mod tests {
            use super::*;

            fn dead_test_helper() {}

            #[test]
            fn it_works() {
                assert_eq!(only_in_tests(), 1);
            }
        }
    """,
    "src/util.rs": """
        pub fn go() {
            super::helper_for_child();
        }
    """,
}


def test_rust_dead_code_and_traps(make_repo):
    root = make_repo(RUST)
    got = audit(root, ".rs")
    assert {k: v for k, v in got.items() if v[1] == R} == {
        ("src/lib.rs", "dead_fn"): ("function", R),
        ("src/lib.rs", "DEAD_CONST"): ("variable", R),
        ("src/lib.rs", "DEAD_STATIC"): ("variable", R),
        ("src/lib.rs", "dead_test_helper"): ("function", R),
    }
    assert got[("src/lib.rs", "exported_c")] == ("function", A)
    for name in ("only_in_tests", "in_macro", "as_pointer", "helper_for_child", "private_method", "it_works"):
        assert ("src/lib.rs", name) not in got, name


def test_rust_cuts_parse_and_leave_the_rest(make_repo):
    root = make_repo(RUST)
    cut_each(root, ".rs", {"src/lib.rs": ["//! The crate.", "mod util;", "fn only_in_tests", "#[test]",
                                          "#[no_mangle]\nextern", "macro_rules!"]})
    out = edit_source("src/lib.rs", (root / "src/lib.rs").read_text(), removes(root, ".rs"))
    assert "Never called" not in out


# ---------------------------------------------------------------- PHP

PHP = {
    "src/App.php": """
        <?php
        namespace App;

        use App\\Models\\Unused;
        use App\\Models\\InDoc;
        use App\\Attr\\Route;
        use App\\Models\\Checked;
        use App\\Errors\\Failure;
        use App\\Util\\Helper;
        use App\\Models\\Named;
        use App\\Traits\\Greets;
        use App\\Models\\{Kept, GroupDead};

        class App
        {
            use Greets;

            private $deadField;
            private $usedField = 1;

            /**
             * @param InDoc $x
             */
            #[Route('/')]
            public function index($x)
            {
                if ($x instanceof Checked) {
                }
                try {
                    Helper::go();
                } catch (Failure $e) {
                }
                $n = Named::class;
                array_map([$this, 'mapped'], []);
                call_user_func([$this, 'called']);
                return $this->usedField . new Kept();
            }

            /** Nobody calls it. */
            private function deadMethod()
            {
            }

            private function mapped() {}

            private function called() {}

            public function __toString() { return ''; }

            private function __clone() {}
        }
    """,
}


def test_php_dead_code_and_traps(make_repo):
    root = make_repo(PHP)
    got = audit(root, ".php")
    assert {k: v for k, v in got.items() if v[1] == R} == {
        ("src/App.php", "Unused"): ("import", R),
        ("src/App.php", "GroupDead"): ("import", R),
        ("src/App.php", "deadField"): ("field", R),
        ("src/App.php", "deadMethod"): ("method", R),
    }
    for name in ("InDoc", "Route", "Checked", "Failure", "Helper", "Named", "Greets", "Kept",
                 "usedField", "mapped", "called", "__toString", "__clone"):
        assert ("src/App.php", name) not in got, name


def test_php_cuts_parse_and_leave_the_rest(make_repo):
    root = make_repo(PHP)
    cut_each(root, ".php", {"src/App.php": ["use App\\Models\\InDoc;", "Kept", "use Greets;", "$usedField",
                                            "function mapped", "__clone"]})
    out = edit_source("src/App.php", (root / "src/App.php").read_text(), removes(root, ".php"))
    assert "use App\\Models\\{Kept};" in out and "Nobody calls it" not in out


# ---------------------------------------------------------------- Swift

SWIFT = {
    "Sources/App/Controller.swift": """
        import UIKit

        final class Controller: UIViewController {
            private var deadField = 0
            private var counter = 0

            /// Nobody calls it.
            private func deadMethod() {}

            private func onTap() {}

            @objc private func objcThing() {}

            private func usedInExtension() {}

            override func viewDidLoad() {
                super.viewDidLoad()
                let b = UIButton()
                b.addTarget(self, action: #selector(onTap), for: .touchUpInside)
                counter += 1
            }
        }

        extension Controller {
            func run() {
                usedInExtension()
            }
        }

        private extension Controller {
            func deadInPrivateExtension() {}
        }

        private func deadTop() {}

        fileprivate let deadTopVar = 1
    """,
}


def test_swift_dead_code_and_traps(make_repo):
    root = make_repo(SWIFT)
    got = audit(root, ".swift")
    rel = "Sources/App/Controller.swift"
    assert {k: v for k, v in got.items() if v[1] == R} == {
        (rel, "deadField"): ("field", R),
        (rel, "deadMethod"): ("method", R),
        (rel, "deadInPrivateExtension"): ("method", R),
        (rel, "deadTop"): ("function", R),
        (rel, "deadTopVar"): ("variable", R),
    }
    assert got[(rel, "objcThing")] == ("method", A)
    for name in ("onTap", "usedInExtension", "counter", "viewDidLoad", "run"):
        assert (rel, name) not in got, name


def test_swift_cuts_parse_and_leave_the_rest(make_repo):
    root = make_repo(SWIFT)
    rel = "Sources/App/Controller.swift"
    cut_each(root, ".swift", {rel: ["import UIKit", "func onTap", "@objc private func objcThing", "counter",
                                    "extension Controller {"]})
    out = edit_source(rel, (root / rel).read_text(), removes(root, ".swift"))
    assert "Nobody calls it" not in out
