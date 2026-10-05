import assert from 'node:assert/strict';
import { setImmediate as nextTurn } from 'node:timers/promises';
import test from 'node:test';

import { createPlaybackAckBridge } from '../playback-ack-bridge.mjs';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function fixture(t, options = {}) {
  const sent = [];
  const invalidated = [];
  const bridge = createPlaybackAckBridge({
    send: async (wire, context) => {
      sent.push({ wire, signal: context.signal });
      return options.send ? options.send(wire, context) : true;
    },
    onInvalidate: (scope) => {
      invalidated.push(scope);
      options.onInvalidate?.(scope);
    },
    ackTimeoutMs: 500,
    ...Object.fromEntries(Object.entries(options).filter(([key]) => !['send', 'onInvalidate'].includes(key))),
  });
  t.after(() => bridge.close());
  const start = (overrides = {}) => bridge.startResponse({
    responseId: 'response-1', binding: 'server-binding-1', trackSid: 'track-1', ...overrides,
  });
  return { bridge, sent, invalidated, start };
}

function metadata(scope, audioSequence = 0, overrides = {}) {
  return {
    scope, audioSequence, sampleRate: 16000,
    sampleStart: audioSequence * 320, sampleEnd: (audioSequence + 1) * 320,
    ...overrides,
  };
}

function rendered(scope, audioSequence = 0, renderedSampleCount = 320) {
  return { scope, audioSequence, renderedSampleCount };
}

function ack(scope, audioSequence) {
  return { v: 1, type: 'playback_ack', binding: scope.binding, response_id: scope.responseId, audio_sequence: audioSequence };
}

function complete(scope, finalAudioSequence) {
  return { v: 1, type: 'playback_complete', binding: scope.binding, response_id: scope.responseId, final_audio_sequence: finalAudioSequence };
}

test('設定不正を生成時に拒否する', () => {
  assert.throws(() => createPlaybackAckBridge(), TypeError);
  assert.throws(() => createPlaybackAckBridge({ send: async () => true }), TypeError);
  assert.throws(() => createPlaybackAckBridge({ onInvalidate() {} }), TypeError);
  for (const maxPendingSegments of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => createPlaybackAckBridge({ send() {}, onInvalidate() {}, maxPendingSegments }), RangeError);
  }
  for (const ackTimeoutMs of [0, -1, NaN, Infinity]) {
    assert.throws(() => createPlaybackAckBridge({ send() {}, onInvalidate() {}, ackTimeoutMs }), RangeError);
  }
});

test('メタデータと生成終了だけでは ACK も再生完了も送らない', async (t) => {
  const { bridge, sent, start } = fixture(t);
  const scope = start();
  assert.ok(Object.isFrozen(scope));
  assert.equal(await bridge.registerSegment(metadata(scope)), true);
  await bridge.finish({ scope, finalAudioSequence: 0 });
  await nextTurn();
  assert.deepEqual(sent, []);
  assert.equal(await bridge.recordRendered(rendered(scope, 0, 319)), true);
  assert.deepEqual(sent, []);
  await bridge.recordRendered(rendered(scope));
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), complete(scope, 0)]);
});

test('累積サンプルを重複加算せず、明示終了までは完了通知しない', async (t) => {
  const { bridge, sent, start } = fixture(t);
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  for (const count of [100, 100, 50, 200, 200, 319]) {
    assert.equal(await bridge.recordRendered(rendered(scope, 0, count)), true);
  }
  assert.deepEqual(sent, []);
  assert.equal(await bridge.recordRendered(rendered(scope)), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0)]);
  await bridge.recordRendered(rendered(scope));
  await bridge.registerSegment(metadata(scope));
  assert.equal(sent.length, 1);
  assert.equal('generation' in sent[0].wire, false);
});

test('後続の描画が先着しても ACK は 0 から順に送る', async (t) => {
  const { bridge, sent, start } = fixture(t);
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await bridge.registerSegment(metadata(scope, 1));
  assert.equal(await bridge.recordRendered(rendered(scope, 1)), true);
  assert.deepEqual(sent, []);
  await bridge.recordRendered(rendered(scope));
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), ack(scope, 1)]);
});

