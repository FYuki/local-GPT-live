# LiveKit 最小アダプターの合成検証（2026-10-05 JST）

## 起点と分担

リモート main / epic は `01a680a`、既存 PR #2 の head は `2459c9b`。
Windows の既存 ACK worktree は `502cc88` で、未pushのテストを保持している。
Ubuntu 内の専用 clone から `origin/epic/transport-playback` を起点に
`feature/livekit-connection-adapter` の独立 worktree を作成した。
既存 checkout、ACK 実装、browser のモジュール・テスト・README・CI は変更していない。

## 検証結果

WSL ディストリビューション `Ubuntu`、Python 3.12.3、uv 0.8.22。
公式 SDK は旧 PoC と同じ `livekit==1.1.16`。既存 lock 内の依存バージョンは維持し、
optional extra と必要な追加依存だけを lock へ加えた。

| 検証 | 結果 |
| --- | --- |
| `uv sync --frozen --extra livekit` | 成功 |
| pytest | 121 passed、skip なし。既存 72 + 入力 30 + 接続/出力 19 |
| ruff | 成功 |
| mypy strict | 16 source files、成功 |
| 日本語 docs / Markdown 相対リンク | 成功 |
| `git diff --check` | 成功 |
| sdist / wheel build | 成功 |
| 合成 `voice-demo` | 7 / 7 成功 |

検証終了時刻は 2026-10-04 23:56 UTC（翌日 08:56 JST）付近。
CI は PR 作成後に対象 head の結果を PR へ記録する。

## 重点ケース

- participant identity / SID / microphone publication の照合、旧 grant、新 track。
- SDK queue 前の sample 時計、統計準備、concealment、欠落・統計逆行・欠測の拒否。
- open 中の取消、reset 中の participant 交代、native 例外の固定 reason 化。
- 接続・publish・capture の遅着、同期 queue 消去と mute、遅着後の再消去。
- 出力 track の準備確認と SID 照合、未確認 timeout、累積 sample 範囲。
- 切断後の unpublish 待機を作らない終了順序、二重 close、資源の一度だけの解放。
- SDK が取消を無視する場合の有界待機と `shutdown_pending`、未完了資源がある間の再開始拒否。
- 送信・生成から実再生 ACK を作らないこと。

固定 SDK のソースと別担当レビューで、Room イベントの引数順、FFI 完了前の取消、
切断後の unpublish、取消を無視した統計 task の増加を検出し、修正・回帰検証した。

## 未実施・残条件

実 LiveKit、STT/TTS/Core endpoint、GPU、マイク・音声品質は **NOT RUN**。
Ubuntu-dogfood の共有サービス、推論、公開・認証設定、資格情報を変更していない。

ネットワーク wire の合意、認証済み端末から入力 ACK / 出力準備を届ける経路、device gate、
ブラウザ出力時計と実再生 ACK の接続は後続。
提案した範囲は [ADR 0003](../adr/0003-livekit-adapter.md) と
[組込み手順](../livekit-adapter.md) に明記した。

CodeRabbit の差分レビューは親作業者が時間枠を管理するため、この担当から追加依頼していない。
ローカルの別担当レビューや CI 成功を CodeRabbit 実レビュー済みとは扱わない。
main / epic へのマージは実施しない。
