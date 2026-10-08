// 実HTML入口の操作試験。公式SDK境界を置換し、実マイク・実RTCは使わない。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, after, test } from 'node:test';

const origin = 'http://127.0.0.1:41738';
const token = 'synthetic-demo-token';
let browser;
const sdkFixture = `
globalThis.__order = [];
const nativeResume = AudioContext.prototype.resume;
AudioContext.prototype.resume = async function() {
  __order.push('resume');
  if (globalThis.__holdResume) await new Promise(resolve => { globalThis.__releaseResume = resolve; });
  if (globalThis.__resumeError) throw new Error('synthetic-private-resume');
  await nativeResume.call(this); __order.push('resumed');
};
export const RoomEvent = { DataReceived:'dataReceived', TrackSubscribed:'trackSubscribed',
  TrackUnsubscribed:'trackUnsubscribed', Disconnected:'disconnected', Reconnecting:'reconnecting',
  ParticipantDisconnected:'participantDisconnected', ParticipantConnected:'participantConnected' };
export const Track = {Kind:{Audio:'audio'},Source:{Microphone:'microphone'}};
export class Room {
  constructor() {
    globalThis.__fixtureRoom = this;
    this.listeners = new Map(); this.calls = []; this.publications = 0;
    this.tracks = []; this.unpublished = []; this.stops = [];
    this.stateSequence = 0; this.connects = 0; this.responses = 0;
    this.current = {session_id:'session-fixture',connection_id:'connection-fixture',
      control_binding:'control-initial',input_revision:0,input_active:false,muted:false,focused:false,
      active_response_id:null,closed:false};
    this.remoteParticipants = new Map([['fixture-host',{identity:'fixture-host',sid:'PA-host'}]]);
    this.localParticipant = {identity:'fixture-user',sid:'PA-fixture',
      performRpc: async options => {
        const message = JSON.parse(options.payload); this.calls.push(message);
        __order.push('rpc:'+message.type+':'+message.enabled);
        if (this.holdType && this.holdType === message.type && (this.holdEnabled === undefined || this.holdEnabled === message.enabled)) {
          await new Promise(resolve => { this.releaseRpc = resolve; });
          __order.push('released:'+message.type);
        }
        if (this.rpcError && this.rpcErrorType === message.type) {
          throw Object.assign(new Error('synthetic-private-error'), {code:this.rpcError});
        }
        if (options.method === 'local-gpt-live.state.v1') {
          if (globalThis.__stateTimeout) throw Object.assign(new Error('synthetic-private-error'), {code:1502});
          if (message.session_id !== this.current.session_id || message.connection_id !== this.current.connection_id) {
            return JSON.stringify({ok:false,reason:'stale_binding'});
          }
          return JSON.stringify({ok:true,v:1,type:'host_state',...this.current,state_sequence:++this.stateSequence});
        }
        if (this.rejectNext && (!this.rejectNextType || this.rejectNextType === message.type)) {
          const reason=this.rejectNext;this.rejectNext=null;
          return JSON.stringify({...this.current,state_sequence:++this.stateSequence,
            binding:this.current.control_binding,ok:false,reason});
        }
        let extra = {};
        if (message.type === 'open_input') {
          this.current.input_revision++; this.current.input_active=true;
          extra = {remaining_ms:5000,grant:{track_sid:message.track_sid,
            request_id:message.request_id,input_generation:this.current.input_revision,
            input_revision:this.current.input_revision}};
        } else if (message.type==='mute' || message.type==='focus') {
          this.current[message.type==='mute'?'muted':'focused']=message.enabled;
          if (message.enabled) { this.current.input_revision++;this.current.input_active=false; }
        } else if (['text','cancel','reconnect','close'].includes(message.type)) {
          this.current.input_revision++; this.current.input_active=false;
          this.current.control_binding='control-'+(++this.responses);
          this.current.active_response_id=message.type==='text'?'response-'+this.responses:null;
          if (message.type==='text') extra.response_id=this.current.active_response_id;
        }
        const result = {...this.current,state_sequence:++this.stateSequence,
          binding:this.current.control_binding,...extra,ok:true};
        if (message.type === 'open_input' && this.remainingMs !== undefined) result.remaining_ms = this.remainingMs;
        if (message.type === 'input_ack' && this.ackTime !== undefined) globalThis.__now = this.ackTime;
        __order.push('response:'+message.type+':'+message.enabled);
        return JSON.stringify(result);
      },
      publishTrack: async track => { this.tracks.push(track); return {trackSid:'TR-mic-'+(++this.publications),track}; },
      unpublishTrack: async track => { this.unpublished.push(track); if (this.closeError) throw new Error('synthetic-private-cleanup'); },
    };
  }
  on(name, callback) { const handlers=this.listeners.get(name)||[];handlers.push(callback);
    this.listeners.set(name,handlers);return this; }
  off(name, callback) { this.listeners.set(name,(this.listeners.get(name)||[]).filter(h=>h!==callback));return this; }
  emit(name,...args) { for(const h of this.listeners.get(name)||[]) h(...args); }
  async connect(url,token) {
    this.connects++;this.url=url;this.token=token;
    this.listenerBeforeConnect=(this.listeners.get('dataReceived')||[]).length>0;
    if (globalThis.__unresolvedInitial) this.remoteParticipants.clear();
    this.emit('dataReceived',new TextEncoder().encode(JSON.stringify({v:1,type:'host_state',
      ...this.current,state_sequence:++this.stateSequence})),
      this.remoteParticipants.get('fixture-host'),0,'local-gpt-live.events.v1');
  }
  async disconnect() { this.emit('disconnected'); }
}
export async function createLocalAudioTrack(options) {
  __order.push('microphone');
  if (globalThis.__denyMicrophone) throw new DOMException('拒否','NotAllowedError');
  if (globalThis.__microphoneError) throw new Error('synthetic-private-error');
  const context=new AudioContext();const destination=context.createMediaStreamDestination();
  const mediaStreamTrack=destination.stream.getAudioTracks()[0];
  return {mediaStreamTrack, async mute(){mediaStreamTrack.enabled=false;},
    stop(){__fixtureRoom.stops.push(mediaStreamTrack.id);mediaStreamTrack.stop();void context.close();}};
}
`;

