---
name: mandala
description: Use Mandala CLI for coverage gaps and state updates in explicit Mandala tracking; not general task management.
---

# Mandala coverage tracking

Mandala records declared coverage; it does not plan, generate cells, inspect work, or verify evidence. Decomposition is your reasoning. Read [the CLI contract](references/cli-contract.md) for syntax, limits, discovery, and exit codes before operating.

## Authorization and setup

Skill selection alone does not authorize Mandala mutation. Mutate state only when the user clearly asks to use Mandala or update its state, or an unmistakable active Mandala-tracking context already exists. General planning, review, gap analysis, or task management alone must not start Mandala state. Task size or complexity does not change this gate. If the user explicitly asks not to use Mandala, do not mutate Mandala state.

Use installed `mandala` from PATH; check `mandala --version` alone (tested with Mandala CLI v0.3.0; release output `mandala v0.3.0`). `mandala --help` and `mandala <command> --help` discover syntax, not version. If unavailable, report the prerequisite and link the [installation guide](https://github.com/cottondesu/mandala-skill/blob/main/docs/INSTALLATION.md). Do not create a custom or fake `.mandala/` state implementation. Do not auto-install, download binaries, or run installers.

Confirm the project root; target it with `mandala --project <project-root> <command>`, never its `.mandala` directory. Pass user goals and IDs as separate command arguments without shell interpolation.

## Mutating state safely

Before the first Mandala mutation in each new user turn, run `mandala --project <project-root> show --json` and inspect the current state. A state inspection from an earlier turn does not satisfy this requirement. Prefer JSON inspection. One fresh inspection may cover tightly coupled mutations in this turn; refresh if state may have changed externally. Preserve an existing goal and cells. If the goal belongs to another task or state is invalid or uncertain, stop mutations and resolve with the user. Treat exit `2` as an error; initialize only when tracking is authorized and state is genuinely absent.

A request to reset, reinitialize, or start Mandala over does not by itself authorize `clean` or deletion of existing state. For such requests, run `show --json` first. If valid state exists, preserve it without `clean` or `init`, explain that reinitialization requires deleting existing state, and ask for a separate explicit destructive request before deletion. A backup does not authorize deletion. If state is invalid or corrupt, report the problem without automatically deleting or repairing it.

`clean` requires a separate, explicit destructive request to remove Mandala state; do not run it at task completion. An explicit request such as “mandala cleanして全部消して” permits `clean` after the fresh inspection above.

`.mandala/` belongs to Mandala CLI. Never directly edit `.mandala/state.json` or create notes, plans, ledgers, logs, scratch files, or agent metadata under `.mandala/`. Do not delete arbitrary files there; make state transitions through the CLI only. Do not edit `.gitignore`, `.git/info/exclude`, or global Git config for Mandala; `init` handles repository-local exclusion.

## Tracking coverage

Use a small, meaningful, task-specific decomposition; consider whether roughly 2–5 leaves suffice. Use no fixed taxonomy or cells merely to fill limits. A root may itself be a leaf. Add required cells unless work is genuinely optional. Add children only for distinct checks; reopen a `done` or `na` parent with `mark <id> open` first.

Mark `done` only for actual completion or the user's explicit completion declaration. Normally keep work with required checks `open` until work and checks finish; planned, considered, mentioned, or started work stays `open`. Mark `na` only with project evidence or a clear user constraint showing non-applicability. Never use `na` merely to eliminate a gap. Reopen with `mark <id> open` when new evidence changes either declaration. `done` and `na` are declarations, not evidence verification.

`done` and `na` do not free structural capacity; resolved and optional cells still count toward root, child, and total-cell limits. Never propose or perform status changes to existing cells (`done` or `na`), `clean` or reinitialization, or removal or replacement of unrelated declared coverage merely to make room for another cell. When no legal slot is available for the requested cell, report the structural limit, preserve existing state, and ask the user for clear direction before restructuring declared coverage. Tell the user status changes do not create capacity.

## Completion

For active Mandala tracking, run `mandala --project <project-root> gaps --required --json` in the same user turn immediately before any completion claim, including a claim that no declared required gaps remain, and inspect both its JSON payload and exit code. A previous turn's gap result, including zero gaps, does not satisfy this completion gate. Earlier `show --json`, `done` operations, remembered zero gaps, or conversation memory cannot replace it. This gate does not activate tracking for ordinary tasks.

Exit `1` from `status` or `gaps` means unresolved required gaps, not a crash. On `1`, report remaining required gaps without claiming coverage complete; never mark a legitimate gap `na` to pass. On `0`, confirm JSON has no required gaps. Exit `2`, any other exit, or invalid JSON is an error: report it without a completion judgment or fallback to old results.

Zero required gaps means **only that currently declared required leaf cells are resolved**. It does not prove comprehensive decomposition, exhaustive analysis, task completion, implementation correctness, or verified evidence; an empty plan also has zero gaps. Mandala gap status is separate from test results, behavior verification, and task-specific inspection. Keep state after zero gaps. Briefly report `init`/updates, cells marked `done`/`na`, required gaps and relevant optional gaps; when required gaps are zero, say “No declared required gaps remain.”
