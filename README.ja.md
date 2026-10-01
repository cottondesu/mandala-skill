# mandala-skill

別リポジトリの [Mandala CLI](https://github.com/cottondesu/mandala) を使い、**宣言した目標の coverage gap** を追跡するための Agent Skill です。Codex と Claude Code に、Mandala の状態をいつ読み、いつ更新し、結果をどう解釈するかを教えます。CLI 本体は含まず、計画の自動生成や作業完了の証明もしません。

v0.1.0 は手動コピーによる配布です。Agent Skill はエージェントへの指示なので、導入前に内容を確認してください。インストールした Skill package 自体はネットワークアクセスや認証情報を必要とせず、テレメトリーもありません。エージェント環境そのものの権限は別です。

## 前提条件と互換性

Mandala CLI を別途インストールし、`mandala` を `PATH` から実行できるようにしてください。実行可否とバージョンの確認には `mandala --version` を使い、検証対象の release では `mandala v0.3.0` と表示されます。この Skill で検証した基準は **Mandala CLI v0.3.0** です。他のバージョンとの互換範囲はまだ定義していません。[CLI の導入手順](docs/INSTALLATION.ja.md#cli-のインストール)には固定コマンド `go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0` があります。構文の確認には `mandala --help` または `mandala <command> --help` を使います。

## 対応エージェントと導入先

`references/cli-contract.md` を含む **正本の package directory `src/mandala/` 全体** をコピーしてください。

| エージェント | 個人用 | プロジェクト用 |
| --- | --- | --- |
| Codex | `~/.codex/skills/mandala/` | `<repo>/.codex/skills/mandala/` |
| Claude Code | `~/.claude/skills/mandala/` | `<repo>/.claude/skills/mandala/` |

macOS / Linux での Codex 個人用の例:

```sh
git clone https://github.com/cottondesu/mandala-skill.git
cd mandala-skill
mkdir -p ~/.codex/skills
cp -R src/mandala ~/.codex/skills/
```

4 種類の導入先、Windows PowerShell、更新、削除、問題が起きた場合の確認事項は[インストールガイド](docs/INSTALLATION.ja.md)を参照してください。導入または更新後はエージェントの新しいセッションを開始します。

## 使い方と安全上の境界

たとえば「この作業の coverage を mandala skill で追跡してください」と明示的に依頼します。エージェントは書き込み前に CLI と現在の状態を確認します。Skill が自動選択されただけでは、状態の作成や変更は許可されません。Mandala は目標、facet、required / optional の leaf cell を記録し、`status` と `gaps` が未解決の宣言済み required leaf を報告します。

`.mandala/` は CLI の管理領域です。Skill は `state.json` の直接編集、そこへのメモや台帳の追加、`.gitignore` や Git exclude の直接変更、`clean` の自動実行を禁止します。required gap が 0 でも、**現在宣言されている** required leaf が解決したことだけを意味します。網羅性や実作業の完了が証明されたわけではありません。

## 開発

`src/mandala/` は Git 管理する正本の Skill package で、そのまま導入できます。`scripts/` は maintainer tooling、`tests/` は検証・評価資料です。`dist/mandala/` は Git ignore する単一の生成 package で、ローカルの package 検証と将来の release packaging に使います。正本の編集後や新しい checkout では、次を順番に実行します。

```sh
make build
make check
make test
```

`make build` は `dist/mandala/` に正本と byte 単位で一致する共通 package を生成し、Codex と Claude Code の両方で使えます。`make check` と `make test` はこの生成物を必要とします。正本をコピーする導入には build も Python も不要です。

build、validation、test は Python 標準ライブラリだけで動き、ネットワークは不要です。package の整合性、有効な指示文にある必須安全契約、`tests/evals/cases.json` の行動評価 fixture の構造と coverage metadata を検査します。Codex / Claude Code を起動したり、実際の agent behavior を保証したりはしません。release 前に[手動の行動評価](tests/README.ja.md)を実施してください。`dist/` を直接編集しないでください。

## ライセンス

MIT。[LICENSE](LICENSE) を参照してください。
