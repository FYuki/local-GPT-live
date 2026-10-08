import assert from 'node:assert/strict';
import { test } from 'node:test';
import { connection, sender, state, event, deferred, until, transportDouble } from './demo-fixture.mjs';

async function fixture(t, initialize = true) {
  const { createConversation } = await import('../conversation.mjs');
  const transport = transportDouble();
  const views = [];
  let now = 0;
  const conversation = createConversation({ transport, connection: { ...connection },
    onChange: view => views.push(structuredClone(view)), now: () => now });
  t.after(() => conversation.disconnect());
  if (initialize) conversation.acceptEvent(event('host_state'), sender);
  return { conversation, transport, views, advance: milliseconds => { now += milliseconds; } };
}

test('初期host状態の受領前には制御を送らない', async t => {
  const { conversation, transport } = await fixture(t, false);
  await conversation.submitText('合成入力').catch(() => {});
  assert.equal(transport.calls.length, 0);
  conversation.acceptEvent(event('host_state'), sender);
  await conversation.submitText('合成入力');
  assert.equal(transport.calls[0].message.type, 'text');
});

test('初期通知以外または不正な状態順序で制御を有効化しない', async t => {
  const { conversation, transport } = await fixture(t, false);
  conversation.acceptEvent(event('response_started'), sender);
  conversation.acceptEvent(event('host_state', { state_sequence: 1.5 }), sender);
  await conversation.submitText('合成入力').catch(() => {});
  assert.equal(transport.calls.length, 0);
  conversation.acceptEvent(event('host_state'), sender);
  await conversation.submitText('合成入力');
  assert.equal(transport.calls.length, 1);
});

for (const text of ['{"type":"cancel"}', '"cancel"', '/* cancel */ {}',
  '```json\n{"type":"cancel"}\n```', '~~~json\n{"type":"cancel"}\n~~~',
  '{"nested":{"type":"cancel"}}', '```json\n{"type":"cancel"}']) {
  test(`text本文を制御として解釈せず配送する: ${JSON.stringify(text)}`, async t => {
    const { conversation, transport } = await fixture(t);
    await conversation.submitText(text);
    assert.equal(transport.calls.length, 1);
    assert.equal(transport.calls[0].message.type, 'text');
    assert.equal(transport.calls[0].message.text, text);
  });
}

for (const mismatch of [{ identity: 'other-host' }, { sid: 'PA-old' }, { topic: 'other-topic' }]) {
  test(`通知の送信者とtopicの不一致を拒否する: ${JSON.stringify(mismatch)}`, async t => {
    const { conversation, transport } = await fixture(t, false);
    conversation.acceptEvent(event('host_state'), { ...sender, ...mismatch });
    await conversation.submitText('合成入力').catch(() => {});
    assert.equal(transport.calls.length, 0);
    conversation.acceptEvent(event('host_state'), sender);
    await conversation.submitText('合成入力');
    assert.equal(transport.calls.length, 1);
  });
}

for (const fields of [{ session_id: 'old-session' }, { connection_id: 'old-connection' }]) {
  test(`別Sessionまたは旧接続の状態を採用しない: ${JSON.stringify(fields)}`, async t => {
    const { conversation, transport } = await fixture(t);
    conversation.acceptEvent(event('host_state', { ...fields, state_sequence: 99,
      control_binding: 'old-control', input_revision: 99 }), sender);
    await conversation.submitText('合成入力');
    assert.equal(transport.calls[0].message.binding, 'control-initial');
  });
}

test('ACK待ちのinput_activeを正式入力中として表示しない', async t => {
  const { conversation, transport, views } = await fixture(t);
  const ack = deferred();
  const rpc = transport.rpc.bind(transport);
  transport.rpc = async (message, budget) => {
    const result = await rpc(message, budget);
    return message.type === 'input_ack' ? ack.promise.then(() => result) : result;
  };
  const opening = conversation.startMicrophone();
  await until(() => transport.calls.some(call => call.message.type === 'input_ack'));
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal(views.at(-1).inputAuthorized, false);
  assert.equal(transport.calls[0].message.track_sid, transport.microphones[0].trackSid);
  assert.deepEqual(transport.calls[1].message.grant, {
    track_sid: transport.microphones[0].trackSid,
    request_id: transport.calls[0].message.request_id, input_generation: 1, input_revision: 1,
  });
  ack.resolve();
  await opening;
  assert.equal(conversation.snapshot().inputAuthorized, true);
  assert.equal(views.at(-1).inputAuthorized, true);
});

