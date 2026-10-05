# ブラウザ描画と個別ACKの接続

このローカル統合は、[純粋ACKコントローラー](../browser/README.md)と
[PR2の再生履歴契約](adr/0002-playback-ack.md)を接続します。
識別済みPCMをAudioWorkletへ渡す経路が対象です。LiveKitのRTP音声をこのPCMへ
変換する機能、認証済みRPCのネットワークendpoint、画面UIは含みません。
[提案ADR](adr/0004-browser-playback-bridge.md)にこの境界を記録しています。

## 再生証拠

`pcm-renderer-worklet.mjs`は、識別済みのmono PCMを実際に`outputs`へコピーした
サンプルだけを数えます。元PCMに含まれる無音は数え、underflowを埋めるゼロは数えません。
`pcm-renderer.mjs`は、全PCMのコピー完了に加え、running中のAudioContextの
`getOutputTimestamp().contextTime`が最終サンプルの出力時刻へ到達してから通知します。
送出メタデータ、受信パケット数、wall clock、`currentTime`だけではACKを作りません。

初版は`sampleRate === audioContext.sampleRate`を必須にします。RTPではcodec遅延、
損失補完、速度調整、途中subscribe等により元PCMとの対応が失われるため、
`SegmentSent.sample_start/end`と受信PCM数を直接対応付けてはいけません。
出力時計はブラウザが報告する装置位置です。物理スピーカーの可聴性を保証するものではありません。

## 構成API

| ファイル | 境界 |
| --- | --- |
| `browser/pcm-renderer.mjs` | 既知PCM、AudioWorklet、出力時計、node/port/timer解放 |
| `browser/playback-ack-bridge.mjs` | ローカルscope、連続metadata、個別ACK、backend受理待ち、再試行 |
| `src/local_gpt_live/playback_ack_transport.py` | 認証済みhostからのJSON検証、binding失効、既存VoiceSession呼び出し |

`createPlaybackAckBridge({send, onInvalidate, ackTimeoutMs=1000, maxPendingSegments=64})`
は次の操作を返します。

| 操作 | 入力と結果 |
| --- | --- |
| `startResponse` | `{responseId,binding,trackSid}`から凍結scopeを返す。同IDでも別generation |
| `registerSegment` | `{scope,audioSequence,sampleRate,sampleStart,sampleEnd}`。0から連続、sampleStartも0から連続 |
| `recordRendered` | `{scope,audioSequence,renderedSampleCount}`。rendererの実測累積値のみ |
| `finish` | `{scope,finalAudioSequence}`。空応答は-1。生成完了と全metadata受領後に呼ぶ |
| `retry` | ACKまたは完了通知の失敗を明示再試行。自動再送しない |
| `cancel/reconnect/close` | scope・保留証拠・送信待ちを失効。close後は再開始不可 |

各async操作は`Promise<boolean>`を返し、送信拒否や期限超過はrejectします。
`finish`は全区間のACK受理前ならfalseで保留し、最後のACK受理後に完了通知を送ります。
ACK受理だけではSessionを完了させません。完了通知の失敗も`retry()`が必要です。
scopeは同一参照を使います。値のコピー、旧scope、描画先着は拒否します。
確認済みsequenceの重複は履歴を保持せずno-opにします。

## hostとの接続例

以下の`rpc`、`revokeLocalScope`、`reportTransportError`はhostが実装する境界です。
`onRendered`は**同期observer**です。ACK送信Promiseをreturnせず、rejectを明示処理します。
これにより一時的なRPC失敗でもcontroller内の実測証拠を保持し、`retry()`で再送できます。

```js
import { createPlaybackAckBridge } from './browser/playback-ack-bridge.mjs';
import { createPcmRenderer } from './browser/pcm-renderer.mjs';

let renderer;
const bridge = createPlaybackAckBridge({
  send: (wire, { signal }) => rpc.requestPlaybackReceipt(wire, { signal }),
  onInvalidate(scope) {
    renderer?.cancel();
    revokeLocalScope(scope); // host接続の旧scopeを同期失効
  },
});
renderer = await createPcmRenderer({
  audioContext,
  onRendered(event) {
    void bridge.recordRendered(event).catch(reportTransportError);
  },
  onError(error) {
    bridge.cancel();
    reportTransportError(error);
  },
});

// descriptorは認証済みhostが発行する。bindingは応答ごとに新規発行する。
const scope = bridge.startResponse(descriptor);
renderer.startResponse(scope);
// 各metadata/PCM組を順番に処理する。PCMはこの区間に属する既知のFloat32Array。
try {
  if (!await bridge.registerSegment({ scope, ...metadata })) throw new Error('invalid metadata');
  if (pcm.length !== metadata.sampleEnd - metadata.sampleStart) throw new Error('PCM mismatch');
  renderer.enqueue({ scope, audioSequence: metadata.audioSequence, pcm,
    sampleRate: metadata.sampleRate });
} catch (error) {
  bridge.cancel(); // metadataとPCMの片方だけが進んだ応答を継続しない
  throw error;
}
// hostの生成完了通知で、最終metadataを確認してから呼ぶ。
await bridge.finish({ scope, finalAudioSequence: lastSequence });
// transport失敗の処理後に明示操作: await bridge.retry();
// 切断時: bridge.reconnect(); 最終終了時: bridge.close(); renderer.close();
```

