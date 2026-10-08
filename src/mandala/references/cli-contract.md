# Mandala CLI contract

Tested baseline: Mandala CLI v0.4.0
CLI check: `mandala --version`

This reference describes the production code and tests at tag `v0.4.0`, release commit `2fd15fb1ae32d0e13b6d9a1ed8107eb3f3a9deb9`. The Skill does not bundle the CLI.

## Invocation

```text
mandala [--project DIR] <command> [flags] [arguments]
mandala --version
mandala --project /path/to/project show --json
mandala --project /path/to/project status --json
```

`--project` is a global option **before** the command. It takes a project root, not `<root>/.mandala`. Command flags precede positional arguments, for example `mandala add --optional security.audit`. `mandala --help` and `mandala <command> --help` provide command syntax discovery.

`mandala --version` checks CLI availability and the installed build version. Use it alone, without `--project`, a command, or other arguments. A parsed `--version` combined with other arguments returns `E_USAGE` and exit `2`; root `--help` can short-circuit parsing before later flags (for example, `mandala --help --version` returns help with exit `0`). The version check does not inspect or change project state. A release installed with `go install github.com/cottondesu/mandala/cmd/mandala@v0.4.0` (Go 1.26 or later) prints `mandala v0.4.0` followed by a newline, with exit `0`. The version comes from Go module build metadata; a local development build may report `mandala (devel)` instead and does not confirm the released baseline. A build installed with `@latest` is not the pinned baseline either.

| Command | Syntax and result |
| --- | --- |
| `init` | `init <goal>` creates one active state at the target directory. |
| `add` | `add [--optional] <id>` adds an open cell. |
| `mark` | `mark <id> <open\|done\|na>` changes a leaf status. |
| `done` | `done <id>` is shorthand for `mark <id> done`. |
| `status` | `status [--json]` prints counts and required gap count; exit `1` means required gaps remain. |
| `gaps` | `gaps [--required] [--json]` lists open leaves. |
| `show` | `show [--json]` shows the goal and complete sparse cell tree. |
| `clean` | `clean` removes valid Mandala state only on explicit request. |

All commands are noninteractive. Successful mutations have no stdout. Errors go to stderr.

## Project and state lifecycle

State lives at `<project>/.mandala/state.json` with `schema_version: 1`. `init` uses the explicit `--project` directory or the current directory, refuses to overwrite state, and refuses a nested project below an existing `.mandala` boundary. For `add`, `mark`, `done`, `status`, `gaps`, and `show`, an explicit project directory is checked directly. Without `--project`, the CLI resolves symlinks and searches the physical current directory and its parents; the nearest `.mandala` boundary wins. A broken nearest boundary is an error, not a reason to fall back to an ancestor.

`clean` does **not** search parents. It acts only on the current directory or explicit `--project` directory. It removes a valid `state.json`, retains unrelated `.mandala` files, and removes `.mandala` only if empty afterward. A missing state is a successful no-op; an invalid state is retained. Completion or zero gaps never invokes `clean` automatically.

When `init` creates state within a Git working tree, the CLI ensures `.mandala/` is ignored via repository-local Git exclude when needed. It does not modify `.gitignore` or global Git configuration. The Skill must not edit either Git exclude or `.gitignore` itself. State files and auxiliary files under `.mandala/` are CLI-owned; the Skill uses CLI commands for transitions and creates no auxiliary files there.

## Cells and gaps

IDs are lowercase ASCII, one or two dot-separated segments (`root` or `root.child`). Each segment begins with `a`–`z`, followed by at most 63 lowercase letters, digits, or hyphens; each segment is at most 64 characters. There are at most eight root cells, eight children per root, 72 cells total, and two levels. The CLI cannot rename IDs.

`add <id>` creates a required cell. `add --optional <id>` creates an optional one. Children of an optional parent remain optional even without `--optional`; a required parent may have optional children. Adding the first child expands its parent, which then ceases to count as a leaf gap. A `done` or `na` parent must be reopened before a child can be added. Expanded parents cannot be marked `open`, `done`, or `na` while they have children.

Only open **leaf** cells are gaps. `gaps` includes optional open leaves by default, with `[optional]` in text. `gaps --required` filters optional leaves; optional gaps never cause exit `1`. `show --json` has `schema_version`, `goal`, and ID-sorted `cells` with `id`, `parent`, `status`, and `required`. `gaps --json` has `schema_version` and a `gaps` array of `id` and `required` entries. JSON is valid even when required gaps make `gaps` exit `1`; an empty result uses an empty array. JSON output schema versions (`show`, `gaps`, `status`) and the state schema version are distinct contracts, all `1` in this baseline.

| Exit | Meaning |
| --- | --- |
| `0` | Success; for `status` and `gaps`, no required gaps. |
| `1` | `status` or `gaps` found required gaps; this is a valid domain result. |
| `2` | Usage, project, state, or I/O error. |

Zero required gaps means only that the **currently declared required leaf cells are resolved**. It does not establish a comprehensive decomposition, task completion, or verified evidence. `done` and `na` are declarations, not evidence verification. An empty plan also has zero required gaps.

## Status JSON

`mandala --project <project-root> status --json` prints a read-only coverage summary. `--json` is a `status` command-local flag placed after the command; placed before `status`, it is an unknown global flag and returns `E_USAGE` with exit `2`. `--json=false` keeps the text output byte-for-byte identical to plain `status`. Output is compact single-line JSON with exactly one trailing newline, stable field names and field order, and zero-valued fields retained:

```json
{"schema_version":1,"goal":"Implement OAuth","cells":6,"groups":1,"required":{"open":2,"done":1,"na":0},"optional":{"open":1,"done":1,"na":0},"required_gaps":2}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer status output schema version, `1`; distinct from the state schema version. |
| `goal` | The state goal as a JSON string. Quotes, backslashes, and control characters are escaped, and `<`, `>`, `&` may appear as `\u003c`-style escapes; decode the JSON rather than matching raw bytes. |
| `cells` | All declared cells, including expanded parents. |
| `groups` | Expanded parents (cells with children). |
| `required`, `optional` | Objects with integer `open`, `done`, and `na` counts of **leaf** cells only. |
| `required_gaps` | Open required leaves; always equal to `required.open`. |

All counts are non-negative integers, and `cells` equals `groups` plus the six leaf counts. Exit codes match text `status`: `0` with no required gaps (an empty plan or optional-only gaps), `1` with required gaps remaining, `2` for usage, project, state, or I/O errors. Exit `1` still prints valid JSON; parse it as a domain result, not a crash. On usage, project, or state errors stdout is empty; a failing stdout writer can leave partial output, so do not assume every error has empty stdout. `status` never changes state; identical state yields byte-identical output.

Roles stay separate. `show --json` is the pre-mutation inspection of goal, cells, and per-cell status. `status --json` is an optional aggregate for progress reports. `gaps --required --json` lists open required leaf IDs and remains the completion gate. `status --json`, including `required_gaps: 0`, does not replace `show --json` before mutations or `gaps --required --json` before a completion claim, and it is not required in every turn.
