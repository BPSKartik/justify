"""C#: private members and file-local aliases, found dead only where nothing — partial parts,
markup, reflection, the runtime — could still reach them. Each trap sits beside dead code that
must still be found, so a trap that passes is not just a file that failed to parse."""

import pytest

pytest.importorskip("tree_sitter_language_pack")

from justify.engine import run  # noqa: E402
from justify.langs import edit_source, error_count, parser_for  # noqa: E402
from justify.proof import SharedLine  # noqa: E402

R, A = "REMOVE", "AMBIGUOUS"


def audit(root) -> dict[tuple[str, str], tuple[str, str]]:
    """(file, name) → (kind, verdict) for every C# finding."""
    res = run(root, record=False)
    assert not res.metrics["audited"]["unparsed"], res.metrics["audited"]["unparsed"]
    return {(f.file, f.name): (f.kind, f.verdict) for f in res.findings
            if f.file.endswith(".cs") and f.kind != "duplicate"}


def findings(root, rel):
    return [f for f in run(root, record=False).findings if f.file == rel and f.kind != "duplicate"]


def parses(rel, text) -> bool:
    return error_count(parser_for("C#", rel).parse(text.encode())) == 0


# ---------------------------------------------------------------- what is a unit

def test_using_aliases_are_imports_and_namespace_usings_are_never_touched(make_repo):
    root = make_repo({"App.cs": """
        using System;
        using System.Linq;
        global using System.Text;
        using static System.Math;
        using Json = Newtonsoft.Json.JsonConvert;
        using Builder = System.Text.StringBuilder;
        global using Gone = System.Text.Encoding;

        public class App
        {
            public string Save(object o) => Json.SerializeObject(o) + Abs(-1);
        }
    """})
    assert audit(root) == {("App.cs", "Builder"): ("import", R)}


def test_private_and_default_private_members(make_repo):
    root = make_repo({"Shop.cs": """
        namespace Shop;

        public class Cart
        {
            private int _count;
            int _total;
            private string Label { get; set; }
            string Code => "x";
            private void Recount() { Recount(); }
            void Reprice() { }
            private class Line { }
            enum Kind { A }
            delegate void Changed();

            public int Shown;
            protected int _guarded;
            internal void Ship() { }
            private protected void Audit() { }
            protected internal int Kept;
        }

        public interface IStore
        {
            void Save();
            class Nested { }
        }

        public struct Point
        {
            private static int s_made;
            private static void Log() { }
        }

        public record Order(int Id)
        {
            private string Note => "n";
        }
    """})
    assert audit(root) == {
        ("Shop.cs", "_count"): ("field", R), ("Shop.cs", "_total"): ("field", R),
        ("Shop.cs", "Label"): ("field", R), ("Shop.cs", "Code"): ("field", R),
        ("Shop.cs", "Recount"): ("method", R), ("Shop.cs", "Reprice"): ("method", R),
        ("Shop.cs", "Line"): ("class", R), ("Shop.cs", "Kind"): ("class", R),
        ("Shop.cs", "Changed"): ("class", R),
        ("Shop.cs", "s_made"): ("field", R), ("Shop.cs", "Log"): ("method", R),
        ("Shop.cs", "Note"): ("field", R),
    }


def test_members_of_a_dead_nested_type_go_with_it(make_repo):
    root = make_repo({"Outer.cs": """
        public class Outer
        {
            private class Gone
            {
                private void AlsoGone() { }
                private int _alsoGone;
            }

            private class Used
            {
                private void Dead() { }
                public void Live() { }
            }

            public object Make() => new Used();
        }
    """})
    assert audit(root) == {("Outer.cs", "Gone"): ("class", R), ("Outer.cs", "Dead"): ("method", R)}


def test_names_the_word_index_cannot_see_whole(make_repo):
    root = make_repo({"Odd.cs": """
        public class Odd
        {
            private int @lock;
            private int größe;
            private int _dead;
            public void Set() { @lock = 1; }
        }
    """})
    assert audit(root) == {("Odd.cs", "_dead"): ("field", R)}


