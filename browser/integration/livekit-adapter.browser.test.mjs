// 実ChromiumのWeb AudioとローカルWebRTCを検証する。SDK/Roomはdouble、実出音は未検証。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, after, test } from 'node:test';

const origin = 'http://127.0.0.1:41737';
let browser;
before(async () => {
  const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
    args: ['--autoplay-policy=no-user-gesture-required'] });
});
after(async () => { await browser?.close(); });

async function fixture(t) {
  // 先に本番moduleを読み、依存fixture不備と未実装による失敗を区別する。
  const source = await readFile(new URL('../livekit-adapter.mjs', import.meta.url), 'utf8');
  const context = await browser.newContext({ serviceWorkers: 'block' });
  t.after(() => context.close());
  const page = await context.newPage();
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    assert.equal(url.origin, origin, '共有サービスへ接続しない');
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html',
      body: '<!doctype html><meta charset="utf-8"><title>RTP準備の合成検証</title>' });
    if (url.pathname === '/livekit-adapter.mjs') return route.fulfill({
      contentType: 'text/javascript', body: source });
    if (/livekit-client/.test(url.pathname)) return route.fulfill({
      contentType: 'text/javascript', body: `
        export const RoomEvent = {DataReceived:'dataReceived',TrackSubscribed:'trackSubscribed',
          TrackUnsubscribed:'trackUnsubscribed',Disconnected:'disconnected',Reconnecting:'reconnecting',
          ParticipantDisconnected:'participantDisconnected',ParticipantConnected:'participantConnected'};
        export class Room { constructor() { return globalThis.room; } }
        export const Track = {Kind:{Audio:'audio'},Source:{Microphone:'microphone'}};
        export function createLocalAudioTrack(options) {
          return globalThis.sdk.createLocalAudioTrack(options);
        }` });
    return route.abort();
  });
  await page.goto(origin);
  await page.evaluate(() => {
    const listeners = new Map();
    globalThis.records = { rpc: [], capture: [], publish: [], stop: 0, unpublish: 0,
      sourceConnections: 0, sourceDisconnections: 0, mediaElementPlays: 0,
      notifications: [], notificationListenerBeforeConnect: false };
    globalThis.room = {
      remoteParticipants: new Map([['fixture-host', { identity: 'fixture-host', sid: 'PA-host' }]]),
      on(name, callback) { listeners.set(name, callback); return this; },
      off(name) { listeners.delete(name); return this; },
      emit(name, ...args) { listeners.get(name)?.(...args); },
      async connect(url, token) {
        records.notificationListenerBeforeConnect = listeners.has('dataReceived');
        records.url = url; records.token = token;
        this.emit('dataReceived', new TextEncoder().encode('{"v":1,"type":"host_state"}'),
          this.remoteParticipants.get('fixture-host'), 0, 'local-gpt-live.events.v1');
      },
      async disconnect() { records.disconnected = true; },
      localParticipant: { identity: 'fixture-user', sid: 'PA-fixture',
        async performRpc(options) { records.rpc.push(options); return '{"ok":true}'; },
        async publishTrack(track, options) {
          records.publish.push(options); return { trackSid: 'TR-microphone', track };
        },
        async unpublishTrack(track, stopOnUnpublish = true) {
          if (!track?.mediaStreamTrack) throw new TypeError('SDKはtrackを受け取る');
          records.unpublish += 1;
          records.enabledAtUnpublish = track.mediaStreamTrack.enabled;
          if (stopOnUnpublish) track.stop();
          return { trackSid: 'TR-microphone' };
        },
      },
    };
    globalThis.sdk = {
      RoomEvent: { DataReceived: 'dataReceived', TrackSubscribed: 'trackSubscribed',
        TrackUnsubscribed: 'trackUnsubscribed', Disconnected: 'disconnected',
        Reconnecting: 'reconnecting', ParticipantDisconnected: 'participantDisconnected',
        ParticipantConnected: 'participantConnected' },
      Track: { Kind: { Audio: 'audio' }, Source: { Microphone: 'microphone' } },
      async createLocalAudioTrack(options) {
        records.capture.push(options);
        const context = new AudioContext();
        const destination = context.createMediaStreamDestination();
        return { mediaStreamTrack: destination.stream.getAudioTracks()[0],
          async mute() { this.mediaStreamTrack.enabled = false; },
          stop() { records.stop += 1; this.mediaStreamTrack.stop(); void context.close(); } };
      },
    };
    const nativePlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      records.mediaElementPlays += 1; return nativePlay.call(this);
    };
  });
  return page;
}

