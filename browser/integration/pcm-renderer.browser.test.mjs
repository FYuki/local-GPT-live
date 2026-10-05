// 既存 Playwright/Chromium を使う合成検証。サーバー・マイク・実接続は不要。
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

const origin = 'http://127.0.0.1:41736';
const moduleFiles = new Map([
  ['/pcm-renderer.mjs', new URL('../pcm-renderer.mjs', import.meta.url)],
  ['/pcm-renderer-worklet.mjs', new URL('../pcm-renderer-worklet.mjs', import.meta.url)],
  ['/playback-ack.mjs', new URL('../playback-ack.mjs', import.meta.url)],
  ['/playback-ack-bridge.mjs', new URL('../playback-ack-bridge.mjs', import.meta.url)],
]);
let browser;

before(async () => {
  const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  browser = await chromium.launch({
    ...(process.env.CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
    headless: true,
    args: ['--autoplay-policy=no-user-gesture-required'],
  });
});

after(async () => { await browser?.close(); });

function pythonHost(t) {
  const child = spawn(fileURLToPath(new URL('../../.venv/bin/python', import.meta.url)), [
    fileURLToPath(new URL('./ack_host.py', import.meta.url)),
  ], { stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map();
  let nextId = 0;
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4096); });
  const lines = createInterface({ input: child.stdout });
  function failAll(error) {
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(error);
    }
    pending.clear();
  }
  child.on('error', failAll);
  child.on('exit', code => failAll(new Error(`synthetic host exited ${code}: ${stderr}`)));
  lines.on('line', line => {
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
      child.stdin.write(`${JSON.stringify({ id, command, ...values })}\n`, error => {
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
  return { request, receive: wire => request('receive', { wire: JSON.stringify(wire) }) };
}

async function openFixture(t) {
  const context = await browser.newContext({ serviceWorkers: 'block' });
  t.after(() => context.close());
  const page = await context.newPage();
  const unexpectedRequests = [];
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      unexpectedRequests.push(url.origin);
      return route.abort();
    }
    if (url.pathname === '/') {
      return route.fulfill({
        contentType: 'text/html',
        body: '<!doctype html><meta charset="utf-8"><title>合成 PCM 検証</title>',
      });
    }
    const file = moduleFiles.get(url.pathname);
    if (!file) return route.abort();
    return route.fulfill({ contentType: 'text/javascript', body: await readFile(file) });
  });
  t.after(() => assert.deepEqual(unexpectedRequests, [], '外部通信が発生しない'));
  await page.goto(origin);
  await page.evaluate(async () => {
    // Chromium の Worklet fetch は page.route を通らないため、同じ未変更本文を
    // route 経由で取得し Blob URL として渡す。HTTP サーバーは起動しない。
    const workletSource = await (await fetch('/pcm-renderer-worklet.mjs')).text();
    globalThis.workletUrl = URL.createObjectURL(new Blob([workletSource], {
      type: 'text/javascript',
    }));
    // OfflineAudioContext は port 配送より先に全描画を終える場合がある。
    // 既知入力だけを constructor で実 processor の入力 handler へ渡し、
    // process と出力バッファは実 AudioWorklet のまま検証する。
    const primeOffline = `
      const nativeRegister = registerProcessor;
      globalThis.registerProcessor = (name, Processor) => {
        nativeRegister(name, class extends Processor {
          constructor(options) {
            super(options);
            const pcm = options.processorOptions.fixturePcm;
            this.port.onmessage({ data: { kind: 'enqueue', audioSequence: 0, pcm } });
          }
        });
      };
    `;
    globalThis.offlineWorkletUrl = URL.createObjectURL(new Blob([primeOffline, workletSource], {
      type: 'text/javascript',
    }));
    globalThis.pause = ms => new Promise(resolve => setTimeout(resolve, ms));
    globalThis.until = async predicate => {
      const deadline = performance.now() + 3000;
      while (!predicate()) {
        if (performance.now() >= deadline) throw new Error('合成音声待機が期限を超過');
        await pause(5);
      }
    };
    // ノードは実物を使い、解放と実メッセージの遅着だけを観測する。
    const NativeNode = AudioWorkletNode;
    globalThis.nodeRecords = [];
    globalThis.AudioWorkletNode = class extends NativeNode {
      constructor(...args) {
        super(...args);
        const record = { disconnects: 0, portCloses: 0, messages: [], delayed: [] };
        nodeRecords.push(record);
        const disconnect = this.disconnect.bind(this);
        this.disconnect = (...values) => {
          record.disconnects += 1;
          return disconnect(...values);
        };
        const close = this.port.close.bind(this.port);
        this.port.close = () => {
          record.portCloses += 1;
          return close();
        };
        this.port.addEventListener('message', event => record.messages.push(event.data));
        this.port.start();
        const descriptor = Object.getOwnPropertyDescriptor(MessagePort.prototype, 'onmessage');
        if (descriptor?.set) {
          const port = this.port;
          Object.defineProperty(port, 'onmessage', {
            configurable: true,
            set(handler) {
              descriptor.set.call(port, handler && (event => {
                if (globalThis.delayRendererMessages) {
                  record.delayed.push(() => handler.call(port, event));
                } else {
                  handler.call(port, event);
                }
              }));
            },
          });
        }
      }
    };
  });
  return page;
}