test('text送信で既存マイクを失効し遅着入力失効通知で新認可を止めない', async t => {
  const { conversation, transport } = await fixture(t);
  await conversation.startMicrophone();
  await conversation.submitText('合成入力');
  assert.equal(transport.microphones[0].closed, true);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  await conversation.startMicrophone();
  assert.equal(conversation.snapshot().inputAuthorized, true);
  conversation.acceptEvent(event('input_invalidated', { state_sequence: 2,
    control_binding: 'control-1', input_revision: 1, input_active: false }), sender);
  assert.equal(conversation.snapshot().inputAuthorized, true);
  assert.equal(transport.microphones[1].closed, false);
});

test('再生準備の拒否を明示しreadyを成功扱いしない', async t => {
  const { conversation, transport } = await fixture(t);
  transport.prepareOutput = async () => { throw new DOMException('拒否', 'NotAllowedError'); };
  conversation.acceptEvent(event('output_track', { state_sequence: 3, response_id: 'response',
    active_response_id: 'response', binding: 'ack', track_sid: 'TR-output' }), sender);
  conversation.subscribeOutput({ trackSid: 'TR-output', participantIdentity: sender.identity,
    participantSid: sender.sid, track: { kind: 'audio' } });
  await until(() => conversation.snapshot().error);
  assert.equal(transport.calls.filter(call => call.message.type === 'confirm_output_ready').length, 0);
});

for (const field of ['track_sid', 'request_id', 'input_revision']) {
  test(`開始操作と違うgrantをACKしない: ${field}`, async t => {
    const { conversation, transport } = await fixture(t);
    const rpc = transport.rpc.bind(transport);
    transport.rpc = async (message, budget) => {
      const result = await rpc(message, budget);
      if (message.type === 'open_input') result.grant[field] = field === 'input_revision' ? 99 : 'other';
      return result;
    };
    await conversation.startMicrophone().catch(() => {});
    assert.equal(transport.calls.filter(call => call.message.type === 'input_ack').length, 0);
    assert.equal(conversation.snapshot().inputAuthorized, false);
    assert.equal(transport.microphones[0].closed, true);
  });
}

test('openとACKの待機を同じ5秒予算に収める', async t => {
  const { conversation, transport, advance } = await fixture(t);
  const rpc = transport.rpc.bind(transport);
  transport.rpc = async (message, budget) => {
    const result = await rpc(message, budget);
    if (message.type === 'open_input') { advance(4500); result.remaining_ms = 700; }
    if (message.type === 'input_ack') advance(501);
    return result;
  };
  await conversation.startMicrophone().catch(() => {});
  const ack = transport.calls.find(call => call.message.type === 'input_ack');
  assert.ok(ack.timeoutMs > 0 && ack.timeoutMs <= 500);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal(transport.microphones[0].closed, true);
  assert.ok(conversation.snapshot().error);
});

test('permission拒否を明示し再試行で新しい入力を開始できる', async t => {
  const { conversation, transport } = await fixture(t);
  const microphone = transport.microphone.bind(transport);
  transport.microphone = async () => { throw new DOMException('拒否', 'NotAllowedError'); };
  await conversation.startMicrophone().catch(() => {});
  assert.ok(conversation.snapshot().error);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal(transport.calls.length, 0);
  transport.microphone = microphone;
  await conversation.startMicrophone();
  assert.equal(conversation.snapshot().inputAuthorized, true);
});

for (const gate of ['mute', 'focus']) {
  test(`${gate}解除後は同じcontrollerで新trackと認可を取得する`, async t => {
    const { conversation, transport } = await fixture(t);
    await conversation.startMicrophone();
    assert.equal(conversation.snapshot().inputAuthorized, true);
    const old = transport.microphones[0];
    await conversation.setGate(gate, true);
    assert.equal(old.suppressed, true);
    assert.equal(old.closed, true);
    assert.equal(conversation.snapshot().inputAuthorized, false);
    await conversation.setGate(gate, false);
    assert.equal(conversation.snapshot().inputAuthorized, false);
    await conversation.startMicrophone();
    const opens = transport.calls.filter(call => call.message.type === 'open_input');
    assert.notEqual(opens[1].message.track_sid, opens[0].message.track_sid);
    assert.ok(opens[1].message.expected_revision > opens[0].message.expected_revision);
    assert.equal(conversation.snapshot().inputAuthorized, true);
  });
}

