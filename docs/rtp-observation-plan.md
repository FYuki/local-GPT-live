# RTPを維持する元PCM区間・ブラウザ出力の測定計画

状態: オフライン観測準備。RTP区間対応と実ブラウザ受入は未確認。

LiveKitの音声トラックを維持し、元PCM区間からブラウザ出力区間までの対応根拠を測る。
PCMを別経路で配送して音声トラックを置き換える変更は、この計画に含めない。
[ADR 0003](adr/0003-livekit-adapter.md)の送出境界と
[ADR 0004](adr/0004-browser-playback-bridge.md)の識別済みPCM描画境界の間を調査する。
推定位置を[ADR 0002](adr/0002-playback-ack.md)の確定ACKへ昇格させない。

## 現時点の根拠と不足

固定PoC `fce7382884d981c42be7fbd3ddaffe7469e27588` の
[観測実装](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/frontend/src/livekit/room-audio-graph.ts)
には、受信Opus packetの復号、区間を識別したWorklet出力、出力時計の照合がある。
ただし[受入記録](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/validation/issue-3-acceptance-20260929.md)は
`source_pcm_offset_verified=false`、`production_playback_verified=false` のままである。
総入力sample数とmetadata総数の一致から元PCMのprefixを推定する経路は移植しない。

本repoの[合成RTC実接続](evidence/2026-10-05-livekit-rtc.md)は受信PCMと取消・解放まで、
[ブラウザ合成検証](evidence/2026-10-05-browser-playback-bridge.md)は既知PCMのsoftware renderまでの証拠である。
両方が成功しても、その間の元PCM対応は埋まらない。

| 境界 | 今得られる値 | 確定ACKまでに足りない値・根拠 |
| --- | --- | --- |
| host → SDK | `SegmentSent`のresponse、sequence、track SID、rate、半開区間`[sample_start, sample_end)` | 元PCM原点と送出RTP timestampの対応。callbackは配送情報のみ |
| Python SDK → native | `AudioFrame`のrate、channel数、sample数。`capture_frame`の完了 | encoder入力区間、lookahead、padding/trim、送出packetへの帰属 |
| RTP受信統計 | 実装が提供するSSRC、codec、packet・sampleの累積値、損失補完・速度調整等 | 各packetのsequence/timestampと復号区間。統計収集時刻はRTP timestampではない |
| 受信PCM | frameごとのrate、channel数、sample数 | 元PCM区間、PLC/FEC/再標本化/速度調整の由来と区間対応 |
| ブラウザ出力 | 既知PCM用rendererにはWorklet出力数とnative出力時計の観測がある | RTP復号PCMに同じ帰属を保ったまま適用できるか。別decoderの結果だけでは現在の出力を証明しない |