before(async () => {
  const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
    args: ['--autoplay-policy=no-user-gesture-required'] });
});
after(async () => { await browser?.close(); });

async function fixture(t) {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const context = await browser.newContext({ serviceWorkers: 'block' });
  t.after(() => context.close());
  const page = await context.newPage(), logs = [], errors = [];
  page.setDefaultTimeout(5000);
  page.on('console', message => logs.push(message.text()));
  page.on('pageerror', error => errors.push(String(error)));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    assert.equal(url.origin, origin, '合成試験から外部へ通信しない');
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: html });
    if (/livekit-client/.test(url.pathname)) return route.fulfill({
      contentType: 'text/javascript', body: sdkFixture });
    const filename = url.pathname.slice(1);
    if (!['demo.mjs', 'conversation.mjs', 'livekit-adapter.mjs'].includes(filename)) return route.abort();
    return route.fulfill({ contentType: 'text/javascript',
      body: await readFile(new URL(`../${filename}`, import.meta.url), 'utf8') });
  });
  await page.goto(origin);
  t.after(() => assert.deepEqual(errors, [], '実DOMからの実行で例外が発生しない'));
  return { page, logs };
}

async function connect(page) {
  for (const [label, value] of [['接続URL', 'ws://127.0.0.1:7880'], ['トークン', token],
    ['host identity', 'fixture-host'], ['host SID', 'PA-host'],
    ['Session ID', 'session-fixture'], ['connection ID', 'connection-fixture']]) {
    await page.getByLabel(label, { exact: true }).fill(value);
  }
  await page.getByRole('button', { name: '接続', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom?.connects === 1);
}

async function focusStart(page) {
  await page.getByLabel('テキスト', { exact: true }).focus();
  await page.waitForFunction(() => __fixtureRoom.current.focused);
  await page.evaluate(() => {
    __order.length = 0;
    __fixtureRoom.holdType = 'focus'; __fixtureRoom.holdEnabled = false;
    globalThis.__holdResume = true;
  });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.releaseRpc && globalThis.__releaseResume);
}

