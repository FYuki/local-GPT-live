import { createConversation } from './conversation.mjs';
import { createLiveKitAdapter } from './livekit-adapter.mjs';

const element = id => document.getElementById(id);
let conversation = null, adapter = null, context = null, connecting = false, disconnecting = null;
let pendingNotifications = [], pendingTracks = [];
let synchronizing = null;
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
element('connect').addEventListener('click', () => run(connect));
element('synchronize').addEventListener('click', () => run(synchronize));
element('disconnect').addEventListener('click', () => run(disconnect));
element('start').addEventListener('click', () => run(async () => {
  await context.resume();
  if (conversation.snapshot().muted) await conversation.setGate('mute', false);
  await conversation.startMicrophone();
}));
element('stop').addEventListener('click', () => run(() => conversation.setGate('mute', true)));
element('text').addEventListener('focus', () => {
  if (conversation?.snapshot().connected) run(() => conversation.setGate('focus', true));
});
element('text').addEventListener('blur', () => {
  if (conversation?.snapshot().connected) run(() => conversation.setGate('focus', false));
});
element('send').addEventListener('click', () => run(async () => {
  await context.resume();
  await conversation.submitText(element('text').value);
}));
element('cancel').addEventListener('click', () => run(() => conversation.cancel()));
element('reconnect').addEventListener('click', () => run(() => conversation.reconnect()));