test('通知購読を接続前に登録し公式RPC引数へ制御JSONとミリ秒期限を渡す', { timeout: 10000 }, async t => {
  const page = await fixture(t);
  const result = await page.evaluate(async () => {
    const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
    const context = new AudioContext();
    const adapter = createLiveKitAdapter({ sdk, room, audioContext: context,
      hostIdentity: 'fixture-host', onNotification: (...args) => records.notifications.push(args),
      onTrack() {}, onDisconnect() {} });
    await adapter.connect('ws://127.0.0.1:7880', 'synthetic-test-token');
    const message = { v: 1, type: 'text', session_id: 'session', binding: 'control',
      request_id: 'request', text: '{"type":"cancel"}' };
    const answer = await adapter.rpc(message, 500);
    await adapter.disconnect();
    await context.close();
    return { ...records, message, answer };
  });
  assert.equal(result.notificationListenerBeforeConnect, true);
  assert.equal(result.notifications.length, 1);
  assert.deepEqual(result.answer, { ok: true });
  assert.equal(result.rpc[0].destinationIdentity, 'fixture-host');
  assert.equal(result.rpc[0].method, 'local-gpt-live.control.v1');
  assert.equal(result.rpc[0].responseTimeout, 500);
  assert.deepEqual(JSON.parse(result.rpc[0].payload), result.message);
});

test('マイク取得にechoCancellationとnoiseSuppressionを渡し停止でpublish資源を解放する', { timeout: 10000 }, async t => {
  const page = await fixture(t);
  const result = await page.evaluate(async () => {
    const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
    const context = new AudioContext();
    const adapter = createLiveKitAdapter({ sdk, room, audioContext: context,
      hostIdentity: 'fixture-host', onNotification() {}, onTrack() {}, onDisconnect() {} });
    const microphone = await adapter.microphone();
    const sid = microphone.trackSid;
    microphone.suppress();
    await microphone.close();
    await adapter.disconnect();
    await context.close();
    return { ...records, sid };
  });
  assert.equal(result.capture[0].echoCancellation, true);
  assert.equal(result.capture[0].noiseSuppression, true);
  assert.equal(result.publish[0].dtx, false);
  assert.equal(result.sid, 'TR-microphone');
  assert.equal(result.stop, 1);
  assert.equal(result.unpublish, 1);
  assert.equal(result.enabledAtUnpublish, false);
});

test('無音のlive trackを実出力へ一度接続しPCMを待たず準備を完了する', { timeout: 10000 }, async t => {
  const page = await fixture(t);
  const result = await page.evaluate(async () => {
    const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
    const context = new AudioContext();
    const destination = context.createMediaStreamDestination();
    const track = { mediaStreamTrack: destination.stream.getAudioTracks()[0], kind: 'audio' };
    const createSource = context.createMediaStreamSource.bind(context);
    context.createMediaStreamSource = stream => {
      const node = createSource(stream);
      const connect = node.connect.bind(node), disconnect = node.disconnect.bind(node);
      node.connect = (...args) => { records.sourceConnections += 1; return connect(...args); };
      node.disconnect = (...args) => { records.sourceDisconnections += 1; return disconnect(...args); };
      return node;
    };
    const adapter = createLiveKitAdapter({ sdk, room, audioContext: context,
      hostIdentity: 'fixture-host', onNotification() {}, onTrack() {}, onDisconnect() {} });
    await context.resume();
    const output = await adapter.prepareOutput(track, () => {});
    const preparedState = context.state;
    output.close();
    await adapter.disconnect();
    track.mediaStreamTrack.stop();
    await context.close();
    return { ...records, preparedState };
  });
  assert.equal(result.preparedState, 'running');
  assert.equal(result.sourceConnections, 1);
  assert.equal(result.sourceDisconnections, 1);
  assert.equal(result.mediaElementPlays, 1, '消音elementでdecoderを駆動する');
  assert.equal(result.rpc.length, 0, '端末準備だけで実再生ACKを送らない');
});