for (const late of [false, true]) {
  test(`focus解除応答がresumeの${late ? '後' : '前'}でも一度の開始からACKへ進む`, async t => {
    const { page } = await fixture(t);
    await connect(page);
    await focusStart(page);
    if (late) {
      await page.evaluate(() => __releaseResume());
      await page.waitForFunction(() => __order.includes('resumed'));
    }
    await page.evaluate(() => __fixtureRoom.releaseRpc());
    await page.waitForFunction(() => !__fixtureRoom.current.focused);
    if (!late) await page.evaluate(() => __releaseResume());
    t.diagnostic(JSON.stringify(await page.evaluate(() => __order)));
    await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
    const order = await page.evaluate(() => __order);
    assert.ok(order.indexOf('response:focus:false') < order.indexOf('microphone'));
    assert.equal(await page.evaluate(() => __fixtureRoom.publications), 1);
  });
}

async function finalAlert(page, pattern) {
  await page.getByRole('alert').waitFor({ state: 'visible' });
  // RPC帰還・後片付けから操作catchまでのmicrotaskを終えたDOMを読む。
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
  const message = await page.getByRole('alert').textContent();
  assert.match(message, pattern);
  assert.doesNotMatch(message, /synthetic-private/);
}

for (const [type, enabled, button] of [
  ['mute', false, 'マイク開始'], ['mute', true, 'マイク停止'],
  ['focus', true, null], ['focus', false, null], ['text', undefined, '送信'],
  ['cancel', undefined, '取消'], ['reconnect', undefined, '再接続'],
]) {
  for (const reason of ['input_timeout', 'operation_failed']) {
    test(`${type}:${enabled}の${reason}を操作終了後のalertへ保持する`, async t => {
      const { page, logs } = await fixture(t);
      await connect(page);
      if (type === 'mute' && !enabled) {
        await page.getByRole('button', { name: 'マイク停止', exact: true }).click();
        await page.waitForFunction(() => __fixtureRoom.current.muted);
      }
      if (type === 'focus' && !enabled) {
        await page.getByLabel('テキスト', { exact: true }).focus();
        await page.waitForFunction(() => __fixtureRoom.current.focused);
      }
      await page.evaluate(({ type, reason }) => {
        __fixtureRoom.rejectNext = reason; __fixtureRoom.rejectNextType = type;
        document.getElementById('text').value = '合成入力';
      }, { type, reason });
      if (type === 'focus') {
        if (enabled) await page.getByLabel('テキスト', { exact: true }).focus();
        else await page.getByLabel('テキスト', { exact: true }).evaluate(node => node.blur());
      } else await page.getByRole('button', { name: button, exact: true }).click();
      await finalAlert(page, reason === 'input_timeout' ? /タイムアウト/ : /host.*拒否/);
      assert.equal(await page.evaluate(() => __fixtureRoom.publications), 0);
      assert.doesNotMatch(logs.join('\n'), /synthetic-private/);
    });
  }
}