def test_members_inside_preprocessor_blocks(make_repo):
    root = make_repo({"Build.cs": """
        public class Build
        {
        #if DEBUG
            private void Trace() { }
        #else
            private void Quiet() { }
        #endif
            #region helpers
            private void Helper() { }
            #endregion
            public void Run() { Helper(); }
        }
    """})
    assert audit(root) == {("Build.cs", "Trace"): ("method", R), ("Build.cs", "Quiet"): ("method", R)}


def test_generated_files_are_not_judged(make_repo):
    root = make_repo({
        "Form1.Designer.cs": "partial class Form1\n{\n    private int _neverUsed;\n}\n",
        "Model.g.cs": "class Model\n{\n    private int _neverUsed;\n}\n",
        "Real.cs": "class Real\n{\n    private int _dead;\n}\n",
    })
    assert audit(root) == {("Real.cs", "_dead"): ("field", R)}


# ---------------------------------------------------------------- the traps

def test_reflection_by_nameof_and_by_string(make_repo):
    root = make_repo({"Plugin.cs": """
        using System;
        using System.Reflection;

        public class Plugin
        {
            private void Helper() { }
            private void ByString() { }
            private void Dead() { }

            public string Name() => nameof(Helper);
            public MethodInfo Find() => typeof(Plugin).GetMethod("ByString");
        }
    """})
    assert audit(root) == {("Plugin.cs", "Dead"): ("method", R)}


def test_a_string_in_another_file_may_reach_a_private_member(make_repo):
    root = make_repo({
        "Calc.cs": """
            public class Calc
            {
                private int Secret() => 42;
                private int Dead() => 0;
            }
        """,
        "Tests/CalcTests.cs": """
            using System.Reflection;

            public class CalcTests
            {
                public object Call() => typeof(Calc).GetMethod("Secret", Flags.Private).Invoke(new Calc(), null);
                public int Dead() => 1;
            }
        """,
    })
    assert audit(root) == {("Calc.cs", "Secret"): ("method", A), ("Calc.cs", "Dead"): ("method", R)}


def test_a_file_that_looks_up_non_public_members_keeps_its_own(make_repo):
    root = make_repo({"Dispatch.cs": """
        using System.Reflection;

        public class Dispatch
        {
            private void HandleClick() { }
            private void HandleKey() { }

            public void On(string evt) =>
                GetType().GetMethod("Handle" + evt, BindingFlags.NonPublic | BindingFlags.Instance).Invoke(this, null);
        }
    """})
    assert audit(root) == {("Dispatch.cs", "HandleClick"): ("method", A), ("Dispatch.cs", "HandleKey"): ("method", A)}


def test_serialized_private_fields(make_repo):
    root = make_repo({"Player.cs": """
        using Newtonsoft.Json;
        using UnityEngine;

        public class Player : MonoBehaviour
        {
            [JsonProperty] private string _name;
            [SerializeField] private int speed;
            [field: SerializeField] private int Lives { get; set; }
            private int _dead;
        }
    """})
    assert audit(root) == {("Player.cs", "_name"): ("field", A), ("Player.cs", "speed"): ("field", A),
                           ("Player.cs", "Lives"): ("field", A), ("Player.cs", "_dead"): ("field", R)}


def test_unity_messages_are_called_by_name(make_repo):
    root = make_repo({"Mover.cs": """
        using UnityEngine;

        public class Mover : MonoBehaviour
        {
            private void Update() { }
            void Awake() { }
            void OnTriggerEnter2D(Collider2D other) { }
            void OnMouseDown() { }
            private void Unused() { }
        }
    """})
    found = audit(root)
    assert found.pop(("Mover.cs", "Unused")) == ("method", R)
    assert set(found.values()) == {("method", A)} and len(found) == 4


