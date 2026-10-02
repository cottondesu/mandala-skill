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

Build, validation, and tests use the Python standard library and require no network. `make check` validates package integrity, required safety clauses in active instruction prose, the Agent Skills `name`/`description` metadata constraints, behavioral fixture metadata in `tests/evals/cases.json`, and activation-routing fixture metadata in `tests/evals/activation.json`. They do not run Codex or Claude Code or establish live agent behavior. Run the [manual behavioral evaluation](tests/README.md) before release. Do not edit `dist/` directly.

`make release-check` runs `make build`, `make check`, and `make test` in order, then local Git hygiene checks: `git diff --check`, no tracked generated or cache files, `dist/mandala/` ignored by the tracked `.gitignore`, and no stale split-distribution layout references. It allows uncommitted changes, does not run live agents, and does not publish a release.

## License

MIT. See [LICENSE](LICENSE).