for (const type of ['open_input', 'input_ack']) {
  for (const reason of ['input_timeout', 'operation_failed']) {
    test(`${type}の${reason}を回収完了後も表示する`, async t => {
      const { page } = await fixture(t);
      await connect(page);
      await page.evaluate(({type, reason}) => {
        __fixtureRoom.rejectNext = reason; __fixtureRoom.rejectNextType = type;
      }, {type, reason});
      await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
      await page.waitForFunction(() => __fixtureRoom.stops.length === 1);
      await finalAlert(page, reason === 'input_timeout' ? /タイムアウト/ : /host.*拒否/);
      assert.equal(await page.evaluate(() => __fixtureRoom.calls.filter(c => c.type === 'input_ack').length), type === 'input_ack' ? 1 : 0);
      assert.equal(await page.evaluate(() => __fixtureRoom.tracks[0].mediaStreamTrack.readyState), 'ended');
      assert.match(await page.locator('#input-state').textContent(), /停止/);
    });
  }
  test(`${type}の5秒待機期限を操作完了後も表示する`, async t => {
    const { page } = await fixture(t);
    await connect(page);
    await page.clock.install();
    await page.evaluate(type => { __fixtureRoom.holdType = type; }, type);
    await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
    await page.waitForFunction(() => __fixtureRoom.releaseRpc);
    await page.clock.runFor(5000);
    await page.waitForFunction(() => __fixtureRoom.stops.length === 1);
    // fake clock下のrequestAnimationFrameも進める。
    await page.clock.resume();
    await finalAlert(page, /タイムアウト/);
    assert.match(await page.locator('#input-state').textContent(), /停止/);
    assert.equal(await page.evaluate(() => __fixtureRoom.calls.filter(c => c.type === 'input_ack').length), type === 'input_ack' ? 1 : 0);
    await page.evaluate(() => __fixtureRoom.releaseRpc());
  });
}

for (const [remaining, ackTime, active] of [[1, 0, true], [0, 0, false], [5000, 4999, true], [5000, 5000, false]]) {
  test(`残予算${remaining}・ACK時刻${ackTime}の入力表示と期限分類`, async t => {
    const { page } = await fixture(t);
    await connect(page);
    await page.evaluate(({remaining, ackTime}) => {
      globalThis.__now = 0; performance.now = () => __now;
      __fixtureRoom.remainingMs = remaining; __fixtureRoom.ackTime = ackTime;
    }, {remaining, ackTime});
    await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
    if (active) await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
    else {
      await page.waitForFunction(() => __fixtureRoom.stops.length === 1);
      await finalAlert(page, /タイムアウト/);
    }
    assert.equal(await page.evaluate(() => __fixtureRoom.calls.filter(c => c.type === 'input_ack').length), remaining === 0 ? 0 : 1);
  });
}

for (const code of [1502, 1503]) {
  test(`通常制御RPCのSDK code${code}を同期timeoutと区別する`, async t => {
    const { page, logs } = await fixture(t);
    await connect(page);
    await page.evaluate(code => { __fixtureRoom.rpcError = code; __fixtureRoom.rpcErrorType = 'text'; }, code);
    await page.getByRole('button', { name: '送信', exact: true }).click();
    await finalAlert(page, code === 1502 ? /タイムアウト/ : /失敗/);
    assert.doesNotMatch(await page.getByRole('alert').textContent(), /host同期/);
    assert.doesNotMatch(logs.join('\n'), /synthetic-private/);
  });
}

test('未知のマイク取得例外の本文を表示・ログへ出さない', async t => {
  const { page, logs } = await fixture(t);
  await connect(page);
  await page.evaluate(() => { globalThis.__microphoneError = true; });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await finalAlert(page, /失敗/);
  assert.doesNotMatch(logs.join('\n'), /synthetic-private/);
});

test('未知のhost拒否reasonの本文を表示・ログへ出さない', async t => {
  const { page, logs } = await fixture(t);
  await connect(page);
  await page.evaluate(() => {
    __fixtureRoom.rejectNext = 'synthetic-private-reason'; __fixtureRoom.rejectNextType = 'text';
  });
  await page.getByRole('button', { name: '送信', exact: true }).click();
  await finalAlert(page, /host.*拒否/);
  assert.doesNotMatch(logs.join('\n'), /synthetic-private/);
});

test('過去の権限拒否を端末resume失敗や開始の事前拒否へ流用しない', async t => {
  const { page } = await fixture(t);
  await connect(page);
  await page.evaluate(() => { globalThis.__denyMicrophone = true; });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await finalAlert(page, /権限/);
  await page.evaluate(() => { globalThis.__resumeError = true; });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await finalAlert(page, /失敗/);
  assert.doesNotMatch(await page.getByRole('alert').textContent(), /拒否/);
  await page.evaluate(() => { globalThis.__resumeError = false; globalThis.__denyMicrophone = false; });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await finalAlert(page, /失敗/);
  assert.equal(await page.evaluate(() => __fixtureRoom.publications), 1);
});

