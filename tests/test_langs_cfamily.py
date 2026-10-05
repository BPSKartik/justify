"""C and C++: file-local functions and variables, found dead only where nothing the preprocessor,
the linker or the runtime can do could still reach them. Each trap sits beside dead code that
must still be found, so a trap that passes is not just a file that failed to parse."""

import pytest

pytest.importorskip("tree_sitter_language_pack")

from justify.engine import run  # noqa: E402
from justify.langs import edit_source, error_count, parser_for  # noqa: E402

R, A = "REMOVE", "AMBIGUOUS"
LANG = {".c": "C", ".cc": "C++", ".cpp": "C++"}


def audit(root) -> dict[tuple[str, str], str]:
    """(file, name) → verdict for every C/C++ finding."""
    res = run(root, record=False)
    assert not res.metrics["audited"]["unparsed"], res.metrics["audited"]["unparsed"]
    return {(f.file, f.name): f.verdict for f in res.findings
            if f.file.endswith((".c", ".cc", ".cpp", ".h")) and f.kind != "duplicate"}


def audit_any(root) -> dict[tuple[str, str], str]:
    """Like audit, but files the grammar cannot read are allowed (they are left unjudged)."""
    return {(f.file, f.name): f.verdict for f in run(root, record=False).findings if f.kind != "duplicate"}


def removes(root) -> set[tuple[str, str]]:
    return {k for k, v in audit(root).items() if v == R}


def parses(rel, text) -> bool:
    return error_count(parser_for(LANG[rel[rel.rfind("."):]], rel).parse(text.encode())) == 0


def cut_all(root, rel) -> str:
    """Cut every REMOVE in the file at once; the result must still parse."""
    text = (root / rel).read_text()
    found = [f for f in run(root, record=False).findings if f.file == rel and f.verdict == R]
    assert found
    out = edit_source(rel, text, found)
    assert parses(rel, out), out
    return out


# ---------------------------------------------------------------- the rules

def test_dead_static_function_and_variable_are_removed(make_repo):
    root = make_repo({"a.c": """
        #include <stdio.h>

        static int counter = 0;
        static const char *unused_name = "x";

        /* adds one */
        static int bump(void) { return ++counter; }

        static int gone(int x)
        {
            return gone(x - 1);
        }

        int main(void)
        {
            printf("%d\\n", bump());
            return 0;
        }
    """})
    assert removes(root) == {("a.c", "unused_name"), ("a.c", "gone")}
    out = cut_all(root, "a.c")
    assert "gone" not in out and "unused_name" not in out and "adds one" in out
    assert "static int bump(void)" in out and "int main(void)" in out


def test_public_functions_and_entry_points_are_never_removed(make_repo):
    root = make_repo({"lib.c": """
        int api_call(void) { return 1; }

        int WinMain(void) { return 0; }

        static void dead(void) {}
    """, "m.c": """
        int main(int argc, char **argv) { return 0; }
    """})
    got = audit(root)
    assert got[("lib.c", "dead")] == R
    assert got.get(("lib.c", "api_call")) != R
    assert ("lib.c", "WinMain") not in got and ("m.c", "main") not in got


def test_cpp_unnamed_namespace_and_static(make_repo):
    root = make_repo({"x.cpp": """
        #include <vector>

        namespace {
        int helper(int x) { return x * 2; }
        int tmpl_helper(int x) { return x + 1; }
        int dead_anon(int x) { return x; }
        static int dead_static_count = 3;
        }  // namespace

        static int dead_static(int y) { return y; }

        template <class T>
        T twice(T v) { return tmpl_helper(v); }

        auto doubler = [](int v) { return helper(v); };

        int run_all() { return doubler(1) + twice(2); }
    """})
    got = audit(root)
    assert {k for k, v in got.items() if v == R} == {
        ("x.cpp", "dead_anon"), ("x.cpp", "dead_static"), ("x.cpp", "dead_static_count")}
    out = cut_all(root, "x.cpp")
    assert "helper(int x)" in out and "tmpl_helper(int x)" in out and "namespace {" in out
    assert "dead_" not in out


# ---------------------------------------------------------------- traps beside dead code

def test_function_pointer_table_macro_body_and_struct_initializer(make_repo):
    root = make_repo({"ops.c": """
        struct ops { int (*fn)(int); };

        static int op_add(int a) { return a + 1; }
        static int op_sub(int a) { return a - 1; }
        static int by_macro(int a) { return a; }
        static int by_addr(int a) { return a * 3; }
        static int dead_op(int a) { return a; }

        static int (*table[])(int) = { op_add, op_sub };

        #define CALL(v) by_macro(v)

        static const struct ops my_ops = { .fn = &by_addr };

        int dispatch(int i, int v) { return table[i](v) + CALL(v) + my_ops.fn(v); }
    """})
    assert removes(root) == {("ops.c", "dead_op")}
    out = cut_all(root, "ops.c")
    assert "op_add" in out and "by_addr" in out and "dead_op" not in out


