# 完了通知の調査

変更前の引用の行番号は `git show HEAD:パス`、変更後は作業ツリーの行番号。

## 変更前の経路と問題

- `src/delegate/tasks.py:246`: handle は `"completion_delivery": "async_rewake"` を返す。`src/delegate/tasks.py:441`: submit は `register_completion_watch(project_root, task_id)` を呼ぶ。
- `src/delegate/tasks.py:65-76`: pending のキーは `identity = f"{project_root}\0{task_id}".encode()`、記録は `{"project_root": str(project_root), "task_id": task_id}`。セッション ID はない。
- `hooks/hooks.json:15-25,31-41`: Bash の成功・失敗後に `_hook-watch` を `"async": true`、`"asyncRewake": true` で起動する。
- `src/delegate/cli.py:386,394-400`: hook 入力から `session_id = hook_input["session_id"]` を読むが、`registered = tasks.pending_completion_watches()` に ID を渡さず、`[*_read_session_watches(session_id), *handles, *registered]` を監視する。`src/delegate/cli.py:405-408` はそれをこのセッションに記録し `tasks.clear_pending_completion_watches(registered)` で共有 pending を消す。
- したがって A が submit 後、A の hook より先に B の Bash hook が動くと、B が A の pending を監視・削除できる。A の handle が pipeline で隠れた場合、A はその pending を再発見できない。handle が見えていても `src/delegate/cli.py:465-478` の task ごとの `watch.lock` を B が先に持つと A は `except liveness.AlreadyHeld: continue` でその task を待たない。
- `src/delegate/cli.py:495-503` は terminal かつ `not state.get("delivered_at")` なら `ready` を出力して `return WAKE`。`src/delegate/cli.py:212-216` は `WAKE = 2`。通知自体は delivered を記録せず、`src/delegate/cli.py:100` の `events.emit(task_dir, state["status"], delivered_at=events.now_iso())` は collect 時にだけ実行される。別セッションが起きても collect しなければ null のままになり得る。今回実際にこの競合が起きたとは断定できない。
- `src/delegate/tasks.py:230` の重複判定は fingerprint と active status のみなので、別セッションの同一 submit が同じ task にまとまる経路もある。`src/delegate/tasks.py:417` の `session_id` は worker の ID で、`src/delegate/runner.py:62` は `"session_id": view.session_id` を保存する。submit 元の ID ではない。
- `src/delegate/cli.py:194-200` の SessionStart 時 reconcile は root 内の全 task を対象に未回収 terminal を ready にする。明示的な `watch`・`reconcile`・`collect` もセッションを認証しない。

## 変更後

- `src/delegate/tasks.py:392,430` で `CLAUDE_CODE_SESSION_ID` を `submitter_session_id` として記録。Claude Code の [環境変数リファレンス](https://code.claude.com/docs/en/env-vars) はこの値が Bash subprocess に自動設定され hook の `session_id` と一致すると説明する。
- `src/delegate/tasks.py:80-99,235-241` は pending と重複判定をセッション別にする。`src/delegate/cli.py:346-363,412-425` は handle と registry の両方を所有セッションで絞ってから監視・削除する。ID 不明の task は従来の共有挙動を保つ。
- `hooks/hooks.json:9` と `src/delegate/cli.py:194-219` は SessionStart の hook 入力 ID で reconcile の表示を絞る。手動の診断コマンドは従来どおり project 全体が対象であり、アクセス制御ではない。
- `tests/test_watch.py:142-179` の 2 件は合成した 2 セッションで、隠れた handle と他セッションの handle、SessionStart の表示、同一 active submit の独立性を確認する。
- `UV_CACHE_DIR=/tmp/delegate-uv-cache uv run pytest -q`: 235 passed、13 skipped。

## git diff --stat

```text
 CHANGELOG.md          |  4 ++++
 README.ja.md          |  2 ++
 README.md             |  4 ++++
 hooks/hooks.json      |  2 +-
 src/delegate/cli.py   | 33 +++++++++++++++++++++++++++------
 src/delegate/tasks.py | 21 +++++++++++++++++----
 tests/conftest.py     |  2 +-
 tests/test_watch.py   | 39 +++++++++++++++++++++++++++++++++++++++
 8 files changed, 95 insertions(+), 12 deletions(-)
```

この証拠ファイルは新規未追跡なので `git diff --stat` には含まれない。既存の未追跡 `tests/change-evidence.txt` は変更していない。