for (const reason of [null, 'input_timeout', 'operation_failed']) {
  test(`停止の後片付け失敗で主操作${reason}の分類を上書きしない`, async t => {
    const { page } = await fixture(t);
    await connect(page);
    await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
    await page.evaluate(reason => {
      __fixtureRoom.closeError = true; __fixtureRoom.rejectNext = reason; __fixtureRoom.rejectNextType = 'mute';
    }, reason);
    await page.getByRole('button', { name: 'マイク停止', exact: true }).click();
    await page.waitForFunction(() => __fixtureRoom.stops.length === 1);
    await finalAlert(page, reason === 'input_timeout' ? /タイムアウト/ : reason ? /host.*拒否/ : /失敗/);
    assert.match(await page.locator('#input-state').textContent(), /停止/);
  });
}

test('open拒否の回収失敗でも主操作のtimeoutを表示する', async t => {
  const { page } = await fixture(t);
  await connect(page);
  await page.evaluate(() => {
    __fixtureRoom.closeError = true; __fixtureRoom.rejectNext = 'input_timeout'; __fixtureRoom.rejectNextType = 'open_input';
  });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.stops.length === 1);
  await finalAlert(page, /タイムアウト/);
});

for (const failure of ['operation_failed', 'input_timeout', 'sdk-timeout']) {
  test(`開始待機中のfocus解除${failure}では新trackを取得しない`, async t => {
    const { page } = await fixture(t);
    await connect(page);
    await focusStart(page);
    await page.evaluate(failure => {
      const room = __fixtureRoom;
      if (failure === 'sdk-timeout') { room.rpcError = 1502; room.rpcErrorType = 'focus'; }
      else { room.rejectNext = failure; room.rejectNextType = 'focus'; }
      __releaseResume(); room.releaseRpc();
    }, failure);
    await finalAlert(page, failure === 'operation_failed' ? /host.*拒否/ : /タイムアウト/);
    assert.equal(await page.evaluate(() => __fixtureRoom.publications), 0);
    assert.equal(await page.evaluate(() => __fixtureRoom.calls.filter(c => ['open_input','input_ack'].includes(c.type)).length), 0);
    await page.evaluate(() => {
      __fixtureRoom.holdType = null; __fixtureRoom.rpcError = null; globalThis.__holdResume = false;
    });
    await page.getByLabel('テキスト', { exact: true }).focus();
    await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
  });
}

for (const interruption of ['マイク停止', '送信', 'focus設定', '取消', '再接続', '切断', '接続交代']) {
  test(`focus解除待機中の${interruption}で旧開始を失効する`, async t => {
    const { page } = await fixture(t);
    await connect(page);
    await focusStart(page);
    await page.evaluate(() => { globalThis.__oldRoom = __fixtureRoom; __releaseResume(); });
    await page.waitForFunction(() => __order.includes('resumed'));
    await page.evaluate(() => { globalThis.__holdResume = false; });
    if (interruption === '接続交代') await connect(page);
    else if (interruption === 'focus設定') await page.getByLabel('テキスト', { exact: true }).focus();
    else await page.getByRole('button', { name: interruption, exact: true }).click();
    if (['切断', '接続交代'].includes(interruption)) {
      await page.waitForFunction(() => document.getElementById('input-state').textContent === '停止');
    } else if (interruption === 'focus設定') await page.waitForFunction(() => __fixtureRoom.calls.filter(c => c.type === 'focus' && c.enabled).length === 2);
    else await page.waitForFunction(type => __fixtureRoom.calls.some(c => c.type === type),
      interruption === 'マイク停止' ? 'mute' : interruption === '送信' ? 'text' : interruption === '取消' ? 'cancel' : 'reconnect');
    await page.evaluate(() => __oldRoom.releaseRpc());
    await page.waitForFunction(() => __order.includes('released:focus'));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    assert.equal(await page.evaluate(() => __oldRoom.publications + (__oldRoom === __fixtureRoom ? 0 : __fixtureRoom.publications)), 0);
    assert.doesNotMatch(await page.locator('#input-state').textContent(), /正式入力中/);
  });
}