test('実 AudioWorklet の部分出力は既知 PCM の先頭だけで完了通知を出さない', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const context = new OfflineAudioContext(1, 128, 48000);
    await context.audioWorklet.addModule(offlineWorkletUrl);
    const pcm = Float32Array.from({ length: 300 }, (_, index) => (index % 17 - 8) / 16);
    const node = new AudioWorkletNode(context, 'local-gpt-live-pcm-renderer-v1', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
      processorOptions: { maxQueuedSamples: 4096, maxSegments: 64, fixturePcm: pcm },
    });
    node.connect(context.destination);
    const output = await context.startRendering();
    await pause(20);
    return { expected: Array.from(pcm.subarray(0, 128)),
      actual: Array.from(output.getChannelData(0)), messages: nodeRecords[0].messages };
  });
  assert.deepEqual(result.actual, result.expected);
  assert.deepEqual(result.messages.filter(message => message.kind === 'rendered'), []);
});

test('実 AudioWorklet はセグメント PCM と不足分のゼロを出し不足分を数えない', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const context = new OfflineAudioContext(1, 512, 48000);
    await context.audioWorklet.addModule(offlineWorkletUrl);
    const pcm = Float32Array.from({ length: 173 }, (_, index) => index % 2 ? 0.25 : -0.5);
    const node = new AudioWorkletNode(context, 'local-gpt-live-pcm-renderer-v1', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
      processorOptions: { maxQueuedSamples: 4096, maxSegments: 64, fixturePcm: pcm },
    });
    node.connect(context.destination);
    const output = await context.startRendering();
    await until(() => nodeRecords[0].messages.some(message => message.kind === 'rendered'));
    return { actual: Array.from(output.getChannelData(0)),
      expected: [...pcm, ...Array(512 - pcm.length).fill(0)],
      messages: nodeRecords[0].messages.filter(message => message.kind === 'rendered') };
  });
  assert.deepEqual(result.actual, result.expected);
  assert.equal(result.messages.length, 1);
  assert.equal(result.messages[0].audioSequence, 0);
  assert.equal(result.messages[0].renderedSampleCount, 173);
  assert.equal(result.messages[0].endContextTime, 173 / 48000);
});

test('停止メッセージと同期 disconnect は未出力 PCM を端末出力へ渡さない', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const context = new OfflineAudioContext(1, 256, 48000);
    await context.audioWorklet.addModule(offlineWorkletUrl);
    const node = new AudioWorkletNode(context, 'local-gpt-live-pcm-renderer-v1', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
      processorOptions: { maxQueuedSamples: 4096, maxSegments: 64,
        fixturePcm: new Float32Array(200).fill(0.5) },
    });
    node.connect(context.destination);
    node.port.postMessage({ kind: 'stop' });
    node.disconnect();
    const output = await context.startRendering();
    await pause(20);
    return { actual: Array.from(output.getChannelData(0)),
      rendered: nodeRecords[0].messages.filter(message => message.kind === 'rendered') };
  });
  assert.deepEqual(result.actual, Array(256).fill(0));
  // disconnect 後も Worklet 自体は遅れて通知できる。renderer 側の取消テストで拒否する。
});

