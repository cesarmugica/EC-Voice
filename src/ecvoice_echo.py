"""Quitar del micrófono la música que regresa por la consola.

Muchas consolas USB mandan de vuelta a la PC la mezcla completa: tu micrófono
y también lo que la PC les envía (la música de YouTube). EC-Voice conoce esa
música exacta (la copia antes de que llegue a la consola), así que puede
calcular cómo «suena» al regresar (retraso, volumen, ecualización de la
consola) y restarla, igual que las videollamadas quitan el eco.

Filtro adaptativo en frecuencia por particiones (PBFDAF/MDF), con
normalización por banda y freno cuando cantas encima de la música.
"""

from __future__ import annotations

import numpy as np


class EchoRemover:
    def __init__(self, block: int = 512, partitions: int = 16, step: float = 0.35) -> None:
        self.B = block                      # 512 muestras ≈ 10.7 ms a 48 kHz
        self.P = partitions                 # 16 × 512 ≈ 170 ms de «eco» que puede modelar
        self.step = step
        self.bins = block + 1
        self.reset()

    def reset(self) -> None:
        self.X = np.zeros((self.P, self.bins), dtype=np.complex128)   # espectros de la música (más reciente primero)
        self.W = np.zeros((self.P, self.bins), dtype=np.complex128)   # el «camino» de la consola
        self.prev_ref = np.zeros(self.B)
        self.power = np.full(self.bins, 1e-6)
        self.erle_db = 0.0                  # cuánto se está quitando (para el registro)
        self.blocks = 0

    def process(self, ref: np.ndarray, mic: np.ndarray) -> np.ndarray:
        """ref y mic: B muestras mono. Devuelve el micrófono sin la música."""
        B = self.B
        ref = np.asarray(ref, dtype=np.float64)
        mic = np.asarray(mic, dtype=np.float64)
        X0 = np.fft.rfft(np.concatenate([self.prev_ref, ref]))
        self.prev_ref = ref
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = X0

        Y = np.sum(self.X * self.W, axis=0)
        echo = np.fft.irfft(Y)[B:]
        err = mic - echo

        ref_pow = float(np.mean(ref ** 2))
        mic_pow = float(np.mean(mic ** 2))
        err_pow = float(np.mean(err ** 2))
        echo_pow = float(np.mean(echo ** 2))
        self.blocks += 1
        if echo_pow > 1e-10 and err_pow > 1e-12:
            self.erle_db = 0.95 * self.erle_db + 0.05 * 10 * np.log10(mic_pow / err_pow + 1e-12)

        # Adaptar solo si hay música; frenar si cantas encima (el error es mucho
        # más fuerte que el eco estimado) para no «aprender» tu voz.
        if ref_pow > 1e-7:
            self.power = 0.85 * self.power + 0.15 * (np.abs(X0) ** 2)
            norm = self.power * self.P + 1e-6 * B
            talk = err_pow / (echo_pow + 1e-9)
            mu = self.step if (self.blocks < 200 or talk < 4.0) else self.step * 0.05
            E = np.fft.rfft(np.concatenate([np.zeros(B), err]))
            G = mu * np.conj(self.X) * E[None, :] / norm[None, :]
            # Restricción de gradiente: mantener cada partición causal y de B muestras.
            g = np.fft.irfft(G, axis=1)[:, :B]
            G = np.fft.rfft(np.concatenate([g, np.zeros_like(g)], axis=1), axis=1)
            self.W += G
            # Si diverge (resta más de lo que entra), volver a empezar.
            if err_pow > 4 * mic_pow + 1e-6:
                self.W[:] = 0
        return err.astype(np.float32)