test('blur単独は旧trackを再開せず次の明示開始は新trackのACKを待つ', async t => {
  const { page } = await fixture(t);
  await connect(page);
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
  await page.getByLabel('テキスト', { exact: true }).focus();
  await page.waitForFunction(() => __fixtureRoom.stops.length === 1);
  await page.getByLabel('テキスト', { exact: true }).evaluate(node => node.blur());
  await page.waitForFunction(() => !__fixtureRoom.current.focused);
  assert.equal(await page.evaluate(() => __fixtureRoom.publications), 1);
  assert.match(await page.locator('#input-state').textContent(), /停止/);
  await page.evaluate(() => { __fixtureRoom.holdType = 'input_ack'; });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.releaseRpc);
  assert.match(await page.locator('#input-state').textContent(), /認可待ち/);
  const sids = await page.evaluate(() => __fixtureRoom.calls.filter(c => c.type === 'open_input').map(c => c.track_sid));
  assert.notEqual(sids[0], sids[1]);
  await page.evaluate(() => __fixtureRoom.releaseRpc());
  await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
});

for (const [event, identity] of [['disconnected', null], ['reconnecting', null],
  ['participantDisconnected', 'fixture-host'], ['participantDisconnected', 'other-participant']]) {
  test(`正式入力中のSDK ${event}:${identity}を同じ画面で観測する`, async t => {
    const { page } = await fixture(t);
    await connect(page);
    await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('input-state').textContent === '正式入力中');
    assert.equal(await page.evaluate(() => __fixtureRoom.tracks[0].mediaStreamTrack.readyState), 'live');
    for (const id of ['start','stop','send','cancel','reconnect']) assert.equal(await page.locator('#'+id).isDisabled(), false);
    await page.evaluate(({event, identity}) => __fixtureRoom.emit(event, {identity}), {event, identity});
    const stopped = identity !== 'other-participant';
    if (stopped) await page.waitForFunction(() => __fixtureRoom.stops.length === 1 && document.getElementById('start').disabled);
    else await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    assert.equal(await page.evaluate(() => __fixtureRoom.tracks[0].mediaStreamTrack.readyState), stopped ? 'ended' : 'live');
    assert.equal(await page.evaluate(() => __fixtureRoom.tracks[0].mediaStreamTrack.enabled), !stopped);
    assert.equal(await page.evaluate(() => __fixtureRoom.unpublished.length), stopped ? 1 : 0);
    for (const id of ['start','stop','send','cancel','reconnect']) assert.equal(await page.locator('#'+id).isDisabled(), stopped);
    assert.equal((await page.locator('#input-state').textContent()).includes('正式入力中'), !stopped);
  });
}

