# インストール

このリポジトリは Agent Skill の指示を手動コピーで配布します。`src/mandala/` は Git 管理する正本の完全な package です。導入前に確認し、`SKILL.md` と `references/cli-contract.md` を含む `mandala` ディレクトリ全体をコピーします。この導入に build や Python は不要です。`dist/mandala/` は Git ignore する単一の生成 package で、maintainer は必要に応じて `make build` 後に package 検証や将来の release packaging に使えます。プロジェクト固有の目標やメモをインストール済み Skill 内に書かないでください。Mandala の project state は CLI が管理します。

## CLI のインストール

Mandala CLI は別途必要です。Skill は CLI をダウンロードしません。Go を導入済みの場合、基本のコマンドは次のとおりです。

```sh
go install github.com/cottondesu/mandala/cmd/mandala@v0.4.0
mandala --version
```

期待する表示は `mandala v0.4.0`（exit `0`）です。`mandala --version` は project option や command と組み合わせず単独で実行します。実行可否と build version の確認に使い、構文の確認には `mandala --help` または `mandala <command> --help` を使います。

この Skill の検証済み基準は Mandala CLI v0.4.0 で、build には Go 1.26 以上が必要です。`@latest` で導入される新しい版は、この Skill との互換性が未検証の場合があります。Go の binary directory を `PATH` に含めます（[`mandala` が見つからない場合](#mandala-が見つからない場合)を参照）。Go 要件や CLI の説明は [Mandala CLI リポジトリ](https://github.com/cottondesu/mandala)を確認してください。ローカルの開発 build では `mandala (devel)` と表示される場合があり、それでは release の基準を確認できません。

### `mandala` が見つからない場合

`go install` が成功した直後に `mandala --version` が `command not found`（zsh では `command not found: mandala`）になる場合、CLI は導入済みで、その directory が `PATH` に含まれていないだけのことがよくあります。`PATH` はシェルがコマンドを探す directory の一覧で、`go install` はこれを変更しません。`go install` を繰り返す前に、まず実行ファイルの場所を確認してください。

Go はコマンドを次の場所に導入します。

1. `GOBIN` が設定されている場合は `GOBIN`
2. それ以外の場合は `GOPATH` の**先頭**要素の下の `bin`。`GOPATH` の区切り文字は macOS / Linux では `:`、Windows では `;` です。`GOPATH` が未設定の場合、Go は `$HOME/go`（Windows では `%USERPROFILE%\go`）を使います。

そのため `~/go/bin` は既定値にすぎず、`GOBIN` や `GOPATH` を変更している環境では別の場所になります。

#### macOS と Linux で CLI を探す

次の確認は zsh と bash で動作する読み取り専用の手順です。`PATH`、Go の設定、Mandala state は変更しません。

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

結果の見方:

- `mandala v0.4.0`: CLI は導入済みで、`PATH` だけが不足しています。下の手順に進みます。
- `No mandala in ...`: Go の binary directory に CLI がありません。上の固定コマンドで `go install` を実行し、出力にエラーがないか確認します。
- `exists but is not an executable file`: file、権限、この OS / CPU 向けの build かを確認します。権限を編集するより固定コマンドで再導入する方が簡単な場合が多いです。
- 別の version、または `mandala (devel)`: 検証済みの v0.4.0 基準ではありません。基準が必要な場合は固定コマンドで再導入します。
- Go が `PATH` にない: 先に Go を導入するか、Go 自体の `PATH` 設定を直します。
- `Error: go env ... failed`: Go が設定を取得できなかったため、directory を推測せず、CLI も実行しません。`go env GOBIN` または `go env GOPATH` を実行して Go のエラーを確認し、先に Go の設定を直します。

絶対パスで起動できることは、その file が実行でき、表示された version を報告したことしか示しません。binary の入手元や真正性は確認できません。

**現在のシェルのみ。** 絶対パスでの確認で `mandala v0.4.0` と表示された場合だけ、`GO_BIN` を設定したのと同じシェルで、この terminal session の `PATH` に directory を追加します。

```sh
export PATH="$GO_BIN:$PATH"
command -v mandala
mandala --version
```

`command -v mandala` は `$GO_BIN/mandala` を表示するはずです。別のパスが表示された場合は、別の `mandala` が使われています。この設定は terminal を閉じると失われます。

**永続設定。** シェルの startup file をエディタで開き、1 行追加します。zsh（macOS の既定）では `~/.zshrc` を使います。Go の既定配置の場合は次の行です。

```sh
export PATH="$HOME/go/bin:$PATH"
```

`GOBIN` や `GOPATH` を変更している場合は、`$HOME/go/bin` の代わりに `Go binary directory:` として表示された directory を、二重引用符を残したまま記述します。bash の場合は、使用中の bash が読む startup file（Linux では `~/.bashrc`、macOS では `~/.bash_profile` が一般的）に同じ行を追加します。どの file が読まれるかは OS やシェルの起動方法によって異なります。追加後は新しい terminal を開き、`command -v mandala` と `mandala --version` を再度実行します。

#### Windows PowerShell で CLI を探す

次の確認は読み取り専用で、`PATH`、Go の設定、Mandala state は変更しません。

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

この確認は完全修飾パスだけを受け付けます。`C:\Users\name\go\bin` のような drive パスと、`\\server\share\go\bin` のような UNC パスです。空の値、drive 相対パス（`C:go\bin`）、root 相対パス（`\go\bin`）、相対パス、Windows で使えない文字を含むパスは拒否します。`\\?\` 形式の device パスも受け付けません。パスは文字列として組み立てるため、存在しない drive や share は「見つからない」と報告されます。Windows PowerShell 5.1 と PowerShell 7 の両方を想定した書き方です。

結果は macOS / Linux と同じように読みます。`&` は指定したパスのプログラムを実行し、空白を含むパスにも対応します。`mandala v0.4.0` と表示された場合は、現在の PowerShell session だけに directory を追加します。

```powershell
$env:Path = "$GoBin;$env:Path"
(Get-Command mandala).Source
mandala --version
```

最初のコマンドは同じ `mandala.exe` のパスを表示するはずです。永続化する場合は、Windows の**設定**で「アカウントの環境変数を編集」を検索し、`Path` を編集して新しい項目として directory を自分で追加した後、新しい terminal を開きます。既存のユーザー `PATH` を上書き・切り詰める可能性がある `setx PATH ...` のようなコマンドは使わないでください。

#### デスクトップアプリや IDE から起動したエージェント

Skill はエージェントの `PATH` 上の `mandala` を実行します。Go の directory を自動で探す機能はありません。デスクトップアプリ、IDE、その他の GUI アプリケーションから起動した Codex や Claude Code は terminal の `PATH` を引き継がない場合があるため、terminal では `mandala --version` が成功しても、エージェントでは見つからないことがあります。`~/.zshrc` を編集しても GUI アプリで解決するとは限りません。シェル設定を変更した後はアプリケーションを完全に再起動し、エージェントに `command -v mandala`（PowerShell では `Get-Command mandala`）と `mandala --version` を実行させて、エージェントの環境で何が見つかるか確認します。それでも失敗する場合は、`mandala` が動作する terminal からアプリを起動するか、そのアプリまたは OS の環境変数設定で Go の binary directory を `PATH` に追加してください。

## macOS / Linux

まず Skill リポジトリを clone します。

```sh
git clone https://github.com/cottondesu/mandala-skill.git
cd mandala-skill
```

Codex 個人用:

```sh
mkdir -p ~/.codex/skills
cp -R src/mandala ~/.codex/skills/
```

Codex プロジェクト用（Skill リポジトリから実行し、`PROJECT_ROOT` に対象 project directory を設定）:

```sh
mkdir -p "$PROJECT_ROOT/.codex/skills"
cp -R src/mandala "$PROJECT_ROOT/.codex/skills/"
```

Claude Code 個人用:

```sh
mkdir -p ~/.claude/skills
cp -R src/mandala ~/.claude/skills/
```

Claude Code プロジェクト用:

```sh
mkdir -p "$PROJECT_ROOT/.claude/skills"
cp -R src/mandala "$PROJECT_ROOT/.claude/skills/"
```

プロジェクト用コマンドの前に、たとえば `PROJECT_ROOT=/path/to/repo` のように設定します。いずれの導入先にも `SKILL.md` と `references/cli-contract.md` が必要です。

## Windows PowerShell

Skill リポジトリを clone して移動します。

```powershell
git clone https://github.com/cottondesu/mandala-skill.git
Set-Location mandala-skill
```

Codex 個人用:

```powershell
New-Item -ItemType Directory -Force "$HOME/.codex/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$HOME/.codex/skills/"
```

Codex プロジェクト用（`$ProjectRoot` を対象 project root に変更）:

```powershell
$ProjectRoot = "C:\path\to\repo"
New-Item -ItemType Directory -Force "$ProjectRoot/.codex/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$ProjectRoot/.codex/skills/"
```

Claude Code 個人用:

```powershell
New-Item -ItemType Directory -Force "$HOME/.claude/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$HOME/.claude/skills/"
```

Claude Code プロジェクト用:

```powershell
New-Item -ItemType Directory -Force "$ProjectRoot/.claude/skills" | Out-Null
Copy-Item -Recurse "src/mandala" "$ProjectRoot/.claude/skills/"
```

PowerShell でも CLI は別途必要です。`go install github.com/cottondesu/mandala/cmd/mandala@v0.4.0` を実行し、Go の binary directory を `PATH` に設定した後、`mandala --version` で `mandala v0.4.0` と表示されることを確認してください。`mandala` が見つからない場合は [Windows PowerShell で CLI を探す](#windows-powershell-で-cli-を探す)を参照してください。

## 確認

導入・更新後は**新しいエージェントセッション**を開始してください。導入先に二つの package file があることを確認し、対象プロジェクトで「この作業の coverage を mandala skill で追跡してください」などと明示的に依頼します。エージェントは CLI と既存状態を確認し、依頼に応じてのみ状態を作成・更新します。

## 更新と削除

更新時はリポジトリの新しい内容を取得して確認した後、導入済み `mandala` ディレクトリ全体を正本の `src/mandala/` package で置き換えます。macOS / Linux の Codex 個人用の例:

```sh
rm -rf ~/.codex/skills/mandala
cp -R src/mandala ~/.codex/skills/
```

他の導入先も同じ方法です。PowerShell の例:

```powershell
Remove-Item -Recurse -Force "$HOME/.codex/skills/mandala"
Copy-Item -Recurse "src/mandala" "$HOME/.codex/skills/"
```

Skill を削除する場合は、導入先の `mandala` ディレクトリだけを削除します。Mandala CLI や各プロジェクトの `.mandala/` state は削除されません。更新・削除後も新しいセッションを開始してください。

## 問題が起きた場合

- `mandala: command not found` の場合: 「見つからない」は「導入されていない」と同じではありません。再導入の前に、[`mandala` が見つからない場合](#mandala-が見つからない場合)の手順で Go の binary directory と `PATH` を確認します。Skill は CLI を導入しません。
- `go env GOBIN` が空の場合: Go は `GOPATH` の先頭要素の下の `bin` を使います。
- 独自の `GOBIN` を設定している場合: `~/go/bin` ではなく、その directory を `PATH` に追加します。
- `mandala` は存在するが実行できない場合: 通常の file であること、実行権限、この OS / CPU 向けの build かを確認します。
- `mandala --version` が `mandala v0.4.0` 以外を表示する場合: その binary は検証済み基準ではありません。基準が必要なら固定コマンドで再導入します。
- terminal では動くがエージェントでは動かない場合: エージェントの環境の `PATH` が異なります。[デスクトップアプリや IDE から起動したエージェント](#デスクトップアプリや-ide-から起動したエージェント)を参照してください。
- `go` 自体が見つからない場合: [Mandala CLI リポジトリ](https://github.com/cottondesu/mandala)を参照し、先に Go を導入・設定します。
- Skill が認識されない場合: 導入先の名前と二つの file を確認し、新しいセッションを開始します。
- 既存 Mandala project の目標と依頼が衝突する場合: 状態を確認し、`clean` や file の直接編集で初期化しないでください。
- `mandala gaps` が exit `1` の場合: required gap が残る正常な結果です。exit `2` はエラーです。
- `mandala status --json` が exit `1` の場合: required gap が残っており、stdout には `required_gaps` が 0 より大きい有効な JSON が出力されています。クラッシュとして扱わず JSON を解析します。`--json` は `status` の option なので `mandala --project <project-root> status --json` と書きます。`--json` を command より前に置くと exit `2` で失敗します。`status --json` は完了判定の `gaps --required --json` の代わりにはなりません。