def test_winforms_handlers_wired_in_the_designer_file(make_repo):
    root = make_repo({
        "Form1.cs": """
            using System;
            using System.Windows.Forms;

            public partial class Form1 : Form
            {
                public Form1() { InitializeComponent(); }

                private void button1_Click(object sender, EventArgs e) { }
                private void Form1_Load(object sender, EventArgs e) { }
                private void Unwired(object sender, EventArgs e) { }
            }
        """,
        "Form1.Designer.cs": """
            partial class Form1
            {
                private System.Windows.Forms.Button button1;

                private void InitializeComponent()
                {
                    this.button1 = new System.Windows.Forms.Button();
                    this.button1.Click += new System.EventHandler(this.button1_Click);
                    this.Load += new System.EventHandler(this.Form1_Load);
                }
            }
        """,
    })
    res = findings(root, "Form1.cs")
    assert [(f.kind, f.name, f.verdict) for f in res] == [("method", "Unwired", R)]
    assert "anywhere in the repository" in res[0].reason


def test_wpf_handler_named_only_in_xaml(make_repo):
    root = make_repo({
        "MainWindow.xaml": """
            <Window x:Class="App.MainWindow" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
                <Button Content="Go" Click="Go_Click" />
                <TextBlock Text="{Binding Greeting}" />
            </Window>
        """,
        "MainWindow.xaml.cs": """
            using System.Windows;

            namespace App
            {
                public partial class MainWindow : Window
                {
                    private void Go_Click(object sender, RoutedEventArgs e) { }
                    private string Greeting => "hello";
                    private void Stale() { }
                }
            }
        """,
    })
    assert audit(root) == {("MainWindow.xaml.cs", "Stale"): ("method", R)}


def test_razor_code_blocks_and_code_behind(make_repo):
    root = make_repo({
        "Pages/Counter.razor": """
            @page "/counter"
            <button @onclick="Increment">Count: @count</button>
            <p>@Describe()</p>

            @code {
                private int count;
                private void Increment() => count++;
            }
        """,
        "Pages/Counter.razor.cs": """
            namespace App.Pages
            {
                public partial class Counter
                {
                    private string Describe() => "counter";
                    private void Leftover() { }
                }
            }
        """,
    })
    assert audit(root) == {("Pages/Counter.razor.cs", "Leftover"): ("method", R)}


def test_methods_used_as_delegates_and_method_groups(make_repo):
    root = make_repo({"Pipeline.cs": """
        using System;
        using System.Linq;
        using System.Collections.Generic;

        public class Pipeline
        {
            private static int Map(int x) => x + 1;
            private void Helper() { }
            private bool Keep(int x) => x > 0;
            private int Unused(int x) => x;

            public IEnumerable<int> Run(IEnumerable<int> xs)
            {
                Action a = Helper;
                a();
                return xs.Select(Map).Where(Keep);
            }
        }
    """})
    assert audit(root) == {("Pipeline.cs", "Unused"): ("method", R)}


def test_expression_bodied_members(make_repo):
    root = make_repo({"Shape.cs": """
        public class Shape
        {
            private int Twice(int x) => x * 2;
            private int Side => 3;
            private int Thrice(int x) => x * 3;
            private int Area => Side * Side;
            public int Four => Twice(2) + Area;
        }
    """})
    assert audit(root) == {("Shape.cs", "Thrice"): ("method", R)}


def test_private_nested_class_used_as_a_generic_argument(make_repo):
    root = make_repo({"Graph.cs": """
        using System.Collections.Generic;

        public class Graph
        {
            private class Node { }
            private class Edge { }
            private class Orphan { }
            private readonly List<Node> _nodes = new List<Node>();
            private readonly Dictionary<int, Edge> _edges = new();
            public int Count => _nodes.Count + _edges.Count;
        }
    """})
    assert audit(root) == {("Graph.cs", "Orphan"): ("class", R)}