test('バックエンド受理を待ち、ACK 送信中に完了を先行させない', async (t) => {
  const receipt = deferred();
  const { bridge, sent, start } = fixture(t, { send: (wire) => wire.type === 'playback_ack' ? receipt.promise : true });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  const pending = bridge.recordRendered(rendered(scope));
  await nextTurn();
  assert.equal(sent.length, 1);
  assert.ok(sent[0].signal instanceof AbortSignal);
  const finishing = bridge.finish({ scope, finalAudioSequence: 0 });
  assert.equal(await bridge.retry(), false);
  assert.equal(sent.length, 1);
  receipt.resolve(true);
  await Promise.all([pending, finishing]);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), complete(scope, 0)]);
});

test('バックエンド拒否後は自動再送せず、retry で失敗位置と後続を回復する', async (t) => {
  let accepted = false;
  const { bridge, sent, start } = fixture(t, { send: () => accepted });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await bridge.registerSegment(metadata(scope, 1));
  await assert.rejects(bridge.recordRendered(rendered(scope)));
  await bridge.recordRendered(rendered(scope));
  await bridge.recordRendered(rendered(scope, 1));
  await bridge.finish({ scope, finalAudioSequence: 1 });
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0)]);
  accepted = true;
  assert.equal(await bridge.retry(), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), ack(scope, 0), ack(scope, 1), complete(scope, 1)]);
  assert.equal(await bridge.retry(), false);
});

test('送信例外を呼び出し元へ返し、明示 retry まで保持する', async (t) => {
  const failure = new Error('synthetic transport failure');
  let failing = true;
  const { bridge, sent, start } = fixture(t, { send: () => { if (failing) throw failure; return true; } });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await assert.rejects(bridge.recordRendered(rendered(scope)), (error) => error === failure);
  await bridge.recordRendered(rendered(scope));
  assert.equal(sent.length, 1);
  failing = false;
  assert.equal(await bridge.retry(), true);
  assert.equal(sent.length, 2);
});

test('ACK タイムアウトは送信を abort し、遅延成功から進めず明示 retry で回復する', async (t) => {
  const late = deferred();
  let first = true;
  const { bridge, sent, start } = fixture(t, {
    ackTimeoutMs: 25,
    send: () => { if (first) { first = false; return late.promise; } return true; },
  });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await assert.rejects(bridge.recordRendered(rendered(scope)), { name: 'TimeoutError' });
  assert.equal(sent[0].signal.aborted, true);
  late.resolve(true);
  await nextTurn();
  await bridge.recordRendered(rendered(scope));
  assert.equal(sent.length, 1);
  assert.equal(await bridge.retry(), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), ack(scope, 0)]);
});

for (const method of ['cancel', 'reconnect', 'close']) {
  test(`${method} は binding を同期失効し、保留送信と古い描画を無効化する`, async (t) => {
    const late = deferred();
    const { bridge, sent, invalidated, start } = fixture(t, { send: () => late.promise });
    const scope = start();
    await bridge.registerSegment(metadata(scope));
    const pending = bridge.recordRendered(rendered(scope));
    const rejected = assert.rejects(pending, { name: 'AbortError' });
    await nextTurn();
    bridge[method]();
    assert.deepEqual(invalidated, [scope]);
    assert.equal(sent[0].signal.aborted, true);
    await rejected;
    late.resolve(true);
    await nextTurn();
    assert.equal(await bridge.recordRendered(rendered(scope)), false);
    assert.equal(await bridge.registerSegment(metadata(scope)), false);
    assert.equal(await bridge.finish({ scope, finalAudioSequence: 0 }), false);
    assert.equal(await bridge.retry(), false);
    assert.equal(sent.length, 1);
    if (method === 'close') assert.throws(() => start(), /closed/i);
    else assert.ok(start({ binding: 'server-binding-2' }).generation > scope.generation);
  });
}

