# Mandala CLI contract

Tested baseline: Mandala CLI v0.3.0
CLI check: `mandala --version`

This reference describes the production code and tests at tag `v0.3.0`, release commit `27d7a05f6111e74bd0bb5f4f813c9f3529745956`. The Skill does not bundle the CLI.

## Invocation

```text
mandala [--project DIR] <command> [flags] [arguments]
mandala --version
mandala --project /path/to/project show --json
```

`--project` is a global option **before** the command. It takes a project root, not `<root>/.mandala`. Command flags precede positional arguments, for example `mandala add --optional security.audit`. `mandala --help` and `mandala <command> --help` provide command syntax discovery.

`mandala --version` checks CLI availability and the installed build version. Use it alone, without `--project`, a command, or other arguments. A parsed `--version` combined with other arguments returns `E_USAGE` and exit `2`; root `--help` can short-circuit parsing before later flags (for example, `mandala --help --version` returns help with exit `0`). The version check does not inspect or change project state. A release installed with `go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0` prints `mandala v0.3.0` followed by a newline, with exit `0`. The version comes from Go module build metadata; a local development build may report `mandala (devel)` instead and does not confirm the released baseline.

| Command | Syntax and result |
| --- | --- |
| `init` | `init <goal>` creates one active state at the target directory. |
| `add` | `add [--optional] <id>` adds an open cell. |
| `mark` | `mark <id> <open\|done\|na>` changes a leaf status. |
| `done` | `done <id>` is shorthand for `mark <id> done`. |
| `status` | `status` prints counts and required gap count; exit `1` means required gaps remain. |
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

Only open **leaf** cells are gaps. `gaps` includes optional open leaves by default, with `[optional]` in text. `gaps --required` filters optional leaves; optional gaps never cause exit `1`. `show --json` has `schema_version`, `goal`, and ID-sorted `cells` with `id`, `parent`, `status`, and `required`. `gaps --json` has `schema_version` and a `gaps` array of `id` and `required` entries. JSON is valid even when required gaps make `gaps` exit `1`; an empty result uses an empty array. The JSON output schema version and state schema version are distinct contracts, both `1` in this baseline.

| Exit | Meaning |
| --- | --- |
| `0` | Success; for `status` and `gaps`, no required gaps. |
| `1` | `status` or `gaps` found required gaps; this is a valid domain result. |
| `2` | Usage, project, state, or I/O error. |

Zero required gaps means only that the **currently declared required leaf cells are resolved**. It does not establish a comprehensive decomposition, task completion, or verified evidence. `done` and `na` are declarations, not evidence verification. An empty plan also has zero required gaps.
