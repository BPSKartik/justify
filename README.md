# Justify

**Every line earns its place.** Justify measures what AI-written code costs to keep. It finds code
that cannot justify its existence, works out from the git history whether an AI assistant or a
person wrote it, removes it only with proof, and reports the payoff as a number a team can track.

It answers one question with data: *does the AI assistant actually pay off?*

Real output, on the Bennett face-attendance system:

```
$ justify scan ./face-attendance --prove "<run the tests>"

Justify  ·  /Users/kartik/face-attendance
  Stage 1  24 Python files, 4,829 lines, each hashed
  Stage 3  1 AMBIGUOUS, 12 REMOVE, 6 SIMPLIFY
  Stage 6  11 of 12 removals proved by the tests · 1 not provable (the tests never load this file)

  KEEP     import     app.py:21          date          proof: not provable (the tests never load this file)
  REMOVE   import     core/teams.py:40   io            no use anywhere in the file, not re-exported
  REMOVE   function   core/clock.py:51   clock_time    no reference to 'clock_time' anywhere in the repository
  SIMPLIFY duplicate  serve.py:62        main          same body as main() in serve.py:32 — merge into one
  ...
  Justified Line Ratio  99.09%   ·   dead weight 44 lines (9.11 per 1,000)   ·   duplicate lines 58
```

`app.py:21` really is unused — but the tests never import `app.py`, so a passing run would prove
nothing. Justify says so instead of calling it proved.

## What it found on public AI-assisted repositories

Five public Python repositories whose history carries AI-assistant commit trailers — 3,247 files,
989,614 lines — scanned without proof (static stages + attribution). Numbers are per 1,000 lines
of the code each kind of commit wrote and that still survives.

| Repository | Lines | AI-assisted commits | Unused code / 1,000 (AI · human) | Duplicate code / 1,000 (AI · human) | Lines later rewritten (AI · human) |
|---|---:|---:|---|---|---|
| PrefectHQ/fastmcp | 250,117 | 434 of 4,041 | 0.00 · 0.12 | 1.67 · 3.23 | 36.1% · 39.9% |
| jmorrison-juniper/MistHelper | 606,946 | 815 of 2,024 | 0.00 · 0.20 | **7.59 · 0.99** | 16.7% · 40.3% |
| Azure/azure-functions-agents-runtime | 54,240 | 140 of 465 | 0.00 · 0.00 | 2.39 · 2.09 | 12.5% · 42.1% |
| MasterworkTools/openforge-catalog | 37,123 | 113 of 611 | 0.00 · 0.00 | 3.23 · 8.81 | 7.2% · 0.1% |
| judeper/FSI-CopilotGov | 41,188 | 217 of 530 | 0.45 · 0.58 | 0.56 · 0.00 | 5.7% · 0.0% |

What the data says, honestly:

- **Unused code is not where AI-assisted code costs.** In all five, AI-assisted lines carry no more
  unused imports or functions than human lines. Two repositories have none at all: their linters
  already remove them.
- **Duplication is.** AI-assisted code duplicates more in three of five — 7.7× the human rate in
  the largest repository.
- **There is no single answer.** The same assistant pays off in one repository and costs in
  another, which is why it has to be measured per repository rather than argued in general.

Caveats: authorship comes from commit trailers, so the AI share is a lower bound; rework counts
from the first AI-assisted commit, and newer code has had less time to be rewritten.

## Install

```bash
pip install -e ".[mcp]"        # Python 3.10+; the core has no dependencies
```

## The seven stages

| # | Stage | What it does | Uses a model |
|---|---|---|---|
| 1 | Ingest | Reads every Python file once and hashes it, so later runs skip what has not changed | no |
| 2 | Static facts | Parses syntax trees; builds who-uses-what from names, attributes, imports, parameters, code-like strings and config files | no |
| 3 | Candidates | Unused imports, functions, classes, dependencies; duplicate helpers. Anything a static graph can misjudge goes to AMBIGUOUS | no |
| 4 | Justify | One structured question per candidate; every reason must cite a file:line that is then checked | yes |
| 5 | Challenge | A second, independent call tries to prove the code IS needed | yes |
| 6 | Proof | Removes candidates in a temporary copy, checks every edit compiles, runs your tests; isolates the one removal that was needed | no |
| 7 | Report | One pull-request report with a reason on every line, a dashboard, and a ledger of every run | no |

## Commands

```bash
justify scan PATH                              # stages 1-3, attribution, metrics
justify scan PATH --prove "python -m pytest"   # + stage 6
justify scan PATH --judge                      # + stages 4-5 with the configured model
justify scan PATH --report pr.md --dashboard dash.html --json
justify history PATH                           # the ledger: JLR and dead weight over time
justify mcp                                    # run as an MCP server (also: justify-mcp)
```

## Use it from any AI assistant (MCP)