test('pending openを待たずに停止し遅着grantからACKを送らない', async t => {
  const { conversation, transport } = await fixture(t);
  const prepared = deferred();
  const rpc = transport.rpc.bind(transport);
  transport.rpc = async (message, budget) => {
    const result = await rpc(message, budget);
    return message.type === 'open_input' ? prepared.promise.then(() => result) : result;
  };
  const opening = conversation.startMicrophone().catch(() => {});
  await until(() => transport.calls.some(call => call.message.type === 'open_input'));
  await conversation.setGate('mute', true);
  assert.equal(transport.microphones[0].suppressed, true);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  prepared.resolve();
  await opening;
  assert.equal(transport.calls.filter(call => call.message.type === 'input_ack').length, 0);
  assert.equal(transport.microphones[0].closed, true);
});

test('切断後のpermission帰還がマイクを再開しない', async t => {
  const { conversation, transport } = await fixture(t);
  const permission = deferred();
  const microphone = transport.microphone.bind(transport);
  transport.microphone = () => permission.promise;
  const opening = conversation.startMicrophone().catch(() => {});
  await conversation.disconnect();
  const late = await microphone();
  permission.resolve(late);
  await opening;
  assert.equal(late.closed, true);
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal(transport.calls.filter(call => call.message.type === 'open_input').length, 0);
});

test('host拒否の現在状態を次操作に使い拒否結果を成功にしない', async t => {
  const { conversation, transport } = await fixture(t);
  transport.rpc = async (message, timeoutMs) => {
    transport.calls.push({ message, timeoutMs });
    return { ok: false, reason: 'stale_binding', ...state({ state_sequence: 20,
      control_binding: 'control-current', input_revision: 4 }) };
  };
  await conversation.submitText('合成入力').catch(() => {});
  assert.ok(conversation.snapshot().error);
  assert.equal(conversation.snapshot().outputActive, false);
  assert.equal(transport.calls.length, 1);
  await conversation.submitText('別の明示入力').catch(() => {});
  assert.equal(transport.calls[1].message.binding, 'control-current');
});

for (const trackFirst of [true, false]) {
  test(`古い共通状態でも現応答のmetadataを購読へ結合する: track先着=${trackFirst}`, async t => {
    const { conversation, transport } = await fixture(t);
    conversation.acceptEvent(event('response_started', { state_sequence: 10,
      control_binding: 'current-control', active_response_id: 'voice-response',
      response_id: 'voice-response' }), sender);
    const track = { trackSid: 'TR-output', participantIdentity: sender.identity,
      participantSid: sender.sid, track: { kind: 'audio' } };
    const metadata = event('output_track', { state_sequence: 9, response_id: 'voice-response',
      active_response_id: 'voice-response', control_binding: 'current-control',
      binding: 'ack-binding', track_sid: 'TR-output' });
    if (trackFirst) conversation.subscribeOutput(track);
    conversation.acceptEvent(metadata, sender);
    if (!trackFirst) conversation.subscribeOutput(track);
    await until(() => transport.calls.some(call => call.message.type === 'confirm_output_ready'));
    const ready = transport.calls.find(call => call.message.type === 'confirm_output_ready').message;
    assert.equal(ready.binding, 'current-control');
    assert.equal(ready.response_id, 'voice-response');
    assert.equal(ready.track_sid, 'TR-output');
    assert.equal(transport.outputs.length, 1);
  });
}

test('output_track先着後のresponse_startedがmetadataを消さない', async t => {
  const { conversation, transport } = await fixture(t);
  conversation.acceptEvent(event('output_track', { state_sequence: 4, response_id: 'voice-response',
    active_response_id: 'voice-response', control_binding: 'voice-control',
    binding: 'ack-binding', track_sid: 'TR-output' }), sender);
  conversation.acceptEvent(event('response_started', { state_sequence: 3,
    active_response_id: 'voice-response', control_binding: 'voice-control',
    response_id: 'voice-response' }), sender);
  conversation.subscribeOutput({ trackSid: 'TR-output', participantIdentity: sender.identity,
    participantSid: sender.sid, track: { kind: 'audio' } });
  await until(() => transport.calls.some(call => call.message.type === 'confirm_output_ready'));
  assert.equal(transport.outputs.length, 1);
});

