import assert from 'node:assert/strict';
import test from 'node:test';

import { createPlaybackAckController } from '../playback-ack.mjs';

function metadata(generation, audioSequence = 0, overrides = {}) {
  return {
    responseId: 'response',
    audioSequence,
    generation,
    pcmSampleCount: 8,
    ...overrides,
  };
}

function rendered(generation, audioSequence = 0, renderedSampleCount = 8, overrides = {}) {
  return {
    responseId: 'response',
    audioSequence,
    generation,
    renderedSampleCount,
    ...overrides,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function startWithTrackedPending(controller) {
  const OriginalMap = globalThis.Map;
  let pending;
  globalThis.Map = class extends OriginalMap {
    constructor(...args) {
      super(...args);
      pending = this;
    }
  };
  try {
    const generation = controller.startResponse('response');
    return { generation, pending };
  } finally {
    globalThis.Map = OriginalMap;
  }
}

test('out-of-order evidence acknowledges complete segments individually from zero', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    onAck: (ack) => { acknowledgements.push(ack); },
  });
  const generation = controller.startResponse('response');

  await controller.recordRendered(rendered(generation, 2));
  await controller.registerSegment(metadata(generation, 1));
  await controller.recordRendered(rendered(generation, 1));
  assert.deepEqual(acknowledgements, []);

  await controller.registerSegment(metadata(generation, 0));
  await controller.registerSegment(metadata(generation, 2));
  assert.deepEqual(acknowledgements, []);
  await controller.recordRendered(rendered(generation, 0));
  assert.deepEqual(acknowledgements, [0, 1, 2].map((audioSequence) => ({
    responseId: 'response', audioSequence, generation,
  })));
});

test('metadata and partial cumulative rendering do not imply playback', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({ onAck: (ack) => { acknowledgements.push(ack); } });
  const generation = controller.startResponse('response');

  await controller.registerSegment(metadata(generation));
  await controller.recordRendered(rendered(generation, 0, 3));
  await controller.recordRendered(rendered(generation, 0, 3));
  assert.deepEqual(acknowledgements, []);

  await controller.recordRendered(rendered(generation, 0, 8));
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation }]);
});

test('oversized rendering before metadata can be corrected by a matching snapshot', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({ onAck: (ack) => { acknowledgements.push(ack); } });
  const generation = controller.startResponse('response');

  assert.equal(await controller.recordRendered(rendered(generation, 0, 9)), true);
  assert.equal(await controller.registerSegment(metadata(generation)), true);
  assert.deepEqual(acknowledgements, []);
  assert.equal(await controller.recordRendered(rendered(generation, 0, 7)), true);
  assert.deepEqual(acknowledgements, []);
  assert.equal(await controller.recordRendered(rendered(generation, 0, 8)), true);
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation }]);
});

test('unregistered and mismatched evidence does not acknowledge a segment', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({ onAck: (ack) => { acknowledgements.push(ack); } });
  const generation = controller.startResponse('response');

  await controller.recordRendered(rendered(generation));
  await controller.recordRendered(rendered(generation, 0, 8, { responseId: 'other' }));
  await controller.recordRendered(rendered(generation + 1));
  assert.deepEqual(acknowledgements, []);

  await controller.registerSegment(metadata(generation, 0, { responseId: 'other' }));
  await controller.registerSegment(metadata(generation + 1));
  assert.deepEqual(acknowledgements, []);

  await controller.registerSegment(metadata(generation, 1));
  await controller.recordRendered(rendered(generation, 1, 9));
  assert.deepEqual(acknowledgements, []);
});

