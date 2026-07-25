# claude-code-delegate

[![ci](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml/badge.svg)](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | [日本語](README.ja.md)

**外に出せる作業に context を使わない。**

claude-code-delegate は、Claude Code から境界の決まった作業を Codex / OpenCode / headless の Claude へ渡し、実行記録ではなく数行の証拠を受け取る plugin です。poll はしません。session は task を渡したら turn を終え、回収できるものができたときだけ起こされます。

## 解決したい問題

長い session は context を使い切ります。使い切る先は、log の出力、ファイルの中身、ディレクトリの一覧、同じ確認の繰り返しです。context を必要とする判断が、必要としない作業に押し出されます。

作業を外へ出すこと自体は難しくありません。難しいのは失敗したときの扱いです。止まった、死んだ、何も返さなかった、答えらしく見えるだけのものを返した。これらが成功として返ってくると、context と引き換えに疑いを持ち込むことになります。

## 5 分で試す

```bash
claude plugin marketplace add seigo2016/claude-code-delegate
claude plugin install delegate@claude-code-delegate --scope project

cp examples/delegate.toml .claude/delegate.toml
# worker を 1 つ enabled = true にし、model を書く

cat > /tmp/packet.json <<'JSON'
{
  "objective": "Check that CHANGELOG.md lists every tag reachable from main.",
  "read": ["CHANGELOG.md"],
  "allowed_writes": [],
  "required_evidence": ["tags missing from the changelog", "tags listed but not in git"],
  "host_only": false
}
JSON

delegate submit --role artifact-auditor --title changelog --packet /tmp/packet.json
# → {"task_id": "…", "status": "starting", "worker": "claude", "model": "sonnet"}

delegate collect <task-id>
```

Claude Code の session では `/delegate` skill が同じ流れを担当し、task の完了時に hook が session を起こします。

## 必要なもの

- PATH 上の Python 3.11 以上。設定の読み込みに `tomllib` を使うためです。3.11 で標準ライブラリに入ったもので、macOS の `python3` は 3.9 のことが多いため、別途用意が必要です。他に依存はありません。
- `codex` / `opencode` / `claude` のいずれか。
- Linux または macOS。Windows は WSL 経由。

plugin を install しても、main agent は差し替わらず、tool も外れず、backend も設定で宣言するまで 1 つも使われません。

## 設定

```toml
default_worker = "claude"

[workers.claude]
adapter = "claude"
enabled = true
models = { light = "haiku", standard = "sonnet", frontier = "opus" }

[roles.artifact-auditor]
capability = "standard"
effort = "high"
task_class = "review"
```

role が指定するのは capability であり、model 名ではありません。そのため backend を変えても role の定義は変わりません。選んだ worker にその capability の model が設定されていなければ、worker を起動する前に拒否します。

| adapter | 起動 | 結果の受け取り |
|---|---|---|
| `codex` | `codex exec --json` | 出力ファイル。JSON schema でも拘束する |
| `opencode` | `opencode run --format json` | 最後の text part |
| `claude` | `claude -p --output-format stream-json` | 終了時の result event |

同梱の role は `artifact-auditor` / `repo-cartographer` / `bounded-implementer` / `verification-runner` / `consistency-auditor` / `adversarial-critic` です。

## 返ってくるもの

| 起きたこと | 返る状態 |
|---|---|
| tool 呼び出しが返らない | `timeout` / `failure_class: tool_stall` |
| 応答は完了したが結果を渡さない | `timeout` / `finalization_timeout`。最後のメッセージは別に保存 |
| 非ゼロ終了 | `failed` / `nonzero_exit` |
| 結果を出力しない | `failed` / `empty_result` |
| 形式を満たさない結果 | `failed` / `invalid_result` と、その理由 |
| 宣言した範囲の外へ書き込んだ | `failed` / `write_scope_violation` と、その path |
| 実行中に機械が停止した | `orphaned`。結果が残っていれば `degraded` |

正しい結果に見えるだけのメッセージを、正常な結果として扱うことはありません。task directory に保存するので、読んでから判断してください。

結果は 1 つの list につき、300 文字以内の文字列を 5 件までです。一目で読めない量を返されると、委譲して減らしたはずの context を session 側で使うことになります。長い一覧が必要な場合は、worker にファイルを書かせて path を返させてください。

session の外へ出せない作業もあります。live な session state を必要とするもの、人が責任を負う判断がそれにあたります。`"host_only": true` を付けると、送信せずに拒否します。

## コマンド

| コマンド | 用途 |
|---|---|
| `delegate submit --role R --title T --packet P` | worker を起動し handle を受け取る |
| `delegate collect <task-id>` | 結果を 1 度だけ回収する |
| `delegate status <task-id>` | 明示的な診断。待機ループには使わない |
| `delegate cancel <task-id>` | 誤った task や暴走した task を止める |
| `delegate reconcile` | worker が消えた task を分類する |
| `delegate watch` | hook が実行する。回収できるものを待つ |

## 制約

- **書き込み範囲の検査は git で行うため、work tree の中しか見えません。** home ディレクトリ、システムパス、ネットワーク越しの書き込みは検出できません。git 管理下にない repository では検査を行わず、その旨を task の状態に記録します。
- **sandbox はありません。** worker は各 CLI が与える権限で動作します。
- **adapter は 3 つの CLI の出力形式に依存しています。** 形式は実行を観測して確認したもので、backend の更新で変わる可能性があります。

## 開発

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ty check
```

テストでは代役の worker を使います。止まる、死ぬ、何も返さない、答えらしく見えるだけのものを返す、といった状態を指示どおりに再現できるため、上記の失敗の扱いは実際に検証されています。

## License

MIT