test('autoplay拒否とended trackを準備成功として返さない', { timeout: 10000 }, async t => {
  const page = await fixture(t);
  const result = await page.evaluate(async () => {
    const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
    const context = new AudioContext();
    const destination = context.createMediaStreamDestination();
    const track = { mediaStreamTrack: destination.stream.getAudioTracks()[0], kind: 'audio' };
    context.resume = async () => { throw new DOMException('拒否', 'NotAllowedError'); };
    const adapter = createLiveKitAdapter({ sdk, room, audioContext: context,
      hostIdentity: 'fixture-host', onNotification() {}, onTrack() {}, onDisconnect() {} });
    const rejected = [];
    try { await adapter.prepareOutput(track, () => {}); rejected.push(false); } catch { rejected.push(true); }
    track.mediaStreamTrack.stop();
    try { await adapter.prepareOutput(track, () => {}); rejected.push(false); } catch { rejected.push(true); }
    await adapter.disconnect();
    await context.close();
    return { rejected, rpc: records.rpc };
  });
  assert.deepEqual(result.rejected, [true, true]);
  assert.deepEqual(result.rpc, []);
});

test('ローカルWebRTCのready後toneを製品adapterのWeb Audio出力で観測する', { timeout: 15000 }, async t => {
  const page = await fixture(t);
  const result = await page.evaluate(async () => {
    const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
    const context = new AudioContext();
    const sendContext = new AudioContext();
    const destination = sendContext.createMediaStreamDestination();
    const oscillator = sendContext.createOscillator();
    const gain = sendContext.createGain();
    oscillator.frequency.value = 440; gain.gain.value = 0.2;
    oscillator.connect(gain); gain.connect(destination);
    const sending = new RTCPeerConnection({iceServers:[]});
    const receiving = new RTCPeerConnection({iceServers:[]});
    const sender = sending.addTransceiver('audio', {direction:'sendonly'}).sender;
    const subscribed = new Promise(resolve => { receiving.ontrack = event => resolve(event.track); });
    async function gather(peer, description) {
      await peer.setLocalDescription(description);
      if (peer.iceGatheringState !== 'complete') await new Promise(resolve => {
        peer.addEventListener('icegatheringstatechange', function changed() {
          if (peer.iceGatheringState === 'complete') {
            peer.removeEventListener('icegatheringstatechange', changed); resolve();
          }
        });
      });
    }
    const analyser = context.createAnalyser();
    const createSource = context.createMediaStreamSource.bind(context);
    let source;
    context.createMediaStreamSource = stream => {
      source = createSource(stream); source.connect(analyser); return source;
    };
    let failure = null;
    const adapter = createLiveKitAdapter({sdk,room,audioContext:context,
      hostIdentity:'fixture-host',onNotification(){},onTrack(){},onDisconnect(){}});
    try {
      await gather(sending, await sending.createOffer());
      await receiving.setRemoteDescription(sending.localDescription);
      await gather(receiving, await receiving.createAnswer());
      await sending.setRemoteDescription(receiving.localDescription);
      const track = await subscribed;
      const beforeStats = [...(await receiving.getStats()).values()].find(value => value.type==='inbound-rtp' && value.kind==='audio');
      let timer;
      const output = await Promise.race([adapter.prepareOutput({mediaStreamTrack:track}, cause => {
        failure = cause.message;
      }), new Promise((_,reject) => { timer=setTimeout(() => reject(new Error('PCM待ちによる循環')),1000); })])
        .finally(() => clearTimeout(timer));
      await sender.replaceTrack(destination.stream.getAudioTracks()[0]);
      await sendContext.resume(); oscillator.start();
      const samples = new Float32Array(analyser.fftSize);
      let peak = 0, stats;
      const deadline = performance.now()+5000;
      while (performance.now()<deadline && (peak<0.02 || !(stats?.packetsReceived>0))) {
        analyser.getFloatTimeDomainData(samples);
        peak = Math.max(peak,...samples.map(Math.abs));
        stats = [...(await receiving.getStats()).values()].find(value => value.type==='inbound-rtp' && value.kind==='audio');
        await new Promise(resolve => setTimeout(resolve,20));
      }
      output.close();
      return {beforePackets:beforeStats?.packetsReceived || 0,peak,failure,plays:records.mediaElementPlays,
        packets:stats?.packetsReceived,energy:stats?.totalAudioEnergy};
    } finally {
      await adapter.disconnect(); sending.close(); receiving.close();
      destination.stream.getTracks().forEach(track => track.stop());
      await context.close(); await sendContext.close();
    }
  });
  assert.equal(result.beforePackets, 0, 'ready準備前にはRTPを送信していない');
  assert.equal(result.failure, null);
  assert.equal(result.plays, 1);
  assert.ok(result.packets > 0, JSON.stringify(result));
  assert.ok(result.peak > 0.02, JSON.stringify(result));
  t.diagnostic(JSON.stringify(result));
});

