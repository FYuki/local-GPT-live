# LiveKit 接続アダプター

既存の `AudioInput` / `VoiceSession` / `Playback` と公式 LiveKit RTC SDK を接続するための Python API。
ホストが認可した既存参加者を対象とする。ブラウザの制御メッセージ、実再生 ACK、実マイク受入は未接続。
設計の判断は [ADR 0003](adr/0003-livekit-adapter.md)、音声側の不変条件は
[ADR 0001](adr/0001-voice-boundary.md) を参照する。

## 準備

開発・合成試験は WSL ディストリビューション `Ubuntu` の独立 worktree で行う。
任意依存の公式 RTC SDK を既存 lock から導入する。

```sh
uv sync --frozen --extra livekit
```

`livekit==1.1.16` を使用する。SDK を使わない既存 fixture 実行へ接続処理を追加しない。
アダプター自身は token を発行せず、サービスを起動せず、環境変数から接続先や資格情報を自動取得しない。

ホストが渡す設定は次のとおり。

| 設定 | 内容 |
| --- | --- |
| `url` | 利用が認められた既存 LiveKit の接続 URL |
| `token` | ホストが取得済みの接続 token。repr に含めず、ログや commit に保存しない |
| `participant_identity` | 入力を許可する remote participant の identity |
| `participant_sid` | 現在の remote participant の SID。同じ identity の再参加と区別する |
| `connect_timeout` | 接続を待つ秒数。既定 10 秒 |
| `close_timeout` | リソースの終了処理を待つ秒数。既定 2 秒 |
| `output_ready_timeout` | 出力 track の端末準備完了を待つ秒数。既定 3 秒 |

token の発行や既存サービスの設定変更はこの API の責務に含めない。

## ホストからの呼出順序

以下は、ホストが provider と接続情報を用意した後の組込み例である。
endpoint や資格情報を作成する CLI ではない。

```python
from local_gpt_live.input import AudioInput
from local_gpt_live.livekit_transport import LiveKitConfig, LiveKitPlayback, LiveKitTransport
from local_gpt_live.session import VoiceSession
from local_gpt_live.voice_input.pipeline import VoiceInputPipeline

playback = LiveKitPlayback()
session = VoiceSession(stt, core, tts, playback)
audio_input = AudioInput(session, VoiceInputPipeline())
transport = LiveKitTransport(
    audio_input,
    LiveKitConfig(
        url=existing_livekit_url,
        token=issued_token,
        participant_identity=authorized_identity,
        participant_sid=authorized_participant_sid,
    ),
    # ホスト側で認証済み端末へ公開trackを知らせる。ここで準備完了を合成しない。
    on_track=notify_output_track_to_authorized_client,
)
try:
    await transport.connect()
    # ホストが device gate と認可を確認した後、新しい microphone track を開く。
    grant = await transport.open_input(
        track_sid=new_microphone_track_sid,
        request_id=input_request_id,
        revision=next_input_revision,
    )
    # ここでホスト自身のセッション処理を実行する。
finally:
    await transport.aclose()
```

`cancel()` は現在の回答を取り消す。`aclose()` は入力・回答を失効させ、reader、出力 source、
所有 track、Room 接続を終了する。終了は必ず `finally` 等から呼ぶ。
SDK への接続・track 公開・音声送信が進行中なら、その task の完了応答を引き続き観測して資源を回収する。
出力は先に mute と queue 消去で停止し、遅着した処理から旧回答を再開しない。
`close_timeout` 内に処理が戻らなければ `shutdown_pending` を通知するため、ホストは正常終了と区別する。
SDK の `reconnecting` / `disconnected`、対象 participant の切断、入力 microphone の mute / unsubscribe は
この transport を終了させる。再接続や participant の交代後は、ホストが認可を確認し直し、新しい transport を作る。
旧 track や旧 grant を再利用しない。Room の自動購読を有効にしても、入力処理へ流すのはホストが開いた対象 track だけとする。

`on_track` は `TrackPublished(response_id, track_sid)` を受け取る同期 callback。
ホストはこの情報を対象端末へ通知し、認証済み端末の購読と再生準備を確認してから
`transport.confirm_output_ready(response_id, track_sid)` を呼ぶ。
アダプターは response と track SID の一致を検証する。確認前は PCM を送信せず、既定 3 秒で期限切れとなる。
data-channel wire はホストの責務として未定義のまま残す。track 公開の通知だけで準備確認を自動発行しない。

## 音声形式と送信情報

入力は SDK が 16 kHz mono PCM16 に変換した frame を受け取る。
sample 位置は SDK queue に入る前に記録し、ローカル queue 欠落を後段の連続性検査へ伝える。
開始前は有効な Opus 48 kHz 受信統計を最大 4 秒待つ。
開始後は 10 ms 以下の frame を 10 個ずつ保留し、前進した統計を最大 1 秒確認した後に後段へ渡す。
通常の 10 ms frame では音声量で 100 ms 分を保留し、さらに統計確認を待つ。
統計 ID の交代、counter の逆行、欠測、非無音 concealment 増分 80 ms 以上は入力を停止する。
未完了の統計取得・SDK 解放が残る間は `microphone_cleanup_pending` で再開始を拒否し、
待機 task の追加を抑える。遅着した処理が完了した後は再試行できる。
全ネットワーク欠落の検出や、実通信での遅延品質を保証する検査ではない。

出力は mono PCM16 WAV の 16 / 22.05 / 24 / 44.1 / 48 kHz に対応する。
同じ response 内の sample rate 変更は受け付けない。既存 provider の検証範囲を維持し、独自 resampler は追加しない。

response ごとの出力 track 名は `ds-response-v1:<response_id>`。
`SegmentSent` は response ID、0 始まりの sequence、track SID、sample rate、
当該 track 内の送信 sample 範囲 `[sample_start, sample_end)` をホストへ渡す。
必要な場合は `LiveKitTransport(..., on_segment=callback)` で同期 callback を指定する。
この通知は配信側の事実であり、端末で聞こえた範囲を保証しない。

アダプターは ACK を受信せず、`playback_completed` を呼ばない。
生成や SDK 送信が終わっても実再生完了とせず、response は後続の取消や新回答で失効できる。
別担当のブラウザ ACK モジュールと Backend ACK 契約を接続するまで、実再生完了経路は未実装となる。

## 合成試験と実接続受入

通常の確認では偽 SDK と合成 PCM/WAV を使い、マイク、GPU、LiveKit サーバーへ接続しない。

```sh
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync python tools/check_docs.py
```

特に、接続途中の失敗、参加者・track の不一致、旧 grant、出力準備の不一致・未確認・期限切れ、
送信中の取消、SDK 処理の遅着、
切断中の frame、source / stream の close と track の unpublish を確認する。
合成試験の成功は、実通信、音声品質、ブラウザの実出力を確認した証拠にしない。

実接続には、ホストによる参加者認証、受信統計と PCM 欠落検査の実通信受入、5 秒以内の入力 ACK、
mute/focus/text/reconnect の device gate、echoCancellation/noiseSuppression、
出力 track の購読・再生準備完了をホスト API へ伝える経路、実再生 ACK が必要。
これらを揃えた後、親作業者と既存 endpoint・利用時間・実マイク受入条件を確認する。
Ubuntu-dogfood の共有サービスや GPU の設定を変更せず、既存承認のない実接続を合成試験に混ぜない。

[Ubuntu での合成検証結果](evidence/2026-10-05-livekit-adapter.md)を別途記録する。