test('同じ応答 ID の再開でも旧 receipt を新しい binding の進捗に使わない', async (t) => {
  const late = deferred();
  const { bridge, sent, invalidated, start } = fixture(t, {
    send: (wire) => wire.binding === 'server-binding-1' ? late.promise : true,
  });
  const oldScope = start();
  await bridge.registerSegment(metadata(oldScope));
  const pending = bridge.recordRendered(rendered(oldScope));
  const rejected = assert.rejects(pending, { name: 'AbortError' });
  await nextTurn();
  const newScope = start({ binding: 'server-binding-2' });
  assert.ok(newScope.generation > oldScope.generation);
  assert.deepEqual(invalidated, [oldScope]);
  await rejected;
  late.resolve(true);
  await bridge.registerSegment(metadata(newScope));
  assert.equal(await bridge.recordRendered(rendered(oldScope)), false);
  await bridge.recordRendered(rendered(newScope));
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(oldScope, 0), ack(newScope, 0)]);
});

test('同じ値の scope コピーと異なる識別子を受け付けない', async (t) => {
  const { bridge, sent, start } = fixture(t);
  const scope = start();
  for (const other of [{ ...scope }, { ...scope, generation: scope.generation + 1 }, undefined, null]) {
    assert.equal(await bridge.registerSegment(metadata(other)), false);
    assert.equal(await bridge.recordRendered(rendered(other)), false);
    assert.equal(await bridge.finish({ scope: other, finalAudioSequence: -1 }), false);
  }
  await bridge.registerSegment(metadata(scope));
  await bridge.recordRendered(rendered(scope));
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0)]);
});

test('不正な開始入力は現在の世代を失効させない', async (t) => {
  const { bridge, sent, invalidated, start } = fixture(t);
  const scope = start();
  for (const overrides of [{ responseId: '' }, { binding: '' }, { trackSid: '' }, { responseId: 1 }, { binding: null }]) {
    assert.throws(() => start(overrides), TypeError);
  }
  assert.deepEqual(invalidated, []);
  await bridge.registerSegment(metadata(scope));
  await bridge.recordRendered(rendered(scope));
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0)]);
});

test('終了通知は ACK と独立し、重複呼び出しを一件の送信へ集約する', async (t) => {
  const receipt = deferred();
  const { bridge, sent, start } = fixture(t, { send: (wire) => wire.type === 'playback_complete' ? receipt.promise : true });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await bridge.recordRendered(rendered(scope));
  const first = bridge.finish({ scope, finalAudioSequence: 0 });
  const second = bridge.finish({ scope, finalAudioSequence: 0 });
  await nextTurn();
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), complete(scope, 0)]);
  receipt.resolve(true);
  await Promise.all([first, second]);
  assert.equal(await bridge.finish({ scope, finalAudioSequence: 0 }), true);
  assert.equal(sent.length, 2);
});

test('終了通知の失敗も明示 retry を必要とする', async (t) => {
  let accepted = false;
  const { bridge, sent, start } = fixture(t, { send: () => accepted });
  const scope = start();
  await assert.rejects(bridge.finish({ scope, finalAudioSequence: -1 }));
  assert.equal(await bridge.finish({ scope, finalAudioSequence: -1 }), false);
  assert.equal(sent.length, 1);
  accepted = true;
  assert.equal(await bridge.retry(), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [complete(scope, -1), complete(scope, -1)]);
  assert.equal(await bridge.retry(), false);
});

test('空応答は final -1 の完了だけを送り、存在しない ACK を生成しない', async (t) => {
  const { bridge, sent, start } = fixture(t);
  const scope = start();
  assert.equal(await bridge.finish({ scope, finalAudioSequence: 0 }), false);
  assert.equal(await bridge.finish({ scope, finalAudioSequence: -1 }), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [complete(scope, -1)]);
});

