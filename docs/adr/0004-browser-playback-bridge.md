# ADR 0004: 識別済みPCMのブラウザ描画と個別ACKの受信境界

状態: 提案（ローカル統合、ネットワークhostとLiveKit RTP対応付けは未実装）

## 背景

[ADR 0002](0002-playback-ack.md)は配送と再生ACKを分離します。
[ADR 0003](0003-livekit-adapter.md)のSegmentSentは送出PCM範囲を通知しますが、
RTP受信PCMと元PCMの対応を定義していません。ブラウザの純粋controllerは
実測描画数を受け取るだけで、描画やネットワークを所有しません。

## 提案

既知segmentのPCMを直接AudioWorklet出力へコピーし、出力時計通過後に通知する小さなrenderer、
controllerとhost RPCをつなぐbridge、既存VoiceSessionへ個別ACKを渡す受信クラスを追加します。
実装・ワイヤ・ライフサイクルは[接続手順](../browser-playback-bridge.md)を正とします。

wireはv1の`playback_ack`と`playback_complete`を分けます。host発行bindingは応答開始ごとに回転し、
認証済みidentity/SID、Session世代、trackメタデータへ束縛します。ブラウザgenerationは送信しません。
ACKはbackend受理で確定し、完了通知は生成完了かつ全区間ACK後だけ受理します。
失敗は明示再試行とし、取消はrendererとhostの受付をそれぞれ失効させます。

## 制約と後続

既存core、LiveKit SDK、純粋controllerの契約を変更しません。sample rate変換、RTPの元PCM区間対応、
認証済みRPC endpointは追加契約が必要です。対応根拠がないRTP受信量からACKを生成しません。
既存RTP受信音声を鳴らしながら本rendererでも同音声を鳴らす二重出力は禁止します。

headlessの合成検証はsoftware renderの証拠です。音声装置の可聴受入やLiveKit経由ACKの本番受入とは
区別します。未公開browser依存を含む間はローカル専用とし、公開は依存所有者の公開後に調整します。