test('renderer は実出力時刻が PCM 終端を越えた後だけ通知する', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const context = new AudioContext();
    await context.suspend();
    const rendered = [], errors = [];
    const renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      onRendered: event => rendered.push({ ...event, timestamp: context.getOutputTimestamp() }),
      onError: error => errors.push(String(error)),
    });
    const scope = Object.freeze({ responseId: 'fixture', generation: 0,
      binding: 'fixture-binding', trackSid: 'TR-fixture' });
    renderer.startResponse(scope);
    const pcm = new Float32Array(1024).fill(0.125);
    const accepted = renderer.enqueue({ scope, audioSequence: 0, pcm, sampleRate: context.sampleRate });
    await pause(40);
    const queuedCount = rendered.length;
    await context.resume();
    try { await until(() => rendered.length === 1); } catch (error) {
      throw new Error(JSON.stringify({ failure: String(error), errors,
        messages: nodeRecords[0].messages, timestamp: context.getOutputTimestamp(),
        state: context.state }));
    }
    const callerPcmPreserved = pcm.length === 1024 && pcm[0] === 0.125;
    const scopePreserved = rendered[0].scope === scope;
    renderer.close();
    const callerState = context.state;
    await context.close();
    return { accepted, queuedCount, rendered, errors, callerPcmPreserved, scopePreserved,
      callerState, sampleRate: context.sampleRate };
  });
  assert.notEqual(result.accepted, false);
  assert.equal(result.queuedCount, 0);
  assert.deepEqual(result.errors, []);
  assert.equal(result.rendered.length, 1);
  assert.equal(result.rendered[0].renderedSampleCount, 1024);
  assert.equal(result.rendered[0].audioSequence, 0);
  assert.ok(result.rendered[0].timestamp.contextTime >= result.rendered[0].endContextTime);
  assert.ok(result.rendered[0].timestamp.performanceTime > 0);
  assert.ok(result.callerPcmPreserved);
  assert.ok(result.scopePreserved);
  assert.equal(result.callerState, 'running');
  t.diagnostic(`実 Chromium 出力時計で確認: sampleRate=${result.sampleRate}`);
});

test('suspend 中は PCM を投入しても再生通知を出さない', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const context = new AudioContext();
    await context.suspend();
    const rendered = [];
    const renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      onRendered: event => rendered.push(event),
    });
    const scope = { responseId: 'suspended', generation: 0,
      binding: 'fixture-binding', trackSid: 'TR-fixture' };
    renderer.startResponse(scope);
    renderer.enqueue({ scope, audioSequence: 0,
      pcm: new Float32Array(256).fill(0.125), sampleRate: context.sampleRate });
    await pause(80);
    const state = context.state;
    renderer.close();
    await context.close();
    return { rendered, state };
  });
  assert.equal(result.state, 'suspended');
  assert.deepEqual(result.rendered, []);
});

test('renderer は取消後に届く実 Worklet メッセージを ACK にしない', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const context = new AudioContext();
    await context.resume();
    globalThis.delayRendererMessages = true;
    const rendered = [];
    const renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      onRendered: event => rendered.push(event),
    });
    const scope = { responseId: 'old', generation: 0, binding: 'old-binding', trackSid: 'TR-old' };
    renderer.startResponse(scope);
    renderer.enqueue({ scope, audioSequence: 0,
      pcm: new Float32Array(256).fill(0.125), sampleRate: context.sampleRate });
    await until(() => nodeRecords[0].delayed.length > 0);
    renderer.cancel();
    const lateCount = nodeRecords[0].delayed.length;
    for (const deliver of nodeRecords[0].delayed) deliver();
    await pause(100);
    const staleAccepted = renderer.enqueue({ scope, audioSequence: 1,
      pcm: new Float32Array(128), sampleRate: context.sampleRate });
    renderer.close();
    await context.close();
    return { lateCount, rendered, staleAccepted,
      disconnects: nodeRecords[0].disconnects, portCloses: nodeRecords[0].portCloses };
  });
  assert.ok(result.lateCount > 0);
  assert.deepEqual(result.rendered, []);
  assert.equal(result.staleAccepted, false);
  assert.equal(result.disconnects, 1);
  assert.equal(result.portCloses, 1);
});

