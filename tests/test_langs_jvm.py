"""Java and Kotlin: private members and imports, found dead only where nothing — reflection,
frameworks, serializers, configuration — could still reach them. Each trap sits beside dead code
that must still be found, so a trap that passes is not just a file that failed to parse."""

import textwrap

import pytest

pytest.importorskip("tree_sitter_language_pack")

from justify.engine import run  # noqa: E402
from justify.langs import edit_source, error_count, parser_for  # noqa: E402

R, A = "REMOVE", "AMBIGUOUS"


def audit(root) -> dict[tuple[str, str], tuple[str, str]]:
    """(file, name) → (kind, verdict) for every Java and Kotlin finding."""
    res = run(root, record=False)
    assert not res.metrics["audited"]["unparsed"], res.metrics["audited"]["unparsed"]
    return {(f.file, f.name): (f.kind, f.verdict) for f in res.findings
            if f.file.endswith((".java", ".kt")) and f.kind != "duplicate"}


def verdicts(root, rel) -> dict[str, str]:
    return {name: v for (file, name), (_, v) in audit(root).items() if file == rel}


def findings(root, rel):
    return [f for f in run(root, record=False).findings if f.file == rel and f.kind != "duplicate"]


def parses(lang, rel, text) -> bool:
    return error_count(parser_for(lang, rel).parse(text.encode())) == 0


# ---------------------------------------------------------------- Java: what is found

def test_java_finds_each_kind_of_dead_code(make_repo):
    root = make_repo({"src/p/Shop.java": """
        package p;

        import java.util.List;
        import java.util.Map;
        import static java.lang.Math.max;
        import static java.lang.Math.min;

        public class Shop {
            private static final Logger LOG = LoggerFactory.getLogger(Shop.class);
            private static final List<String> NAMES = List.of("a");
            private int deadCount;
            private int live = 1;

            private void deadHelper() { deadHelper(); }

            private static class DeadBox {}

            public int total(List<Integer> xs) { return max(live, xs.size()); }
        }
        """})
    assert audit(root) == {
        ("src/p/Shop.java", "Map"): ("import", R), ("src/p/Shop.java", "min"): ("import", R),
        ("src/p/Shop.java", "LOG"): ("field", R), ("src/p/Shop.java", "NAMES"): ("field", R),
        ("src/p/Shop.java", "deadCount"): ("field", R), ("src/p/Shop.java", "deadHelper"): ("method", R),
        ("src/p/Shop.java", "DeadBox"): ("class", R)}


# ---------------------------------------------------------------- Java: traps

def test_java_traps_stay_out_of_remove(make_repo):
    root = make_repo({
        "src/p/Svc.java": """
            package p;

            import java.util.List;
            import java.util.Map;
            import javax.annotation.Nullable;
            import p.q.Tag;
            import p.q.Linked;
            import p.q.Gone;
            import static p.q.Color.RED;
            import static p.q.Color.BLUE;
            import org.junit.jupiter.api.Test;
            import org.junit.jupiter.api.BeforeEach;

            /** Works with {@link Linked}. */
            public class Svc implements Runnable {
                private static final long serialVersionUID = 1L;
                private static int deadStatic;
                private @Tag String tagged;
                private List<Map<String, Nullable>> generic = null;

                private void readObject(java.io.ObjectInputStream in) {}
                private void writeObject(java.io.ObjectOutputStream out) {}
                private Object readResolve() { return this; }
                private void byReflection() {}
                private void deadMethod() {}

                @Test private void testIt() {}
                @BeforeEach private void setUp() {}
                @javax.annotation.PostConstruct private void init() {}
                @org.springframework.context.annotation.Bean private Object bean() { return null; }
                @org.springframework.context.event.EventListener private void onEvent(Object e) {}

                private void viaRef() {}
                private void over(int x) {}
                private void over(String s) {}
                private int forNested = 3;
                private static class Inner { int read(Svc s) { return s.forNested; } }
                private static class DeadInner {}

                public void run() {
                    Runnable r = this::viaRef;
                    over(1);
                    Svc.Inner i = new Svc.Inner();
                    System.out.println(generic);
                }

                int shade(p.q.Color c) {
                    switch (c) { case RED: return 1; default: return 0; }
                }
            }
            """,
        "src/p/Mirror.java": """
            package p;

            class Mirror {
                Object m() throws Exception { return Svc.class.getDeclaredMethod("byReflection"); }
            }
            """,
        "src/p/Lombok.java": """
            package p;

            import lombok.Data;
            import lombok.Getter;

            @Data
            class Bean { private int x; }

            class Half { @Getter private int shown; private static int deadHalf; }
            """,
    })
    got = verdicts(root, "src/p/Svc.java")
    for dead in ("Gone", "BLUE", "deadStatic", "deadMethod", "DeadInner"):
        assert got.pop(dead) == R, dead
    for live in ("List", "Map", "Nullable", "Tag", "Linked", "RED", "serialVersionUID", "readObject",
                 "writeObject", "readResolve", "viaRef", "over", "forNested", "Inner", "generic"):
        assert live not in got, live
    assert set(got.values()) <= {A}                    # every other finding is for judgement
    assert {"tagged", "byReflection", "testIt", "setUp", "init", "bean", "onEvent"} <= set(got)
    assert verdicts(root, "src/p/Lombok.java") == {"x": A, "shown": A, "deadHalf": R}


