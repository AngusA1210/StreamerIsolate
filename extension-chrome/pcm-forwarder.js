// AudioWorkletProcessor that extracts raw interleaved stereo float32 PCM from
// the captured tab audio and forwards it to the main thread in fixed-size
// chunks, for sending over the WebSocket to the local backend.
class PCMForwarder extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunkFrames = 2048;
    this._writeIndex = 0;
    this._buffer = new Float32Array(this._chunkFrames * 2);
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channelCount = input.length;
    const frameCount = input[0].length;

    for (let frame = 0; frame < frameCount; frame++) {
      const left = input[0][frame];
      const right = channelCount > 1 ? input[1][frame] : left;
      this._buffer[this._writeIndex * 2] = left;
      this._buffer[this._writeIndex * 2 + 1] = right;
      this._writeIndex++;
      if (this._writeIndex >= this._chunkFrames) {
        this.port.postMessage(this._buffer.slice(0, this._writeIndex * 2));
        this._writeIndex = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-forwarder", PCMForwarder);