test('未対応または進まない出力時計では通知しない', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const unsupported = new AudioContext();
    unsupported.getOutputTimestamp = undefined;
    let rejected = false;
    try {
      await createPcmRenderer({ audioContext: unsupported, workletUrl, onRendered() {} });
    } catch { rejected = true; }
    await unsupported.close();
    const context = new AudioContext();
    await context.resume();
    // 意図的な非対応入力。成功判定用の時刻は合成しない。
    context.getOutputTimestamp = () => ({ contextTime: 0, performanceTime: 0 });
    const rendered = [];
    let renderer;
    try {
      renderer = await createPcmRenderer({ audioContext: context, workletUrl,
        onRendered: event => rendered.push(event), onError() {},
      });
      const scope = { responseId: 'fixture', generation: 0,
        binding: 'fixture-binding', trackSid: 'TR-fixture' };
      renderer.startResponse(scope);
      renderer.enqueue({ scope, audioSequence: 0,
        pcm: new Float32Array(128), sampleRate: context.sampleRate });
      await pause(150);
    } catch (error) {
      if (!/clock|timestamp/i.test(String(error))) throw error;
    }
    renderer?.close();
    await context.close();
    return { rejected, rendered };
  });
  assert.ok(result.rejected);
  assert.deepEqual(result.rendered, []);
});

test('空 PCM・異なる標本化周波数は拒否し close はノードだけを解放する', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const context = new AudioContext();
    await context.resume();
    const rendered = [];
    const renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      onRendered: event => rendered.push(event),
    });
    const scope = { responseId: 'fixture', generation: 0,
      binding: 'fixture-binding', trackSid: 'TR-fixture' };
    renderer.startResponse(scope);
    const rejected = [];
    for (const packet of [
      { scope, audioSequence: 0, pcm: new Float32Array(0), sampleRate: context.sampleRate },
      { scope, audioSequence: 0, pcm: new Float32Array(128), sampleRate: context.sampleRate + 1 },
    ]) {
      try { rejected.push(renderer.enqueue(packet) === false); } catch { rejected.push(true); }
    }
    renderer.startResponse({ ...scope, responseId: 'new', generation: 1 });
    renderer.close();
    renderer.close();
    const closedAccepted = renderer.enqueue({ scope, audioSequence: 0,
      pcm: new Float32Array(128), sampleRate: context.sampleRate });
    const callerState = context.state;
    await pause(50);
    await context.close();
    return { rejected, rendered, closedAccepted, callerState,
      resources: nodeRecords.map(({ disconnects, portCloses }) => ({ disconnects, portCloses })) };
  });
  assert.deepEqual(result.rejected, [true, true]);
  assert.deepEqual(result.rendered, []);
  assert.equal(result.closedAccepted, false);
  assert.equal(result.callerState, 'running');
  assert.deepEqual(result.resources, [
    { disconnects: 1, portCloses: 1 }, { disconnects: 1, portCloses: 1 },
  ]);
});

