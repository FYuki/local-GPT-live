// 既知PCMだけを描画する。RTPの受信量や時計から元sample数を推定しない。
const PROCESSOR_NAME = 'local-gpt-live-pcm-renderer-v1';
const MAX_SEGMENTS = 64;
const moduleLoads = new WeakMap();

export async function createPcmRenderer({
  audioContext,
  workletUrl = new URL('./pcm-renderer-worklet.mjs', import.meta.url),
  onRendered,
  onError = () => {},
  maxQueuedSamples = audioContext?.sampleRate * 2,
} = {}) {
  if (!audioContext || typeof audioContext.audioWorklet?.addModule !== 'function'
    || typeof audioContext.getOutputTimestamp !== 'function'
    || typeof audioContext.addEventListener !== 'function'
    || typeof audioContext.removeEventListener !== 'function'
    || !Number.isSafeInteger(audioContext.sampleRate) || audioContext.sampleRate <= 0
    || typeof globalThis.AudioWorkletNode !== 'function') {
    throw new TypeError('AudioWorklet and an output clock are required');
  }
  if (typeof onRendered !== 'function' || typeof onError !== 'function') {
    throw new TypeError('Renderer callbacks must be functions');
  }
  if (!Number.isSafeInteger(maxQueuedSamples) || maxQueuedSamples <= 0) {
    throw new RangeError('maxQueuedSamples must be a positive safe integer');
  }
  if (audioContext.state === 'closed') throw new Error('AudioContext is closed');

  const moduleKey = String(workletUrl);
  let load = moduleLoads.get(audioContext);
  if (load && load.key !== moduleKey) throw new Error('Worklet URL is already bound');
  if (!load) {
    load = { key: moduleKey, promise: audioContext.audioWorklet.addModule(workletUrl) };
    moduleLoads.set(audioContext, load);
  }
  try {
    await load.promise;
  } catch (error) {
    if (moduleLoads.get(audioContext) === load) moduleLoads.delete(audioContext);
    throw error;
  }
  if (audioContext.state === 'closed') throw new Error('AudioContext is closed');

  let closed = false;
  let active = null;
  const usedScopes = new WeakSet();

  function detach(state) {
    clearInterval(state.timer);
    state.node.port.onmessage = null;
    state.node.port.onmessageerror = null;
    state.node.onprocessorerror = null;
    try {
      state.node.port.postMessage({ kind: 'stop' });
    } finally {
      try {
        state.node.disconnect();
      } finally {
        state.node.port.close();
        state.segments.clear();
        state.pendingSamples = 0;
      }
    }
  }

  function cancel() {
    const previous = active;
    active = null; // disconnect中に届いた旧通知も無効にする。
    if (previous) detach(previous);
  }

  function close() {
    if (closed) return;
    closed = true;
    audioContext.removeEventListener('statechange', stateChanged);
    cancel();
  }

  function fail(state, error) {
    if (closed || active !== state) return;
    try {
      close();
    } catch {
      // 元の失敗を報告する。detachは全ての解放処理をfinallyで試みる。
    }
    try {
      Promise.resolve(onError(error)).catch(() => {});
    } catch {
      // エラー通知側の失敗から再び通知しない。
    }
  }

  function stateChanged() {
    if (audioContext.state === 'closed' && active) {
      fail(active, new Error('AudioContext closed during playback'));
    }
  }
  audioContext.addEventListener('statechange', stateChanged);

  function poll(state) {
    if (closed || active !== state || audioContext.state !== 'running') return;
    try {
      const time = audioContext.getOutputTimestamp()?.contextTime;
      if (!Number.isFinite(time) || time < 0) {
        throw new Error('Output clock is unavailable');
      }
      for (const [audioSequence, segment] of state.segments) {
        if (active !== state || closed || audioContext.state !== 'running') return;
        if (segment.notified) continue;
        if (segment.endContextTime === null || time < segment.endContextTime) break;
        segment.notified = true;
        state.segments.delete(audioSequence);
        state.pendingSamples -= segment.sampleCount;
        // 証拠通知は同期observer。ACK送信のPromise/再試行はhostが所有する。
        const result = onRendered(Object.freeze({
          scope: state.scope,
          audioSequence,
          renderedSampleCount: segment.sampleCount,
          endContextTime: segment.endContextTime,
        }));
        if (result !== null && (typeof result === 'object' || typeof result === 'function')
          && typeof result.then === 'function') {
          Promise.resolve(result).catch(() => {});
          throw new TypeError('onRendered must be synchronous');
        }
      }
    } catch (error) {
      fail(state, error);
    }
  }

  function startResponse(scope) {
    if (closed) throw new Error('PCM renderer is closed');
    if (audioContext.state === 'closed') throw new Error('AudioContext is closed');
    if (scope === null || typeof scope !== 'object') throw new TypeError('scope must be an object');
    if (usedScopes.has(scope)) throw new Error('scope cannot be reused');
    cancel();
    const node = new AudioWorkletNode(audioContext, PROCESSOR_NAME, {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: { maxQueuedSamples, maxSegments: MAX_SEGMENTS },
    });
    const state = {
      scope, node, timer: null, segments: new Map(), pendingSamples: 0,
      nextSequence: 0, nextRenderedSequence: 0, lastEndTime: 0,
    };
    usedScopes.add(scope);
    active = state;
    node.port.onmessage = ({ data }) => {
      if (closed || active !== state) return false;
      const segment = state.segments.get(data?.audioSequence);
      if (data?.kind !== 'rendered' || !segment || segment.endContextTime !== null
        || data.audioSequence !== state.nextRenderedSequence
        || data.renderedSampleCount !== segment.sampleCount
        || !Number.isFinite(data.endContextTime) || data.endContextTime <= state.lastEndTime) {
        fail(state, new Error('Invalid PCM render evidence'));
        return false;
      }
      segment.endContextTime = data.endContextTime;
      state.lastEndTime = data.endContextTime;
      state.nextRenderedSequence += 1;
      poll(state);
      return true;
    };
    node.port.onmessageerror = () => fail(state, new Error('PCM message could not be decoded'));
    node.onprocessorerror = () => fail(state, new Error('PCM processor failed'));
    try {
      node.connect(audioContext.destination);
      state.timer = setInterval(() => poll(state), 10);
    } catch (error) {
      fail(state, error);
      throw error;
    }
    return true;
  }

  function enqueue({ scope, audioSequence, pcm, sampleRate } = {}) {
    const state = active;
    if (closed || !state || state.scope !== scope) return false;
    if (!Number.isSafeInteger(audioSequence) || audioSequence < 0 || audioSequence >= Number.MAX_SAFE_INTEGER
      || audioSequence !== state.nextSequence) {
      throw new RangeError('audioSequence must be contiguous from zero');
    }
    if (sampleRate !== audioContext.sampleRate) throw new RangeError('PCM sample rate must match AudioContext');
    if (!(pcm instanceof Float32Array) || pcm.length === 0) throw new TypeError('PCM must be a nonempty Float32Array');
    if (state.segments.size >= MAX_SEGMENTS || pcm.length > maxQueuedSamples - state.pendingSamples) {
      throw new RangeError('PCM queue capacity exceeded');
    }
    for (const sample of pcm) {
      if (!Number.isFinite(sample)) throw new TypeError('PCM samples must be finite');
    }
    const ownedPcm = pcm.slice(); // 呼出側の再利用・書換えに影響されない所有コピー。
    state.segments.set(audioSequence, { sampleCount: pcm.length, endContextTime: null, notified: false });
    state.pendingSamples += pcm.length;
    state.nextSequence += 1;
    try {
      state.node.port.postMessage({ kind: 'enqueue', audioSequence, pcm: ownedPcm }, [ownedPcm.buffer]);
    } catch (error) {
      fail(state, error);
      throw error;
    }
    return true;
  }

  return Object.freeze({ startResponse, enqueue, cancel, close });
}