固定SDKは`livekit==1.1.16`。インストール済みの`AudioFrame._proto_info()`は
buffer pointer、rate、channel数、sample数をnativeへ渡し、`userdata`を渡さない。
`userdata`に区間IDを付けてもRTP受信側へ運ばれる契約にはならない。
公式の[AudioFrame](https://docs.livekit.io/reference/python/livekit/rtc/audio_frame.html)と
[AudioSource](https://docs.livekit.io/reference/python/livekit/rtc/audio_source.html)も参照するが、
公開Web文書の更新と固定版の差は実インストール版で確認する。
SDK queue、`capture_frame`、`wait_for_playout`の完了は、遠隔ブラウザの出力証拠にしない。

## 小さなオフラインハーネス

`tools/rtp_observation.py`は、既存`SegmentSent`と公式SDKの`AudioFrame`、
`InboundRtpStreamStats` protobufを合成fixtureとして並べ、観測境界を確認する。
Room、音声デバイス、token、推論サービスは使わない。

```sh
uv sync --frozen --extra livekit
uv run --no-sync python tools/rtp_observation.py
```

出力は`mode: offline_synthetic`、`numeric_values_are_fixtures: true`、
`connection_attempted: false`を明記する。
実SDKから調べたversion・schemaと、投入した数値fixtureを分離する。
protobufのfield名は確認するが、buffer pointerの値、PCM本文、資格情報は出力しない。
受信統計の未設定値をゼロへ補完しない。統計側のcodec・sample時計は未観測であり、
fixtureのraw counter値と受信PCMのsample数を同一単位として突合しない。
Python SDKの統計timestampの単位・原点を
固定版で確認するまでは、ブラウザの`DOMHighResTimeStamp`やAudioContext時計と直結しない。

すべてのcaseで`source_pcm_offset_verified=false`、`ack_eligible=false`、
`browser_output_clock_observed=false`を保持する。
正常に見える連続fixtureでも、このハーネスからACKを生成・送信しない。
`continuous`、`equal_count_substitution`、`cancel_then_late_frame`、
`receiver_rate_change`の4例で、同数でも損失補完で内容が置換された場合、取消後の遅着、
受信rateの相違を模す。「数が合う」「統計が進む」と区間帰属を区別する。
PCMとprotobufの数値を合成するだけで、encode/decode、PLC、resamplingは実行しない。
これは観測値の扱いの検査であり、ネットワーク、codec、ブラウザの実測ではない。

## 次の測定を小さく進める順序

1. **送出原点を調べる。** 固定SDK/native実装のどこで元PCM区間からencoder入力、
   RTP packetへ対応を引けるか確認する。rate変換とencoder lookahead、開始・末尾のpadding/trimを
   実装・版とともに記録する。公開APIで得られなければ、その不足を結論として残す。
   native拡張が必要なら、変更案を別途レビューしてから進める。metadataだけで不足を補わない。
2. **認可済みRTCでpacketまで測る。** 親作業者が確認した既存認証で専用の合成試行を行う。
   既知PCMの区間境界と送出・受信packetのSSRC、sequence、RTP timestamp、codec/rateを照合する。
   SFU等による識別値の書換えがあれば、その対応も必要になる。同じ数値を前提にしない。
   marker音や相互相関はずれの調査に使えるが、lossy codec後の似た波形を厳密な区間IDとして扱わない。
   送出原点が未観測なら、後段が成功しても元PCM対応は未確認とする。
3. **同じRTP音声の出力まで追う。** 元packetから復号PCM、出力区間への対応を保ち、
   実際の再生経路上でWorklet出力区間とnative `getOutputTimestamp()`を照合する。
   独立decoderの観測は補助証拠とし、既存再生との同一性を仮定しない。
   観測用の二重再生は作らない。出力経路・SDKの変更が必要なら別の設計差分として示す。
4. **取消と欠落で崩す。** 下表の条件を個別試行し、対応できない区間は未確認のまま保つ。
   測定成功とACKを有効にする判断を分け、ACK接続は根拠と失効規則をレビューした後の別変更とする。

各試行にはcommit、SDK/native/browser版、合成fixture名、stream世代、source/decode/contextのrate、
観測点ごとの有無と時刻の単位・原点を記録する。秘密値や音声本文は保存しない。
許容ずれは「何sampleをどの時計で比較したか」とともに先に定義する。
観測できないずれを固定遅延や平均値で埋めない。

## 測定条件と失効条件

| 条件 | 確認する対応・判断 |
| --- | --- |
| 開始・末尾、無音、短い最終frame | encoder lookahead、padding、trim、DTXによる区間の欠落・拡張。元PCMの無音と補完無音を区別 |
| rate・時計の相違 | source / decoder / AudioContextのrateを分離。再標本化の位相・遅延・丸めと元区間対応を確認 |
| packet欠落、遅着、順序逆転、重複 | 受信順とmedia順を分離し、PLC/FECと元packet由来を識別。累積counterから影響区間を逆算しない |
| sequence / timestampのwrap、SSRC交代 | 生値と拡張値、stream世代を保持。曖昧なwrapや再接続を旧区間へ接続しない |
| jitter buffer・速度調整 | sample挿入・除去がどの元区間を変えたか。集計値だけなら対応未確認 |
| 途中subscribe・track切替 | 最初に届いたframeを元PCMの0として採番しない。未受信prefixを確認済みにしない |
| Worklet underflow、context suspend | 埋めたゼロを元PCMとして数えない。running状態と当該出力区間の時計通過を確認 |
| 取消・切断と遅着 | local出力停止、server binding失効、遅着frame・ACKの拒否を別々に記録。旧世代を次responseへ流用しない |

OpusのRTP時計は入力rateと独立した48 kHzである。
sequenceは16 bit、RTP timestampは32 bitで、原点とwrapの扱いが必要になる。
仕様根拠は[RFC 7587 §4.1](https://www.rfc-editor.org/rfc/rfc7587.html#section-4.1)と
[RFC 3550 §5.1](https://www.rfc-editor.org/rfc/rfc3550.html#section-5.1)。
比率が分かることと、実装上の元PCM原点が分かることは別である。

[WebRTC Stats](https://www.w3.org/TR/webrtc-stats/#dom-rtcinboundrtpstreamstats-concealedsamples)では
`totalSamplesReceived`に`concealedSamples`が含まれ、速度調整の挿入・除去も別に集計する。
補完数を総数から引いても、欠けた元区間の位置は復元できない。
[Web Audioの出力時計](https://www.w3.org/TR/webaudio/#dom-audiocontext-getoutputtimestamp)は
AudioContextと出力側の位置を対応付けるものであり、元PCMの識別情報を生成しない。

## 受入の判定と残る判断

オフライン段階の受入は、実SDKの観測範囲と不足を再現でき、すべてのfixtureでACKを無効のまま
保つこととする。これはRTP再生対応の受入ではない。

後続の区間受入には、同じresponse・track・接続世代について、元PCM区間からpacket、復号区間、
実際のWorklet出力区間、native出力時計までの対応根拠をそろえる。
欠測・曖昧さ・失効があれば、その区間を確定しない。前の未確認区間を飛び越してprefixを進めない。
対応が確認できても、認証済みhostのbindingとACK受理の接続は別の検証を必要とする。

目標はブラウザが報告する出力区間の根拠である。物理スピーカーの可聴性や人が聞いたこと、
改変されたclientが虚偽ACKを返さないことは保証対象外とする。

本sliceでは新規token発行、grant追加、Room接続、共有サービス変更を行わない。
実接続を始める前に親作業者へ既存認証の利用範囲を確認し、追加tokenが必要なら判断を求める。
音声トラックをPCM配送へ置き換える案は未承認のまま維持する。