test('実 renderer と bridge は2セグメントの順序付き受領と完了を送り取消後は送らない', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const { createPlaybackAckBridge } = await import('/playback-ack-bridge.mjs');
    const context = new AudioContext({ sampleRate: 16000 });
    await context.resume();
    await until(() => context.getOutputTimestamp().contextTime > 0.05);
    const wires = [], rendered = [], errors = [], invalidated = [];
    let renderer;
    const bridge = createPlaybackAckBridge({
      // backend 受理はこのテストだけ合成。Python receiver は別の合成テストで検証する。
      send: async wire => { wires.push(wire); return true; },
      onInvalidate: scope => { invalidated.push(scope.responseId); renderer?.cancel(); },
    });
    renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      onRendered: event => {
        rendered.push({ ...event, timestamp: context.getOutputTimestamp() });
        void bridge.recordRendered(event).catch(error => errors.push(String(error)));
      },
      onError: error => errors.push(String(error)),
    });
    const scope = bridge.startResponse({ responseId: 'fixture-response',
      binding: 'fixture-binding', trackSid: 'TR-fixture' });
    renderer.startResponse(scope);
    let sampleStart = 0;
    for (const [audioSequence, size] of [1600, 1600].entries()) {
      const sampleEnd = sampleStart + size;
      const registered = await bridge.registerSegment({ scope, audioSequence,
        sampleRate: context.sampleRate, sampleStart, sampleEnd });
      if (!registered) throw new Error('合成 metadata が拒否された');
      renderer.enqueue({ scope, audioSequence, pcm: new Float32Array(size).fill(0.125),
        sampleRate: context.sampleRate });
      sampleStart = sampleEnd;
    }
    await bridge.finish({ scope, finalAudioSequence: 1 });
    await until(() => wires.length === 3 || errors.length > 0);
    if (errors.length > 0) throw new Error(errors.join('; '));

    globalThis.delayRendererMessages = true;
    const cancelledScope = bridge.startResponse({ responseId: 'cancelled-response',
      binding: 'cancelled-binding', trackSid: 'TR-cancelled' });
    renderer.startResponse(cancelledScope);
    await bridge.registerSegment({ scope: cancelledScope, audioSequence: 0,
      sampleRate: context.sampleRate, sampleStart: 0, sampleEnd: 256 });
    renderer.enqueue({ scope: cancelledScope, audioSequence: 0,
      pcm: new Float32Array(256).fill(0.25), sampleRate: context.sampleRate });
    await until(() => nodeRecords[1].delayed.length > 0);
    bridge.cancel();
    for (const deliver of nodeRecords[1].delayed) deliver();
    await pause(100);
    bridge.close();
    renderer.close();
    await context.close();
    return { wires, rendered, invalidated, errors };
  });
  assert.deepEqual(result.wires, [
    { v: 1, type: 'playback_ack', binding: 'fixture-binding',
      response_id: 'fixture-response', audio_sequence: 0 },
    { v: 1, type: 'playback_ack', binding: 'fixture-binding',
      response_id: 'fixture-response', audio_sequence: 1 },
    { v: 1, type: 'playback_complete', binding: 'fixture-binding',
      response_id: 'fixture-response', final_audio_sequence: 1 },
  ]);
  assert.deepEqual(result.errors, []);
  assert.equal(result.rendered.length, 2);
  for (const event of result.rendered) {
    assert.equal(event.renderedSampleCount, 1600);
    assert.ok(event.timestamp.contextTime >= event.endContextTime);
  }
  assert.deepEqual(result.invalidated, ['fixture-response', 'cancelled-response']);
});

test('満杯だった PCM queue は描画通知内で次セグメントを投入できる', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const context = new AudioContext();
    await context.resume();
    const scope = { responseId: 'continuous', generation: 0,
      binding: 'fixture-binding', trackSid: 'TR-fixture' };
    const rendered = [], errors = [];
    let renderer;
    renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      maxQueuedSamples: 128,
      onRendered: event => {
        rendered.push(event);
        if (event.audioSequence === 0) {
          renderer.enqueue({ scope, audioSequence: 1,
            pcm: new Float32Array(128).fill(-0.125), sampleRate: context.sampleRate });
        }
      },
      onError: error => errors.push(String(error)),
    });
    renderer.startResponse(scope);
    renderer.enqueue({ scope, audioSequence: 0,
      pcm: new Float32Array(128).fill(0.125), sampleRate: context.sampleRate });
    await until(() => rendered.length === 2 || errors.length > 0);
    renderer.close();
    await context.close();
    return { rendered, errors };
  });
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.rendered.map(event => [event.audioSequence, event.renderedSampleCount]),
    [[0, 128], [1, 128]]);
});