test('日本語画面の明示操作から接続・マイク・text・取消・再接続へ到達する', { timeout: 15000 }, async t => {
  const { page, logs } = await fixture(t);
  assert.equal(await page.evaluate(() => globalThis.__fixtureRoom?.connects || 0), 0);
  await connect(page);
  assert.equal(await page.evaluate(() => __fixtureRoom.listenerBeforeConnect), true);
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(c => c.type === 'input_ack'));
  await page.getByRole('button', { name: 'マイク停止', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(c => c.type === 'mute' && c.enabled));
  await page.getByLabel('テキスト', { exact: true }).fill('{"type":"cancel"}');
  await page.getByRole('button', { name: '送信', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(c => c.type === 'text'));
  const text = await page.evaluate(() => __fixtureRoom.calls.find(c => c.type === 'text'));
  assert.equal(text.text, '{"type":"cancel"}');
  assert.equal(await page.evaluate(() => __fixtureRoom.calls.filter(c => c.type === 'cancel').length), 0);
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(c => c.type === 'cancel'));
  await page.getByRole('button', { name: '再接続', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(c => c.type === 'reconnect'));
  assert.equal(await page.evaluate(() => localStorage.length), 0);
  assert.equal(new URL(page.url()).search, '');
  for (const line of logs) assert.equal(line.toLowerCase().includes(token.toLowerCase()), false);
});

test('Room参加後にhost設定を渡し保留した初期通知を同じ画面で照合する', { timeout: 10000 }, async t => {
  const { page } = await fixture(t);
  for (const [label, value] of [['接続URL', 'ws://127.0.0.1:7880'], ['トークン', token],
    ['host identity', 'fixture-host']]) await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: '接続', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom?.connects === 1);
  assert.equal(await page.getByRole('button', { name: '送信', exact: true }).isDisabled(), true);
  assert.match(await page.getByRole('status', { name: '参加者', exact: true }).textContent(), /PA-fixture/);
  for (const [label, value] of [['host SID', 'PA-host'], ['Session ID', 'session-fixture'],
    ['connection ID', 'connection-fixture']]) await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: 'host同期', exact: true }).click();
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(call => call.type === 'input_ack'));
  assert.equal(await page.evaluate(() => __fixtureRoom.connects), 1);
});

test('送信者未解決の初期通知を拒否しhost参加確認後に状態取得で同じ画面を同期する', { timeout: 10000 }, async t => {
  const { page } = await fixture(t);
  await page.evaluate(() => { globalThis.__unresolvedInitial = true; });
  for (const [label, value] of [['接続URL', 'ws://127.0.0.1:7880'], ['トークン', token],
    ['host identity', 'fixture-host']]) await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: '接続', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom?.connects === 1);
  for (const [label, value] of [['host SID', 'PA-host'], ['Session ID', 'session-fixture'],
    ['connection ID', 'connection-fixture']]) await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: 'host同期', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: '送信', exact: true }).isDisabled(), true);
  assert.equal(await page.evaluate(() => __fixtureRoom.calls.length), 0);
  await page.evaluate(() => {
    const host = {identity:'fixture-host',sid:'PA-host'};
    __fixtureRoom.remoteParticipants.set(host.identity, host);
    __fixtureRoom.emit('participantConnected', host);
  });
  await page.waitForFunction(() => !document.getElementById('send').disabled);
  assert.deepEqual(await page.evaluate(() => __fixtureRoom.calls),
    [{v:1,session_id:'session-fixture',connection_id:'connection-fixture'}]);
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(call => call.type === 'input_ack'));
  assert.equal(await page.evaluate(() => __fixtureRoom.connects), 1);
});

test('初期状態取得のtimeoutを表示し同じRoomで明示再試行できる', { timeout: 10000 }, async t => {
  const { page } = await fixture(t);
  for (const [label, value] of [['接続URL', 'ws://127.0.0.1:7880'], ['トークン', token],
    ['host identity', 'fixture-host']]) await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: '接続', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom?.connects === 1);
  for (const [label, value] of [['host SID', 'PA-host'], ['Session ID', 'session-fixture'],
    ['connection ID', 'connection-fixture']]) await page.getByLabel(label, { exact: true }).fill(value);
  await page.evaluate(() => { globalThis.__stateTimeout = true; });
  await page.getByRole('button', { name: 'host同期', exact: true }).click();
  await page.getByRole('alert').waitFor({state:'visible'});
  assert.match(await page.getByRole('alert').textContent(), /host同期.*タイムアウト/);
  assert.equal(await page.getByRole('button', { name: '送信', exact: true }).isDisabled(), true);
  await page.evaluate(() => { globalThis.__stateTimeout = false; });
  await page.getByRole('button', { name: 'host同期', exact: true }).click();
  await page.waitForFunction(() => !document.getElementById('send').disabled);
  assert.equal(await page.evaluate(() => __fixtureRoom.connects), 1);
});

