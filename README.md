# mandala-skill

An Agent Skill for using the separate [Mandala CLI](https://github.com/cottondesu/mandala) to track **declared goal coverage gaps**. The Skill teaches Codex and Claude Code when to read or update Mandala state and how to interpret the results. It does not contain the CLI, generate a plan, or verify that work is complete.

This Skill is distributed by manual copy. Review the Skill instructions before installing them: an Agent Skill is instructions to an agent. The installed Skill package itself needs no network access or credentials and has no telemetry. Your agent environment may have its own capabilities.

## Prerequisites and compatibility

Install Mandala CLI separately and make `mandala` available on `PATH`. Check availability and version with `mandala --version`; the tested release reports `mandala v0.3.0`. The tested baseline for this Skill is **Mandala CLI v0.3.0**; compatibility with other releases is not yet declared. [CLI installation](docs/INSTALLATION.md#install-the-cli) includes the pinned command `go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0`. Use `mandala --help` or `mandala <command> --help` for command syntax discovery.

## Supported agents and installation

Copy the **whole canonical package directory** `src/mandala/`, including `references/cli-contract.md`, into one of these locations:

| Agent | Personal | Project-local |
| --- | --- | --- |
| Codex | `~/.codex/skills/mandala/` | `<repo>/.codex/skills/mandala/` |
| Claude Code | `~/.claude/skills/mandala/` | `<repo>/.claude/skills/mandala/` |

For example, on macOS or Linux:

```sh
git clone https://github.com/cottondesu/mandala-skill.git
cd mandala-skill
mkdir -p ~/.codex/skills
cp -R src/mandala ~/.codex/skills/
```

See the [installation guide](docs/INSTALLATION.md) for all four destinations, Windows PowerShell, updates, removal, and troubleshooting. Start a new agent session after installing or updating.

## Quick use and safety

Try an explicit request such as: “Use the mandala skill to track coverage for this task.” The agent checks the CLI and current state before writing. Skill auto-selection alone does not authorize state creation or changes. Mandala records a goal, facets, and required or optional leaf cells; `status` and `gaps` report unresolved declared required leaves.

`.mandala/` belongs to the CLI. The Skill instructs agents never to edit `state.json`, add notes or ledgers there, change `.gitignore` or Git exclude directly, or automatically run `clean`. Zero required gaps means the **currently declared** required leaves are resolved. It is not proof of comprehensive coverage or verified completion.

## Development

`src/mandala/` is the canonical, Git-tracked Skill package and can be installed directly. `scripts/` contains maintainer tooling; `tests/` contains validation and evaluation material. `dist/mandala/` is the single Git-ignored generated package for local package validation and future release packaging. Edit the source, then run these commands in order, including after a fresh checkout:

```sh
make build
make check
make test
make release-check
```

`make build` creates a byte-equivalent package at `dist/mandala/` for both Codex and Claude Code; `make check` and `make test` require that output. Source-copy installation needs neither this build nor Python.

Build, validation, and tests use the Python standard library and require no network. `make check` validates package integrity, the 23 safety contracts in active instruction prose (stable IDs such as `STATE-001` in `tests/evals/contracts.json`), the Agent Skills `name`/`description` metadata constraints, a 6214-byte UTF-8 size budget for the canonical `SKILL.md` (a deterministic context-size proxy, not a tokenizer-specific token budget), behavioral fixture metadata in `tests/evals/cases.json`, and activation-routing fixture metadata in `tests/evals/activation.json`, the live-suite manifest in `tests/evals/live_suites.json`, and the real-world coverage profile catalog in `tests/evals/profiles.json`. They do not run Codex or Claude Code or establish live agent behavior. Run the [manual behavioral evaluation](tests/README.md) before release. Do not edit `dist/` directly.

`make release-check` runs `make build`, `make check`, and `make test` in order, then local Git hygiene checks: `git diff --check`, no tracked generated or cache files, `dist/mandala/` ignored by the tracked `.gitignore`, and no stale split-distribution layout references. It allows uncommitted changes, does not run live agents, and does not publish a release.

The local live-agent harness runs twelve high-risk scenarios (suites `focused`, `capacity`, `boundaries`, and `release` for all twelve) against Codex or Claude Code in disposable projects and checks command traces and Mandala state. The release suite's declared fixture scope covers all 23 safety contracts; that is declared scope, not proof that each contract was automatically verified. It runs only when invoked, never in CI, and needs the agent CLI's own login. Automated passes do not replace manual response review. See the [live evaluation guide](tests/README.md#live-agent-evaluation-harness).

```sh
make eval-live AGENT=codex SUITE=release
python3 scripts/eval_live.py --agent claude --preflight
python3 scripts/eval_live.py --agent claude --case R1
```

Recorded runs can be inspected offline without an agent, Mandala CLI, or network. Replay re-grades recorded evidence with the current deterministic graders (it never reruns an agent or Mandala CLI and never re-grades final prose). The coverage report separates declared fixture scope from observed automated evidence and manual-review requirements. A coverage profile (`tracked-design-review`, `capacity-constrained-expansion`, or `reset-recovery`) is a post-hoc view that classifies the release cases as CORE, CONDITIONAL, or NOT_APPLICABLE for one practical workflow, derives contract scope from current fixture declarations, and shows CORE and CONDITIONAL evidence separately; it runs no agent, does not replace the release suite, and is not a pass rate or score. The sanitizer writes a whitelist-based, privacy-reduced share bundle; it is not a secret scanner, its output must be reviewed before sharing, and it cannot be replayed. See [offline evaluation tools](tests/README.md#offline-evaluation-tools).

```sh
python3 scripts/eval_replay.py .eval-live/<run>/<agent> --case B5
python3 scripts/eval_coverage.py .eval-live/<run>/<agent>
python3 scripts/eval_profile.py --list-profiles
python3 scripts/eval_profile.py .eval-live/<run>/<agent> --profile tracked-design-review
python3 scripts/eval_sanitize.py \
  .eval-live/<run>/<agent> \
  --output-dir .eval-live/exports/example
```

## License

MIT. See [LICENSE](LICENSE).