def test_macro_in_an_included_header_calls_the_static(make_repo):
    root = make_repo({"call.h": """
        #define RUN() hidden_impl()
    """, "c.c": """
        #include "call.h"

        static int hidden_impl(void) { return 1; }
        static int dead_impl(void) { return 2; }

        int go(void) { return RUN(); }
    """})
    assert removes(root) == {("c.c", "dead_impl")}


def test_token_pasting_macros_keep_what_they_may_build(make_repo):
    root = make_repo({"cmd.c": """
        #define HANDLER(n) { #n, cmd_##n }

        struct cmd { const char *name; int (*fn)(void); };

        static int cmd_open(void) { return 0; }
        static int cmd_close(void) { return 1; }
        static int unrelated(void) { return 2; }

        static const struct cmd cmds[] = {
            HANDLER(open),
            HANDLER(close),
        };

        const struct cmd *table(void) { return cmds; }
    """})
    got = audit(root)
    assert got[("cmd.c", "unrelated")] == R
    assert got.get(("cmd.c", "cmd_open")) != R and got.get(("cmd.c", "cmd_close")) != R


def test_same_static_name_in_two_files(make_repo):
    root = make_repo({"one.c": """
        static int helper(void) { return 1; }

        int one(void) { return helper(); }
    """, "two.c": """
        static int helper(void) { return 2; }

        int two(void) { return 2; }
    """})
    got = audit(root)
    assert got.get(("one.c", "helper")) != R
    assert got[("two.c", "helper")] == R
    out = cut_all(root, "two.c")
    assert "int two(void)" in out and "helper" not in out


def test_k_and_r_definitions(make_repo):
    root = make_repo({"kr.c": """
        static int add(a, b)
        int a;
        int b;
        {
            return a + b;
        }

        static int old_dead(a)
        int a;
        {
            return a;
        }

        int use(void) { return add(1, 2); }
    """})
    got = audit(root)
    assert got.get(("kr.c", "add")) != R
    assert got.get(("kr.c", "old_dead")) in (R, None)
    if got.get(("kr.c", "old_dead")) == R:
        out = cut_all(root, "kr.c")
        assert "static int add(a, b)" in out and "int b;" in out and "old_dead" not in out


def test_static_inline_functions_in_headers_are_never_units(make_repo):
    root = make_repo({"util.h": """
        static inline int never_called(int x) { return x; }
        static int also_never(void) { return 0; }
    """, "u.c": """
        #include "util.h"

        static int dead_here(void) { return 0; }

        int f(void) { return 1; }
    """, "u.hpp": """
        static inline int hpp_unused() { return 0; }
    """})
    got = audit(root)
    assert not any(f in ("util.h", "u.hpp") for f, _ in got)
    assert got[("u.c", "dead_here")] == R


def test_a_c_file_included_by_another_shares_its_statics(make_repo):
    root = make_repo({"src/impl.c": """
        static int impl_helper(void) { return 7; }
        static int impl_dead(void) { return 8; }
    """, "src/unity.c": """
        #include "impl.c"

        int run(void) { return impl_helper(); }
    """, "tests/t.c": """
        #include "../src/deep.c"

        int t(void) { return deep_helper(); }
    """, "src/deep.c": """
        static int deep_helper(void) { return 1; }
    """, "src/wrap.h": """
        #include "inner.c"
    """, "src/inner.c": """
        static int inner_helper(void) { return 2; }
    """, "src/top.c": """
        #include "wrap.h"

        int top(void) { return inner_helper(); }
    """})
    got = audit(root)
    assert got.get(("src/impl.c", "impl_helper")) != R
    assert got.get(("src/deep.c", "deep_helper")) != R
    assert got.get(("src/inner.c", "inner_helper")) != R
    assert got[("src/impl.c", "impl_dead")] == R


def test_a_c_file_included_by_macro_name_is_shared(make_repo):
    root = make_repo({"impl_fast.c": """
        static int fast_helper(void) { return 1; }
    """, "front.c": """
        #define IMPL "impl_fast.c"
        #include IMPL

        int front(void) { return fast_helper(); }
    """})
    assert audit(root).get(("impl_fast.c", "fast_helper")) != R


