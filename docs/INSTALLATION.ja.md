# インストール

このリポジトリは Agent Skill の指示を手動コピーで配布します。`src/mandala/` は Git 管理する正本の完全な package です。導入前に確認し、`SKILL.md` と `references/cli-contract.md` を含む `mandala` ディレクトリ全体をコピーします。この導入に build や Python は不要です。`dist/mandala/` は Git ignore する単一の生成 package で、maintainer は必要に応じて `make build` 後に package 検証や将来の release packaging に使えます。プロジェクト固有の目標やメモをインストール済み Skill 内に書かないでください。Mandala の project state は CLI が管理します。

## CLI のインストール

Mandala CLI は別途必要です。Skill は CLI をダウンロードしません。Go を導入済みの場合、基本のコマンドは次のとおりです。

```sh
go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0
mandala --version
```

期待する表示は `mandala v0.3.0`（exit `0`）です。`mandala --version` は project option や command と組み合わせず単独で実行します。実行可否と build version の確認に使い、構文の確認には `mandala --help` または `mandala <command> --help` を使います。

この Skill の検証済み基準は Mandala CLI v0.3.0 です。`@latest` で導入される新しい版は、この Skill との互換性が未検証の場合があります。Go の binary directory を `PATH` に含めます。Go 要件や CLI の説明は [Mandala CLI リポジトリ](https://github.com/cottondesu/mandala)を確認してください。ローカルの開発 build では `mandala (devel)` と表示される場合があり、それでは release の基準を確認できません。

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

PowerShell でも CLI は別途必要です。`go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0` を実行し、Go の binary directory を `PATH` に設定した後、`mandala --version` で `mandala v0.3.0` と表示されることを確認してください。

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

- `mandala` が見つからない場合: CLI を別途導入し、`PATH` を確認して `mandala --version` を実行します。
- Skill が認識されない場合: 導入先の名前と二つの file を確認し、新しいセッションを開始します。
- 既存 Mandala project の目標と依頼が衝突する場合: 状態を確認し、`clean` や file の直接編集で初期化しないでください。
- `mandala gaps` が exit `1` の場合: required gap が残る正常な結果です。exit `2` はエラーです。