test('invalid segment numbers and inconsistent metadata never create an ACK', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({ onAck: (ack) => { acknowledgements.push(ack); } });
  const generation = controller.startResponse('response');

  for (const audioSequence of [-1, 0.5, Number.MAX_SAFE_INTEGER + 1]) {
    await controller.registerSegment(metadata(generation, audioSequence));
  }
  for (const pcmSampleCount of [0, -1, 0.5, Number.MAX_SAFE_INTEGER + 1]) {
    await controller.registerSegment(metadata(generation, 0, { pcmSampleCount }));
  }
  for (const renderedSampleCount of [-1, 0.5, Number.MAX_SAFE_INTEGER + 1]) {
    await controller.recordRendered(rendered(generation, 0, renderedSampleCount));
  }
  await controller.registerSegment(metadata(-1));
  await controller.recordRendered(rendered(-1));
  assert.deepEqual(acknowledgements, []);

  await controller.registerSegment(metadata(generation, 0));
  await controller.registerSegment(metadata(generation, 0, { pcmSampleCount: 9 }));
  await controller.recordRendered(rendered(generation, 0, 7));
  assert.deepEqual(acknowledgements, []);
  await controller.recordRendered(rendered(generation));
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation }]);
});

test('duplicate evidence while ACK is pending and after success does not duplicate callback', async () => {
  const pending = deferred();
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    onAck: (ack) => {
      acknowledgements.push(ack);
      return pending.promise;
    },
  });
  const generation = controller.startResponse('response');
  await controller.registerSegment(metadata(generation));
  const first = controller.recordRendered(rendered(generation));
  const duplicateMetadata = controller.registerSegment(metadata(generation));
  const duplicateRendered = controller.recordRendered(rendered(generation));
  await Promise.resolve();
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation }]);

  pending.resolve();
  await Promise.all([first, duplicateMetadata, duplicateRendered]);
  await controller.registerSegment(metadata(generation));
  await controller.recordRendered(rendered(generation));
  assert.equal(acknowledgements.length, 1);
});

test('cancel, reconnect, new response and close invalidate prior evidence', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({ onAck: (ack) => { acknowledgements.push(ack); } });
  let generation = controller.startResponse('response');

  for (const invalidate of [() => controller.cancel(), () => controller.reconnect(), () => controller.startResponse('response')]) {
    await controller.registerSegment(metadata(generation));
    invalidate();
    await controller.recordRendered(rendered(generation));
    const previous = generation;
    generation = controller.startResponse('response');
    assert.notEqual(generation, previous);
  }
  assert.deepEqual(acknowledgements, []);

  await controller.registerSegment(metadata(generation));
  await controller.recordRendered(rendered(generation));
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation }]);
  controller.close();
  await Promise.allSettled([
    Promise.resolve().then(() => controller.registerSegment(metadata(generation, 1))),
    Promise.resolve().then(() => controller.recordRendered(rendered(generation, 1))),
  ]);
  assert.equal(acknowledgements.length, 1);
});

test('late success from a cancelled ACK cannot advance a reused response ID', async () => {
  const oldAck = deferred();
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    onAck: (ack) => {
      acknowledgements.push(ack);
      if (ack.generation === oldGeneration) return oldAck.promise;
    },
  });
  const oldGeneration = controller.startResponse('response');
  await controller.registerSegment(metadata(oldGeneration));
  const oldOperation = controller.recordRendered(rendered(oldGeneration));
  controller.cancel();
  const newGeneration = controller.startResponse('response');
  await controller.registerSegment(metadata(newGeneration, 1));
  await controller.recordRendered(rendered(newGeneration, 1));
  oldAck.resolve();
  await oldOperation;
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation: oldGeneration }]);

  await controller.registerSegment(metadata(newGeneration));
  await controller.recordRendered(rendered(newGeneration));
  assert.deepEqual(acknowledgements.slice(1), [0, 1].map((audioSequence) => ({
    responseId: 'response', audioSequence, generation: newGeneration,
  })));
});

test('close invalidates an in-flight ACK and prevents a new response', async () => {
  const pending = deferred();
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    onAck: (ack) => {
      acknowledgements.push(ack);
      return pending.promise;
    },
  });
  const generation = controller.startResponse('response');
  await controller.registerSegment(metadata(generation));
  const operation = controller.recordRendered(rendered(generation));
  controller.close();
  pending.resolve();
  await operation;

  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation }]);
  assert.throws(() => controller.startResponse('response'));
  assert.equal(await controller.recordRendered(rendered(generation, 1)), false);
});

