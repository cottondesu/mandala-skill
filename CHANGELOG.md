# Changelog

## v0.5.0 (2026-10-09)

- Move the tested CLI baseline from Mandala CLI v0.3.0 to **Mandala CLI v0.4.0** (release commit `2fd15fb1ae32d0e13b6d9a1ed8107eb3f3a9deb9`, Go 1.26 or later): `go install github.com/cottondesu/mandala/cmd/mandala@v0.4.0`, then `mandala --version` printing `mandala v0.4.0`. v0.3.0 remains the historical baseline of mandala-skill v0.1.0 through v0.4.1.
- Document `mandala --project <project-root> status --json` in the CLI contract: status output schema `1`, field meanings and order, count invariants, exit `0`/`1`/`2` (exit `1` still prints valid JSON), read-only and deterministic output, `--json` as a `status` command-local flag, and unchanged text `status` with `--json=false`.
- Keep roles separate: `show --json` remains the pre-mutation inspection and `gaps --required --json` remains the completion gate; `status --json` is an optional progress summary and never replaces either. The canonical Skill changes only its baseline version and stays within the 6214-byte budget; all 23 safety contracts are unchanged.
- Validate the new baseline: any other versioned CLI baseline token (including `cmd/mandala@latest`) in the Skill package, READMEs, installation guides, and evaluation guides is rejected, the `status --json` contract clauses are required, and global `--json` placement and common phrasings that offer `status --json` as a completion-gate substitute are rejected (a wording tripwire, not a semantic proof).
- Add `scripts/check_cli_compat.py` (`make cli-compat`), an explicit real-CLI compatibility check (`C01`–`C14`) that uses an installed CLI in disposable projects, never installs it, and reports a missing CLI or another version as `NOT_RUN`. It is not part of `make release-check` or CI.
- Update the live-evaluation preflight to require `mandala v0.4.0`; the 12 release cases, 3 coverage profiles, graders, adapters, and artifact schemas are otherwise unchanged.

## v0.4.1 (2026-10-08)

- Improve Go binary `PATH` troubleshooting in the English and Japanese installation guides: locate the binary through `GOBIN` or the first `GOPATH` entry, check `go env` exit status, verify absolute paths in Windows PowerShell, and explain `PATH` differences for agents started from desktop apps or IDEs. The CLI is still never installed automatically.

## v0.4.0 (2026-10-08)

- Add three curated real-world coverage profiles (`tracked-design-review`, `capacity-constrained-expansion`, `reset-recovery`) in `tests/evals/profiles.json`, validated by `make check` against the current release suite.
- Add `scripts/eval_profile.py`, an offline post-hoc profile view over an existing live run or replay output; it runs no agent or Mandala CLI and never modifies its source.
- Define explicit CORE / CONDITIONAL / NOT_APPLICABLE semantics, derive contract scope from current fixture declarations, and keep CORE and CONDITIONAL evidence separate; profiles report no pass rates, percentages, or scores.
- Harden default coverage-report output naming so untrusted agent labels cannot influence directory paths.
- Keep the canonical Skill, CLI contract, behavioral and activation fixtures, live suites, graders, adapters, replay, sanitizer, and existing artifact schemas unchanged.

## v0.3.0 (2026-10-08)

- Harden capacity-limit responses: the Skill now says to tell the user that status changes do not create capacity, without increasing the canonical Skill size (a 6214-byte UTF-8 budget is now validated).
- Add four boundary live scenarios using existing fixtures: generic gap analysis (A1), explicit non-use (A2), corrupt state on reset (R3), and an agent-side missing CLI (P1).
- Require the release suite's declared fixture scope to cover all 23 safety contracts; this is declared scope, not automated verification of every contract.

## v0.2.1 (2026-10-07)

- Add offline replay/re-grade for recorded live-evaluation evidence, including compatible reconstruction of v0.2.0 snapshots.
- Add detailed safety-contract coverage reports that distinguish declared scope, observed automated evidence, unobservable checks, and manual-review requirements.
- Add privacy-reduced share bundles and a sanitized real-world release/version-contract tracking example.

## v0.2.0 (2026-10-07)

- Assign stable IDs to the 23 safety contracts and use them throughout behavioral fixture coverage.
- Add a local cross-agent live-evaluation harness for the focused freshness/reset/completion and capacity scenarios.
- Capture normalized structured traces, deterministic state/process checks, and machine-readable evaluation reports without adding live-agent execution to CI.

## v0.1.1 (2026-10-02)

- Validate the canonical package against the Agent Skills name and description metadata constraints used by this repository.
- Add bilingual activation-routing fixtures for explicit Mandala use and adjacent non-Mandala requests.
- Add a deterministic local `make release-check` for build, validation, tests, and repository hygiene.

## v0.1.0 (2026-10-01)

- Add the canonical Skill source package for manual-copy installation with Codex and Claude Code.
- Document Mandala CLI v0.3.0 as the tested baseline, use `mandala --version` for availability/version checks, and make CLI-owned state boundaries explicit.
- Add a deterministic source-to-distribution build, package validation, tests, and behavioral eval fixtures.
