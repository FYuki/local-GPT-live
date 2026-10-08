// Backendが状態を所有し、端末は現在の操作と資源だけを所有する。
export function createConversation({ transport, connection, onChange, now }) {
  let current = null, disconnected = false, disconnecting = null, inputEpoch = 0, microphone = null;
  let authorization = null, inputPhase = '停止', error = null, terminal = '待機';
  let metadata = null, output = null, preparing = null, suppressedResponse = null;
  const tracks = new Map();
  const snapshot = () => ({
    connected: !disconnected && current !== null,
    inputAuthorized: authorization !== null,
    inputPhase, outputActive: current?.active_response_id != null &&
      current.active_response_id !== suppressedResponse && !disconnected,
    outputState: disconnected ? '切断' : current?.active_response_id != null &&
      current.active_response_id !== suppressedResponse ? '出力中' : terminal,
    realPlaybackConfirmed: false, error,
    muted: current?.muted === true, focused: current?.focused === true,
  });
  function changed() { onChange(snapshot()); }
  function releaseOutput() {
    preparing = null;
    metadata = null;
    output?.close();
    output = null;
  }
  function invalidateInput() {
    inputEpoch++;
    authorization = null;
    inputPhase = '停止';
    const old = microphone;
    microphone = null;
    old?.suppress();
    changed();
    return old;
  }
  function matches(value) {
    return !disconnected && value?.session_id === connection.sessionId &&
      value.connection_id === connection.connectionId;
  }
  function validState(value) {
    return matches(value) && Number.isSafeInteger(value.state_sequence) &&
      value.state_sequence >= 0 && typeof value.control_binding === 'string' &&
      value.control_binding.length > 0 && Number.isSafeInteger(value.input_revision) &&
      value.input_revision >= 0 && typeof value.input_active === 'boolean' &&
      typeof value.muted === 'boolean' && typeof value.focused === 'boolean' &&
      typeof value.closed === 'boolean' &&
      (value.active_response_id === null || typeof value.active_response_id === 'string');
  }
  function adopt(value) {
    if (!validState(value)) return false;
    if (current && value.state_sequence <= current.state_sequence) return false;
    const previous = current;
    current = { ...value };
    if (previous && (previous.control_binding !== current.control_binding ||
        previous.active_response_id !== current.active_response_id)) releaseOutput();
    if (authorization && (!current.input_active ||
        current.input_revision !== authorization.input_revision || current.muted || current.focused)) {
      const old = invalidateInput();
      if (old) void old.close().catch(fail);
    }
    if (current.closed) void disconnect().catch(fail);
    changed();
    return true;
  }
  function fail(cause) {
    error = cause.name === 'NotAllowedError' ? '権限または再生準備が拒否されました' :
      ['input_timeout', 'rpc_timeout'].includes(cause.message) ? '操作がタイムアウトしました' :
      cause.message === 'host_rejected' ? 'hostが操作を拒否しました' :
      '操作に失敗しました。接続・hostの状態を確認してください';
    changed();
  }
  function requireState() {
    if (!current || disconnected || current.closed) throw new Error('host_state_required');
  }
  function base(type, fields) {
    requireState();
    return { v: 1, type, session_id: connection.sessionId, binding: current.control_binding,
      request_id: crypto.randomUUID(), ...fields };
  }
  async function invoke(message, timeoutMs) {
    let timer;
    try {
      const result = await Promise.race([transport.rpc(message, timeoutMs),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('input_timeout')), timeoutMs); })]);
      adopt(result);
      if (!result?.ok) throw new Error(result?.reason === 'input_timeout' ? 'input_timeout' : 'host_rejected');
      if (!validState(result)) throw new Error('invalid_state');
      return result;
    } finally { clearTimeout(timer); }
  }
  async function operation(type, fields) {
    let old = null, failed = false;
    if (!['mute', 'focus'].includes(type)) error = null;
    try {
      old = ['mute', 'focus'].includes(type) && !fields.enabled ? null : invalidateInput();
      if (['text', 'cancel', 'reconnect'].includes(type)) {
        suppressedResponse = current?.active_response_id;
        terminal = type === 'cancel' ? '取消' : '待機';
        releaseOutput();
      }
      return await invoke(base(type, fields), 5000);
    } catch (cause) { failed = true; fail(cause); throw cause; }
    finally {
      try { if (old) await old.close(); }
      catch (cause) {
        // 主操作の拒否理由は後片付けの失敗で置き換えない。
        if (!failed) { fail(cause); throw cause; }
      }
      changed();
    }
  }
  async function startMicrophone() {
    let mic = null, epoch;
    const valid = () => epoch === inputEpoch && !disconnected;
    try {
      if (microphone || inputPhase !== '停止') throw new Error('input_already_open');
      requireState();
      if (current.muted || current.focused) throw new Error('input_suppressed');
      error = null;
      epoch = ++inputEpoch;
      inputPhase = '準備中'; changed();
      mic = await transport.microphone();
      if (!valid()) throw new Error('operation_invalidated');
      microphone = mic;
      const message = base('open_input', { track_sid: mic.trackSid,
        expected_revision: current.input_revision });
      const deadline = now() + 5000;
      inputPhase = '認可待ち'; changed();
      const prepared = await invoke(message, 5000);
      if (!valid()) throw new Error('operation_invalidated');
      const grant = prepared.grant;
      if (!grant || Object.keys(grant).sort().join(',') !==
          'input_generation,input_revision,request_id,track_sid' ||
          grant.track_sid !== mic.trackSid || grant.request_id !== message.request_id ||
          grant.input_revision !== message.expected_revision + 1 ||
          grant.input_revision !== current.input_revision ||
          !Number.isSafeInteger(grant.input_generation) || grant.input_generation <= 0 ||
          prepared.control_binding !== current.control_binding ||
          !current.input_active || current.muted || current.focused) throw new Error('stale_grant');
      const budget = Math.min(prepared.remaining_ms, deadline - now());
      if (!Number.isFinite(budget) || budget <= 0) throw new Error('input_timeout');
      const acknowledged = await invoke(base('input_ack', { grant }), budget);
      if (!valid()) throw new Error('operation_invalidated');
      if (now() >= deadline) throw new Error('input_timeout');
      if (!current.input_active || current.input_revision !== grant.input_revision ||
          acknowledged.control_binding !== current.control_binding ||
          current.muted || current.focused) throw new Error('stale_grant');
      authorization = { ...grant };
      inputPhase = '正式入力中'; changed();
    } catch (cause) {
      if (valid()) {
        microphone = null; authorization = null; inputPhase = '停止';
      }
      if (epoch === undefined || valid()) fail(cause);
      try { if (mic) { mic.suppress(); await mic.close(); } }
      catch {
        // 分類済みの主操作失敗を保持し、元の例外を呼び出し側へ返す。
      }
      throw cause;
    }
  }
  async function prepareOutput() {
    if (!metadata || output || preparing || !current ||
        current.active_response_id === suppressedResponse) return;
    const subscribed = tracks.get(metadata.track_sid);
    if (!subscribed) return;
    const scope = metadata;
    preparing = scope;
    try {
      const resource = await transport.prepareOutput(subscribed.track, cause => {
        if (preparing !== scope || disconnected) return;
        suppressedResponse = scope.response_id;
        terminal = '失敗';
        releaseOutput();
        fail(cause);
      });
      if (preparing !== scope || disconnected || current.control_binding !== scope.control_binding ||
          current.active_response_id !== scope.response_id) { resource.close(); return; }
      output = resource;
      await invoke(base('confirm_output_ready', {
        response_id: scope.response_id, track_sid: scope.track_sid,
      }), 3000);
    } catch (cause) {
      if (preparing === scope) { output?.close(); output = null; fail(cause); }
    }
  }
  function acceptEvent(value, sender) {
    if (sender?.identity !== connection.hostIdentity || sender.sid !== connection.hostSid ||
        sender.topic !== 'local-gpt-live.events.v1' || value?.v !== 1 || !matches(value)) return;
    if (typeof value.type !== 'string' || !validState(value) ||
        (!current && value.type !== 'host_state')) return;
    const accepted = adopt(value);
    if (!current || value.control_binding !== current.control_binding) return;
    if (value.type === 'input_invalidated' && accepted && !current.input_active) {
      const old = invalidateInput();
      if (old) void old.close().catch(fail);
    }
    if (value.type === 'output_track' && value.response_id === current.active_response_id &&
        typeof value.track_sid === 'string' && value.track_sid) {
      if (!metadata) metadata = { ...value };
      void prepareOutput();
    }
    if (accepted && value.type.startsWith('output_estimated_')) {
      terminal = '推定終了（実再生の確認ではありません）';
    } else if (accepted && value.type === 'response_cancelled') terminal = '取消';
    if (accepted && ['input_failed', 'input_rejected', 'response_failed', 'transport_failed']
      .includes(value.type)) fail(new Error('host_failed'));
    changed();
  }
  function subscribeOutput(value) {
    if (disconnected || value.participantIdentity !== connection.hostIdentity ||
        value.participantSid !== connection.hostSid) return;
    tracks.set(value.trackSid, value);
    void prepareOutput();
  }
  function unsubscribeOutput(value) {
    tracks.delete(value.trackSid);
    if (metadata?.track_sid === value.trackSid) releaseOutput();
  }
  function disconnect() {
    if (disconnecting) return disconnecting;
    disconnected = true;
    const old = invalidateInput();
    releaseOutput(); tracks.clear();
    disconnecting = (async () => {
      try { if (old) await old.close(); }
      finally { await transport.disconnect(); changed(); }
    })();
    return disconnecting;
  }
  return { snapshot, acceptEvent, subscribeOutput, unsubscribeOutput, startMicrophone, disconnect,
    submitText: text => operation('text', { text }),
    setGate: (gate, enabled) => {
      if (!['mute', 'focus'].includes(gate)) throw new Error('invalid_gate');
      return operation(gate, { enabled });
    },
    cancel: () => operation('cancel', { response_id: current?.active_response_id }),
    reconnect: () => operation('reconnect', {}),
  };
}
