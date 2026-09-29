"""Small NumPy/OpenCV adapter for the UVR MDX vocals model.

The MDX tensor layout and parameters follow UVR. No model code is downloaded
or executed at runtime; inference uses a pinned ONNX graph on the CPU.
See docs/AUDIO.md for the upstream sources and limitations.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import wave

import numpy as np

from .audio_model import COMPENSATE, DIM_F, DIM_T, HOP, N_FFT, SAMPLE_RATE, require_model


class Spectrogram:
    def __init__(self, n_fft=N_FFT, hop=HOP, bins=DIM_F):
        self.n_fft, self.hop, self.bins = n_fft, hop, bins
        self.window = np.hanning(n_fft+1)[:-1].astype(np.float32)

    def encode(self, audio):
        padded = np.pad(audio, ((0,0),(self.n_fft//2,self.n_fft//2)), mode='reflect')
        frames = np.lib.stride_tricks.sliding_window_view(padded, self.n_fft, axis=-1)[:, ::self.hop]
        spectrum = np.fft.rfft(frames*self.window, axis=-1).transpose(0,2,1)
        return np.ascontiguousarray(np.stack((spectrum.real,spectrum.imag), axis=1).reshape(1,4,spectrum.shape[1],spectrum.shape[2])[:, :, :self.bins], dtype=np.float32)

    def decode(self, tensor):
        channels = tensor.reshape(2,2,self.bins,-1)
        spectrum = channels[:,0] + 1j*channels[:,1]
        spectrum = np.pad(spectrum, ((0,0),(0,self.n_fft//2+1-self.bins),(0,0)))
        frames = np.fft.irfft(spectrum.transpose(0,2,1), n=self.n_fft, axis=-1).astype(np.float32)
        length = (frames.shape[1]-1)*self.hop+self.n_fft
        result = np.zeros((2,length), dtype=np.float32)
        weights = np.zeros(length, dtype=np.float32)
        for i in range(frames.shape[1]):
            start = i*self.hop
            result[:,start:start+self.n_fft] += frames[:,i]*self.window
            weights[start:start+self.n_fft] += self.window**2
        result /= np.maximum(weights, np.finfo(np.float32).tiny)
        return result[:,self.n_fft//2:-self.n_fft//2]


def separate_array(mix, infer, progress=lambda done,total: None):
    """Predict vocals, then subtract them from the original stereo waveform."""
    if mix.ndim != 2 or mix.shape[0] != 2 or mix.shape[1] == 0 or not np.isfinite(mix).all():
        raise ValueError('人声模型输入必须是有效的双声道音频。')
    transform = Spectrogram()
    chunk = HOP*(DIM_T-1)
    trim = N_FFT//2
    core = chunk-2*trim
    step = core//2
    length = mix.shape[1]
    padded = np.pad(mix, ((0,0),(trim,chunk)))
    vocals = np.zeros_like(mix)
    weights = np.zeros(length, dtype=np.float32)
    window = np.hanning(core+2)[1:-1].astype(np.float32)
    starts = range(0,length,step)
    for index,start in enumerate(starts):
        spectrum = transform.encode(padded[:,start:start+chunk])
        spectrum[:,:,:3] = 0
        predicted = infer(spectrum)
        if predicted.shape != spectrum.shape or not np.isfinite(predicted).all():
            raise RuntimeError('人声模型返回了无效结果。')
        wave_prediction = transform.decode(predicted)[:,trim:trim+core]
        count = min(core,length-start)
        vocals[:,start:start+count] += wave_prediction[:,:count]*window[:count]
        weights[start:start+count] += window[:count]
        progress(index+1,len(starts))
    vocals /= np.maximum(weights, np.finfo(np.float32).tiny)
    return mix-COMPENSATE*vocals


def save_progress(path, **fields):
    partial = path.with_suffix('.partial.json')
    partial.write_text(json.dumps(fields), encoding='utf-8')
    partial.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('progress', type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    model = require_model(verify=True)
    with wave.open(str(args.source), 'rb') as source:
        if source.getframerate() != SAMPLE_RATE or source.getnchannels() != 2 or source.getsampwidth() != 2:
            raise ValueError('分离输入需要 44.1 kHz 双声道 PCM16 WAV。')
        if not 0 < source.getnframes() <= SAMPLE_RATE*121:
            raise ValueError('仅处理最长 120 秒的入选音频。')
        mix = np.frombuffer(source.readframes(source.getnframes()), '<i2').reshape(-1,2).T.astype(np.float32)/32768
    save_progress(args.progress, done=0, total=1, phase='loading')
    import cv2
    cv2.setNumThreads(2)
    network = cv2.dnn.readNetFromONNX(str(model))
    network.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
    network.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    def infer(value):
        network.setInput(value)
        return network.forward()
    output = separate_array(mix, infer, lambda done,total:save_progress(args.progress,done=done,total=total,phase='separating')) if np.max(np.abs(mix))>1e-6 else mix.copy()
    peak = float(np.max(np.abs(output)))
    gain = min(1., .99/max(peak,1e-8))
    pcm = (np.clip(output*gain,-1,1)*32767).T.astype('<i2').tobytes()
    partial = args.destination.with_suffix('.partial.wav')
    with wave.open(str(partial),'wb') as dest:
        dest.setparams((2,2,SAMPLE_RATE,0,'NONE','not compressed'))
        dest.writeframes(pcm)
    partial.replace(args.destination)
    save_progress(args.progress,done=1,total=1,phase='complete',gain=gain,elapsed_seconds=round(time.perf_counter()-started,3))


if __name__ == '__main__':
    main()