def test_java_gson_and_jackson_fields_are_kept_but_static_ones_are_not(make_repo):
    root = make_repo({
        "src/p/User.java": """
            package p;

            public class User {
                private String name;
                private transient int cache;
                private static int deadStatic;
            }
            """,
        "src/p/Codec.java": """
            package p;

            import com.google.gson.Gson;

            class Codec { User read(String s) { return new Gson().fromJson(s, User.class); } }
            """,
        "src/p/Dto.java": """
            package p;

            import com.fasterxml.jackson.annotation.JsonProperty;

            public class Dto { @JsonProperty private int id; }
            """,
    })
    assert verdicts(root, "src/p/User.java") == {"name": A, "cache": R, "deadStatic": R}
    assert verdicts(root, "src/p/Dto.java") == {"id": A}


# ---------------------------------------------------------------- Java: regressions

def test_a_nested_class_named_by_its_binary_name_in_config_is_kept(make_repo):
    root = make_repo({
        "src/p/Host.java": """
            package p;

            public class Host {
                private static class Plugin {}
                private static class Unlisted {}
            }
            """,
        "src/main/resources/beans.xml": '<beans><bean class="p.Host$Plugin"/></beans>\n',
        "proguard-rules.pro": "-keep class p.Host$Plugin { *; }\n",
    })
    assert verdicts(root, "src/p/Host.java") == {"Plugin": A, "Unlisted": R}


def test_a_project_factory_named_like_a_jdk_one_runs_code(make_repo):
    root = make_repo({"src/p/Stats.java": """
        package p;

        import java.util.List;
        import java.util.regex.Pattern;

        class Stats {
            private static final Object REGISTERED = Registry.of("hits");
            private static final Object BUILT = Metrics.compile("q");
            private static final Object PARSED = Level.valueOf("TRACE");
            private static final Object UNQUALIFIED = of("x");
            private static final List<String> NAMES = List.of("a");
            private static final Pattern WORD = Pattern.compile("\\\\w+");
            private static final List<Object> WRAPPED = List.of(register());
        }
        """})
    assert verdicts(root, "src/p/Stats.java") == {
        "REGISTERED": A, "BUILT": A, "PARSED": A, "UNQUALIFIED": A, "WRAPPED": A, "NAMES": R, "WORD": R}


def test_a_serializer_reached_through_an_adapter_or_a_build_file_keeps_fields(make_repo):
    model = "package p;\n\npublic class Model { private String name; }\n"
    adapter = make_repo({
        "src/p/Model.java": model,
        "src/p/Api.java": "package p;\n\nimport retrofit2.converter.gson.GsonConverterFactory;\n\n"
                          "class Api { Object f = GsonConverterFactory.create(); }\n"}, name="adapter")
    assert verdicts(adapter, "src/p/Model.java") == {"name": A}
    build = make_repo({"src/p/Model.java": model,
                       "build.gradle": "dependencies { implementation 'com.google.code.gson:gson:2.10' }\n"},
                      name="build")
    assert verdicts(build, "src/p/Model.java") == {"name": A}
    plain = make_repo({"src/p/Model.java": model}, name="plain")
    assert verdicts(plain, "src/p/Model.java") == {"name": R}


def test_exceptions_and_classes_with_a_serial_version_keep_instance_fields(make_repo):
    root = make_repo({"src/p/Errs.java": """
        package p;

        class Failure extends IllegalStateException { private int code; private static int deadA; }

        class Saved { private static final long serialVersionUID = 2L; private int kept; }

        class Plain { private int deadB; }
        """})
    assert verdicts(root, "src/p/Errs.java") == {"code": A, "deadA": R, "kept": A, "deadB": R}


# ---------------------------------------------------------------- Java: cuts

JAVA = """\
package p;

import java.util.List;
import java.util.Map;
import static java.lang.Math.max;

public class Shop {
    private int live = 1;

    /** Never read. */
    private int deadCount;

    /**
     * Never called.
     */
    private void deadHelper() {
        System.out.println("x");
    }

    private static class DeadBox {
        int y;
    }

    public int total(List<Integer> xs) { return max(live, xs.size()); }
}
"""


def test_every_java_removal_kind_cuts_cleanly(make_repo):
    root = make_repo({"src/p/Shop.java": JAVA})
    found = {f.name: f for f in findings(root, "src/p/Shop.java")}
    assert {n: (f.kind, f.verdict) for n, f in found.items()} == {
        "Map": ("import", R), "deadCount": ("field", R), "deadHelper": ("method", R), "DeadBox": ("class", R)}
    keep = ("import java.util.List;", "import static java.lang.Math.max;", "private int live = 1;",
            "public int total(List<Integer> xs) { return max(live, xs.size()); }")
    for f in found.values():
        out = edit_source("src/p/Shop.java", JAVA, [f])
        assert parses("Java", "Shop.java", out) and all(k in out for k in keep), f.name
    out = edit_source("src/p/Shop.java", JAVA, list(found.values()))
    assert parses("Java", "Shop.java", out) and all(k in out for k in keep)
    for gone in ("Map", "deadCount", "Never", "deadHelper", "DeadBox", "int y;"):
        assert gone not in out
    assert "\n\n\n" not in out


