# Installation

This repository distributes Agent Skill instructions by manual copy. `src/mandala/` is the canonical, complete package tracked in Git; inspect it before installing. Copy the complete `mandala` directory, including `SKILL.md` and `references/cli-contract.md`. No build or Python is needed for this installation. `dist/mandala/` is the single Git-ignored generated package; maintainers may optionally run `make build` before using a generated package for validation or future release packaging. Keep project-specific goals and notes outside the installed Skill package; Mandala project state is managed by the CLI.

## Install the CLI

Mandala CLI is a separate prerequisite and is not downloaded by the Skill. With Go installed, the primary installation command is:

```sh
go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0
mandala --version
```

Expected version output: `mandala v0.3.0` (exit `0`). Run `mandala --version` alone, without a project option or command. This checks CLI availability and build version; use `mandala --help` or `mandala <command> --help` for syntax discovery.

The Skill baseline is Mandala CLI v0.3.0. A newer release installed with `@latest` may not have been validated against this Skill. Ensure your Go binary directory is on `PATH`. Consult the [Mandala CLI repository](https://github.com/cottondesu/mandala) for its Go requirement and CLI setup. A local development build may report `mandala (devel)` instead; that does not confirm the released baseline.

## macOS and Linux

Clone the Skill repository once:

```sh
git clone https://github.com/cottondesu/mandala-skill.git
cd mandala-skill
```

Codex personal:

```sh
mkdir -p ~/.codex/skills
cp -R src/mandala ~/.codex/skills/
```

Codex project-local, from the Skill repository (`PROJECT_ROOT` is your project directory):

```sh
mkdir -p "$PROJECT_ROOT/.codex/skills"
cp -R src/mandala "$PROJECT_ROOT/.codex/skills/"
```

Claude Code personal:

```sh
mkdir -p ~/.claude/skills
cp -R src/mandala ~/.claude/skills/
```

Claude Code project-local:

```sh
mkdir -p "$PROJECT_ROOT/.claude/skills"
cp -R src/mandala "$PROJECT_ROOT/.claude/skills/"
```

Set `PROJECT_ROOT` to the target repository's root before running a project-local command. For example, `PROJECT_ROOT=/path/to/repo`. Each destination must contain both `SKILL.md` and `references/cli-contract.md`.

## Windows PowerShell

Clone and enter the Skill repository:

```powershell
git clone https://github.com/cottondesu/mandala-skill.git
Set-Location mandala-skill
```

Codex personal:

```powershell
New-Item -ItemType Directory -Force "$HOME/.codex/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$HOME/.codex/skills/"
```

Codex project-local (set `$ProjectRoot` to your project root):

```powershell
$ProjectRoot = "C:\path\to\repo"
New-Item -ItemType Directory -Force "$ProjectRoot/.codex/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$ProjectRoot/.codex/skills/"
```

Claude Code personal:

```powershell
New-Item -ItemType Directory -Force "$HOME/.claude/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$HOME/.claude/skills/"
```

Claude Code project-local:

```powershell
New-Item -ItemType Directory -Force "$ProjectRoot/.claude/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$ProjectRoot/.claude/skills/"
```

The CLI prerequisite is the same in PowerShell: `go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0`, then `mandala --version` after Go's binary directory is on `PATH`. Expect `mandala v0.3.0`.

## Verify

Start a **new agent session** after installation or update so the agent discovers the new instructions. Check both package files at the chosen destination. In the target project, use an explicit prompt such as: “Use the mandala skill to track coverage for this task.” The agent should check CLI availability, read existing state, and create or update state only if your request calls for it.

## Update or remove

To update, fetch a new copy of this repository, review it, then replace the entire installed `mandala` directory with the canonical `src/mandala/` directory. On macOS/Linux, from the Skill repository, for example:

```sh
rm -rf ~/.codex/skills/mandala
cp -R src/mandala ~/.codex/skills/
```

Use the analogous destination for another agent or project. In PowerShell, for example:

```powershell
Remove-Item -Recurse -Force "$HOME/.codex/skills/mandala"
Copy-Item -Recurse "src/mandala" "$HOME/.codex/skills/"
```

To remove the Skill, delete only its installed `mandala` directory. These operations do not remove Mandala CLI or any project's `.mandala/` state. Start a new session after updating or removing the Skill.

## Troubleshooting

- `mandala` is unavailable: install the CLI separately, check `PATH`, and run `mandala --version`. The Skill does not install it.
- The Skill is not discovered: confirm the destination spelling and both package files, then start a new session.
- An existing Mandala project conflicts with the requested goal: inspect its state first; do not reset it through `clean` or direct file edits.
- `mandala gaps` exits `1`: required gaps remain. This is an expected result, not a CLI crash. Exit `2` signals an error.
