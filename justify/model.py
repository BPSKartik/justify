"""The one record every stage reads and writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Verdicts, in the order a unit can move through them. KEEP is the default:
# anything that has not climbed every rung of the safety ladder stays.
KEEP = "KEEP"
REMOVE = "REMOVE"            # no use found; still has to pass the proof
AMBIGUOUS = "AMBIGUOUS"      # the graph cannot settle it; needs judgement
SIMPLIFY = "SIMPLIFY"        # used, but duplicates something that exists

KINDS = ("import", "function", "class", "method", "field", "variable", "type", "style", "dependency",
         "duplicate")
# what a removal can be: code that is cut out, then proved
CODE_KINDS = ("import", "function", "class", "method", "field", "variable", "type", "style")


@dataclass
class Finding:
    kind: str                 # one of KINDS
    file: str                 # repository-relative path
    line: int                 # first line (decorators included)
    end_line: int             # last line
    name: str
    verdict: str              # REMOVE / AMBIGUOUS / SIMPLIFY
    reason: str
    lines: int = 1            # how many lines this unit occupies
    authored_by: str = "unknown"   # ai / human / uncommitted / unknown
    evidence: list[str] = field(default_factory=list)   # file:line references
    judgement: dict[str, Any] | None = None   # stages 4-5, when a model ran
    proof: str = "not run"    # passed / failed / compile error / not run / not provable
    final: str = ""           # the verdict after every stage: KEEP / REMOVE / SIMPLIFY

    def key(self) -> str:
        return f"{self.kind}:{self.file}:{self.line}:{self.name}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
