# Changelog

## v0.3.0 (unreleased)

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
