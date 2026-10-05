# 再生 ACK コントローラー

`playback-ack.mjs` は、呼び出し元が計測したセグメント単位の描画サンプル数から ACK を決める、外部依存のない ES モジュールです。音声の再生・計測・送信は行いません。

```js
import { createPlaybackAckController } from './playback-ack.mjs';

const acknowledgements = [];
const controller = createPlaybackAckController({
  onAck: ({ responseId, audioSequence, generation }) => {
    acknowledgements.push({ responseId, audioSequence, generation });
  },
  maxPendingSegments: 64,
});

const generation = controller.startResponse('response-1');
await controller.registerSegment({
  responseId: 'response-1', audioSequence: 0, generation, pcmSampleCount: 480,
});
await controller.recordRendered({
  responseId: 'response-1', audioSequence: 0, generation, renderedSampleCount: 480,
});
```

## 公開 API

`createPlaybackAckController({ onAck, maxPendingSegments = 64 })` は独立した controller を返します。`onAck` は必須の関数で、同期処理でも Promise を返す処理でも構いません。`maxPendingSegments` は正の安全な整数です。不正な設定は生成時に例外となります。

| 操作 | 契約 |
| --- | --- |
| `startResponse(responseId)` | 文字列の応答 ID を開始し、非負の安全な整数である新しい `generation` を返します。以前の応答は同じ ID でも失効します。閉鎖後の呼び出しは例外です。 |
| `registerSegment(metadata)` | メタデータを登録し、`Promise<boolean>` を返します。有効な現在世代の入力なら `true`、不正値・識別子不一致・競合するメタデータなら `false` です。 |
| `recordRendered(rendered)` | 計測済みの累積描画数を受け取り、`Promise<boolean>` を返します。有効な現在世代の入力なら `true`、不正値・識別子不一致・既知のメタデータを超える描画数なら `false` です。 |
| `retry()` | 失敗した ACK がある場合だけ、その sequence から再試行する `Promise<boolean>` を返します。再試行を開始した場合は `true`、失敗待ちがない場合や送信中は `false` です。 |
| `cancel()`・`reconnect()` | 現在の世代と保留証拠を失効させます。続けるには `startResponse()` で新しい世代を開始します。 |
| `close()` | 現在の世代を失効させ、controller を閉じます。以後の証拠は受け付けません。 |

入力操作が ACK を開始した場合、返す Promise はその送信結果を待ちます。`onAck` の throw または Promise の reject は呼び出し元へ伝わり、失敗した ACK は未確定のまま残ります。送信中に受け取った別の入力は、送信の完了を待たず `true` で解決する場合があります。

## 入力と ACK

| 名前 | 必須フィールド |
| --- | --- |
| `SegmentMetadata` | `{ responseId: string, audioSequence: 非負の安全な整数, generation: 非負の安全な整数, pcmSampleCount: 正の安全な整数 }` |
| `RenderedSegment` | `{ responseId: string, audioSequence: 非負の安全な整数, generation: 非負の安全な整数, renderedSampleCount: 非負の安全な整数 }` |
| `onAck` の引数 | `{ responseId, audioSequence, generation }` |

`renderedSampleCount` はそのセグメントで計測された累積サンプル数です。差分ではないため、重複した値を足し合わせません。現在世代のメタデータが登録され、描画数が `pcmSampleCount` と一致したセグメントだけを完了とします。描画がメタデータより先に届いても保留できます。sequence 0 から欠番なく完了したものを、ACK 0、ACK 1 のように個別のコールバックで順番に通知します。

メタデータの受領、SDK や queue への配送、生成の終了、経過時間、無音は描画の証拠ではありません。現在の response ID または generation と異なる入力も ACK に使いません。確定済み sequence の再入力は無視し、ACK を再発行しません。

## 失効、再試行、保留上限

`cancel()`、`reconnect()`、新しい `startResponse()`、`close()` は旧世代の保留証拠を破棄します。旧世代のコールバックが後で成功・失敗しても、新世代の確定位置や失敗状態は変わりません。ただし、すでに呼び出した外部コールバックの副作用は取り消せません。世代を再開するときは、同じ response ID を使う場合も、新しく返された generation を両入力へ付けてください。

ACK コールバックの失敗後は自動再送しません。後続 sequence は待機し、`retry()` が失敗した sequence を先に送ります。送信中の `retry()` は別のコールバックを並列に開始しません。

異なる未 ACK sequence の保留項目数は `maxPendingSegments` 以下に制限されます。メタデータ先着、描画先着、送信中、失敗中の項目をすべて数えます。上限を超える新規項目は保存前に `RangeError` で拒否します。既存項目の重複入力と失敗 ACK の `retry()` は新しい枠を使いません。ACK 成功または世代失効で枠が解放されます。上限に達した呼び出し元は、新規証拠の投入を止め、ACK 成功または世代の切替えを待ってから再投入してください。成功済みセグメントの証拠履歴は保持しません。

## 合成テスト

```sh
node --check browser/playback-ack.mjs
node --test browser/tests/playback-ack.test.mjs
```

テストは合成イベントとコールバックだけを使います。実ブラウザ、実音声、LiveKit との連携を検証するものではありません。
