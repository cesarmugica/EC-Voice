"""Separar la voz de la música (modelo MDX-Net «Kim Vocal 2» de UVR).

Corre con el Python del motor:  python ecvoice_separate.py MODELO.onnx ENTRADA_DIR SALIDA_DIR
Lee cada .wav (44.1 kHz) de ENTRADA_DIR y guarda solo la voz en SALIDA_DIR.
Progreso por stdout:  @@P <archivo actual> <total> <nombre>
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

# Datos del modelo Kim_Vocal_2.onnx (tabla de modelos de UVR)
N_FFT = 7680
DIM_F = 3072
DIM_T = 256
HOP = 1024
COMPENSATE = 1.009
RATE = 44100
OVERLAP = 0.25


class MDX:
    def __init__(self, model_path: str, session=None) -> None:
        if session is None:
            import onnxruntime as ort

            with contextlib.suppress(Exception):
                ort.set_default_logger_severity(3)
            # ONNX Runtime no encuentra solo las DLL de CUDA/cuDNN: cargar las que ya trae PyTorch
            # (o los paquetes nvidia-*) antes de abrir la sesión; si no, se va al procesador (lento).
            with contextlib.suppress(Exception):
                import torch  # noqa: F401  (carga cudart/cudnn de PyTorch en el proceso)
            with contextlib.suppress(Exception):
                ort.preload_dlls()
            providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
            options = ort.SessionOptions()
            options.log_severity_level = 3
            session = ort.InferenceSession(model_path, providers=providers, sess_options=options)
            print("Separador en: " + ", ".join(session.get_providers()), flush=True)
        self.session = session
        n = np.arange(N_FFT)
        self.window = (0.5 - 0.5 * np.cos(2 * np.pi * n / N_FFT)).astype(np.float32)  # Hann periódica
        self.chunk = HOP * (DIM_T - 1)
        self.trim = N_FFT // 2
        self.n_bins = N_FFT // 2 + 1

    # STFT / ISTFT iguales a torch.stft/istft(center=True, reflect)
    def _stft(self, x: np.ndarray) -> np.ndarray:
        """x: (2, T) → (1, 4, DIM_F, frames): [re0, im0, re1, im1]."""
        half = N_FFT // 2
        padded = np.pad(x, ((0, 0), (half, half)), mode="reflect")
        frames = 1 + (padded.shape[1] - N_FFT) // HOP
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
        spec = np.fft.rfft(padded[:, idx] * self.window, axis=-1)  # (2, frames, bins)
        spec = spec.transpose(0, 2, 1)[:, :DIM_F, :]               # (2, DIM_F, frames)
        out = np.stack([spec.real, spec.imag], 1).reshape(1, 4, DIM_F, frames)
        return out.astype(np.float32)

    def _istft(self, spec: np.ndarray) -> np.ndarray:
        """(1, 4, DIM_F, frames) → (2, T)."""
        frames = spec.shape[-1]
        full = np.zeros((4, self.n_bins, frames), np.float32)
        full[:, :DIM_F] = spec[0]
        complex_spec = (full[0::2] + 1j * full[1::2]).transpose(0, 2, 1)  # (2, frames, bins)
        chunks = np.fft.irfft(complex_spec, n=N_FFT, axis=-1) * self.window
        length = N_FFT + HOP * (frames - 1)
        out = np.zeros((2, length), np.float64)
        norm = np.zeros(length, np.float64)
        for i in range(frames):
            out[:, i * HOP:i * HOP + N_FFT] += chunks[:, i]
            norm[i * HOP:i * HOP + N_FFT] += self.window ** 2
        half = N_FFT // 2
        out = out[:, half:length - half] / np.maximum(norm[half:length - half], 1e-8)
        return out.astype(np.float32)

    def _run(self, part: np.ndarray) -> np.ndarray:
        spec = self._stft(part)
        spec[:, :, :3, :] = 0
        pred = self.session.run(None, {"input": spec})[0]
        return self._istft(np.asarray(pred, dtype=np.float32))

    def vocals(self, mix: np.ndarray) -> np.ndarray:
        """mix: (2, T) float32 → voz (2, T)."""
        peak = float(np.abs(mix).max()) or 1.0
        mix = mix / peak
        gen = self.chunk - 2 * self.trim
        pad = gen + self.trim - (mix.shape[-1] % gen)
        mixture = np.concatenate([np.zeros((2, self.trim), np.float32), mix, np.zeros((2, pad), np.float32)], 1)
        step = int((1 - OVERLAP) * self.chunk)
        result = np.zeros((2, mixture.shape[-1]), np.float32)
        divider = np.zeros((2, mixture.shape[-1]), np.float32)
        if True:
            for start in range(0, mixture.shape[-1], step):
                end = min(start + self.chunk, mixture.shape[-1])
                size = end - start
                part = mixture[:, start:end]
                if size < self.chunk:
                    part = np.concatenate([part, np.zeros((2, self.chunk - size), np.float32)], 1)
                out = self._run(part)[:, :size]
                window = np.hanning(size)[None, :].astype(np.float32)
                result[:, start:end] += out * window
                divider[:, start:end] += window
        out = (result / np.maximum(divider, 1e-8))[:, self.trim:self.trim + mix.shape[-1]]
        return out * peak


def main() -> int:
    model, src, dst = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src.iterdir() if p.suffix.lower() == ".wav")
    if not files:
        print("No hay audios para separar.", flush=True)
        return 1
    mdx = MDX(model)
    for number, path in enumerate(files, 1):
        print(f"@@P {number} {len(files)} {path.name}", flush=True)
        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
        if rate != RATE:
            print(f"  {path.name}: se esperaba {RATE} Hz y tiene {rate}; se usa tal cual.", flush=True)
        mix = audio.T
        if mix.shape[0] == 1:
            mix = np.repeat(mix, 2, 0)
        voice = mdx.vocals(mix[:2])
        sf.write(str(dst / path.name), voice.T, rate, subtype="PCM_16")
        print(f"  {path.name}: voz separada ({audio.shape[0] / rate / 60:.1f} min)", flush=True)
    print("Separación lista.", flush=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