Justify is an MCP server, so the assistant that writes the code can check it before handing it
over. Tools: `scan_repository`, `prove_removals`, `payoff_report`, `history`.

**GitHub Copilot in VS Code** — `.vscode/mcp.json` (see `examples/mcp.vscode.json`):

```json
{ "servers": { "justify": { "type": "stdio", "command": "justify-mcp",
  "env": { "JUSTIFY_TEST_COMMAND": "python -m pytest -q" } } } }
```

**Claude Desktop** — `claude_desktop_config.json` (see `examples/claude_desktop_config.json`):

```json
{ "mcpServers": { "justify": { "command": "justify-mcp",
  "env": { "JUSTIFY_TEST_COMMAND": "python -m pytest -q" } } } }
```

**Claude Code**:

```bash
claude mcp add justify -e JUSTIFY_TEST_COMMAND="python -m pytest -q" -- justify-mcp
```

Cursor, Windsurf and other MCP clients take the same `command` + `env`. If `justify-mcp` is not on
the client's PATH, use its full path.

## Models (stages 4-5)

Chosen from the environment; keys are read from environment variables and never stored.

| Provider | Variables |
|---|---|
| Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` |
| Any OpenAI-compatible server (incl. local Ollama) | `JUSTIFY_LLM_BASE_URL`, `JUSTIFY_LLM_API_KEY`, `JUSTIFY_LLM_MODEL` |
| Claude Code CLI | installed and signed in once (`claude`, then `/login`); found on PATH or in `~/.local/bin` |

Force one with `JUSTIFY_LLM_PROVIDER=azure|openai|claude-cli`. With no model, stages 4-5 are
skipped and every undecided unit stays.

## Safety

- **The repository is only ever read.** Edits happen in a temporary copy, made on syntax trees. A
  file reached through a symlink is never written, so an edit cannot leak out of the copy.
- **A proof has to mean something.** The unchanged copy must pass first. That baseline run records
  which files the tests load: a removal in a file they never load is *not provable*, not proved.
  A run that passes with fewer tests passing, or more skipped, is a failure. Removals that each
  pass alone are run again together before any is certified.
- **Asked for proof, only proof counts.** With `--prove`, anything that did not pass stays.
- **The model never deletes.** It can veto a removal; it cannot force one. Invented evidence is discarded.
- **An assistant cannot spend your model credits.** Over MCP, `judge=true` is ignored unless the server's
  owner set `JUSTIFY_ALLOW_JUDGE=1`; the static verdicts come back either way.
- **The AI cannot choose what runs.** Over MCP the test command comes from `JUSTIFY_TEST_COMMAND`,
  set by a person in the configuration.
- **Tests cannot prove changes to themselves**, so helpers inside test code are never "proved".
- **Doubt means keep.** These go to AMBIGUOUS, not REMOVE:
  - imports: try/except imports, side-effect imports, re-exports, `__init__` and shim modules, the
    project's own modules, and files that read their names through `globals()` or `eval`;
  - classes a framework finds by type: `TestCase`, `Model`, `Command`, registry bases,
    `__subclasses__()` scans;
  - functions a tool calls by name: `pytest_*`, Alembic `upgrade()`, gunicorn hooks, Sphinx
    `setup()`, mkdocs hooks;
  - names looked up through `getattr` or `pkgutil`, and a library's public API.
- **Nothing merges automatically.** A person approves every removal.

Every one of these rules exists because a red-team pass built a repository where Justify would
otherwise have removed needed code; each has a regression test in `tests/`.

## Metrics

- **Justified Line Ratio (JLR)** = lines that are not dead weight ÷ all lines.
- **Dead weight per 1,000 lines**, split by **AI-assisted** and **human** authorship.
- **Rework** — of the lines each kind of commit added since the first AI-assisted commit, the share
  later rewritten or deleted (Round 1's measure, kept).
- A commit is AI-assisted when its message carries an assistant trailer (`Co-Authored-By: Claude`,
  `Co-authored-by: Copilot`, `Generated with Claude Code`, …). Assistants used without a trailer
  count as human, so the AI share is a lower bound. Add patterns with `JUSTIFY_AI_PATTERNS`.

## GitHub Action

```yaml
- uses: actions/checkout@v5
  with: { fetch-depth: 0 }        # history is needed for attribution
- uses: BPSKartik/justify@main
  with: { test-command: python -m pytest -q }
```

The report lands in the job summary. See `examples/justify-workflow.yml`.

## Limits

- Python only for now; the parser layer is built to take tree-sitter for other languages.
- Methods are not judged — they are called through objects in ways a static graph cannot see.
- Removing a dependency cannot be proved without a clean install, so dependencies are reported, not removed.
- Passing tests prove behaviour is unchanged, not that the code is better; weak tests mean weak proof.
- A program whose wrong output still exits 0 is only caught if its tests check that output.

---
Team Error 404 · Microsoft Innovate 2026 · Bennett University
