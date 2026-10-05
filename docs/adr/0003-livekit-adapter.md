# ADR 0003: LiveKit SDK と音声境界を接続する最小アダプター

状態: Proposed。判断の範囲は既存の Python API と RTC SDK の接続であり、ブラウザ会話の実接続受入を意味しない。

## 背景と根拠

[ADR 0001](0001-voice-boundary.md) は入力の世代管理、既存 VAD、取消、出力失効を本 repo が所有し、
LiveKit の認証、track の照合、ブラウザの実再生観測を独立した統合対象としている。
`AudioInput`、`VoiceSession`、`Playback` は存在するが、ネットワークの制御メッセージ形式は定義していない。

固定 PoC の [response_audio.py](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/livekit_transport/response_audio.py)
は response ごとに `AudioSource` と `LocalAudioTrack` を所有し、
`ds-response-v1:<response_id>` という track 名で対応を識別する。
[microphone_frames.py](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/livekit_transport/microphone_frames.py)
は SDK の受信 queue に入る前の sample 位置を保持し、ローカル queue の欠落を検出する。
この所有関係と sample 時計の考え方を小さな接続層へ移す。

PoC の送出完了と固定遅延からの再生推定は採用しない。
別作業の [再生 ACK 境界案](https://github.com/FYuki/local-GPT-live/blob/2459c9b/docs/adr/0002-playback-ack.md)
も、SDK への送信や時間経過を実再生の根拠にしていない。

## 決定

### 依存と所有

- 公式 Python RTC SDK `livekit==1.1.16` を任意依存 `livekit` に固定し、既存の uv lock で解決する。
  LiveKit Agents へ音声判断を置き換えない。
- `LiveKitConfig` は既存 endpoint、ホストが取得済みの token、期待する participant identity と SID を受け取る。
  token の発行、Room 管理 API、クラウド契約、公開設定の変更は担当しない。
- `LiveKitTransport` は `AudioInput` と設定を受け取り、自身が接続した RTC Room、音声 reader、
  自身が作成した出力 track/source と task を所有する。共有 GPU/STT/TTS サービスを操作しない。
- `LiveKitPlayback` は既存 `Playback` を拡張し、response の失効を SDK 出力の停止にも反映する。
  provider と `VoiceSession` の Python 契約を維持する。

### 入力の境界

- 入力開始はホストが明示的に呼ぶ `open_input(track_sid, request_id, revision)` のみ。
  remote data message を解析して入力認可へ変換する入口は作らない。
- 期待する participant の identity と SID、およびその microphone publication の track SID を照合する。
  同じ identity でも新しい SID を暗黙に認可しない。
- 正式 grant と revision の正本は既存 `AudioInput` / `BackendVoiceInput` に残す。
  使用済み track SID の再利用、旧 revision、失効した grant を再認可しない。
- SDK から受け取る PCM は 16 kHz mono little-endian PCM16。
  SDK queue に入る前の sample 時計を既存 pipeline へ渡し、reader 側の採番で欠落を埋めない。
- 入力開始前に有効な Opus 48 kHz の受信統計を最大 4 秒待つ。
  受信 frame は最大 10 個ずつ保留し、統計 timestamp の前進を最大 1 秒確認してから後段へ渡す。
  統計 ID の交代、counter の逆行、欠測、非無音 concealment 増分 80 ms 以上は入力を停止する。
  これは固定 PoC を基にした閾値付き検査であり、あらゆるネットワーク欠落を検出する保証ではない。
- 入力の停止・切断後に遅れて届いた frame や grant を処理しない。
  再接続時に旧 track を自動再開しない。

### 出力の境界

- `AudioPacket` の WAV を検証し、PCM16 mono の 16 / 22.05 / 24 / 44.1 / 48 kHz を受け付ける。
  同じ response の中で sample rate を変更しない。独自の resampler は追加しない。
- response ごとに `ds-response-v1:<response_id>` という出力 track を作成する。
  `AudioSource` には WAV と同じ sample rate を指定し、最大 10 ms の frame と 100 ms の SDK queue を使う。
- 公開後に `TrackPublished(response_id, track_sid)` をホストへ通知する。
  ホストが認証済み受信側の購読・再生準備を確認し、`confirm_output_ready` で当該 response と SID を返すまで PCM を送らない。
  待機上限は `output_ready_timeout`（既定 3 秒）。旧 response や異なる SID の通知で待機を解除しない。
- `SegmentSent(response_id, sequence, track_sid, sample_rate, sample_start, sample_end)` はホスト向けの送信情報。
  sample 範囲は当該 response track 内の 0 始まりで、開始を含み末尾を含まない論理 PCM 範囲とする。
  再生 ACK、永続履歴、ネットワーク wire の定義として扱わない。
- SDK の `capture_frame` 完了、queue が空、生成完了から ACK を合成しない。
  アダプターは `playback_completed` を呼ばない。ACK 待ちの response は取消可能なまま保持する。
- 停止時は出力認可を先に失効させ、source queue の消去と track の mute を行う。
  実行中の SDK 接続・公開・送信 task は所有を維持し、完了応答を受け取る前に取り消さない。
  遅れて返る native handle / publication SID を回収し、旧 response の出力を再開せず queue を再度消す。
  所有する track の unpublish と source の解放は非同期 cleanup で完了させる。
- `close_timeout` は終了処理を待つ上限とする。SDK が未完了なら所有 task を保持し、
  `shutdown_pending` を通知する。待機の打切りをリソース解放の成功として扱わない。
  既存 provider の生成取消は従来の `VoiceSession` が実施する。

## ネットワーク契約を追加しない理由

現行 Python API から認証や実出音の事実は導けない。
今回の契約はホストとアダプターの呼出境界、および RTC track の対応関係に限定する。
既存 PoC の広い data-channel protocol や新しい JSON 形式を推測して公開しない。

後続統合では参加者認証と session の対応、受信統計検査の実通信受入、5 秒以内の入力 ACK、
mute/focus/text/reconnect の device gate、echoCancellation/noiseSuppression、
出力 track の購読・再生準備完了通知をホスト API へ運ぶ経路、response に帰属する出力時計と実再生 ACK を揃える。
RTC track の publish 成功だけでは端末の再生準備を保証しない。ホスト API の準備確認を publish 成功から合成しない。
ブラウザ ACK 純粋モジュールとそのテストは別担当の変更として維持する。

## 検証の境界

合成 WAV、偽の Room / track / stream / source、既存の合成 provider を用いて、
接続失敗、認可照合、旧入力の拒否、出力準備確認の一致・不一致・期限、
停止と送信の競合、切断、二重 close とリソース解放を検証する。
SDK の送信完了を実再生としないことも検査する。
実行結果と未受入条件は [利用手順](../livekit-adapter.md) および検証証跡に記録する。