for (const scenario of ['reject', 'late-reject', 'released-reject', 'pending']) {
  test(`消音decoderのplayと資源解放: ${scenario}`, async t => {
    const page = await fixture(t);
    const result = await page.evaluate(async scenario => {
      const {createLiveKitAdapter} = await import('/livekit-adapter.mjs');
      const context = new AudioContext(), destination = context.createMediaStreamDestination();
      let decoder, rejectPlay, failures = 0;
      HTMLMediaElement.prototype.play = function () {
        decoder = this;
        return scenario === 'reject' ? Promise.reject(new DOMException('拒否','NotAllowedError')) :
          new Promise((_,reject) => { rejectPlay = reject; });
      };
      const adapter = createLiveKitAdapter({sdk,room,audioContext:context,
        hostIdentity:'fixture-host',onNotification(){},onTrack(){},onDisconnect(){}});
      let output, rejected = false;
      try { output = await adapter.prepareOutput({mediaStreamTrack:destination.stream.getAudioTracks()[0]}, () => { failures++; }); }
      catch { rejected = true; }
      const muted = decoder.muted && decoder.volume===0;
      if (scenario==='released-reject') output.close();
      if (scenario==='late-reject' || scenario==='released-reject') {
        rejectPlay(new DOMException('拒否','NotAllowedError')); await Promise.resolve();
      }
      if (scenario==='pending') await adapter.disconnect();
      const cleared = decoder.srcObject===null;
      await adapter.disconnect(); destination.stream.getTracks().forEach(track => track.stop());
      await context.close();
      return {rejected,muted,cleared,failures};
    },scenario);
    assert.equal(result.muted, true);
    assert.equal(result.cleared, true);
    assert.equal(result.rejected, scenario==='reject');
    assert.equal(result.failures, ['reject','late-reject'].includes(scenario) ? 1 : 0);
  });
}