test('metadata単独と別participantの購読からreadyを作らない', async t => {
  const { conversation, transport } = await fixture(t);
  conversation.acceptEvent(event('output_track', { state_sequence: 3, response_id: 'response',
    active_response_id: 'response', binding: 'ack', track_sid: 'TR-output' }), sender);
  conversation.subscribeOutput({ trackSid: 'TR-output', participantIdentity: 'other',
    participantSid: 'PA-other', track: { kind: 'audio' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(transport.outputs.length, 0);
  assert.equal(transport.calls.length, 0);
  conversation.subscribeOutput({ trackSid: 'TR-output', participantIdentity: sender.identity,
    participantSid: sender.sid, track: { kind: 'audio' } });
  await until(() => transport.calls.length > 0);
  assert.equal(transport.calls[0].message.type, 'confirm_output_ready');
});

test('取消後の再生準備帰還を解放しreadyを送らない', async t => {
  const { conversation, transport } = await fixture(t);
  const preparation = deferred();
  transport.prepareOutput = () => preparation.promise;
  conversation.acceptEvent(event('output_track', { state_sequence: 3, response_id: 'response',
    active_response_id: 'response', binding: 'ack', track_sid: 'TR-output' }), sender);
  conversation.subscribeOutput({ trackSid: 'TR-output', participantIdentity: sender.identity,
    participantSid: sender.sid, track: { kind: 'audio' } });
  await conversation.cancel();
  let released = false;
  preparation.resolve({ close() { released = true; } });
  await until(() => released);
  assert.equal(transport.calls.filter(call => call.message.type === 'confirm_output_ready').length, 0);
  assert.equal(conversation.snapshot().outputActive, false);
});

test('decoderの準備中のplay拒否ではreadyを送らず返却資源も解放する', async t => {
  const { conversation, transport } = await fixture(t);
  const prepare = transport.prepareOutput.bind(transport);
  transport.prepareOutput = async (track, onFailure) => {
    const resource = await prepare(track, onFailure);
    onFailure(new DOMException('拒否', 'NotAllowedError'));
    return resource;
  };
  conversation.acceptEvent(event('output_track', { state_sequence: 3, response_id: 'response',
    active_response_id: 'response', binding: 'ack', track_sid: 'TR-output' }), sender);
  conversation.subscribeOutput({ trackSid: 'TR-output', participantIdentity: sender.identity,
    participantSid: sender.sid, track: { kind: 'audio' } });
  await until(() => transport.outputs[0]?.released);
  assert.equal(transport.calls.filter(call => call.message.type === 'confirm_output_ready').length, 0);
  assert.equal(conversation.snapshot().outputActive, false);
  assert.match(conversation.snapshot().error, /拒否/);
});

test('decoderの遅いplay拒否を失敗表示し旧応答の失敗を次応答へ適用しない', async t => {
  const { conversation, transport } = await fixture(t);
  for (const [index, response] of ['first', 'next'].entries()) {
    conversation.acceptEvent(event('output_track', { state_sequence: 10 + index,
      response_id: response, active_response_id: response, control_binding: `control-${response}`,
      binding: `ack-${response}`, track_sid: `TR-${response}` }), sender);
    conversation.subscribeOutput({ trackSid: `TR-${response}`, participantIdentity: sender.identity,
      participantSid: sender.sid, track: { kind: 'audio' } });
    await until(() => transport.calls.filter(call => call.message.type === 'confirm_output_ready').length === index + 1);
  }
  transport.outputs[0].onFailure(new DOMException('拒否', 'NotAllowedError'));
  assert.equal(conversation.snapshot().error, null);
  assert.equal(transport.outputs[1].released, false);
  transport.outputs[1].onFailure(new DOMException('拒否', 'NotAllowedError'));
  assert.equal(transport.outputs[1].released, true);
  assert.equal(conversation.snapshot().outputActive, false);
  assert.equal(conversation.snapshot().outputState, '失敗');
  assert.match(conversation.snapshot().error, /拒否/);
});

test('重複metadataで二重再生せず応答交代で旧出力接続を解放する', async t => {
  const { conversation, transport } = await fixture(t);
  for (const [index, response] of ['first', 'next'].entries()) {
    const metadata = event('output_track', { state_sequence: 10 + index,
      response_id: response, active_response_id: response, control_binding: `control-${response}`,
      binding: `ack-${response}`, track_sid: `TR-${response}` });
    conversation.acceptEvent(metadata, sender);
    conversation.subscribeOutput({ trackSid: metadata.track_sid, participantIdentity: sender.identity,
      participantSid: sender.sid, track: { kind: 'audio' } });
    await until(() => transport.outputs.length === index + 1);
    conversation.acceptEvent(metadata, sender);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(transport.outputs.length, index + 1);
  }
  assert.equal(transport.outputs[0].released, true);
  assert.equal(transport.outputs[1].released, false);
  assert.equal(transport.calls.filter(call => ['playback_ack', 'playback_complete']
    .includes(call.message.type)).length, 0);
  await conversation.disconnect();
  assert.equal(transport.outputs[1].released, true);
});

test('生成完了と推定終了を分け同じcontrollerで次発話を受け付ける', async t => {
  const { conversation, transport } = await fixture(t);
  await conversation.submitText('最初の合成入力');
  const first = transport.calls[0].message;
  conversation.acceptEvent(event('generation_completed', { state_sequence: 5,
    control_binding: 'control-1', response_id: 'response-1', active_response_id: 'response-1',
    final_audio_sequence: 0 }), sender);
  assert.equal(conversation.snapshot().outputActive, true);
  conversation.acceptEvent(event('output_estimated_completed', { state_sequence: 6,
    control_binding: 'control-1', response_id: 'response-1', active_response_id: null,
    basis: 'sdk_submitted_elapsed', real_playback_confirmed: false, estimated_complete: true }), sender);
  assert.equal(conversation.snapshot().outputActive, false);
  assert.equal(conversation.snapshot().realPlaybackConfirmed, false);
  transport.setState(state({ state_sequence: 6, control_binding: 'control-1',
    input_revision: 1, active_response_id: null }));
  await conversation.submitText('次の合成入力');
  assert.equal(conversation.snapshot().outputActive, true);
  assert.notEqual(transport.calls[1].message.request_id, first.request_id);
  conversation.acceptEvent(event('output_estimated_completed', { state_sequence: 5,
    control_binding: 'control-1', response_id: 'response-1', active_response_id: null }), sender);
  assert.equal(conversation.snapshot().outputActive, true);
  assert.equal(transport.calls.filter(call => ['playback_ack', 'playback_complete']
    .includes(call.message.type)).length, 0);
});

test('論理再接続後に遅着ACKが入力を有効化せず新trackで再認可する', async t => {
  const { conversation, transport } = await fixture(t);
  const ack = deferred();
  const rpc = transport.rpc.bind(transport);
  let first = true;
  transport.rpc = async (message, budget) => {
    const result = await rpc(message, budget);
    if (message.type === 'input_ack' && first) { first = false; return ack.promise.then(() => result); }
    return result;
  };
  const opening = conversation.startMicrophone().catch(() => {});
  await until(() => transport.calls.some(call => call.message.type === 'input_ack'));
  await conversation.reconnect();
  ack.resolve();
  await opening;
  assert.equal(conversation.snapshot().inputAuthorized, false);
  assert.equal(transport.microphones[0].closed, true);
  await conversation.startMicrophone();
  assert.notEqual(transport.microphones[1].trackSid, transport.microphones[0].trackSid);
  assert.equal(conversation.snapshot().inputAuthorized, true);
});

test('同値のgate解除で正式入力の端末資源を停止しない', async t => {
  const { conversation, transport } = await fixture(t);
  await conversation.startMicrophone();
  await conversation.setGate('focus', false);
  assert.equal(conversation.snapshot().inputAuthorized, true);
  assert.equal(transport.microphones[0].closed, false);
});

test('host入力期限拒否をtimeoutとして表示しACKを送らない', async t => {
  const { conversation, transport } = await fixture(t);
  transport.rpc = async (message, timeoutMs) => {
    transport.calls.push({ message, timeoutMs });
    return { ok: false, reason: 'input_timeout', ...state({ state_sequence: 2 }) };
  };
  await conversation.startMicrophone().catch(() => {});
  assert.match(conversation.snapshot().error, /タイムアウト/);
  assert.equal(transport.calls.filter(call => call.message.type === 'input_ack').length, 0);
  assert.equal(transport.microphones[0].closed, true);
});

test('重複する切断呼出しは同じ資源解放の完了を待つ', async t => {
  const { conversation, transport } = await fixture(t);
  await conversation.submitText('合成入力');
  const closing = deferred();
  transport.disconnect = () => closing.promise;
  let completed = false;
  const first = conversation.disconnect();
  const second = conversation.disconnect().then(() => { completed = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(completed, false);
  assert.equal(conversation.snapshot().outputActive, false);
  assert.match(conversation.snapshot().outputState, /切断/);
  closing.resolve();
  await Promise.all([first, second]);
  assert.equal(completed, true);
});