def test_record_primary_constructors(make_repo):
    root = make_repo({"Geo.cs": """
        public class Geo
        {
            private record Point(int X, int Y);
            private record struct Size(int W, int H);
            private record Unused(int A);

            public object Origin() => new Point(0, 0);
            public object Zero() => new Size(0, 0);
        }

        public record Temperature(double Celsius)
        {
            public double Fahrenheit { get; } = ToF(Celsius);
            static double ToF(double c) => c * 9 / 5 + 32;
            double Kelvin => Celsius + 273.15;
        }
    """})
    assert audit(root) == {("Geo.cs", "Unused"): ("class", R), ("Geo.cs", "Kelvin"): ("field", R)}


def test_getter_only_property_named_in_a_binding_string(make_repo):
    root = make_repo({
        "View.cs": """
            using System.Windows.Data;

            public class View
            {
                private string DisplayName => "x";
                private string Unbound => "y";
                public Binding Bind() => new Binding("DisplayName");
            }
        """,
        "Panel.cs": """
            public class Panel
            {
                private string Caption { get { return "c"; } }
                private string Gone { get { return "g"; } }
            }
        """,
        "Templates/panel.xml": "<Label Text='{Binding Caption}' />\n",
        "README.md": "The panel shows a caption. Gone is a word in prose.\n",
    })
    assert audit(root) == {("View.cs", "Unbound"): ("field", R), ("Panel.cs", "Caption"): ("field", A),
                           ("Panel.cs", "Gone"): ("field", R)}


def test_partial_methods_are_kept(make_repo):
    root = make_repo({"Model.cs": """
        public partial class Model
        {
            partial void OnNameChanged(string value);
            partial void OnNameChanged(string value) { }
            partial void OnSaved();
            private void Unused() { }
        }
    """})
    assert audit(root) == {("Model.cs", "OnSaved"): ("method", A), ("Model.cs", "Unused"): ("method", R)}


# ---------------------------------------------------------------- kept by what the language says

def test_members_the_runtime_or_compiler_reaches(make_repo):
    root = make_repo({"IRunner.cs": "public interface IRunner { void Run(); }\n", "Native.cs": """
        using System;
        using System.Runtime.InteropServices;

        class Program : IRunner
        {
            static void Main(string[] args) { }
            [DllImport("user32.dll")] private static extern int MessageBox(IntPtr h, string t, string c, uint type);
            private static extern void Raw();
            void IRunner.Run() { }
            private void Dispose(bool disposing) { }
            private bool ShouldSerializeColor() => true;
            private int Color { get; set; }
            private void ResetColor() { }
            private void ResetEverything() { }
            protected void Page_Load(object sender, EventArgs e) { }
            private void Application_Start() { }
        }
    """})
    found = audit(root)
    assert found.pop(("Native.cs", "ResetEverything")) == ("method", R)
    assert found.pop(("Native.cs", "Color")) == ("field", R)
    assert set(found.values()) == {("method", A)}
    assert {name for _, name in found} == {"Main", "MessageBox", "Raw", "Run", "Dispose", "ShouldSerializeColor",
                                           "ResetColor", "Application_Start"}


def test_layout_and_serialization_keep_fields(make_repo):
    root = make_repo({"Interop.cs": """
        using System;
        using System.Runtime.InteropServices;

        public struct Header
        {
            private int _magic;
            private int Version { get; set; }
            private int Twice => 2;
            private static int s_count;
        }

        [Serializable]
        public class Saved
        {
            private int _version;
            private static int s_cache;
        }

        [Obsolete]
        public class Old
        {
            private int _unused;
        }
    """})
    assert audit(root) == {
        ("Interop.cs", "_magic"): ("field", A), ("Interop.cs", "Version"): ("field", A),
        ("Interop.cs", "Twice"): ("field", R), ("Interop.cs", "s_count"): ("field", R),
        ("Interop.cs", "_version"): ("field", A), ("Interop.cs", "s_cache"): ("field", R),
        ("Interop.cs", "_unused"): ("field", R),
    }


