import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { connection, sender, transportDouble, deferred, until } from '../tests/demo-fixture.mjs';

async function pythonHost(t) {
  const directory = fileURLToPath(new URL('../../.takt/browser-demo-fixtures/', import.meta.url));
  await mkdir(directory, { recursive: true });
  const cwd = await mkdtemp(`${directory}/host-`);
  const child = spawn(process.env.DEMO_TEST_PYTHON ||
    fileURLToPath(new URL('../../.venv/bin/python', import.meta.url)),
    [fileURLToPath(new URL('./conversation_host_fixture.py', import.meta.url))],
    { cwd, stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map();
  const lines = createInterface({ input: child.stdout });
  let serial = 0, stderr = '';
  child.stderr.on('data', value => { stderr = (stderr + value).slice(-4096); });
  function fail(error) {
    for (const { reject, timer } of pending.values()) { clearTimeout(timer); reject(error); }
    pending.clear();
  }
  child.on('error', fail);
  child.on('exit', code => fail(new Error(`合成host終了 ${code}: ${stderr}`)));
  lines.on('line', raw => {
    const response = JSON.parse(raw), entry = pending.get(response.id);
    if (!entry) return;
    clearTimeout(entry.timer);
    pending.delete(response.id);
    entry.resolve(response.result);
  });
  function request(command, fields = {}) {
    const id = ++serial;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id); reject(new Error(`合成fixture timeout: ${command}`));
      }, 6000);
      pending.set(id, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify({ id, command, ...fields })}\n`);
    });
  }
  t.after(async () => {
    const exited = child.exitCode === null ? once(child, 'exit') : Promise.resolve();
    const kill = setTimeout(() => child.kill('SIGKILL'), 2000);
    try {
      if (child.exitCode === null) { await request('close'); await exited; }
    } finally {
      clearTimeout(kill); lines.close(); child.stdin.destroy();
      if (child.exitCode === null) child.kill('SIGKILL');
      fail(new Error('fixture終了'));
      await exited;
      await rm(cwd, { recursive: true, force: true });
    }
  });
  return { request };
}

async function fixture(t, pendingSubscription = false) {
  const { createConversation } = await import('../conversation.mjs');
  const host = await pythonHost(t);
  const initial = await host.request('initial');
  const transport = transportDouble();
  transport.rpc = (message, timeoutMs) => {
    transport.calls.push({ message, timeoutMs });
    return host.request('control', { message });
  };
  const microphone = transport.microphone.bind(transport);
  transport.microphone = async () => {
    const track = await microphone();
    await host.request('publish', { track_sid: track.trackSid, pending: pendingSubscription });
    return track;
  };
  const conversation = createConversation({ transport, connection: { ...connection,
    sessionId: initial.session_id, connectionId: initial.connection_id },
    now: () => performance.now(), onChange() {} });
  t.after(() => conversation.disconnect());
  const synchronized = await host.request('state', { message: {
    v: 1, session_id: initial.session_id, connection_id: initial.connection_id,
  } });
  assert.equal(synchronized.ok, true);
  assert.ok(synchronized.state_sequence > initial.state_sequence);
  conversation.acceptEvent(synchronized, sender);
  return { host, transport, conversation };
}

test('ブラウザのgrant受渡しは実BackendをACK後だけ開始しgate後に再認可する', async t => {
  const { host, transport, conversation } = await fixture(t);
  const ack = deferred();
  const rpc = transport.rpc;
  let hold = true;
  transport.rpc = async (message, timeoutMs) => {
    if (message.type === 'input_ack' && hold) { hold = false; await ack.promise; }
    return rpc(message, timeoutMs);
  };
  const opening = conversation.startMicrophone();
  await until(() => !hold);
  assert.deepEqual(await host.request('status'),
    { received: 0, readers: 0, confirmed: -1, capture_calls: 0 });
  assert.equal(conversation.snapshot().inputAuthorized, false);
  ack.resolve();
  await opening;
  assert.equal(await host.request('push'), 10);
  assert.equal(conversation.snapshot().inputAuthorized, true);
  await conversation.setGate('mute', true);
  await conversation.setGate('mute', false);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal(transport.microphones[0].closed, true);
  await conversation.startMicrophone();
  assert.equal(await host.request('push'), 20);
  const opens = transport.calls.filter(call => call.message.type === 'open_input');
  assert.notEqual(opens[0].message.track_sid, opens[1].message.track_sid);
});

test('実hostの出力metadataから制御bindingでreadyを送り推定終了後に次入力へ進む', async t => {
  const { host, transport, conversation } = await fixture(t);
  await conversation.submitText('合成入力');
  const metadata = await host.request('output');
  assert.notEqual(metadata.binding, metadata.control_binding);
  conversation.acceptEvent(metadata, sender);
  assert.equal((await host.request('status')).capture_calls, 0);
  conversation.subscribeOutput({ trackSid: metadata.track_sid,
    participantIdentity: sender.identity, participantSid: sender.sid, track: { kind: 'audio' } });
  await until(() => transport.calls.some(call => call.message.type === 'confirm_output_ready'));
  const ready = transport.calls.find(call => call.message.type === 'confirm_output_ready').message;
  assert.equal(ready.binding, metadata.control_binding);
  const estimate = await host.request('estimate');
  conversation.acceptEvent(estimate, sender);
  assert.equal(conversation.snapshot().outputActive, false);
  assert.equal(conversation.snapshot().realPlaybackConfirmed, false);
  assert.equal((await host.request('status')).confirmed, -1);
  await conversation.submitText('次の合成入力');
  assert.equal(conversation.snapshot().outputActive, true);
  assert.equal(transport.calls.filter(call => ['playback_ack', 'playback_complete']
    .includes(call.message.type)).length, 0);
});

test('購読が遅れた実hostは準備を待ちブラウザのACK後だけPCMを受け取る', async t => {
  const { host, transport, conversation } = await fixture(t, true);
  const ack = deferred();
  const rpc = transport.rpc;
  let ackWaiting = false;
  transport.rpc = async (message, timeoutMs) => {
    if (message.type === 'input_ack') { ackWaiting = true; await ack.promise; }
    return rpc(message, timeoutMs);
  };
  t.after(() => ack.resolve());
  const opening = conversation.startMicrophone().then(() => true, () => false);
  await until(() => transport.calls.some(call => call.message.type === 'open_input'));
  assert.equal(await host.request('input_pending'), true);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal((await host.request('status')).readers, 0);
  await host.request('subscribe', { track_sid: transport.microphones[0].trackSid });
  await until(() => ackWaiting);
  assert.equal((await host.request('status')).received, 0);
  assert.equal((await host.request('status')).readers, 0);
  ack.resolve();
  assert.equal(await opening, true);
  assert.equal(conversation.snapshot().inputAuthorized, true);
  assert.equal(await host.request('push'), 10);
});

test('購読待機中のgateと遅着後も同じ会話で明示的な再開始を必要とする', async t => {
  const { host, transport, conversation } = await fixture(t, true);
  const opening = assert.rejects(conversation.startMicrophone());
  await until(() => transport.calls.some(call => call.message.type === 'open_input'));
  assert.equal(await host.request('input_pending'), true);
  const oldSid = transport.microphones[0].trackSid;
  await conversation.setGate('mute', true);
  await opening;
  await host.request('subscribe', { track_sid: oldSid });
  await conversation.setGate('mute', false);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal(transport.microphones[0].closed, true);
  assert.equal((await host.request('status')).readers, 0);
  assert.equal((await host.request('status')).received, 0);
  const replacement = conversation.startMicrophone().then(() => true, () => false);
  await until(() => transport.microphones.length === 2 &&
    transport.calls.filter(call => call.message.type === 'open_input').length === 2);
  assert.equal(await host.request('input_pending'), true);
  await host.request('subscribe', { track_sid: transport.microphones[1].trackSid });
  assert.equal(await replacement, true);
  assert.equal(conversation.snapshot().inputAuthorized, true);
  assert.equal(await host.request('push'), 10);
});
