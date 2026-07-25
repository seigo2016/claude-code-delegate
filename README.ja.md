# claude-code-delegate

Claude Code から外部 CLI agent へ、非同期に委譲し、確実に回収する。

[English](README.md)

判断する場所は Claude Code のままにし、作業そのものは Codex / OpenCode / headless の Claude へ出します。生成物の確認、repository の把握、承認済みの変更の適用、検査の実行。返ってくるのは実行記録ではなく、短い証拠です。

poll はしません。session は task を渡したら turn を終え、回収すべきものができたときだけ起こされます。

## なぜ

長い Claude Code の session は context を使い切ります。しかも使い切る先が間違っています。log の出力、ファイルの中身、ディレクトリの一覧、同じ確認の繰り返し。context を必要としていた判断が、必要としていなかった作業に押し出されます。

作業を外へ出すこと自体は簡単です。この道具が引き受けるのは**安全に出す**部分です。止まった、死んだ、何も返さなかった、答えらしく見えるだけのものを返した — そのどれもが成功として返ってきてはいけません。

## 導入

Python 3.11 以上と、`codex` / `opencode` / `claude` のいずれかが PATH にあること。Linux と macOS。

```bash
git clone https://github.com/seigo2016/claude-code-delegate
claude plugin marketplace add ./claude-code-delegate
claude plugin install delegate@claude-code-delegate --scope project
```

install しても session は何も変わりません。agent は差し替わらず、tool は外れず、宣言するまでどの backend も有効になりません。

## 設定

`examples/delegate.toml` を repository の `.claude/delegate.toml` に複製し、実際に使える model を書き、worker を 1 つ有効にします。

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

role が要求するのは capability (light / standard / frontier の 3 段階) であって model 名ではないため、backend を変えても role はそのまま使えます。選んだ worker にその段階の model がなければ、task は何も起動する前に拒否されます。

| adapter | 起動 | 答えの受け取り |
|---|---|---|
| `codex` | `codex exec --json` | 出力ファイル。加えて JSON schema で拘束する |
| `opencode` | `opencode run --format json` | 最後の text part |
| `claude` | `claude -p --output-format stream-json` | 終了時の result event |

## 使う

Claude Code の session からは `/delegate` skill が一連の流れを担います。直接使う場合:

```bash
delegate submit --role artifact-auditor --title changelog-vs-tags --packet packet.json
# → {"task_id": "…", "status": "starting", "worker": "claude", "model": "sonnet"}

delegate collect <task-id>
```

packet に書くのは、この task が固有に足す情報だけです。role の指示、model、禁止事項、結果契約は自動で付きます。

```json
{
  "objective": "Check that CHANGELOG.md lists every tag reachable from main.",
  "read": ["CHANGELOG.md"],
  "allowed_writes": [],
  "required_evidence": ["tags missing from the changelog", "tags listed but not in git"],
  "host_only": false
}
```

session から出してはいけない仕事 — live な session state を要するもの、人が責任を負う判断 — は `"host_only": true` を付ければ、送られずに拒否されます。

## しないこと

受け取った結果は、そのまま次の行動に使えるものであること。

| 起きたこと | 返るもの |
|---|---|
| tool 呼び出しが返らない | `timeout` / `failure_class: tool_stall` |
| 応答は完成したが結果を渡さない | `timeout` / `finalization_timeout`。最後のメッセージは別に退避 |
| 非ゼロ終了 | `failed` / `nonzero_exit` |
| 何も生成しない | `failed` / `empty_result` |
| 契約を満たさない答え | `failed` / `invalid_result` と、その理由 |
| 宣言した範囲の外へ書いた | `failed` / `write_scope_violation` と、その path |
| 実行中に機械が落ちた | `orphaned`。結果が残っていれば `degraded` |

正しい答えに**見えるだけ**のメッセージが、正常な結果へ昇格することはありません。task の隣に置かれるので、読んでから判断してください。

結果は 1 つの list につき 300 文字以内の文字列 5 件までです。この上限が製品そのものです。一目で読めない答えは、委譲したはずのコストを session へ戻しています。本当に長い一覧が答えになる場合は、worker にファイルを書かせて path を返させてください。

## 限界

境界があると信じているのに実際には無い状態は、最初から無いより悪いので、そのまま書きます。

- **書き込み範囲の検査は git で行うため、work tree の中しか見えません。** home ディレクトリ、システムパス、ネットワーク越しの書き込みは観測できません。git のない repository では検査自体を行わず、その旨を task の状態に残します。
- **sandbox はありません。** worker はそれぞれの CLI が与える権限で動きます。
- **adapter は変化する 3 つの CLI に追従しています。** event の形は実行を観測して採取したもので、backend の更新で変わり得ます。
- **`fcntl` を使うので Linux と macOS のみです。** Windows は WSL 経由。

## 開発

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ty check
```

テストは代役の worker を動かします。止まる、死ぬ、何も返さない、答えらしく見えるだけのものを返す。これらを指示どおりに起こせるので、上の失敗処理は主張ではなく検証されています。

## License

MIT