rendererはAudioContextを所有しません。hostはユーザー操作からresumeし、最終終了時に
自身が所有するAudioContextをcloseしてください。マイク取得は不要です。
rendererへ渡したPCMはコピーされるため、呼出側のbuffer再利用に影響されません。
同じscopeの再使用は不可です。rendererの同期observerへPromiseを返すと契約違反で閉鎖します。

## ワイヤと受信側

UTF-8 JSONのexact field集合を採用します。`generation`はブラウザ内の値で、ワイヤへ出しません。

```json
{"v":1,"type":"playback_ack","binding":"server-issued-opaque-id","response_id":"response-id","audio_sequence":0}
```

```json
{"v":1,"type":"playback_complete","binding":"server-issued-opaque-id","response_id":"response-id","final_audio_sequence":0}
```

hostは接続ごとに`PlaybackAckTransport(session, participant_identity, participant_sid)`を作り、
応答開始時に一度`bind(response_id)`してbindingを発行します。認証済みRPC contextから得た
identity/SIDを`receive(payload: bytes, participant_identity=..., participant_sid=...)`へ渡します。
payloadに含まれる自己申告identityは使いません。`receive`のboolがブラウザの`send`結果です。
SDKがdata packetを送出できたというboolで代替してはいけません。

LiveKitのhost callback `SegmentSent`の各値を`record_segment`へ登録します。
これ自体ではACKを作りません。区間は固定track/rateで0から連続である必要があります。
受信側は既存`Playback`の配送済み範囲・連続ACK規則も検査します。
受理済みACKの重複はactive応答内だけ許可し、完了receiptは最後の1件だけ保持します。

wireは2048 bytes以内、識別子はUTF-8で256 bytes以内、整数はJavaScript safe integer以内です。
unknown field、重複JSON key、bool/floatの整数偽装、NaN、古いbinding、異なるSIDを拒否します。
このbindingは認証資格情報ではなく、host内で接続と応答に束縛された失効可能な識別子です。

取消・切断のhost処理は、**先に`receiver.invalidate()`、続いてSessionの取消**とします。
応答切替も旧scopeの失効を完了してから新しい`bind()`を行います。新binding発行後に旧scopeの
callbackが来る構成では、callbackのbindingがhostの現行bindingに一致するときだけ失効させます。
旧scopeのcallbackで新bindingを無条件に失効させないでください。
ブラウザ側取消だけでは既送出RPCの副作用を取り消せません。ネットワークhostは取消通知、
server側binding失効、新binding発行の順序を実装する必要があります。このendpointは未実装です。

## 有界性と回復

- bridgeの未ACK区間は既定64件。先の欠番や巨大sequenceは枠を使う前に拒否します。
  上限超過は応答を失効させてRangeErrorとなり、新bindingのsequence 0から回復できます。
- rendererは既定2秒分のPCMかつ64区間まで保持します。実測済み通知後に解放します。
  PCM投入の上限・形式エラーは呼出側がcatchし、bridgeをcancelしてください。
- 応答切替時は旧nodeを同期disconnectし、portとpoll timerを閉じます。旧通知は新scopeへ入りません。
- RPCの既定期限は1000ms。期限超過でAbortSignalを中断し、実測証拠は明示retry用に保持します。
- 実送信Promiseはbridge全体で1件までです。transportがAbortSignalを無視して残留している間は
  `TransportBusyError`で新送信を拒否します。元Promiseがsettleした後、明示retryで回復します。
  恒久停止したtransportは接続の所有者が終了させる必要があります。

## 合成検証

```sh
uv sync --frozen --extra livekit
uv run --no-sync pytest -q
node --test browser/tests/playback-ack.test.mjs
node --test browser/integration/playback-ack-bridge.test.mjs browser/integration/playback-ack-host.test.mjs
```

実Chromiumの補助検証は[証拠記録](evidence/2026-10-05-browser-playback-bridge.md)を参照してください。
Python host fixtureはstdin/stdoutだけを使い、実VoiceSessionへ合成WAVを流します。
Room接続、JWT発行、マイク、GPU、共有サービス、常駐serverは使用しません。