test('最終 sequence の不一致や不正数は終了状態にせず、正しい描画を続けられる', async (t) => {
  const { bridge, sent, start } = fixture(t);
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  for (const finalAudioSequence of [-2, -1, 1, true, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(await bridge.finish({ scope, finalAudioSequence }), false);
  }
  await bridge.registerSegment(metadata(scope, 1));
  await bridge.recordRendered(rendered(scope));
  await bridge.recordRendered(rendered(scope, 1));
  assert.equal(await bridge.finish({ scope, finalAudioSequence: 1 }), true);
  assert.equal(await bridge.registerSegment(metadata(scope, 2)), false);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), ack(scope, 1), complete(scope, 1)]);
});

test('sequence の巨大値や欠番で保留枠を消費せず、正常な 0 へ回復できる', async (t) => {
  const { bridge, sent, invalidated, start } = fixture(t, { maxPendingSegments: 1 });
  const scope = start();
  for (const audioSequence of [-1, true, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER + 1, 1]) {
    assert.equal(await bridge.registerSegment(metadata(scope, audioSequence)), false);
    assert.equal(await bridge.recordRendered(rendered(scope, audioSequence)), false);
  }
  assert.deepEqual(invalidated, []);
  assert.equal(await bridge.registerSegment(metadata(scope)), true);
  assert.equal(await bridge.recordRendered(rendered(scope)), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0)]);
});

test('不正なレート、非連続オフセット、超過描画から ACK を作らない', async (t) => {
  const { bridge, sent, start } = fixture(t);
  const scope = start();
  for (const overrides of [
    { sampleRate: 8000 }, { sampleRate: '16000' }, { sampleStart: 1 },
    { sampleStart: false }, { sampleEnd: 0 }, { sampleEnd: 1.5 },
    { sampleEnd: Infinity }, { sampleEnd: Number.MAX_SAFE_INTEGER + 1 },
  ]) assert.equal(await bridge.registerSegment(metadata(scope, 0, overrides)), false);
  assert.equal(await bridge.recordRendered(rendered(scope)), false);
  await bridge.registerSegment(metadata(scope));
  assert.equal(await bridge.registerSegment(metadata(scope, 0, { sampleEnd: 640 })), false);
  assert.equal(await bridge.registerSegment(metadata(scope, 1, { sampleStart: 321 })), false);
  for (const count of [-1, true, 0.5, NaN, Infinity, 321, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(await bridge.recordRendered(rendered(scope, 0, count)), false);
  }
  assert.deepEqual(sent, []);
  await bridge.recordRendered(rendered(scope));
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0)]);
});

test('保留上限超過で世代を失効し、新しい binding の sequence 0 で再開できる', async (t) => {
  const { bridge, sent, invalidated, start } = fixture(t, { maxPendingSegments: 2 });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await bridge.registerSegment(metadata(scope, 1));
  await assert.rejects(bridge.registerSegment(metadata(scope, 2)), RangeError);
  assert.deepEqual(invalidated, [scope]);
  assert.equal(await bridge.recordRendered(rendered(scope)), false);
  assert.deepEqual(sent, []);
  const restarted = start({ binding: 'server-binding-2' });
  await bridge.registerSegment(metadata(restarted));
  await bridge.recordRendered(rendered(restarted));
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(restarted, 0)]);
});

test('ACK 成功で保留枠を解放し、確認済み重複は新しい枠を使わない', async (t) => {
  const { bridge, sent, invalidated, start } = fixture(t, { maxPendingSegments: 1 });
  const scope = start();
  for (let sequence = 0; sequence < 12; sequence += 1) {
    await bridge.registerSegment(metadata(scope, sequence));
    await bridge.recordRendered(rendered(scope, sequence));
    assert.equal(await bridge.registerSegment(metadata(scope, 0)), true);
    assert.equal(await bridge.recordRendered(rendered(scope)), true);
  }
  assert.deepEqual(invalidated, []);
  assert.deepEqual(sent.map(({ wire }) => wire.audio_sequence), Array.from({ length: 12 }, (_, i) => i));
});