# ---------------------------------------------------------------- Kotlin: what is found

def test_kotlin_finds_each_kind_of_dead_code(make_repo):
    root = make_repo({"src/p/Shop.kt": """
        package p

        import kotlin.math.max
        import kotlin.math.min

        private fun deadTop() = 1
        private val deadTopVal = listOf(1)

        class Shop {
            private val deadField = 2
            private val live = 3
            private fun deadMethod() = deadMethod()
            fun total() = max(live, 4)
        }
        """})
    assert audit(root) == {
        ("src/p/Shop.kt", "min"): ("import", R), ("src/p/Shop.kt", "deadTop"): ("function", R),
        ("src/p/Shop.kt", "deadTopVal"): ("variable", R), ("src/p/Shop.kt", "deadField"): ("field", R),
        ("src/p/Shop.kt", "deadMethod"): ("method", R)}


# ---------------------------------------------------------------- Kotlin: traps

def test_kotlin_traps_stay_out_of_remove(make_repo):
    root = make_repo({"src/p/K.kt": """
        package p

        import a.b.Gen
        import a.b.Doc
        import a.b.Gone
        import androidx.compose.runtime.getValue

        /** See [Doc]. */
        private fun helper() = 1
        private fun dead() = 2
        private fun String.shout() = uppercase()
        private fun String.deadExt() = lowercase()

        class K {
            private val braced = 3
            private val bare = 4
            private val deadField = 5
            private val koin by inject()
            private val ann: Int
                @JvmName("annGet") get() = 1

            companion object {
                @JvmStatic private fun js() = 1
                private fun deadCompanion() = 2
            }

            fun go(): List<Gen> {
                val f = ::helper
                println("a${braced}b $bare".shout())
                return emptyList()
            }
        }
        """})
    got = verdicts(root, "src/p/K.kt")
    for dead in ("Gone", "dead", "deadExt", "deadField", "deadCompanion"):
        assert got.pop(dead) == R, dead
    for live in ("Gen", "Doc", "helper", "shout", "braced", "bare"):
        assert live not in got, live
    assert got == {"getValue": A, "koin": A, "ann": A, "js": A}


def test_kotlin_serializable_and_annotated_classes_keep_properties(make_repo):
    root = make_repo({"src/p/S.kt": """
        package p

        class Saved : java.io.Serializable { private val ser = 1 }

        class Failure(msg: String) : RuntimeException(msg) { private val code = 2 }

        @Parcelize
        class Parcel { private val parcelled = 3 }

        class Plain { private val deadPlain = 4 }
        """})
    assert verdicts(root, "src/p/S.kt") == {"ser": A, "code": A, "parcelled": A, "deadPlain": R}


# ---------------------------------------------------------------- Kotlin: cuts

KOTLIN = """\
package p

import kotlin.math.max
import kotlin.math.min

/** Never called. */
private fun deadTop(): Int {
    return 1
}

private val deadTopVal: Int
    get() = 2

private var deadVar = 0
    private set

private fun chained() = listOf(1)
    .map { it + 1 }

class Shop {
    private val live = 3

    private val deadField: Int
        get() = live

    private fun deadMethod() = 4

    fun total() = max(live, 5)
}
"""


def test_every_kotlin_removal_kind_cuts_cleanly(make_repo):
    root = make_repo({"src/p/Shop.kt": KOTLIN})
    found = {f.name: f for f in findings(root, "src/p/Shop.kt")}
    assert {n: (f.kind, f.verdict) for n, f in found.items()} == {
        "min": ("import", R), "deadTop": ("function", R), "deadTopVal": ("variable", R),
        "deadVar": ("variable", R), "chained": ("function", R), "deadField": ("field", R),
        "deadMethod": ("method", R)}
    keep = ("import kotlin.math.max", "private val live = 3", "fun total() = max(live, 5)", "class Shop {")
    for f in found.values():
        out = edit_source("src/p/Shop.kt", KOTLIN, [f])
        assert parses("Kotlin", "Shop.kt", out) and all(k in out for k in keep), f.name
    out = edit_source("src/p/Shop.kt", KOTLIN, list(found.values()))
    assert parses("Kotlin", "Shop.kt", out) and all(k in out for k in keep)
    for gone in ("min", "dead", "Never", "get()", "set", ".map", "listOf"):     # accessors go too
        assert gone not in out, gone
    assert out == textwrap.dedent("""\
        package p

        import kotlin.math.max

        class Shop {
            private val live = 3

            fun total() = max(live, 5)
        }
        """)
