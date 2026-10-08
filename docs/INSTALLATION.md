# Installation

This repository distributes Agent Skill instructions by manual copy. `src/mandala/` is the canonical, complete package tracked in Git; inspect it before installing. Copy the complete `mandala` directory, including `SKILL.md` and `references/cli-contract.md`. No build or Python is needed for this installation. `dist/mandala/` is the single Git-ignored generated package; maintainers may optionally run `make build` before using a generated package for validation or future release packaging. Keep project-specific goals and notes outside the installed Skill package; Mandala project state is managed by the CLI.

## Install the CLI

Mandala CLI is a separate prerequisite and is not downloaded by the Skill. With Go installed, the primary installation command is:

```sh
go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0
mandala --version
```

Expected version output: `mandala v0.3.0` (exit `0`). Run `mandala --version` alone, without a project option or command. This checks CLI availability and build version; use `mandala --help` or `mandala <command> --help` for syntax discovery.

The Skill baseline is Mandala CLI v0.3.0. A newer release installed with `@latest` may not have been validated against this Skill. Ensure your Go binary directory is on `PATH` (see [If `mandala` is not found](#if-mandala-is-not-found)). Consult the [Mandala CLI repository](https://github.com/cottondesu/mandala) for its Go requirement and CLI setup. A local development build may report `mandala (devel)` instead; that does not confirm the released baseline.

### If `mandala` is not found

If `mandala --version` reports `command not found` (zsh: `command not found: mandala`) right after `go install` succeeded, the CLI is often installed but its directory is not on `PATH`. `PATH` is the list of directories your shell searches for commands; `go install` does not change it. Locate the binary first instead of running `go install` again.

Go installs commands into:

1. `GOBIN`, if it is set;
2. otherwise the `bin` directory under the **first** entry of `GOPATH`. `GOPATH` entries are separated by `:` on macOS/Linux and `;` on Windows. When `GOPATH` is not set, Go uses `$HOME/go` (`%USERPROFILE%\go` on Windows).

`~/go/bin` is therefore only the default; custom `GOBIN` or `GOPATH` settings put the binary elsewhere.

#### Find the CLI on macOS and Linux

This read-only check works in zsh and bash. It does not change `PATH`, Go settings, or Mandala state:

```sh
GO_BIN=""
if ! command -v go >/dev/null 2>&1; then
  printf 'Go is not on PATH. Install or configure Go first.\n'
elif ! GO_BIN="$(go env GOBIN)"; then
  printf 'Error: go env GOBIN failed. Fix the Go setup first.\n'
  GO_BIN=""
elif [ -z "$GO_BIN" ] && ! GO_PATH="$(go env GOPATH)"; then
  printf 'Error: go env GOPATH failed. Fix the Go setup first.\n'
else
  if [ -z "$GO_BIN" ]; then
    GO_BIN="${GO_PATH%%:*}"
    [ -n "$GO_BIN" ] && GO_BIN="$GO_BIN/bin"
  fi
  case "$GO_BIN" in
    /*) printf 'Go binary directory: %s\n' "$GO_BIN" ;;
    *) printf 'Could not determine an absolute Go binary directory: "%s"\n' "$GO_BIN"; GO_BIN="" ;;
  esac
fi
if [ -n "$GO_BIN" ]; then
  if [ ! -e "$GO_BIN/mandala" ]; then
    printf 'No mandala in %s. The CLI is not installed there.\n' "$GO_BIN"
  elif [ ! -f "$GO_BIN/mandala" ] || [ ! -x "$GO_BIN/mandala" ]; then
    printf '%s exists but is not an executable file.\n' "$GO_BIN/mandala"
  else
    "$GO_BIN/mandala" --version
  fi
fi
```

How to read the result:

- `mandala v0.3.0`: the CLI is installed; only `PATH` is missing. Continue below.
- `No mandala in ...`: the CLI is not in Go's binary directory. Run the pinned `go install` command above (check its output for errors).
- `exists but is not an executable file`: check the file, its permissions, and that it was built for this OS and CPU. Reinstalling with the pinned command is usually simpler than editing permissions.
- Another version, or `mandala (devel)`: this binary is not the tested v0.3.0 baseline. Reinstall with the pinned command if you want the baseline.
- Go is not on `PATH`: install Go (or fix Go's own `PATH` setup) first.
- `Error: go env ... failed`: Go could not report its settings, so no directory is guessed and the CLI is not run. Run `go env GOBIN` or `go env GOPATH` to see Go's error and fix the Go setup first.

Running the binary by absolute path confirms only that this file runs and which version it reports. It does not verify where the binary came from or that it is authentic.

**Current shell only.** If the absolute-path check printed `mandala v0.3.0`, add the directory to `PATH` for this terminal session, in the same shell where `GO_BIN` was set:

```sh
export PATH="$GO_BIN:$PATH"
command -v mandala
mandala --version
```

`command -v mandala` should print `$GO_BIN/mandala`; if it prints another path, a different `mandala` is being used. This change is lost when the terminal closes.

**Persistent setting.** Open your shell startup file in an editor and add one line. For zsh (the macOS default), use `~/.zshrc`. With the default Go layout the line is:

```sh
export PATH="$HOME/go/bin:$PATH"
```

If you use a custom `GOBIN` or `GOPATH`, put the directory printed as `Go binary directory:` in place of `$HOME/go/bin`, keeping the double quotes. For bash, add the same line to the startup file your bash reads, commonly `~/.bashrc` on Linux or `~/.bash_profile` on macOS; which file is read depends on the OS and on how the shell starts. Open a new terminal afterwards and run `command -v mandala` and `mandala --version` again.

#### Find the CLI in Windows PowerShell

This read-only check does not change `PATH`, Go settings, or Mandala state:

```powershell
$GoBin = $null
$GoFailed = $true
if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    Write-Host 'Go is not on PATH. Install or configure Go first.'
} else {
    $GoBin = [string](go env GOBIN)
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Error: go env GOBIN failed. Fix the Go setup first.'
    } elseif ($GoBin) {
        $GoFailed = $false
    } else {
        $GoPath = [string](go env GOPATH)
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Error: go env GOPATH failed. Fix the Go setup first.'
        } else {
            $GoFailed = $false
            $GoBin = ($GoPath -split ';')[0]
            if ($GoBin) { $GoBin = $GoBin.TrimEnd('\', '/') + '\bin' }
        }
    }
}
$Qualified = '^(?:[A-Za-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+)'
$Invalid = '[<>"|?*\x00-\x1F]|^.{2,}:'
if ($GoFailed) {
    $GoBin = $null
} elseif ($GoBin -match $Qualified -and $GoBin -notmatch $Invalid) {
    Write-Host "Go binary directory: $GoBin"
} else {
    Write-Host "Could not determine a fully qualified Go binary directory: '$GoBin'"
    $GoBin = $null
}
if ($GoBin) {
    $Mandala = $GoBin.TrimEnd('\', '/') + '\mandala.exe'
    if (Test-Path -LiteralPath $Mandala -PathType Leaf) {
        & $Mandala --version
    } else {
        Write-Host "No mandala.exe in $GoBin. The CLI is not installed there."
    }
}
```

The check accepts only fully qualified paths: a drive path such as `C:\Users\name\go\bin` or a UNC path such as `\\server\share\go\bin`. It rejects an empty value, drive-relative paths (`C:go\bin`), root-relative paths (`\go\bin`), relative paths, and paths with characters Windows does not allow; `\\?\` device paths are not accepted. It builds paths as plain strings, so a missing drive or share is reported as not found. It is written for both Windows PowerShell 5.1 and PowerShell 7.

Read the result as on macOS/Linux. `&` runs the program at that path, including paths with spaces. If it printed `mandala v0.3.0`, add the directory for the current PowerShell session only:

```powershell
$env:Path = "$GoBin;$env:Path"
(Get-Command mandala).Source
mandala --version
```

The first command should print the same `mandala.exe` path. To make the change persistent, add the directory yourself through Windows **Settings** (search for "Edit environment variables for your account", then edit `Path` and add a new entry), then open a new terminal. Avoid commands such as `setx PATH ...`, which overwrite or truncate the existing user `PATH`.

#### Agents started from desktop apps or IDEs

The Skill runs `mandala` from the agent's `PATH`; it does not search Go directories for the CLI. Codex or Claude Code started from a desktop app, an IDE, or another GUI application may not inherit your terminal's `PATH`, so `mandala --version` can succeed in a terminal while the agent reports it missing. Editing `~/.zshrc` does not necessarily fix GUI apps. After changing shell settings, fully restart the application, then ask the agent to run `command -v mandala` (or `Get-Command mandala` in PowerShell) and `mandala --version` to see what its environment finds. If it still fails, start the app from a terminal where `mandala` works, or add the Go binary directory to `PATH` using that app's or OS's own environment settings.

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

The CLI prerequisite is the same in PowerShell: `go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0`, then `mandala --version` after Go's binary directory is on `PATH`. Expect `mandala v0.3.0`. If `mandala` is not found, see [Find the CLI in Windows PowerShell](#find-the-cli-in-windows-powershell).

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

- `mandala: command not found`: “not found” does not mean “not installed”. Locate Go's binary directory and check `PATH` as in [If `mandala` is not found](#if-mandala-is-not-found) before reinstalling. The Skill does not install the CLI.
- `go env GOBIN` is empty: Go uses the `bin` directory under the first `GOPATH` entry.
- A custom `GOBIN` is set: add that directory, not `~/go/bin`, to `PATH`.
- `mandala` exists but does not run: check that it is a regular file, that it is executable, and that it was built for this OS and CPU.
- `mandala --version` reports a version other than `mandala v0.3.0`: that binary is not the tested baseline; reinstall with the pinned command if you need the baseline.
- `mandala` works in a terminal but not in the agent: the agent's environment has a different `PATH`; see [Agents started from desktop apps or IDEs](#agents-started-from-desktop-apps-or-ides).
- `go` itself is not found: install and configure Go first, following the [Mandala CLI repository](https://github.com/cottondesu/mandala).
- The Skill is not discovered: confirm the destination spelling and both package files, then start a new session.
- An existing Mandala project conflicts with the requested goal: inspect its state first; do not reset it through `clean` or direct file edits.
- `mandala gaps` exits `1`: required gaps remain. This is an expected result, not a CLI crash. Exit `2` signals an error.