def test_attributes_keep_what_the_linker_or_runtime_reaches(make_repo):
    root = make_repo({"attr.c": """
        static void __attribute__((constructor)) ctor_a(void) {}

        __attribute__((constructor))
        static void ctor_b(void) {}

        static void ctor_c(void) __attribute__((constructor));
        static void ctor_c(void) {}

        static const char __attribute__((used)) tag[] = "x";

        __attribute__((used)) static int kept_used(void) { return 0; }

        static void dead_plain(void) {}
    """, "attr.cpp": """
        [[maybe_unused]] static int mu_a(void) { return 0; }

        static int dead_cpp(void) { return 0; }

        [[gnu::used]] static int mu_b(void) { return 0; }
    """})
    got = audit(root)
    assert {k for k, v in got.items() if v == R} == {("attr.c", "dead_plain"), ("attr.cpp", "dead_cpp")}
    cut_all(root, "attr.c")
    cut_all(root, "attr.cpp")


def test_attributes_behind_a_macro_keep(make_repo):
    root = make_repo({"defs.h": """
        #define CONSTRUCTOR __attribute__((constructor))
        #define INIT_FN CONSTRUCTOR void
        #define KEPT_INT __attribute__((used)) int
    """, "init.c": """
        #include "defs.h"

        /* runs {before} main */
        static INIT_FN boot(void) {}

        static KEPT_INT pinned = 3;

        static void dead_init(void) {}
    """, "odd.c": """
        #include "defs.h"

        static void CONSTRUCTOR odd_boot(void) {}
    """})
    got = audit_any(root)
    assert got.get(("init.c", "boot")) == A and got.get(("init.c", "pinned")) == A
    assert got[("init.c", "dead_init")] == R
    assert ("odd.c", "odd_boot") not in got          # the grammar cannot read it, so it is not judged


def test_cuts_keep_neighbours_and_preprocessor_lines(make_repo):
    root = make_repo({"p.c": """
        #include <stdlib.h>

        #ifdef FEATURE
        static int feature_dead(void) { return 1; }
        #endif

        static int live(void) { return 0; }

        static int tail_dead(void) { return 2; } /* trailing */

        int main(void) { return live(); }
    """})
    assert removes(root) == {("p.c", "feature_dead"), ("p.c", "tail_dead")}
    out = cut_all(root, "p.c")
    assert "#ifdef FEATURE" in out and "#endif" in out and "static int live(void)" in out
    assert "trailing" not in out


def test_section_pragmas_and_copyright_strings_keep_variables(make_repo):
    root = make_repo({"crt.c": """
        static void init_crt(void) {}

        #pragma data_seg(".CRT$XCU")
        static void (*crt_ptr)(void) = init_crt;
        #pragma data_seg()

        static int dead_fn(void) { return 0; }
    """, "c.c": """
        static const char copyright[] = "Copyright (c) 1990 The Regents";
        static const char sccsid[] = "@(#)c.c 8.1";
        static const char plain[] = "hello";
    """})
    got = audit(root)
    assert got.get(("crt.c", "crt_ptr")) == A and got.get(("crt.c", "init_crt")) != R
    assert got[("crt.c", "dead_fn")] == R
    assert got.get(("c.c", "copyright")) == A and got.get(("c.c", "sccsid")) == A
    assert got[("c.c", "plain")] == R
    assert "plain" not in cut_all(root, "c.c")


def test_a_fixture_a_script_reads_is_shared(make_repo):
    root = make_repo({"t/t1/hello.c": """
        static void hello(void) { }

        static void fixture_only(void) { }
    """, "t/t1-diff.sh": """
        cat t1/hello.c > file.c && git diff -W | grep hello
    """})
    got = audit(root)
    assert got.get(("t/t1/hello.c", "hello")) != R
    assert got[("t/t1/hello.c", "fixture_only")] == R


def test_a_generated_header_may_name_the_statics(make_repo):
    root = make_repo({"lex.c": """
        #include "config.h"
        #include "keywords.inc"

        static int kw_if(void) { return 1; }
        static int kw_never(void) { return 2; }

        int lex(int i) { return table[i](); }
    """, "keywords.txt": """
        kw_if
    """, "plain.c": """
        #include "config.h"

        static int usage(void) { return 0; }
    """, "other.c": """
        int usage_count(void) { return usage; }
    """})
    got = audit(root)
    assert got.get(("lex.c", "kw_if")) != R
    assert got[("lex.c", "kw_never")] == R
    assert got[("plain.c", "usage")] == R              # a missing config.h makes no table


def test_a_c_file_named_by_a_build_file_for_a_macro_include(make_repo):
    root = make_repo({"impl_slow.c": """
        static int slow_helper(void) { return 1; }
    """, "front.c": """
        #include IMPL_SOURCE

        int front(void) { return slow_helper(); }
    """, "CMakeLists.txt": """
        add_definitions(-DIMPL_SOURCE="impl_slow.c")
    """})
    assert audit(root).get(("impl_slow.c", "slow_helper")) != R
