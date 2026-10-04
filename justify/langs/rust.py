"""
Rust.

  function     a private `fn` (no `pub`) at the top of a file or of an inline `mod`, not `main`.
  variable     a private `const` or `static` there.

A private item is seen by its module and every module under it — which may be other files,
reached through `super::` or `crate::` — so each is searched for across the repository (scope
repo). Items in `impl` and `trait` blocks are never units: a trait can need a method without
naming it. Nor are `use` declarations: a trait import is used without its name ever appearing.

Anything with an attribute is kept for judgement: `#[no_mangle]`, `#[cfg(...)]`, `#[allow(...)]`,
`#[inline]`, a proc-macro's own attribute — each may mean something calls it that no search can
see. Test functions (`#[test]`, `#[tokio::test]`, `#[bench]`, ...) are not units at all: the test
harness is what calls them. In a file that pastes identifiers together with a macro, any
function may be called by a name that is never written out, so all are kept.
"""

from __future__ import annotations

from . import Pack, Source, Unit
from .go import leading, trailing

STACKED = {"attribute_item", "line_comment", "block_comment"}
TEST_ATTRS = {"test", "bench", "rstest", "test_case", "test_matrix", "quickcheck", "proptest",
              "wasm_bindgen_test", "googletest"}
PASTING = (b"paste!", b"paste::item!", b"concat_idents!", b"pastey!")


class RustPack(Pack):
    langs = ("Rust",)
    implemented = True

    def units(self, src: Source) -> list[Unit]:
        pasting = any(p in src.data for p in PASTING)
        out: list[Unit] = []
        stack = [src.tree.root_node]
        while stack:
            holder = stack.pop()
            for item in holder.named_children:
                if item.type == "mod_item" and item.child_by_field_name("body") is not None:
                    stack.append(item.child_by_field_name("body"))
                elif item.type in ("function_item", "const_item", "static_item"):
                    u = self._item(src, item, pasting)
                    if u is not None:
                        out.append(u)
        return out

    def _item(self, src: Source, item, pasting: bool) -> Unit | None:
        if any(c.type == "visibility_modifier" for c in item.children):
            return None
        name_node = item.child_by_field_name("name")
        if name_node is None:
            return None
        name = src.data[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace").removeprefix("r#")
        if name in ("main", "_"):
            return None
        attrs, first = _attributes(src, item)
        start = min(leading(src.data, item, STACKED), first)
        if item.type == "function_item" and any(a.split("(")[0].split("::")[-1].strip() in TEST_ATTRS
                                                for a in attrs):
            return None                                  # the test harness calls it
        kind = "function" if item.type == "function_item" else "variable"
        u = Unit(kind=kind, name=name, start=start, end=trailing(src.data, item, STACKED), scope="repo")
        if attrs:
            u.keep = f"it carries #[{attrs[0]}]: an attribute can mean it is called from outside, or built only sometimes"
        elif pasting and kind == "function":
            u.keep = "this file pastes identifiers together with a macro, so its name may be built, not written"
        return u


def _attributes(src: Source, item) -> tuple[list[str], int]:
    """The attributes on an item, as written inside `#[...]`, and where the first one starts.
    An attribute belongs to the item below it across blank lines and comments, so a cut takes
    them all: one left behind would attach itself to the next item."""
    out, first = [], item.start_byte
    prev = item.prev_sibling
    while prev is not None and prev.type in STACKED:
        if prev.type == "attribute_item":
            attr = next((c for c in prev.named_children if c.type == "attribute"), None)
            if attr is not None:
                out.append(src.data[attr.start_byte:attr.end_byte].decode("utf-8", "replace"))
            first = prev.start_byte
        prev = prev.prev_sibling
    return out, first


PACK = RustPack()