def test_initializers_that_do_work_are_kept(make_repo):
    root = make_repo({"Clock.cs": """
        using System;
        using System.Threading;

        public class Clock
        {
            private readonly Timer _timer = new Timer(Tick, null, 0, 1000);
            private static readonly bool s_registered = Registry.Register(typeof(Clock));
            private int Ticks { get; } = Count();
            private readonly object _lock = new object();
            private readonly Func<int> _later = () => Count();
            private const string Key = "k";

            private static void Tick(object state) { }
            private static int Count() => 0;
        }
    """})
    assert audit(root) == {
        ("Clock.cs", "_timer"): ("field", A), ("Clock.cs", "s_registered"): ("field", A),
        ("Clock.cs", "Ticks"): ("field", A), ("Clock.cs", "_lock"): ("field", R),
        ("Clock.cs", "_later"): ("field", R), ("Clock.cs", "Key"): ("field", R),
    }


def test_nested_types_a_framework_may_find_by_scanning(make_repo):
    root = make_repo({"Feature.cs": """
        using System.Runtime.CompilerServices;

        public static class Feature
        {
            class Handler : IRequestHandler<Command> { }
            class Init
            {
                [ModuleInitializer] internal static void Run() { }
            }
            [Serializable] class Dto { }
            class Plain { }
        }
    """})
    assert audit(root) == {("Feature.cs", "Handler"): ("class", A), ("Feature.cs", "Init"): ("class", A),
                           ("Feature.cs", "Dto"): ("class", A), ("Feature.cs", "Plain"): ("class", R)}


def test_names_in_scenes_and_scripts_are_doubted_but_prose_is_not(make_repo):
    root = make_repo({
        "Assets/Hero.cs": """
            using UnityEngine;

            public class Hero : MonoBehaviour
            {
                private void PlayFootstep() { }
                private void Retired() { }
            }
        """,
        "Assets/Walk.anim": "AnimationEvent:\n  functionName: PlayFootstep\n",
        "CHANGELOG.md": "- Retired the old jump.\n",
    })
    assert audit(root) == {("Assets/Hero.cs", "PlayFootstep"): ("method", A),
                           ("Assets/Hero.cs", "Retired"): ("method", R)}


def test_partial_type_members_are_searched_across_the_repository(make_repo):
    root = make_repo({
        "Customer.cs": """
            public partial class Customer
            {
                private string _name;
                private void Validate() { }
                private void Stale() { }
            }
        """,
        "Customer.Rules.cs": """
            public partial class Customer
            {
                public bool Check() { Validate(); return _name != null; }
            }
        """,
    })
    res = findings(root, "Customer.cs")
    assert [(f.kind, f.name, f.verdict) for f in res] == [("method", "Stale", R)]


# ---------------------------------------------------------------- proving a removal: the cut

SOURCE = """\
using System;
using Gone = System.Text.StringBuilder;
using Kept = System.Text.Encoding;

namespace Demo
{
    public class Shop
    {
        private int _dead;
        private int _a = 1, _b;
        private string Label => "x";
        private int Count { get; set; }

        /// <summary>Recounts.</summary>
        /// <remarks>Never called.</remarks>
        private void Recount()
        {
            Recount();
        }

        private class Line
        {
            public int Qty;
        }

        public int Total => _a + Kept.UTF8.CodePage;
    }
}
"""


def test_every_removal_kind_cuts_cleanly(make_repo):
    root = make_repo({"Shop.cs": SOURCE})
    found = {f.name: f for f in findings(root, "Shop.cs")}
    assert {n: (f.kind, f.verdict) for n, f in found.items()} == {
        "Gone": ("import", R), "_dead": ("field", R), "_b": ("field", R), "Label": ("field", R),
        "Count": ("field", R), "Recount": ("method", R), "Line": ("class", R)}
    cuttable = [f for f in found.values() if f.name != "_b"]
    for f in cuttable:                                     # each alone
        out = edit_source("Shop.cs", SOURCE, [f])
        assert parses("Shop.cs", out)
        assert "public int Total => _a + Kept.UTF8.CodePage;" in out and "using Kept" in out
    out = edit_source("Shop.cs", SOURCE, cuttable)          # all together
    assert parses("Shop.cs", out)
    for gone in ("Gone", "_dead", "Label", "Count", "Recount", "class Line", "Never called", "Qty"):
        assert gone not in out
    assert "private int _a = 1, _b;" in out and "public class Shop" in out and "using System;" in out