test('実 Chromium renderer→bridge→Python receiver は全 ACK 後に1回だけ会話を完了する', async t => {
  const page = await openFixture(t);
  const host = pythonHost(t);
  const response = await host.request('start', { count: 2 });
  assert.equal(response.generated, true);
  assert.equal(response.segments.length, 2);
  const initial = await host.request('status');
  assert.equal(initial.active, response.response_id);
  assert.equal(initial.completed_count, 0);
  assert.equal(initial.confirmed_sequence, -1);
  const receipts = [];
  await page.exposeFunction('__hostReceive', async wire => {
    const result = await host.receive(wire);
    receipts.push({ wire, ...result });
    return result.accepted;
  });
  const result = await page.evaluate(async response => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const { createPlaybackAckBridge } = await import('/playback-ack-bridge.mjs');
    const context = new AudioContext({ sampleRate: 16000 });
    await context.resume();
    const rendered = [], errors = [], acknowledgements = [];
    let renderer;
    const bridge = createPlaybackAckBridge({
      send: wire => __hostReceive(wire),
      onInvalidate: () => renderer?.cancel(),
    });
    renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      onRendered: event => {
        rendered.push({ ...event, timestamp: context.getOutputTimestamp() });
        acknowledgements.push(bridge.recordRendered(event)
          .catch(error => errors.push(String(error))));
      },
      onError: error => errors.push(String(error)),
    });
    const scope = bridge.startResponse({ responseId: response.response_id,
      binding: response.binding, trackSid: response.segments[0].track_sid });
    renderer.startResponse(scope);
    for (const segment of response.segments) {
      if (segment.sample_rate !== context.sampleRate) throw new Error('標本化周波数不一致');
      const accepted = await bridge.registerSegment({ scope, audioSequence: segment.sequence,
        sampleRate: segment.sample_rate, sampleStart: segment.sample_start,
        sampleEnd: segment.sample_end });
      if (!accepted) throw new Error('host metadata が拒否された');
    }
    const beforeRenderingComplete = await bridge.finish({ scope,
      finalAudioSequence: response.segments.length - 1 });
    for (const segment of response.segments) {
      // ack_host の FixtureTts は PCM16 mono/16000Hz のゼロ1600samplesを生成する。
      renderer.enqueue({ scope, audioSequence: segment.sequence,
        pcm: new Float32Array(segment.sample_end - segment.sample_start),
        sampleRate: segment.sample_rate });
    }
    await until(() => rendered.length === 2 || errors.length > 0);
    await Promise.all(acknowledgements);
    bridge.close();
    renderer.close();
    await context.close();
    return { beforeRenderingComplete, rendered, errors };
  }, response);
  assert.equal(result.beforeRenderingComplete, false);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.rendered.map(event => event.renderedSampleCount), [1600, 1600]);
  for (const event of result.rendered) {
    assert.ok(event.timestamp.contextTime >= event.endContextTime);
  }
  assert.deepEqual(receipts.map(receipt => [receipt.wire.type, receipt.accepted]), [
    ['playback_ack', true], ['playback_ack', true], ['playback_complete', true],
  ]);
  assert.deepEqual(receipts.map(receipt => receipt.status.completed_count), [0, 0, 1]);
  assert.equal(receipts[0].status.confirmed_sequence, 0);
  assert.equal(receipts[1].status.confirmed_sequence, 1);
  assert.equal(receipts[1].status.active, response.response_id);
  const final = await host.request('status');
  assert.equal(final.active, null);
  assert.equal(final.completed_count, 1);
});

test('描画 observer が Promise を返すと閉じてノードを解放する', async t => {
  const page = await openFixture(t);
  const result = await page.evaluate(async () => {
    const { createPcmRenderer } = await import('/pcm-renderer.mjs');
    const context = new AudioContext();
    await context.resume();
    const rendered = [], errors = [];
    const renderer = await createPcmRenderer({ audioContext: context, workletUrl,
      onRendered: event => { rendered.push(event); return Promise.resolve(); },
      onError: error => errors.push({ name: error.name, message: error.message }),
    });
    const scope = { responseId: 'invalid-observer', generation: 0,
      binding: 'fixture-binding', trackSid: 'TR-fixture' };
    renderer.startResponse(scope);
    for (let audioSequence = 0; audioSequence < 2; audioSequence += 1) {
      renderer.enqueue({ scope, audioSequence, pcm: new Float32Array(128),
        sampleRate: context.sampleRate });
    }
    await until(() => errors.length > 0);
    const afterFailure = renderer.enqueue({ scope, audioSequence: 2,
      pcm: new Float32Array(128), sampleRate: context.sampleRate });
    renderer.close();
    const callerState = context.state;
    await context.close();
    return { errors, renderedCount: rendered.length, afterFailure, callerState,
      disconnects: nodeRecords[0].disconnects, portCloses: nodeRecords[0].portCloses };
  });
  assert.deepEqual(result.errors, [{ name: 'TypeError', message: 'onRendered must be synchronous' }]);
  assert.equal(result.renderedCount, 1);
  assert.equal(result.afterFailure, false);
  assert.equal(result.disconnects, 1);
  assert.equal(result.portCloses, 1);
  assert.equal(result.callerState, 'running');
});
