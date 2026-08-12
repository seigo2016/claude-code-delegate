# claude-code-delegate

[![ci](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml/badge.svg)](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | [日本語](README.ja.md)

**外に出せる作業に context を使わない。**

claude-code-delegate は、Claude Code から範囲を明示した作業を Codex / OpenCode / headless の Claude へ渡す plugin です。返ってきた結果は、検証するまで信用しません。worker が停止も消失もせず終わったか、結果が決められた形式を満たすか、宣言した範囲の外へ書いていないかを確認します。

Claude の session は進捗を poll しません。task を渡したら turn を終え、回収できる結果ができたときだけ起こされます。

## 解決したい問題

長い session では、context が log の出力、ファイルの中身、ディレクトリの一覧、同じ確認の繰り返しに費やされ、それを必要としていた判断に回りません。

worker は止まることも、死ぬことも、何も返さないことも、結果らしく見えるだけのものを返すこともあります。いずれも成功としては返しません。

## 試す

```bash
claude plugin marketplace add seigo2016/claude-code-delegate
claude plugin install delegate@claude-code-delegate --scope user
```

続けて、自分の project に `.claude/delegate.toml` を置き、手元にある backend を 1 つ書きます。

```toml
default_worker = "claude"

[workers.claude]
adapter = "claude"
enabled = true
light = { model = "haiku", effort = "medium" }
standard = { model = "sonnet", effort = "high" }
frontier = { model = "opus", effort = "xhigh" }

[roles.artifact-auditor]
level = "standard"
forbids_allowed_writes = true
repo_local_reads = true
```

この repository の `examples/delegate.toml` には 6 つの role と 3 つの backend が、すべて無効の状態で入っています。

あとは Claude Code の session で指示します。

> /delegate CHANGELOG.md に main から辿れる全 tag が載っているか確認して

Claude が task を組み立てて worker へ渡し、turn を終えます。worker が終わると hook が session を起こし、Claude が結果を回収します。

## 必要なもの

- PATH 上の Python 3.11 以上 (`tomllib` のため)。macOS の `python3` は 3.9 なので別途用意してください (`brew install python@3.12`)。他に依存はありません。
- `codex` / `opencode` / `claude` のいずれか。
- Linux または macOS。Windows は WSL 経由。

plugin を install しても、main agent は差し替わらず、tool も外れず、backend も設定で宣言するまで 1 つも使われません。

## 設定

role が指定するのは level であり、model 名ではありません。その level が何を意味するかは worker 側が定義します。そのため同じ role をどの backend でも使え、`--worker` で task ごとに選べます。選んだ worker がその level を定義していなければ、起動する前に拒否します。

| adapter | 起動 | 結果の受け取り |
|---|---|---|
| `codex` | `codex exec --json` | 出力ファイル。JSON schema でも拘束する |
| `opencode` | `opencode run --format json` | 最後の text part |
| `claude` | `claude -p --output-format stream-json` | 終了時の result event |

同梱の role は `artifact-auditor` / `repo-cartographer` / `bounded-implementer` / `verification-runner` / `consistency-auditor` / `adversarial-critic` です。

task は Claude が組み立てます。何をするか、何を読んでよいか、何を書いてよいか、どんな証拠を持ち帰るかを書きます。

```json
{
  "objective": "Check that CHANGELOG.md lists every tag reachable from main.",
  "read": ["CHANGELOG.md"],
  "allowed_writes": [],
  "required_evidence": ["tags missing from the changelog", "tags listed but not in git"],
  "host_only": false
}
```

## 返ってくる結果

| 起きたこと | 返る状態 |
|---|---|
| tool 呼び出しが返らない | `timeout` / `failure_class: tool_stall` |
| 応答は完了したが結果を渡さない | `timeout` / `finalization_timeout`。最後のメッセージは別に保存 |
| 非ゼロ終了 | `failed` / `nonzero_exit` |
| 結果を出力しない | `failed` / `empty_result` |
| 形式を満たさない結果 | `failed` / `invalid_result` と、その理由 |
| worker backend が報告した変更先が宣言範囲外だった | `failed` / `write_scope_violation` と、その path |
| 実行中に dirty になったがworkerへ帰属できないpathがある | task結果は保持し、`unattributed_workspace_changes`へ記録 |
| backend を起動できない | `failed` / `launch_error` |
| こちらが取り消した | `cancelled` |
| 実行中に機械が落ちた | `orphaned`。結果が残っていれば `degraded` |

timeout には停止位置の読みが付きます。`tool_stall` / `finalization_timeout` / `runtime_stall` / `event_stream_stall` / `wall_clock_timeout` のいずれかです。

正しい結果に見えるだけのメッセージを、正常な結果として扱うことはありません。task directory に保存します。

結果は 1 つの list につき、300 文字以内の文字列を 5 件までです。それより長い結果が必要な場合、書き込み可能な worker はファイルの path を返し、read-only worker は重要度上位 5 件と追加 task の要否を返します。

patcher には \`requires_allowed_writes = true\`、auditor には \`forbids_allowed_writes = true\` を指定できます。絶対 path、親 directory、URL を起動前に拒否する repo-local role には \`repo_local_reads = true\` を指定します。

`required_evidence` は worker への指示で、prompt に載ります。契約が確認するのは、evidence の各欄が存在し、形式と上限を満たすことです。要求した内容に答えているかは照合せず、内容の真偽も判定できません。

live な session state を必要とする作業や、事後承認では済まず人が下すべき判断には、Claude が `"host_only": true` を付けます。この場合 submit は送信せずに拒否します。packet に明示的に書く指定であり、内容を自動分類する機能ではありません。

## コマンド

skill と hook が実行します。人が直接使うのは診断のときだけです。

| コマンド | 用途 |
|---|---|
| `delegate submit --role R --title T --packet P` | worker を起動し handle を受け取る |
| `delegate collect <task-id> --project-root ROOT` | 結果を 1 度だけ回収する |
| `delegate status <task-id>` | 明示的な診断。待機ループには使わない |
| `delegate cancel <task-id>` | 誤った task や暴走した task を止める |
| `delegate reconcile` | worker が消えた task を分類する |
| `delegate watch` | hook が実行する。回収できるものを待つ |

`submit` は `--worker <name>` で backend を task ごとに上書きできます。全コマンドが `--project-root` を受け取ります。submit handle と完了通知にも正規化済みの root が含まれるため、cross-repository task は submit 時と同じ ledger から回収されます。1 回の Bash 呼び出しで複数の独立 task を submit しても、hook は返された全 handle を追跡します。1 件を collect した後は、同じ呼び出しの未回収 task に対して再び待機します。

## 検出の範囲

検出できる:

- 起動しなかった、停止した、死んだ、supervisor を失った worker
- 結果が無い、壊れている、必須項目を欠いている
- 最終メッセージはあるが結果として渡されていない
- git work tree 内での、宣言した範囲外への書き込み

検出できない:

- work tree の外への書き込み。home ディレクトリ、システムパス
- `.gitignore` が除外するファイルへの書き込み
- ネットワーク経由の副作用、資格情報へのアクセス、git に現れない変更
- 形式を満たしているが内容が誤っている結果

sandbox が事前に止められる範囲は backend で異なります。書き込みを宣言していないタスクには、その backend が持つ最も狭い姿勢を与えます。

| backend | 書き込み不可のタスク | 強制する層 |
|---|---|---|
| codex | `--sandbox read-only`、ネットワーク遮断 | OS |
| claude | `auto` から編集ツールを外したもの | 分類器とツールの非付与 |
| opencode | 実行時に注入する `delegate-readonly` agent | OpenCode の権限 |

カーネルで拒否するのは codex だけです。書き込みを試みると `Read-only file system` が返ります。claude では編集ツールをそもそも渡さず、試した迂回 (シェルのリダイレクト、`tee`、Python の 1 行スクリプト) はいずれも分類器が拒みました。ただしこれは境界ではなく、その都度の判断です。opencode の読み取り専用 task には、実行ごとに `edit`、`bash`、`task`、`webfetch`、`websearch` を deny した primary agent を与えます。これは OS sandbox ではなく tool permission の境界ですが、task contract でも `.claude`、`/tmp`、その他への scratch file 作成を禁止します。

テストやビルドを走らせる role には、リポジトリを変更できない場合でも書き込み可能なファイルシステムを与えます。ビルドツールが home ディレクトリ配下にキャッシュを書くためです。この場合リポジトリへの書き込みを事前に止めるものはなく、上記の事後検査が検出します。

adapter は 3 つの CLI の出力形式に依存します。形式は実行を観測して確認したものなので、backend の更新で変わる可能性があります。

## まだ無いもの

見落としではなく、判断して外した項目が 3 つあります。

**委譲の強制。** `PreToolUse` hook は tool 呼び出しを拒否できるため、設定によって skill を「使ってもよいもの」から「使わねばならないもの」に変えられます。現時点で外した理由は 2 つです。Claude は重い読み取り作業を指示なしで委譲し、2 ファイルの照合は自分で処理することが実測で確認できたこと。そして、別の手段で同じ作業を行えば回避できる拒否は、強制とは言えないことです。

**隔離。** worker は利用者が作業している work tree でそのまま動きます。task ごとに worktree を分ければ封じ込められますが、`bounded-implementer` はその tree を変更するために存在するので、役に立てるには封じ込めを解く必要が出てきます。

## 開発

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ty check
```

テストでは代役の worker を使い、失敗を指示どおりに再現します。停止した worker、死んだ worker、起動できない backend、空の結果、壊れた結果、応答は終えたのに結果を渡さない場合、宣言範囲の外への書き込み、orphaned と degraded の分類、結果を一度だけ回収すること、を対象にしています。

## License

MIT
