// 独立于 vad-web 的小 worklet:只管把麦克风原始采样转成 16-bit PCM 转发给主线程,
// 是否真的往 WS 发要不要看 VAD 的判断——这里不做任何"是否在说话"的决策。
// AudioWorklet 通常跑在设备原生采样率(常见 44.1k/48k),而 STT 要 16k——降采样
// 放主线程做更简单(这里只管把样本尽量原样转发出去,减少 worklet 里的复杂度)。
class PcmForwarderProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length) {
      const float32 = input[0];
      const int16 = new Int16Array(float32.length);
      for (let i = 0; i < float32.length; i++) {
        const s = Math.max(-1, Math.min(1, float32[i]));
        int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage({ pcm: int16, sampleRate }, [int16.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm-forwarder", PcmForwarderProcessor);
