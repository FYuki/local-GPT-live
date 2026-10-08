import { createConversation } from './conversation.mjs';
import { createLiveKitAdapter } from './livekit-adapter.mjs';

const element = id => document.getElementById(id);
let conversation = null, adapter = null, context = null, connecting = false, disconnecting = null;
let pendingNotifications = [], pendingTracks = [];
let synchronizing = null;
let focusRelease = null, startEpoch = 0;
function settings() {
  return {
    hostIdentity: element('host-identity').value, hostSid: element('host-sid').value,
    sessionId: element('session').value, connectionId: element('connection').value,
  };
}
function synchronize() {
  if (synchronizing) return synchronizing;
  if (!adapter || conversation) return Promise.resolve();
  const connection = settings();
  if (Object.values(connection).some(value => !value)) return Promise.reject(new Error('missing_settings'));
  const transport = adapter;
  const task = (async () => {
    const { message, sender } = await transport.requestState(connection);
    if (adapter !== transport) return;
    const controller = createConversation({ transport, connection,
      onChange: view => { if (conversation === controller) render(view); },
      now: () => performance.now() });
    controller.acceptEvent(message, sender);
    if (!controller.snapshot().connected) throw new Error('invalid_state');
    conversation = controller;
    for (const [message, sender] of pendingNotifications) conversation.acceptEvent(message, sender);
    for (const track of pendingTracks) conversation.subscribeOutput(track);
    pendingNotifications = []; pendingTracks = [];
    render(conversation.snapshot());
  })();
  const result = task.finally(() => { if (synchronizing === result) synchronizing = null; });
  synchronizing = result;
  return result;
}
function showError(cause) {
  element('error').hidden = false;
  element('error').textContent = cause?.message === 'rpc_timeout' ?
    'host同期がタイムアウトしました。接続設定とhostの参加を確認してください。' :
    '操作に失敗しました。接続設定、権限、hostの状態を確認してください。';
}
function render(view) {
  element('connection-state').textContent = view.connected ? '接続済み・host同期済み' : '接続待ちまたは切断';
  element('input-state').textContent = view.inputPhase;
  element('output-state').textContent = view.outputState;
  element('error').hidden = !view.error;
  element('error').textContent = view.error;
  for (const id of ['start', 'stop', 'send', 'cancel', 'reconnect']) element(id).disabled = !view.connected;
}
function disconnect() {
  if (disconnecting) return disconnecting;
  startEpoch++; focusRelease = null;
  disconnecting = (async () => {
    try {
      if (conversation) await conversation.disconnect();
      else if (adapter) await adapter.disconnect();
    } finally {
      if (context && context.state !== 'closed') await context.close();
      conversation = null; adapter = null; context = null;
      synchronizing = null;
      pendingNotifications = []; pendingTracks = [];
      element('connection-state').textContent = '切断';
      element('input-state').textContent = '停止';
      element('output-state').textContent = '切断';
      for (const id of ['start', 'stop', 'send', 'cancel', 'reconnect']) element(id).disabled = true;
    }
  })().finally(() => { disconnecting = null; });
  return disconnecting;
}
async function connect() {
  if (connecting) return;
  connecting = true;
  try {
    await disconnect();
    const url = element('url').value;
    const parsed = new URL(url);
    if (!['ws:', 'wss:'].includes(parsed.protocol) || parsed.search || parsed.hash ||
        parsed.username || parsed.password) throw new Error('invalid_url');
    const token = element('token').value;
    element('token').value = '';
    const connection = settings();
    if (!token.trim() || !connection.hostIdentity) throw new Error('missing_settings');
    context = new AudioContext();
    await context.resume();
    adapter = createLiveKitAdapter({
      audioContext: context, hostIdentity: connection.hostIdentity,
      onNotification: (message, sender) => {
        if (conversation) conversation.acceptEvent(message, sender);
        else if (pendingNotifications.length < 512) pendingNotifications.push([message, sender]);
        else showError();
      },
      onTrack: track => {
        if (conversation) {
          if (track.removed) conversation.unsubscribeOutput(track);
          else conversation.subscribeOutput(track);
        } else {
          pendingTracks = pendingTracks.filter(old => old.trackSid !== track.trackSid);
          if (!track.removed) pendingTracks.push(track);
        }
      },
      onDisconnect: () => { void disconnect().catch(showError); },
    });
    const participant = await adapter.connect(url, token);
    element('participant').textContent = 'participant: ' + participant.participantIdentity + ' / SID: ' + participant.participantSid;
    if (Object.values(connection).every(value => value)) await synchronize();
  } catch (cause) {
    await disconnect();
    showError(cause);
  } finally { connecting = false; }
}
function run(action) { void action().catch(showError); }
async function runOperation(controller, action) {
  try { await action(); return true; }
  catch {
    if (conversation === controller) render(controller.snapshot());
    return false;
  }
}
function control(action) {
  startEpoch++;
  const controller = conversation;
  if (controller) void runOperation(controller, () => action(controller));
}
element('connect').addEventListener('click', () => run(connect));
element('synchronize').addEventListener('click', () => run(synchronize));
element('disconnect').addEventListener('click', () => run(disconnect));
element('start').addEventListener('click', () => run(async () => {
  const controller = conversation, transport = adapter, audio = context;
  const release = focusRelease;
  const epoch = ++startEpoch;
  const valid = () => conversation === controller && adapter === transport && context === audio &&
    epoch === startEpoch && !disconnecting && controller?.snapshot().connected;
  if (!valid()) return;
  // 端末準備はクリックの有効期間に開始し、focus解除の帰還後も同じ接続を使う。
  try { await audio.resume(); }
  catch (cause) { if (valid()) showError(cause); return; }
  if (!valid()) return;
  if (release?.controller === controller && release.transport === transport) {
    if (!await release.task || !valid()) return;
  }
  if (controller.snapshot().muted) {
    if (!await runOperation(controller, () => controller.setGate('mute', false)) || !valid()) return;
  }
  await runOperation(controller, () => controller.startMicrophone());
}));
element('stop').addEventListener('click', () => control(controller => controller.setGate('mute', true)));
element('text').addEventListener('focus', () => {
  focusRelease = null;
  if (conversation?.snapshot().connected) control(controller => controller.setGate('focus', true));
});
element('text').addEventListener('blur', () => {
  const controller = conversation, transport = adapter;
  if (controller?.snapshot().connected) {
    focusRelease = { controller, transport,
      task: runOperation(controller, () => controller.setGate('focus', false)) };
  }
});
element('send').addEventListener('click', () => run(async () => {
  startEpoch++;
  const controller = conversation, transport = adapter, audio = context, text = element('text').value;
  const valid = () => conversation === controller && adapter === transport && context === audio &&
    !disconnecting && controller?.snapshot().connected;
  if (!valid()) return;
  try { await audio.resume(); }
  catch (cause) { if (valid()) showError(cause); return; }
  if (!valid()) return;
  await runOperation(controller, () => controller.submitText(text));
}));
element('cancel').addEventListener('click', () => control(controller => controller.cancel()));
element('reconnect').addEventListener('click', () => control(controller => controller.reconnect()));