test('ローカルHTTP入口で固定した実SDKを読み込み画面を起動できる', { timeout: 10000 }, async t => {
  const { createDevServer } = await import('../dev-server.mjs');
  const server = createDevServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  const context = await browser.newContext();
  t.after(() => context.close());
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto('http://127.0.0.1:' + server.address().port);
  await page.getByRole('button', { name: '接続', exact: true }).waitFor();
  const sdk = await page.evaluate(async () => {
    const module = await import('/vendor/livekit-client.esm.mjs');
    const room = new module.Room();
    return { microphone: typeof module.createLocalAudioTrack, rpc: typeof room.localParticipant.performRpc };
  });
  assert.deepEqual(sdk, { microphone: 'function', rpc: 'function' });
  assert.deepEqual(errors, []);
  assert.equal(await page.getByRole('button', { name: '送信', exact: true }).isDisabled(), true);
});

test('host拒否を画面で明示しtextを自動再送しない', { timeout: 10000 }, async t => {
  const { page } = await fixture(t);
  await connect(page);
  await page.getByLabel('テキスト', { exact: true }).fill('合成入力');
  await page.evaluate(() => {
    __fixtureRoom.rejectNext = 'operation_failed';
    __fixtureRoom.rejectNextType = 'text';
  });
  await page.getByRole('button', { name: '送信', exact: true }).click();
  await page.getByRole('alert').waitFor({ state: 'visible' });
  assert.match(await page.getByRole('alert').textContent(), /host.*拒否/);
  assert.equal(await page.evaluate(() => __fixtureRoom.calls.filter(c => c.type === 'text').length), 1);
});

test('マイク権限拒否を表示し同じ画面で明示再試行できる', { timeout: 10000 }, async t => {
  const { page } = await fixture(t);
  await connect(page);
  await page.evaluate(() => { globalThis.__denyMicrophone = true; });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.getByRole('alert').waitFor({ state: 'visible' });
  assert.match(await page.getByRole('alert').textContent(), /権限.*拒否/);
  assert.equal(await page.evaluate(() => __fixtureRoom.calls.filter(c => c.type === 'input_ack').length), 0);
  await page.evaluate(() => { globalThis.__denyMicrophone = false; });
  await page.getByRole('button', { name: 'マイク開始', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(c => c.type === 'input_ack'));
});

test('同じ画面で生成完了と推定終了を区別し終了後の次textを受け付ける', { timeout: 10000 }, async t => {
  const { page } = await fixture(t);
  await connect(page);
  const output = page.getByRole('status', { name: '出力状態', exact: true });
  await page.getByLabel('テキスト', { exact: true }).fill('最初の合成入力');
  await page.getByRole('button', { name: '送信', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.some(c => c.type === 'text'));
  assert.match(await output.textContent(), /出力中/);
  await page.evaluate(() => {
    const room = __fixtureRoom;
    room.emit('dataReceived', new TextEncoder().encode(JSON.stringify({ v: 1,
      type: 'generation_completed', ...room.current, state_sequence: ++room.stateSequence,
      response_id: room.current.active_response_id, final_audio_sequence: 0 })),
      room.remoteParticipants.get('fixture-host'), 0, 'local-gpt-live.events.v1');
  });
  assert.match(await output.textContent(), /出力中/);
  await page.evaluate(() => {
    const room = __fixtureRoom, responseId = room.current.active_response_id;
    room.current = { ...room.current, active_response_id: null };
    room.emit('dataReceived', new TextEncoder().encode(JSON.stringify({ v: 1,
      type: 'output_estimated_completed', ...room.current, state_sequence: ++room.stateSequence,
      response_id: responseId, basis: 'sdk_submitted_elapsed', real_playback_confirmed: false,
      estimated_complete: true })), room.remoteParticipants.get('fixture-host'), 0,
      'local-gpt-live.events.v1');
  });
  assert.match(await output.textContent(), /推定/);
  await page.getByLabel('テキスト', { exact: true }).fill('次の合成入力');
  await page.getByRole('button', { name: '送信', exact: true }).click();
  await page.waitForFunction(() => __fixtureRoom.calls.filter(c => c.type === 'text').length === 2);
  assert.match(await output.textContent(), /出力中/);
});