test('all lifecycle operations clear old pending evidence before an ACK settles', async (t) => {
  for (const operation of ['cancel', 'reconnect', 'startResponse', 'close']) {
    await t.test(operation, async () => {
      const oldAck = deferred();
      const acknowledgements = [];
      let oldGeneration;
      const controller = createPlaybackAckController({
        maxPendingSegments: 2,
        onAck: (ack) => {
          acknowledgements.push(ack);
          if (ack.generation === oldGeneration) return oldAck.promise;
        },
      });
      const tracked = startWithTrackedPending(controller);
      oldGeneration = tracked.generation;
      await controller.registerSegment(metadata(oldGeneration));
      const oldOperation = controller.recordRendered(rendered(oldGeneration));
      await controller.registerSegment(metadata(oldGeneration, 1));
      assert.equal(tracked.pending.size, 2);

      let newGeneration;
      if (operation === 'startResponse') {
        newGeneration = controller.startResponse('response');
      } else {
        controller[operation]();
        if (operation !== 'close') newGeneration = controller.startResponse('response');
      }
      assert.equal(tracked.pending.size, 0);

      if (newGeneration !== undefined) {
        await controller.registerSegment(metadata(newGeneration));
        await controller.recordRendered(rendered(newGeneration));
      }
      oldAck.resolve();
      await oldOperation;
      assert.deepEqual(acknowledgements, newGeneration === undefined
        ? [{ responseId: 'response', audioSequence: 0, generation: oldGeneration }]
        : [oldGeneration, newGeneration].map((generation) => ({
          responseId: 'response', audioSequence: 0, generation,
        })));
    });
  }
});

test('synchronous callback failure requires explicit ordered retry', async () => {
  const acknowledgements = [];
  let fail = true;
  const controller = createPlaybackAckController({
    onAck: (ack) => {
      acknowledgements.push(ack);
      if (fail) {
        fail = false;
        throw new Error('synthetic failure');
      }
    },
  });
  const generation = controller.startResponse('response');
  await controller.registerSegment(metadata(generation));
  await assert.rejects(controller.recordRendered(rendered(generation)), /synthetic failure/);
  await controller.registerSegment(metadata(generation, 1));
  await controller.recordRendered(rendered(generation, 1));
  assert.deepEqual(acknowledgements.map((ack) => ack.audioSequence), [0]);

  await controller.retry();
  assert.deepEqual(acknowledgements.map((ack) => ack.audioSequence), [0, 0, 1]);
});

test('asynchronous rejection remains pending and a late failure cannot affect a new generation', async () => {
  const oldAck = deferred();
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    onAck: (ack) => {
      acknowledgements.push(ack);
      if (ack.generation === oldGeneration) return oldAck.promise;
    },
  });
  const oldGeneration = controller.startResponse('response');
  await controller.registerSegment(metadata(oldGeneration));
  const oldOperation = controller.recordRendered(rendered(oldGeneration));
  controller.cancel();
  const newGeneration = controller.startResponse('response');
  await controller.registerSegment(metadata(newGeneration));
  await controller.recordRendered(rendered(newGeneration));
  oldAck.reject(new Error('late failure'));
  await assert.rejects(oldOperation, /late failure/);
  await controller.registerSegment(metadata(newGeneration, 1));
  await controller.recordRendered(rendered(newGeneration, 1));
  assert.deepEqual(acknowledgements.slice(1), [0, 1].map((audioSequence) => ({
    responseId: 'response', audioSequence, generation: newGeneration,
  })));
});

test('retry does not start a parallel callback while a prior ACK is in flight', async () => {
  const pending = deferred();
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    onAck: (ack) => {
      acknowledgements.push(ack);
      return pending.promise;
    },
  });
  const generation = controller.startResponse('response');
  await controller.registerSegment(metadata(generation));
  const operation = controller.recordRendered(rendered(generation));
  const retry = controller.retry();
  await Promise.resolve();
  assert.equal(acknowledgements.length, 1);
  pending.resolve();
  await Promise.all([operation, retry]);
  assert.equal(acknowledgements.length, 1);
});