test('失敗 ACK が占める保留枠も上限へ数え、取消後は再利用できる', async (t) => {
  let accepted = false;
  const { bridge, invalidated, start } = fixture(t, { maxPendingSegments: 1, send: () => accepted });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await assert.rejects(bridge.recordRendered(rendered(scope)));
  await assert.rejects(bridge.registerSegment(metadata(scope, 1)), RangeError);
  assert.deepEqual(invalidated, [scope]);
  accepted = true;
  const restarted = start({ binding: 'server-binding-2' });
  await bridge.registerSegment(metadata(restarted));
  assert.equal(await bridge.recordRendered(rendered(restarted)), true);
});

test('abort を無視する未完了送信は一件に制限し、終了後に新世代を回復する', async (t) => {
  const late = deferred();
  const { bridge, sent, start } = fixture(t, {
    ackTimeoutMs: 25,
    send: (wire) => wire.binding === 'server-binding-1' ? late.promise : true,
  });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await assert.rejects(bridge.recordRendered(rendered(scope)), { name: 'TimeoutError' });
  assert.equal(sent[0].signal.aborted, true);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await assert.rejects(bridge.retry(), { name: 'TransportBusyError' });
    assert.equal(sent.length, 1);
  }
  const restarted = start({ binding: 'server-binding-2' });
  await bridge.registerSegment(metadata(restarted));
  await assert.rejects(bridge.recordRendered(rendered(restarted)), { name: 'TransportBusyError' });
  assert.equal(sent.length, 1);
  late.resolve(true);
  await nextTurn();
  assert.equal(await bridge.retry(), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), ack(restarted, 0)]);
});

test('同期ブロックで timer が遅れても期限超過の receipt を成功としない', async (t) => {
  let first = true;
  const { bridge, sent, start } = fixture(t, {
    ackTimeoutMs: 5,
    send: () => {
      if (first) {
        first = false;
        const deadline = performance.now() + 30;
        while (performance.now() < deadline) { /* 遅延した同期transportを合成する。 */ }
      }
      return true;
    },
  });
  const scope = start();
  await bridge.registerSegment(metadata(scope));
  await assert.rejects(bridge.recordRendered(rendered(scope)), { name: 'TimeoutError' });
  assert.equal(sent[0].signal.aborted, true);
  assert.equal(await bridge.retry(), true);
  assert.deepEqual(sent.map(({ wire }) => wire), [ack(scope, 0), ack(scope, 0)]);
});

test('非同期の失効コールバックは新応答を開始せず、旧scopeを破棄して安全に閉じる', async (t) => {
  const invalidated = [];
  const sent = [];
  const bridge = createPlaybackAckBridge({
    send: async (wire) => { sent.push(wire); return true; },
    onInvalidate: async (scope) => {
      invalidated.push(scope);
      throw new Error('synthetic asynchronous invalidation failure');
    },
  });
  t.after(() => bridge.close());
  const scope = bridge.startResponse({
    responseId: 'response-1', binding: 'server-binding-1', trackSid: 'track-1',
  });
  await bridge.registerSegment(metadata(scope));
  assert.throws(() => bridge.startResponse({
    responseId: 'response-2', binding: 'server-binding-2', trackSid: 'track-2',
  }), { name: 'TypeError', message: 'onInvalidate must be synchronous' });
  assert.deepEqual(invalidated, [scope]);
  assert.equal(await bridge.registerSegment(metadata(scope)), false);
  assert.equal(await bridge.recordRendered(rendered(scope)), false);
  assert.equal(await bridge.finish({ scope, finalAudioSequence: 0 }), false);
  assert.equal(await bridge.retry(), false);
  // reject済みthenableを観測し、unhandled rejectionとして漏らさない。
  await nextTurn();
  assert.deepEqual(sent, []);
  assert.doesNotThrow(() => bridge.close());
  assert.throws(() => bridge.startResponse({
    responseId: 'response-2', binding: 'server-binding-2', trackSid: 'track-2',
  }), /closed/i);
});
