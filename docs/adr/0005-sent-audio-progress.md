# ADR 0005: BE送出台帳による音声進捗と出力終了の推定

状態: 採用（取得APIとSessionの出力中判定。会話履歴更新は対象外）

## 既存ADRとの優先関係

本ADRは、LiveKit adapterへ接続したSessionの出力中判定について次の範囲を更新する。
それ以外の認証、所有、入力世代、実再生ACK、ブラウザ描画の契約は維持する。

| 既存ADR | 本ADRが更新する範囲 |
| --- | --- |
| [ADR 0002](0002-playback-ack.md) | 実再生ACKによる`playback_completed()`に加え、別名の推定終了経路でactiveを閉じる。閉じたresponseの旧ACKは失効する。実再生ACKの受付条件や証拠を変更せず、経過時間からACKを作らない |
| [ADR 0003](0003-livekit-adapter.md) | BE送出と時間による推定を採用しないという判断を、本台帳とSession出力終了に限って置き換える。既存100ms queue、output-ready gate、RTP送信、取消順序は維持する |
| [ADR 0004](0004-browser-playback-bridge.md) | bridgeの実再生確認契約を維持しつつ、推定終了後はactive失効により旧ACK・完了通知を受け付けない。推定をbridgeの実測証拠やPCM/RTP対応付けに使わない |

## 背景

[LiveKit adapter](0003-livekit-adapter.md)の`SegmentSent`は、ブロック全体をSDKへ渡した
論理sample範囲を通知する。ブロック途中での割込みや取消について、渡せた部分範囲と
その時刻を取得する経路は別に必要となる。

旧PoCには[BE送出位置による推定ADR](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/voice-playback-estimation-speech-services-2026-09.md)と、
[capture完了時刻台帳](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/livekit_transport/paced_audio.py#L53)、
[取消時のprefix取得](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/backend/app/livekit_transport/response_audio.py#L193)がある。
ただし旧PoCはSDK queue 0msと独自pacerを使い、推定を履歴の正本へ採用していた。
現在の100ms queueと[実再生ACK契約](0002-playback-ack.md)へ、その前提を暗黙に持ち込まない。

## 決定

1. `sent_audio_progress(response_id, at_ns=None)`で、当該応答の送出範囲と時間による推定を取得する。
   PCMや本文は複製保存せず、response ID、当該出力作成時generation、track SID、
   0始まりaudio sequence、sample範囲、BE単調時計の記述子を保持する。
   track公開に成功して束縛できた最新の応答と直前の応答を対象とし、未公開・未知・
   保持対象外のresponse IDには`None`を返す。
2. SDKのframe書込みが正常完了した地点を送出記録の根拠とする。
   TTS完了、queue投入、metadata通知を送出成功と見なさない。
3. 判定時計から設定した下り遅延（既定300ms）と既存SDK queueの100msを差し引く。
   frame推定終了は`max(capture成功時刻, 前frame推定終了) + frame音声時間`とし、
   captureの瞬間的な連続成功や供給空白で進捗を水増ししない。
4. 完全な連続ブロックprefixと部分ブロック範囲を分ける。
   推定結果には`basis="sdk_submitted_elapsed"`と`real_playback_confirmed=False`を付ける。
5. 取消・割込み・失効の時刻で台帳を固定し、後の経過時間や遅着captureで旧応答を進めない。
   容量超過や時計逆行は既存transportの出力失敗として停止・資源回収へ進める。
   既存記録を維持し、容量を超えた範囲を推測で埋めない。
6. 取得先を接続したSessionでは、生成完了、全enqueueブロックのSDK書込み成功、
   全ブロックの推定終了を照合して、現在の出力中状態を終了する。
   空応答は生成完了とenqueue 0件を確認し、音声なしとして終了する。
   取得だけでは状態を変えず、単一の所有timerと発話開始時の期限再確認から同じguardを通す。
7. 通常の推定終了と取消・割込み時の固定した台帳は、`last_output_estimate`へ1件のmetadataとして保持する。
   Sessionの入力generationを通常の推定終了では進めず、発話開始時のoverlapを以後書き換えない。
8. 推定終了・停止は`output_estimated_completed`・`output_estimated_stopped`として通知し、
   実再生ACKや`playback_completed`を合成しない。`SegmentSent`とACKの受付検証は維持する。
   推定終了でactiveが閉じた後の旧responseのACKは失効する。Core履歴や本文prefixの保存は追加しない。

詳細な利用契約と推定式は[送出台帳の取得](../sent-audio-progress.md)を正とする。

## 制約

capture成功はSDK受理の事実であり、FE到達やブラウザ出力の証明ではない。
固定下り遅延は仮定であり、ネットワーク損失、PLC、再生停止、端末muteを推定から検出できない。
推定値と実際の出力の差は別の観測課題として残る。

RTP音声transport、既存SDK依存、provider設定を変更せず、PCM別配送、認証発行、
実接続、配備は行わない。推定を会話履歴へ利用する場合は、本文との対応と保存の契約を
別に定める。Sessionの出力中判定が終了したことを、実ブラウザ出力の確認とは扱わない。