test('pending limit counts out-of-order evidence and releases capacity after success', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    onAck: (ack) => { acknowledgements.push(ack); },
    maxPendingSegments: 2,
  });
  const generation = controller.startResponse('response');
  await controller.recordRendered(rendered(generation, 2));
  await controller.registerSegment(metadata(generation, 1));
  await controller.registerSegment(metadata(generation, 1));
  await assert.rejects(controller.registerSegment(metadata(generation, 0)), RangeError);
  await controller.recordRendered(rendered(generation, 1));
  assert.deepEqual(acknowledgements, []);
  await assert.rejects(controller.recordRendered(rendered(generation, 3)), RangeError);

  controller.cancel();
  const next = controller.startResponse('response');
  await controller.registerSegment(metadata(next));
  await controller.recordRendered(rendered(next));
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation: next }]);
});

test('failed ACK occupies capacity until explicit retry succeeds', async () => {
  const acknowledgements = [];
  let fail = true;
  const controller = createPlaybackAckController({
    maxPendingSegments: 1,
    onAck: (ack) => {
      acknowledgements.push(ack);
      if (fail) {
        fail = false;
        return Promise.reject(new Error('synthetic failure'));
      }
    },
  });
  const generation = controller.startResponse('response');
  await controller.registerSegment(metadata(generation));
  await assert.rejects(controller.recordRendered(rendered(generation)), /synthetic failure/);
  await assert.rejects(controller.registerSegment(metadata(generation, 1)), RangeError);
  assert.deepEqual(acknowledgements.map((ack) => ack.audioSequence), [0]);

  await controller.retry();
  await controller.registerSegment(metadata(generation, 1));
  await controller.recordRendered(rendered(generation, 1));
  assert.deepEqual(acknowledgements.map((ack) => ack.audioSequence), [0, 0, 1]);
});

test('an ACK in flight occupies the pending limit until it succeeds', async () => {
  const pending = deferred();
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    maxPendingSegments: 1,
    onAck: (ack) => {
      acknowledgements.push(ack);
      if (ack.audioSequence === 0) return pending.promise;
    },
  });
  const generation = controller.startResponse('response');
  await controller.registerSegment(metadata(generation));
  const first = controller.recordRendered(rendered(generation));
  await assert.rejects(controller.registerSegment(metadata(generation, 1)), RangeError);
  assert.deepEqual(acknowledgements.map((ack) => ack.audioSequence), [0]);

  pending.resolve();
  await first;
  await controller.registerSegment(metadata(generation, 1));
  await controller.recordRendered(rendered(generation, 1));
  assert.deepEqual(acknowledgements.map((ack) => ack.audioSequence), [0, 1]);
});

test('rejected oversized rendering does not corrupt existing evidence', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    maxPendingSegments: 1,
    onAck: (ack) => { acknowledgements.push(ack); },
  });
  const generation = controller.startResponse('response');
  await controller.registerSegment(metadata(generation));
  assert.equal(await controller.recordRendered(rendered(generation, 0, 9)), false);
  await controller.recordRendered(rendered(generation));
  assert.deepEqual(acknowledgements, [{ responseId: 'response', audioSequence: 0, generation }]);
});

test('successful ACKs free the finite window without retaining all prior segments', async () => {
  const acknowledgements = [];
  const controller = createPlaybackAckController({
    maxPendingSegments: 1,
    onAck: (ack) => { acknowledgements.push(ack); },
  });
  const generation = controller.startResponse('response');

  for (let audioSequence = 0; audioSequence < 5; audioSequence += 1) {
    await controller.registerSegment(metadata(generation, audioSequence));
    await controller.recordRendered(rendered(generation, audioSequence));
  }
  assert.deepEqual(acknowledgements.map((ack) => ack.audioSequence), [0, 1, 2, 3, 4]);
});

test('pending limit must be a positive safe integer', () => {
  for (const maxPendingSegments of [0, -1, 0.5, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => createPlaybackAckController({ onAck: () => {}, maxPendingSegments }), RangeError);
  }
});
