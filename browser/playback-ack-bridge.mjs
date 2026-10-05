import { createPlaybackAckController } from './playback-ack.mjs';

const RATES = new Set([16000, 22050, 24000, 44100, 48000]);
const encoder = new TextEncoder();
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const identifier = (value) => typeof value === 'string' && value.length > 0
  && encoder.encode(value).length <= 256 && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value);

function namedError(name, message) {
  const error = new Error(message);
  error.name = name;
  return error;
}

// send はSDK送信完了ではなく、認証済みhostが返すbackend受理結果を返す。
export function createPlaybackAckBridge({
  send, onInvalidate, ackTimeoutMs = 1000, maxPendingSegments = 64,
} = {}) {
  if (typeof send !== 'function' || typeof onInvalidate !== 'function') {
    throw new TypeError('send and synchronous onInvalidate are required');
  }
  if (!Number.isFinite(ackTimeoutMs) || ackTimeoutMs <= 0
    || ackTimeoutMs > 2_147_483_647) throw new RangeError('invalid ACK timeout');
  let current = null;
  let closed = false;
  let rawRequest = null;

  function isCurrent(state) {
    return current === state && !state.abort.signal.aborted;
  }

  async function request(state, wire) {
    if (!isCurrent(state)) throw namedError('AbortError', 'response invalidated');
    // AbortSignalを無視するtransportでも、期限切れの実送信を増やさない。
    if (rawRequest) throw namedError('TransportBusyError', 'previous transport request is still pending');
    const operation = new AbortController();
    const deadline = performance.now() + ackTimeoutMs;
    function checkDeadline() {
      if (performance.now() >= deadline) {
        operation.abort();
        throw namedError('TimeoutError', 'backend ACK receipt timed out');
      }
    }
    let timer;
    let abort;
    const expired = new Promise((_, reject) => {
      abort = () => {
        operation.abort();
        reject(namedError('AbortError', 'response invalidated'));
      };
      state.abort.signal.addEventListener('abort', abort, { once: true });
      timer = setTimeout(() => {
        operation.abort();
        reject(namedError('TimeoutError', 'backend ACK receipt timed out'));
      }, ackTimeoutMs);
    });
    try {
      const raw = Promise.resolve().then(() => {
        if (!isCurrent(state) || operation.signal.aborted) {
          throw namedError('AbortError', 'response invalidated');
        }
        checkDeadline();
        return send(Object.freeze(wire), { signal: operation.signal });
      });
      rawRequest = raw;
      const released = () => { if (rawRequest === raw) rawRequest = null; };
      raw.then(released, released);
      const accepted = await Promise.race([
        raw,
        expired,
      ]);
      if (!isCurrent(state)) throw namedError('AbortError', 'response invalidated');
      checkDeadline();
      if (accepted !== true) throw new Error('backend rejected playback receipt');
      return true;
    } finally {
      clearTimeout(timer);
      state.abort.signal.removeEventListener('abort', abort);
    }
  }

  const controller = createPlaybackAckController({
    maxPendingSegments,
    async onAck({ responseId, audioSequence, generation }) {
      const state = current;
      if (!state || state.scope.responseId !== responseId
        || state.scope.generation !== generation) {
        throw namedError('AbortError', 'response invalidated');
      }
      await request(state, {
        v: 1, type: 'playback_ack', binding: state.scope.binding,
        response_id: responseId, audio_sequence: audioSequence,
      });
      state.confirmed = audioSequence;
      state.pending.delete(audioSequence);
    },
  });

  function invalidate() {
    const old = current;
    current = null;
    controller.cancel();
    if (old) {
      old.pending.clear();
      old.abort.abort();
      // hostは旧bindingを同期失効させ、rendererも同期disconnectする。
      const result = onInvalidate(old.scope);
      if (result !== null && (typeof result === 'object' || typeof result === 'function')
        && typeof result.then === 'function') {
        Promise.resolve(result).catch(() => {});
        throw new TypeError('onInvalidate must be synchronous');
      }
    }
  }

  function startResponse({ responseId, binding, trackSid } = {}) {
    if (closed) throw new Error('bridge is closed');
    if (![responseId, binding, trackSid].every(identifier)) {
      throw new TypeError('responseId, binding and trackSid must be bounded identifiers');
    }
    invalidate();
    const scope = Object.freeze({
      responseId, binding, trackSid, generation: controller.startResponse(responseId),
    });
    current = {
      scope, abort: new AbortController(), pending: new Map(),
      next: 0, sampleEnd: 0, sampleRate: null, confirmed: -1,
      final: null, completed: false, completion: null, completionFailed: false,
    };
    return scope;
  }

  async function maybeComplete(state) {
    if (!isCurrent(state)) return false;
    if (state.completed) return true;
    if (state.completion) return state.completion;
    if (state.completionFailed || state.final === null || state.confirmed !== state.final) {
      return false;
    }
    state.completion = request(state, {
      v: 1, type: 'playback_complete', binding: state.scope.binding,
      response_id: state.scope.responseId, final_audio_sequence: state.final,
    }).then(() => {
      state.completed = true;
      return true;
    }).catch((error) => {
      if (isCurrent(state)) state.completionFailed = true;
      throw error;
    }).finally(() => { state.completion = null; });
    return state.completion;
  }

  async function registerSegment(event) {
    const state = current;
    if (!state || event?.scope !== state.scope) return false;
    const { audioSequence, sampleRate, sampleStart, sampleEnd } = event;
    if (!integer(audioSequence) || audioSequence >= Number.MAX_SAFE_INTEGER
      || !RATES.has(sampleRate) || !integer(sampleStart) || !integer(sampleEnd)
      || sampleEnd <= sampleStart) return false;
    if (audioSequence <= state.confirmed) return true;
    const existing = state.pending.get(audioSequence);
    if (existing) {
      return existing.sampleRate === sampleRate && existing.sampleStart === sampleStart
        && existing.sampleEnd === sampleEnd;
    }
    // 欠番を先に登録して保留枠を埋めることを禁止する。
    if (state.final !== null || audioSequence !== state.next || sampleStart !== state.sampleEnd
      || (state.sampleRate !== null && state.sampleRate !== sampleRate)) return false;
    if (state.pending.size >= maxPendingSegments) {
      invalidate();
      throw new RangeError('pending segment limit reached; response invalidated');
    }
    state.pending.set(audioSequence, { sampleRate, sampleStart, sampleEnd });
    state.next += 1;
    state.sampleEnd = sampleEnd;
    state.sampleRate = sampleRate;
    const accepted = await controller.registerSegment({
      responseId: state.scope.responseId, generation: state.scope.generation,
      audioSequence, pcmSampleCount: sampleEnd - sampleStart,
    });
    if (!isCurrent(state)) return false;
    await maybeComplete(state);
    return accepted;
  }

  async function recordRendered(event) {
    const state = current;
    if (!state || event?.scope !== state.scope || !integer(event.audioSequence)
      || !integer(event.renderedSampleCount)) return false;
    if (event.audioSequence <= state.confirmed) return true;
    if (!state.pending.has(event.audioSequence)) return false;
    const accepted = await controller.recordRendered({
      responseId: state.scope.responseId, generation: state.scope.generation,
      audioSequence: event.audioSequence, renderedSampleCount: event.renderedSampleCount,
    });
    if (!isCurrent(state)) return false;
    await maybeComplete(state);
    return accepted;
  }

  async function finish({ scope, finalAudioSequence } = {}) {
    const state = current;
    if (!state || scope !== state.scope || !Number.isSafeInteger(finalAudioSequence)
      || finalAudioSequence < -1 || finalAudioSequence !== state.next - 1
      || (state.final !== null && state.final !== finalAudioSequence)) return false;
    state.final = finalAudioSequence;
    return maybeComplete(state);
  }

  async function retry() {
    const state = current;
    if (!state) return false;
    const completionFailed = state.completionFailed;
    state.completionFailed = false;
    const retried = await controller.retry();
    if (!isCurrent(state)) return false;
    const completed = await maybeComplete(state);
    return retried || (completionFailed && completed);
  }

  function close() {
    closed = true;
    try { invalidate(); } finally { controller.close(); }
  }

  return { startResponse, registerSegment, recordRendered, finish, retry,
    cancel: invalidate, reconnect: invalidate, close };
}