def test_one_of_several_variables_is_not_cut(make_repo):
    with pytest.raises(SharedLine):
        edit_source("Shop.cs", SOURCE, [f for f in findings(make_repo({"Shop.cs": SOURCE}), "Shop.cs")
                                        if f.name == "_b"])


def test_a_member_sharing_its_line_is_not_cut(make_repo):
    text = "public class Tight\n{\n    private int _x; public int Y;\n}\n"
    root = make_repo({"Tight.cs": text})
    [f] = findings(root, "Tight.cs")
    with pytest.raises(SharedLine):
        edit_source("Tight.cs", text, [f])


# ---------------------------------------------------------------- hardening: names reached without being spelled

def test_names_the_compiler_calls_by_pattern(make_repo):
    root = make_repo({
        "Bag.cs": """
            using System.Collections.Generic;
            using System.Linq;
            using System.Runtime.CompilerServices;

            public struct Bag
            {
                private IEnumerator<int> GetEnumerator() { yield return 1; }
                private void Deconstruct(out int a, out int b) { a = 1; b = 2; }
                private TaskAwaiter GetAwaiter() => default;
                private void Add(int x) { }
                private int Count => 1;
                private Bag Select(System.Func<int, int> f) => this;
                private void Dead() { }

                public int Sum() { int s = 0; foreach (var x in this) s += x; var (a, b) = this; return s + a + b + this[^1]; }
                public int this[int i] => i;
                public async System.Threading.Tasks.Task Wait() { await this; }
                public static Bag Make() => new Bag { 1, 2 };
                public Bag Query() => from x in this select x;
            }
        """,
        "Plain.cs": """
            public class Plain
            {
                private void Add(int x) { }
                private int Count => 0;
                private void Select() { }
                public int[] Items() => new int[3];
            }
        """,
    })
    found = audit(root)
    assert found.pop(("Bag.cs", "Dead")) == ("method", R)
    assert {n for (_, n), (_, v) in found.items() if v == A} == {
        "GetEnumerator", "Deconstruct", "GetAwaiter", "Add", "Count", "Select"}
    # without an initializer, an index or a query in the file, these are ordinary names
    assert {n: v for (f, n), (_, v) in found.items() if f == "Plain.cs"} == {"Add": R, "Count": R, "Select": R}


def test_a_sealed_records_print_members_and_equality_contract(make_repo):
    root = make_repo({"Person.cs": """
        using System.Text;

        public sealed record Person(string Name)
        {
            private bool PrintMembers(StringBuilder b) { b.Append(Name); return true; }
            private System.Type EqualityContract => typeof(Person);
            private void Dead() { }
        }
    """})
    assert audit(root) == {("Person.cs", "PrintMembers"): ("method", A),
                           ("Person.cs", "EqualityContract"): ("field", A), ("Person.cs", "Dead"): ("method", R)}


def test_unity_editor_and_asset_processor_messages(make_repo):
    root = make_repo({"Editor/Hooks.cs": """
        using UnityEditor;
        using UnityEngine;

        public class Hooks : AssetModificationProcessor
        {
            private static string[] OnWillSaveAssets(string[] paths) => paths;
            private static AssetDeleteResult OnWillDeleteAsset(string p, RemoveAssetOptions o) => default;
            private static string OnGeneratedCSProject(string path, string content) => content;
            private void ShowButton(Rect r) { }
            private bool HasFrameBounds() => true;
            private void Dead() { }
        }
    """})
    found = audit(root)
    assert found.pop(("Editor/Hooks.cs", "Dead")) == ("method", R)
    assert set(found.values()) == {("method", A)} and len(found) == 5


