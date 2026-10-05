import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { setImmediate as nextTurn } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { createPlaybackAckBridge } from '../playback-ack-bridge.mjs';

function deferred() {
  let resolve;
  const promise = new Promise((ok) => { resolve = ok; });
  return { promise, resolve };
}

function pythonHost(t) {
  const child = spawn(fileURLToPath(new URL('../../.venv/bin/python', import.meta.url)), [
    fileURLToPath(new URL('./ack_host.py', import.meta.url)),
  ], { stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map();
  let nextId = 0;
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk) => { stderr = (stderr + chunk).slice(-4096); });
  const lines = createInterface({ input: child.stdout });
  function failAll(error) {
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(error);
    }
    pending.clear();
  }
  child.on('error', failAll);
  child.on('exit', (code) => failAll(new Error(`synthetic host exited ${code}: ${stderr}`)));
  lines.on('line', (line) => {
    let response;
    try { response = JSON.parse(line); } catch (error) { failAll(error); return; }
    const entry = pending.get(response.id);
    if (!entry) return;
    clearTimeout(entry.timer);
    pending.delete(response.id);
    if (response.ok) entry.resolve(response.result);
    else entry.reject(new Error(response.error));
  });
  function request(command, values = {}) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`synthetic host command timed out: ${command}`));
      }, 6000);
      pending.set(id, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify({ id, command, ...values })}\n`, (error) => {
        if (!error || !pending.has(id)) return;
        clearTimeout(timer);
        pending.delete(id);
        reject(error);
      });
    });
  }
  t.after(async () => {
    try {
      if (child.exitCode === null) {
        const exited = once(child, 'exit');
        await request('close');
        await exited;
      }
    } finally {
      lines.close();
      child.stdin.destroy();
      if (child.exitCode === null) child.kill();
      failAll(new Error('synthetic host fixture closed'));
    }
  });
  return { request, receive: (wire) => request('receive', { wire: JSON.stringify(wire) }) };
}

function bridgeForHost(t, host, options = {}) {
  const sent = [];
  const invalidated = [];
  const bridge = createPlaybackAckBridge({
    send: async (wire, context) => {
      sent.push(wire);
      if (options.send) return options.send(wire, context);
      return (await host.receive(wire)).accepted;
    },
    // 合成hostの取消は各テストで明示awaitし、分散取消の同期保証を仮定しない。
    onInvalidate: (scope) => invalidated.push(scope),
    ackTimeoutMs: options.ackTimeoutMs ?? 1000,
  });
  t.after(() => bridge.close());
  const start = (response) => bridge.startResponse({
    responseId: response.response_id, binding: response.binding,
    trackSid: response.segments[0]?.track_sid ?? 'synthetic-output',
  });
  const register = async (scope, response) => {
    for (const segment of response.segments) {
      assert.equal(await bridge.registerSegment({
        scope, audioSequence: segment.sequence, sampleRate: segment.sample_rate,
        sampleStart: segment.sample_start, sampleEnd: segment.sample_end,
      }), true);
    }
  };
  const render = (scope, segment) => bridge.recordRendered({
    scope, audioSequence: segment.sequence,
    renderedSampleCount: segment.sample_end - segment.sample_start,
  });
  return { bridge, sent, invalidated, start, register, render };
}

test('実セッションは生成済みと描画済みを区別し、連続 ACK と別完了通知で終了する', async (t) => {
  const host = pythonHost(t);
  const response = await host.request('start', { count: 2, hold_generation: true });
  assert.equal(response.generated, false);
  const { bridge, sent, start, register, render } = bridgeForHost(t, host);
  const scope = start(response);
  await register(scope, response);
  let status = await host.request('finish_generation');
  assert.equal(status.generated, response.response_id);
  assert.equal(status.confirmed_sequence, -1);
  assert.equal(status.completed_count, 0);
  assert.deepEqual(sent, []);
  await render(scope, response.segments[1]);
  status = await host.request('status');
  assert.equal(status.confirmed_sequence, -1);
  await render(scope, response.segments[0]);
  status = await host.request('status');
  assert.equal(status.confirmed_sequence, 1);
  assert.equal(status.all_confirmed, true);
  assert.equal(status.active, response.response_id);
  assert.equal(status.completed_count, 0);
  assert.equal(await bridge.finish({ scope, finalAudioSequence: 1 }), true);
  status = await host.request('status');
  assert.equal(status.active, null);
  assert.equal(status.completed_count, 1);
  assert.deepEqual(sent.map((wire) => [wire.type, wire.audio_sequence ?? wire.final_audio_sequence]), [
    ['playback_ack', 0], ['playback_ack', 1], ['playback_complete', 1],
  ]);
});

test('取消後の旧 binding を実受信側が拒否し、遅延 receipt は新応答を進めない', async (t) => {
  const host = pythonHost(t);
  const oldResponse = await host.request('start', { count: 1 });
  const late = deferred();
  let oldWire;
  const { bridge, start, register, render, invalidated } = bridgeForHost(t, host, {
    send: async (wire) => {
      if (wire.binding === oldResponse.binding) { oldWire = wire; return late.promise; }
      return (await host.receive(wire)).accepted;
    },
  });
  const oldScope = start(oldResponse);
  await register(oldScope, oldResponse);
  const pending = render(oldScope, oldResponse.segments[0]);
  const rejected = assert.rejects(pending, { name: 'AbortError' });
  await nextTurn();
  assert.ok(oldWire);
  bridge.cancel();
  assert.deepEqual(invalidated, [oldScope]);
  await host.request('cancel');
  await rejected;
  const response = await host.request('start', { count: 1 });
  const scope = start(response);
  await register(scope, response);
  const lateReceipt = await host.receive(oldWire);
  assert.equal(lateReceipt.accepted, false);
  assert.equal(lateReceipt.status.active, response.response_id);
  assert.equal(lateReceipt.status.confirmed_sequence, -1);
  late.resolve(lateReceipt.accepted);
  await nextTurn();
  assert.equal(await bridge.recordRendered({ scope: oldScope, audioSequence: 0, renderedSampleCount: 1600 }), false);
  await render(scope, response.segments[0]);
  await bridge.finish({ scope, finalAudioSequence: 0 });
  assert.equal((await host.request('status')).completed_count, 1);
});

test('実受理後に receipt を失っても timeout 再送は同じ ACK を冪等に受理する', async (t) => {
  const host = pythonHost(t);
  const response = await host.request('start', { count: 2 });
  const lostReceipt = deferred();
  const received = [];
  let first = true;
  const { bridge, sent, start, register, render } = bridgeForHost(t, host, {
    ackTimeoutMs: 100,
    send: async (wire, { signal }) => {
      const receipt = await host.receive(wire);
      received.push(receipt);
      if (first) {
        first = false;
        return new Promise((resolve, reject) => {
          const abort = () => reject(new DOMException('synthetic receipt cancelled', 'AbortError'));
          signal.addEventListener('abort', abort, { once: true });
          lostReceipt.promise.then((value) => {
            signal.removeEventListener('abort', abort);
            resolve(value);
          });
        });
      }
      return receipt.accepted;
    },
  });
  const scope = start(response);
  await register(scope, response);
  await assert.rejects(render(scope, response.segments[0]), { name: 'TimeoutError' });
  assert.equal(received[0].accepted, true);
  assert.equal((await host.request('status')).confirmed_sequence, 0);
  assert.equal(await bridge.retry(), true);
  assert.equal(received[1].accepted, true);
  assert.equal((await host.request('status')).confirmed_sequence, 0);
  lostReceipt.resolve(true);
  await render(scope, response.segments[1]);
  await bridge.finish({ scope, finalAudioSequence: 1 });
  assert.deepEqual(sent.map((wire) => [wire.type, wire.audio_sequence ?? wire.final_audio_sequence]), [
    ['playback_ack', 0], ['playback_ack', 0], ['playback_ack', 1], ['playback_complete', 1],
  ]);
  assert.equal((await host.request('status')).completed_count, 1);
});

test('生成未完了の完了通知は拒否され、生成終了後の明示 retry でのみ完了する', async (t) => {
  const host = pythonHost(t);
  const response = await host.request('start', { count: 1, hold_generation: true });
  const { bridge, sent, start, register, render } = bridgeForHost(t, host);
  const scope = start(response);
  await register(scope, response);
  await render(scope, response.segments[0]);
  await assert.rejects(bridge.finish({ scope, finalAudioSequence: 0 }), /backend rejected/);
  let status = await host.request('status');
  assert.equal(status.active, response.response_id);
  assert.equal(status.generated, null);
  assert.equal(status.all_confirmed, true);
  assert.equal(status.completed_count, 0);
  await host.request('finish_generation');
  assert.equal(await bridge.finish({ scope, finalAudioSequence: 0 }), false);
  assert.equal(sent.length, 2);
  assert.equal(await bridge.retry(), true);
  status = await host.request('status');
  assert.equal(status.active, null);
  assert.equal(status.completed_count, 1);
});

test('空応答の完了と受理済み完了の重複は、実イベントを一度だけ発行する', async (t) => {
  const host = pythonHost(t);
  const response = await host.request('start', { count: 0 });
  const { bridge, sent, start } = bridgeForHost(t, host);
  const scope = start(response);
  assert.equal(await bridge.finish({ scope, finalAudioSequence: -1 }), true);
  assert.equal(await bridge.finish({ scope, finalAudioSequence: -1 }), true);
  assert.equal(sent.length, 1);
  const duplicate = await host.receive(sent[0]);
  assert.equal(duplicate.accepted, true);
  assert.equal(duplicate.status.active, null);
  assert.equal(duplicate.status.completed_count, 1);
});
