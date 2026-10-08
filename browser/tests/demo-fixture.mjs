// アプリの端末境界用double。RTC配送・permission・端末出音の証明には使わない。
import assert from 'node:assert/strict';

export const connection = {
  sessionId: 'session-fixture', connectionId: 'connection-fixture',
  hostIdentity: 'fixture-host', hostSid: 'PA-host',
  participantIdentity: 'fixture-user', participantSid: 'PA-fixture',
};
export const sender = { identity: connection.hostIdentity, sid: connection.hostSid,
  topic: 'local-gpt-live.events.v1' };

export function state(fields = {}) {
  return { session_id: connection.sessionId, connection_id: connection.connectionId,
    state_sequence: 1, control_binding: 'control-initial', input_revision: 0,
    input_active: false, muted: false, focused: false, active_response_id: null,
    closed: false, ...fields };
}

export function event(type, fields = {}) { return { v: 1, type, ...state(fields) }; }

export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

export async function until(predicate) {
  const deadline = performance.now() + 2000;
  while (!predicate()) {
    assert.ok(performance.now() < deadline, '期待した状態に遷移しませんでした');
    await new Promise(resolve => setImmediate(resolve));
  }
}

export function transportDouble() {
  const calls = [], microphones = [], outputs = [];
  let current = state(), sequence = 1, response = 0;
  const transport = {
    calls, microphones, outputs,
    setState(value) { current = { ...value }; sequence = value.state_sequence; },
    async microphone() {
      const mic = { trackSid: `TR-mic-${microphones.length + 1}`, suppressed: false,
        closed: false, suppress() { this.suppressed = true; },
        async close() { this.closed = true; } };
      microphones.push(mic);
      return mic;
    },
    async rpc(message, timeoutMs) {
      calls.push({ message: structuredClone(message), timeoutMs });
      let extra = {};
      if (message.type === 'open_input') {
        current = { ...current, input_revision: current.input_revision + 1, input_active: true };
        extra = { remaining_ms: 5000, grant: { track_sid: message.track_sid,
          request_id: message.request_id, input_generation: 1,
          input_revision: current.input_revision } };
      } else if (message.type === 'mute' || message.type === 'focus') {
        current = { ...current, [message.type === 'mute' ? 'muted' : 'focused']: message.enabled,
          ...(message.enabled ? { input_active: false,
            input_revision: current.input_revision + 1 } : {}) };
      } else if (['text', 'cancel', 'reconnect', 'close'].includes(message.type)) {
        response += 1;
        current = { ...current, input_active: false, input_revision: current.input_revision + 1,
          control_binding: `control-${response}`,
          active_response_id: message.type === 'text' ? `response-${response}` : null,
          closed: message.type === 'close' };
        if (message.type === 'text') extra.response_id = current.active_response_id;
      }
      current = { ...current, state_sequence: ++sequence };
      return { ok: true, ...extra, ...current, binding: current.control_binding };
    },
    async prepareOutput(track, onFailure) {
      const output = { track, onFailure, released: false, close() { this.released = true; } };
      outputs.push(output);
      return output;
    },
    async disconnect() {
      for (const mic of microphones) { mic.suppress(); await mic.close(); }
      for (const output of outputs) output.close();
    },
  };
  return transport;
}
