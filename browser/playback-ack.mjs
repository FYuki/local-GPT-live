const DEFAULT_MAX_PENDING_SEGMENTS = 64;

function isNonnegativeSafeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function matchesCurrent(state, event) {
  return state !== null
    && event?.responseId === state.responseId
    && event.generation === state.generation
    && isNonnegativeSafeInteger(event.audioSequence);
}

export function createPlaybackAckController({
  onAck,
  maxPendingSegments = DEFAULT_MAX_PENDING_SEGMENTS,
} = {}) {
  if (typeof onAck !== 'function') {
    throw new TypeError('onAck must be a function');
  }
  if (!Number.isSafeInteger(maxPendingSegments) || maxPendingSegments < 1) {
    throw new RangeError('maxPendingSegments must be a positive safe integer');
  }

  let nextGeneration = 0;
  let current = null;
  let closed = false;

  function pendingSegment(state, audioSequence) {
    let segment = state.pending.get(audioSequence);
    if (segment === undefined) {
      if (state.pending.size >= maxPendingSegments) {
        throw new RangeError('pending segment limit reached');
      }
      segment = { pcmSampleCount: undefined, renderedSampleCount: undefined };
      state.pending.set(audioSequence, segment);
    }
    return segment;
  }

  function isNextSegmentComplete(state) {
    const segment = state.pending.get(state.nextAck);
    return segment !== undefined
      && segment.pcmSampleCount !== undefined
      && segment.renderedSampleCount === segment.pcmSampleCount;
  }

  async function drain(state) {
    if (current !== state || state.inFlight || state.failed) return true;

    while (current === state) {
      if (!isNextSegmentComplete(state)) break;

      state.inFlight = true;
      try {
        await onAck({
          responseId: state.responseId,
          audioSequence: state.nextAck,
          generation: state.generation,
        });
      } catch (error) {
        if (current === state) state.failed = true;
        throw error;
      } finally {
        state.inFlight = false;
      }

      if (current !== state) break;
      state.pending.delete(state.nextAck);
      state.nextAck += 1;
    }
    return true;
  }

  function startResponse(responseId) {
    if (closed) throw new Error('controller is closed');
    if (typeof responseId !== 'string') {
      throw new TypeError('responseId must be a string');
    }
    if (!Number.isSafeInteger(nextGeneration)) {
      throw new RangeError('generation limit reached');
    }
    const generation = nextGeneration;
    nextGeneration += 1;
    invalidate();
    current = {
      responseId,
      generation,
      pending: new Map(),
      nextAck: 0,
      inFlight: false,
      failed: false,
    };
    return generation;
  }

  async function registerSegment(event) {
    const state = current;
    if (!matchesCurrent(state, event)
      || !Number.isSafeInteger(event.pcmSampleCount)
      || event.pcmSampleCount < 1) return false;
    if (event.audioSequence < state.nextAck) return true;

    const segment = pendingSegment(state, event.audioSequence);
    if (segment.pcmSampleCount !== undefined && segment.pcmSampleCount !== event.pcmSampleCount) {
      return false;
    }
    segment.pcmSampleCount = event.pcmSampleCount;
    if (segment.renderedSampleCount > segment.pcmSampleCount) {
      segment.renderedSampleCount = undefined;
    }
    return drain(state);
  }

  async function recordRendered(event) {
    const state = current;
    if (!matchesCurrent(state, event)
      || !isNonnegativeSafeInteger(event.renderedSampleCount)) return false;
    if (event.audioSequence < state.nextAck) return true;

    const existing = state.pending.get(event.audioSequence);
    if (existing?.pcmSampleCount !== undefined
      && event.renderedSampleCount > existing.pcmSampleCount) return false;
    const segment = pendingSegment(state, event.audioSequence);
    if (segment.renderedSampleCount === undefined
      || event.renderedSampleCount > segment.renderedSampleCount) {
      segment.renderedSampleCount = event.renderedSampleCount;
    }
    return drain(state);
  }

  function retry() {
    const state = current;
    if (state === null) return Promise.resolve(false);
    if (state.inFlight || !state.failed) return Promise.resolve(false);
    state.failed = false;
    return drain(state);
  }

  function invalidate() {
    if (current !== null) current.pending.clear();
    current = null;
  }

  function close() {
    invalidate();
    closed = true;
  }

  return {
    startResponse,
    registerSegment,
    recordRendered,
    retry,
    cancel: invalidate,
    reconnect: invalidate,
    close,
  };
}