def test_unity_input_actions_are_sent_as_on_messages(make_repo):
    root = make_repo({
        "Assets/Controls.inputactions": '{"maps": [{"name": "Player", "actions": [{"name": "Jump"}]}]}\n',
        "Assets/Hero.cs": """
            using UnityEngine;

            public class Hero : MonoBehaviour
            {
                private void OnJump() { }
                private void OnDash() { }
            }
        """,
    })
    assert audit(root) == {("Assets/Hero.cs", "OnJump"): ("method", A), ("Assets/Hero.cs", "OnDash"): ("method", R)}


def test_an_attribute_alias_used_without_its_suffix(make_repo):
    root = make_repo({"Dto.cs": """
        using JsonAttribute = Newtonsoft.Json.JsonPropertyAttribute;
        using GoneAttribute = System.ObsoleteAttribute;

        public class Dto
        {
            [Json("x")] public int X;
        }
    """})
    assert audit(root) == {("Dto.cs", "JsonAttribute"): ("import", A), ("Dto.cs", "GoneAttribute"): ("import", R)}


def test_unsafe_accessor_in_another_file_reaches_a_private_member(make_repo):
    root = make_repo({
        "Calc.cs": """
            public class Calc
            {
                private int Secret() => 42;
                private int Dead() => 0;
            }
        """,
        "Tests/Spy.cs": """
            using System.Runtime.CompilerServices;

            public static class Spy
            {
                [UnsafeAccessor(UnsafeAccessorKind.Method)]
                public static extern int Secret(Calc c);
            }
        """,
    })
    assert audit(root) == {("Calc.cs", "Secret"): ("method", A), ("Calc.cs", "Dead"): ("method", R)}


def test_host_builders_and_fody_change_handlers(make_repo):
    root = make_repo({
        "Program.cs": """
            using Microsoft.Extensions.Hosting;

            public class Program
            {
                private static IHostBuilder CreateHostBuilder(string[] args) => Host.CreateDefaultBuilder(args);
                private static void Unused() { }
            }
        """,
        "Person.cs": """
            using System.ComponentModel;

            public class Person : INotifyPropertyChanged
            {
                public event PropertyChangedEventHandler PropertyChanged;
                public string Name { get; set; }
                private void OnNameChanged() { }
                private void OnAgeChanged() { }
            }
        """,
    })
    assert audit(root) == {("Program.cs", "CreateHostBuilder"): ("method", A), ("Program.cs", "Unused"): ("method", R),
                           ("Person.cs", "OnNameChanged"): ("method", A), ("Person.cs", "OnAgeChanged"): ("method", R)}


EDGES = ("public class Edges\r\n{\r\n    #region Helpers\r\n    /// <summary>Gone.</summary>\r\n"
         "    private void Dead() { }\r\n    #endregion\r\n#if DEBUG\r\n    private int _debugOnly;\r\n#endif\r\n"
         "    private static readonly int[] Table = { 1, 2 };\r\n    public void Live() { }\r\n}\r\n")


def test_cuts_beside_regions_conditionals_and_crlf(make_repo):
    root = make_repo({"Edges.cs": EDGES})
    found = {f.name: f for f in findings(root, "Edges.cs")}
    assert {n: (f.kind, f.verdict) for n, f in found.items()} == {
        "Dead": ("method", R), "_debugOnly": ("field", R), "Table": ("field", R)}
    for f in found.values():
        out = edit_source("Edges.cs", EDGES, [f])
        assert parses("Edges.cs", out) and "public void Live() { }\r\n" in out and f.name not in out
    out = edit_source("Edges.cs", EDGES, list(found.values()))
    assert parses("Edges.cs", out)
    assert out == ("public class Edges\r\n{\r\n    #region Helpers\r\n    #endregion\r\n#if DEBUG\r\n#endif\r\n"
                   "    public void Live() { }\r\n}\r\n")