test('初期通知の送信者未解決を拒否しparticipant確認後だけ専用RPCで同期する', { timeout: 10000 }, async t => {
  const page = await fixture(t);
  const result = await page.evaluate(async () => {
    const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
    const context = new AudioContext();
    const adapter = createLiveKitAdapter({ sdk, room, audioContext: context,
      hostIdentity: 'fixture-host', onNotification: (...args) => records.notifications.push(args),
      onTrack() {}, onDisconnect() {} });
    room.remoteParticipants.clear();
    room.emit('dataReceived', new TextEncoder().encode('{"v":1,"type":"host_state"}'),
      undefined, 0, 'local-gpt-live.events.v1');
    room.localParticipant.performRpc = async options => {
      records.rpc.push(options);
      return JSON.stringify({ok:true,v:1,type:'host_state',session_id:'session',connection_id:'connection'});
    };
    const waiting = adapter.requestState({hostIdentity:'fixture-host',hostSid:'PA-host',
      sessionId:'session',connectionId:'connection'});
    const before = records.rpc.length;
    const host = {identity:'fixture-host',sid:'PA-host'};
    room.remoteParticipants.set(host.identity, host);
    room.emit('participantConnected', host);
    const state = await waiting;
    await adapter.disconnect(); await context.close();
    return {before,state,...records};
  });
  assert.equal(result.before, 0);
  assert.deepEqual(result.notifications, []);
  assert.equal(result.rpc.length, 1);
  assert.equal(result.rpc[0].method, 'local-gpt-live.state.v1');
  assert.deepEqual(JSON.parse(result.rpc[0].payload), {v:1,session_id:'session',connection_id:'connection'});
  assert.equal(result.state.sender.sid, 'PA-host');
});

for (const scenario of ['wrong-sid', 'wrong-session', 'wrong-connection', 'replaced-sid', 'disconnect', 'wait-disconnect']) {
  test(`状態同期は不一致または遅着応答を採用しない: ${scenario}`, { timeout: 10000 }, async t => {
    const page = await fixture(t);
    const result = await page.evaluate(async scenario => {
      const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
      const context = new AudioContext();
      const adapter = createLiveKitAdapter({ sdk, room, audioContext: context,
        hostIdentity: 'fixture-host', onNotification() {}, onTrack() {}, onDisconnect() {} });
      let finish, invoked;
      const called = new Promise(resolve => { invoked = resolve; });
      const response = new Promise(resolve => { finish = resolve; });
      room.localParticipant.performRpc = async options => {
        records.rpc.push(options); invoked(); return response;
      };
      if (scenario === 'wrong-sid') room.remoteParticipants.get('fixture-host').sid = 'PA-other';
      if (scenario === 'wait-disconnect') room.remoteParticipants.clear();
      const pending = adapter.requestState({hostIdentity:'fixture-host',hostSid:'PA-host',
        sessionId:'session',connectionId:'connection'}).then(() => false, () => true);
      if (scenario === 'wait-disconnect') await adapter.disconnect();
      else if (scenario !== 'wrong-sid') {
        await called;
        if (scenario === 'replaced-sid') room.remoteParticipants.get('fixture-host').sid = 'PA-new';
        if (scenario === 'disconnect') await adapter.disconnect();
        finish(JSON.stringify({ok:true,v:1,type:'host_state',
          session_id:scenario === 'wrong-session' ? 'old' : 'session',
          connection_id:scenario === 'wrong-connection' ? 'old' : 'connection'}));
      }
      const rejected = await pending;
      await adapter.disconnect(); await context.close();
      return {rejected,calls:records.rpc.length};
    }, scenario);
    assert.equal(result.rejected, true);
    assert.equal(result.calls, ['wrong-sid', 'wait-disconnect'].includes(scenario) ? 0 : 1);
  });
}

test('公式RPCのtimeout codeを本文を公開しない固定エラーへ変換する', { timeout: 10000 }, async t => {
  const page = await fixture(t);
  const result = await page.evaluate(async () => {
    const { createLiveKitAdapter } = await import('/livekit-adapter.mjs');
    const context = new AudioContext();
    const adapter = createLiveKitAdapter({ sdk, room, audioContext: context,
      hostIdentity: 'fixture-host', onNotification() {}, onTrack() {}, onDisconnect() {} });
    room.localParticipant.performRpc = async () => {
      throw Object.assign(new Error('synthetic-private-error'), { code: 1502 });
    };
    let message;
    try { await adapter.rpc({ type: 'text' }, 500); }
    catch (error) { message = error.message; }
    await adapter.disconnect();
    await context.close();
    return message;
  });
  assert.equal(result, 'rpc_timeout');
});
