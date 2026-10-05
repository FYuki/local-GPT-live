// 入力に識別済みPCMを持つrenderer。補充無音を描画済みPCMとして報告しない。
class PcmRendererProcessor extends AudioWorkletProcessor {
  constructor({ processorOptions = {} } = {}) {
    super();
    const { maxQueuedSamples, maxSegments = 64 } = processorOptions;
    if (!Number.isSafeInteger(maxQueuedSamples) || maxQueuedSamples <= 0
      || !Number.isSafeInteger(maxSegments) || maxSegments <= 0 || maxSegments > 64) {
      throw new RangeError('Invalid PCM queue limits');
    }
    this.maxQueuedSamples = maxQueuedSamples;
    this.maxSegments = maxSegments;
    this.queue = [];
    this.queuedSamples = 0;
    this.nextSequence = 0;
    this.stopped = false;
    this.port.onmessage = ({ data }) => {
      if (this.stopped) return;
      if (data?.kind === 'stop') {
        this.stop();
        return;
      }
      if (data?.kind !== 'enqueue' || !Number.isSafeInteger(data.audioSequence)
        || data.audioSequence < 0 || data.audioSequence >= Number.MAX_SAFE_INTEGER
        || data.audioSequence !== this.nextSequence
        || !(data.pcm instanceof Float32Array) || data.pcm.length === 0) {
        this.fail('invalid_pcm');
        return;
      }
      if (this.queue.length >= this.maxSegments
        || data.pcm.length > this.maxQueuedSamples - this.queuedSamples) {
        this.fail('queue_full');
        return;
      }
      for (const sample of data.pcm) {
        if (!Number.isFinite(sample)) {
          this.fail('invalid_pcm');
          return;
        }
      }
      this.queue.push({ audioSequence: data.audioSequence, pcm: data.pcm, offset: 0 });
      this.queuedSamples += data.pcm.length;
      this.nextSequence += 1;
    };
    this.port.onmessageerror = () => this.fail('invalid_message');
  }

  stop() {
    this.stopped = true;
    this.queue.length = 0;
    this.queuedSamples = 0;
    this.port.onmessage = null;
    this.port.onmessageerror = null;
    this.port.close();
  }

  fail(reason) {
    this.port.postMessage({ kind: 'error', reason });
    this.stop();
  }

  process(_inputs, outputs) {
    for (const output of outputs) {
      for (const channel of output) channel.fill(0);
    }
    if (this.stopped) return false;
    const output = outputs[0];
    if (!output || output.length !== 1) {
      this.fail('mono_output_required');
      return false;
    }
    const channel = output[0];
    let outputOffset = 0;
    while (outputOffset < channel.length && this.queue.length > 0) {
      const segment = this.queue[0];
      const count = Math.min(channel.length - outputOffset, segment.pcm.length - segment.offset);
      channel.set(segment.pcm.subarray(segment.offset, segment.offset + count), outputOffset);
      segment.offset += count;
      outputOffset += count;
      this.queuedSamples -= count;
      if (segment.offset === segment.pcm.length) {
        this.queue.shift();
        this.port.postMessage({
          kind: 'rendered',
          audioSequence: segment.audioSequence,
          renderedSampleCount: segment.pcm.length,
          endContextTime: (currentFrame + outputOffset) / sampleRate,
        });
      }
    }
    return true;
  }
}

registerProcessor('local-gpt-live-pcm-renderer-v1', PcmRendererProcessor);
