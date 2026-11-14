"""Quitar del micrófono la música que regresa por la consola.

Consolas USB como la BOMGE F4-MP3 mandan de vuelta a la PC la mezcla
completa: tu micrófono y también la música que la PC les envía. EC-Voice
conoce esa música exacta (la copia antes de que llegue a la consola), así que
puede calcular cómo «suena» al regresar y restarla, igual que las
videollamadas quitan el eco.

Dos partes:
1. **Retraso:** cada segundo se mide cuánto tarda la música en regresar
   (GCC-PHAT). Con la F4-MP3 son ~80 ms y crecen ~3.6 muestras por segundo
   porque la entrada y la salida USB no tienen exactamente el mismo reloj.
2. **Filtro adaptativo corto** (4 × 512 muestras ≈ 43 ms, en frecuencia por
   particiones) alineado con ese retraso. Si el retraso se mueve, el filtro se
   recorre en el tiempo sin olvidar lo aprendido.

Con el diagnóstico real de la F4-MP3 quita ~15-17 dB de música (antes ~2 dB).
"""

from __future__ import annotations

import numpy as np

RATE = 48000


class EchoRemover:
    def __init__(self, block: int = 512, partitions: int = 4, step: float = 0.2,
                 max_delay_ms: float = 400, lead: int = 900) -> None:
        self.B = block
        self.P = partitions
        self.step = step
        self.lead = lead                    # el filtro empieza 900 muestras antes del retraso medido
        self.max_lag = int(max_delay_ms * RATE / 1000)
        self.hist_len = self.max_lag + int(1.6 * RATE) + self.P * block
        self.reset()

    def reset(self) -> None:
        B, P = self.B, self.P
        self.X = np.zeros((P, B + 1), dtype=np.complex128)
        self.W = np.zeros((P, B + 1), dtype=np.complex128)
        self.prev_ref = np.zeros(B)
        self.power = np.full(B + 1, 1e-6)
        self.ref_hist = np.zeros(self.hist_len)     # referencia tal como llega (la más nueva al final)
        self.mic_hist = np.zeros(int(1.5 * RATE))   # micrófono crudo para medir el retraso
        self.pre = None                              # retraso aplicado a la referencia (muestras)
        self.delay = 0                               # retraso medido (muestras)
        self.confidence = 0.0
        self.blocks = 0
        self.erle_db = 0.0

    # ---------- retraso ----------
    def _measure_delay(self) -> None:
        mic = self.mic_hist
        n = len(mic)
        ref = self.ref_hist[-(n + self.max_lag):]
        if np.mean(ref[-n:] ** 2) < 1e-6:      # sin música no se puede medir
            return
        size = 1 << int(np.ceil(np.log2(len(ref) + n)))
        M = np.fft.rfft(mic, size)
        R = np.fft.rfft(ref, size)
        cross = R * np.conj(M)   # cc[k] = Σ ref[t+k]·mic[t]: pico en k = max_lag - retraso
        cc = np.fft.irfft(cross / (np.abs(cross) + 1e-12), size)
        # mic[t] ≈ ref[t + max_lag - lag]: buscar lag en 0..max_lag
        window = cc[: self.max_lag + 1]
        k = int(np.argmax(np.abs(window)))
        peak = float(np.abs(window[k]))
        lag = self.max_lag - k
        if peak < 0.06:
            return
        self.confidence = peak
        if self.pre is None:
            self.delay = lag
            self.pre = max(0, lag - self.lead)
            self.W[:] = 0
            return
        self.delay = int(round(0.7 * self.delay + 0.3 * lag)) if abs(lag - self.delay) < 400 else lag
        target = max(0, self.delay - self.lead)
        shift = target - self.pre
        if abs(shift) >= 96:
            self._shift_filter(shift)
            self.pre = target

    def _shift_filter(self, shift: int) -> None:
        """Recorrer el filtro en el tiempo (el retraso cambió) sin perder lo aprendido."""
        B, P = self.B, self.P
        h = np.concatenate([np.fft.irfft(self.W[p])[:B] for p in range(P)])
        h = np.roll(h, -shift)
        if shift > 0:
            h[-shift:] = 0
        else:
            h[:-shift] = 0
        self.W = np.stack([np.fft.rfft(np.concatenate([h[p * B:(p + 1) * B], np.zeros(B)])) for p in range(P)])
        self.X[:] = 0
        self.prev_ref = np.zeros(B)

    # ---------- proceso ----------
    def process(self, ref: np.ndarray, mic: np.ndarray) -> np.ndarray:
        """ref: la música que se mandó (bloque actual); mic: lo que regresó. Devuelve mic sin la música."""
        B = self.B
        ref = np.asarray(ref, dtype=np.float64)
        mic = np.asarray(mic, dtype=np.float64)
        self.ref_hist = np.concatenate([self.ref_hist[B:], ref])
        self.mic_hist = np.concatenate([self.mic_hist[B:], mic])
        self.blocks += 1
        if self.blocks % 47 == 0 and self.blocks * B >= len(self.mic_hist):  # cada ~0.5 s
            self._measure_delay()
        if self.pre is None:
            return mic.astype(np.float32)

        end = len(self.ref_hist) - self.pre
        x = self.ref_hist[end - B:end]
        X0 = np.fft.rfft(np.concatenate([self.prev_ref, x]))
        self.prev_ref = x
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = X0
        echo = np.fft.irfft(np.sum(self.X * self.W, axis=0))[B:]
        err = mic - echo

        x_pow = float(np.mean(x ** 2))
        mic_pow = float(np.mean(mic ** 2))
        err_pow = float(np.mean(err ** 2))
        echo_pow = float(np.mean(echo ** 2))
        if echo_pow > 1e-10 and err_pow > 1e-12:
            self.erle_db = 0.97 * self.erle_db + 0.03 * 10 * np.log10(mic_pow / err_pow + 1e-12)

        if x_pow > 1e-7:
            self.power = 0.9 * self.power + 0.1 * np.sum(np.abs(self.X) ** 2, axis=0)
            norm = self.power + 1e-6 * B + 1e-3 * float(np.mean(self.power))
            # Cantando encima: el error es mucho mayor que el eco estimado → aprender muy poco.
            singing = err_pow > 4.0 * echo_pow and self.erle_db > 6
            mu = self.step * (0.05 if singing else 1.0)
            E = np.fft.rfft(np.concatenate([np.zeros(B), err]))
            G = mu * np.conj(self.X) * E[None, :] / norm[None, :]
            g = np.fft.irfft(G, axis=1)[:, :B]
            self.W += np.fft.rfft(np.concatenate([g, np.zeros_like(g)], axis=1), axis=1)
            if not np.isfinite(err_pow) or err_pow > 8 * mic_pow + 1e-6:   # diverge: empezar de nuevo
                self.W[:] = 0
                return mic.astype(np.float32)
        return err.astype(np.float32)


class VoiceGate:
    """Deja pasar el micrófono a la voz de IA solo cuando hablas o cantas.

    Con la consola que regresa la música, en el micrófono siempre queda algo de
    música que no se alcanza a quitar; la IA la convierte en «alguien cantando».
    La música que regresa la consola llega al micrófono con un nivel casi fijo
    respecto a la música de la PC (en la BOMGE, unos −10 dB en 1–4 kHz). Si el
    micrófono está bastante por encima de ese nivel, eres tú.
    """

    def __init__(self, rate: int = 48000, block: int = 512, threshold_db: float = 4.0, keep_db: float = 1.5,
                 hold_s: float = 0.5) -> None:
        freqs = np.fft.rfftfreq(block, 1 / rate)
        self.band = (freqs >= 1000) & (freqs <= 4000)
        self.window = np.hanning(block).astype(np.float32)
        self.block = block
        self.threshold = threshold_db    # para abrir: tantos dB arriba del nivel «solo música»
        self.keep = keep_db              # ya abierta (cantando): seguir abierta mientras pase esto
        self.hold_blocks = max(1, int(hold_s * rate / block))
        self.ref_hist: list[float] = []
        self.ratios: list[float] = []
        self.music_levels: list[float] = []  # nivel del micrófono cuando solo suena la música
        self.music_level = -200.0
        self.em = self.er = 1e-12
        self.hold = 0
        self.gain = 1.0
        self.open = True

    def reset(self) -> None:
        self.ref_hist.clear()
        self.ratios.clear()
        self.music_levels.clear()
        self.em = self.er = 1e-12
        self.hold = 0

    def _energy(self, x: np.ndarray) -> float:
        spec = np.fft.rfft(x[:self.block] * self.window)
        return float(np.mean(np.abs(spec[self.band]) ** 2)) + 1e-12

    def process(self, mic: np.ndarray, ref: np.ndarray, delay: int, out: np.ndarray) -> np.ndarray:
        self.ref_hist.append(self._energy(ref))
        back = min(len(self.ref_hist) - 1, max(0, int(round(delay / self.block))))
        er = self.ref_hist[-1 - back]
        del self.ref_hist[:-64]
        em = self._energy(mic)
        self.em += 0.15 * (em - self.em)
        self.er += 0.15 * (er - self.er)
        lm, lr = 10 * np.log10(self.em), 10 * np.log10(self.er)
        voice = False
        if lm > -30:                      # algo suena en el micrófono (energía por bin, ventana Hann)
            if lr < lm - 25:
                # La música de la PC está callada. Si el micrófono suena como cuando solo había música,
                # es la captura de la PC que se cortó (la consola sigue): no abrir. Si suena más fuerte, eres tú.
                voice = lm > self.music_level + 3 if self.music_levels else True
            else:
                ratio = lm - lr
                self.ratios.append(ratio)
                del self.ratios[:-400]    # ~4 s
                base = float(np.percentile(self.ratios, 20)) if len(self.ratios) >= 30 else ratio + self.threshold
                voice = ratio > base + (self.keep if self.hold else self.threshold)
                if not voice and ratio < base + self.keep:
                    self.music_levels.append(lm)
                    del self.music_levels[:-400]
                    self.music_level = float(np.median(self.music_levels))
        if voice:
            self.hold = self.hold_blocks  # abierta un rato después de la última sílaba (notas largas, respiraciones)
        elif self.hold:
            self.hold -= 1
        target = 1.0 if self.hold else 0.0
        self.open = bool(self.hold)
        ramp = np.linspace(self.gain, target, out.size, dtype=np.float32)
        self.gain = target
        return out * ramp


class SelfCancel:
    """Quita de la captura de la música lo que EC-Voice mismo sonó en esa salida.

    La captura de Windows («loopback») es la suma digital exacta de lo que suena
    en esa salida: lo nuestro aparece igual, solo con un retraso y un volumen
    fijos. Se miden cada segundo y se resta (sin filtro adaptable, que la música
    confundiría).
    """

    def __init__(self, rate: int = 48000, max_delay_ms: float = 300) -> None:
        self.rate = rate
        self.max_delay = int(max_delay_ms * rate / 1000)
        self.hist_len = self.max_delay + rate * 2
        self.played = np.zeros(self.hist_len, np.float32)  # historial de lo que sonamos
        self.captured = np.zeros(rate * 2, np.float32)
        self.delay = -1
        self.gain = 0.0
        self.since = 0

    def process(self, played: np.ndarray, captured: np.ndarray) -> np.ndarray:
        n = captured.size
        self.played = np.concatenate([self.played[n:], played.astype(np.float32)])
        self.captured = np.concatenate([self.captured[n:], captured.astype(np.float32)])
        self.since += n
        if self.since >= self.rate // 2:
            self.since = 0
            self._measure()
        if self.delay < 0 or self.gain == 0.0:
            return captured
        end = self.hist_len - self.delay
        echo = self.played[end - n:end]
        return captured - self.gain * echo

    def _measure(self) -> None:
        cap = self.captured
        ref = self.played[-(cap.size + self.max_delay):]
        if float(np.mean(ref[-cap.size:] ** 2)) < 1e-7:
            return  # no sonamos nada: nada que medir
        size = 1 << int(np.ceil(np.log2(ref.size + cap.size)))
        spectrum = np.fft.rfft(cap, size) * np.conj(np.fft.rfft(ref, size))
        corr = np.fft.irfft(spectrum / (np.abs(spectrum) + 1e-9), size)
        # cap[t] = g·ref[t + max_delay − d]  →  pico en el desfase k = d − max_delay (entre −max_delay y 0)
        window = np.concatenate([corr[size - self.max_delay:], corr[:1]])  # k = −max_delay … 0
        i = int(np.argmax(window))
        if window[i] < 0.05:
            return
        delay = i  # i = k + max_delay = d
        end = self.hist_len - delay
        echo = self.played[end - cap.size:end]
        power = float(np.dot(echo, echo))
        if power > 1e-6:
            estimate = float(np.clip(np.dot(cap, echo) / power, 0.0, 2.0))  # la música lo ensucia un poco: promediar
            if delay != self.delay:
                self.gains = []
            self.gains = (getattr(self, "gains", []) + [estimate])[-12:]
            self.delay, self.gain = delay, float(np.median(self.gains))


class LatencyMeter:
    """Mide de verdad cuánto tarda tu voz desde que entra hasta que sale (con IA o sin ella).

    Guarda la envolvente (volumen cada 10 ms, con su hora) de tu voz al entrar y
    de la voz al salir, y busca el desfase en que más se parecen. Así no hay que
    adivinar el retraso de la conversión.
    """

    def __init__(self, bin_ms: float = 10.0, window_s: float = 6.0, max_ms: float = 1200.0) -> None:
        self.bin = bin_ms / 1000
        self.size = int(window_s / self.bin)
        self.max_lag = int(max_ms / bin_ms)
        self.t0 = None
        self.inp: dict[int, float] = {}
        self.out: dict[int, float] = {}
        self.results: list[float] = []
        self.value_ms: float | None = None
        self.last = 0.0

    def _add(self, store: dict, t: float, block: np.ndarray, rate: int) -> None:
        if self.t0 is None:
            self.t0 = t
        # repartir el bloque en pedazos de 10 ms (el bloque termina en t)
        step = max(1, int(self.bin * rate))
        n = block.size
        for end in range(n, 0, -step):
            piece = block[max(0, end - step):end]
            k = int(round((t - (n - end) / rate - self.t0) / self.bin))
            store[k] = float(np.sqrt(np.mean(piece * piece)) + 1e-7)

    def add_in(self, t: float, block: np.ndarray, rate: int) -> None:
        self._add(self.inp, t, block, rate)

    def add_out(self, t: float, block: np.ndarray, rate: int) -> None:
        self._add(self.out, t, block, rate)

    def reset(self) -> None:
        self.__init__(self.bin * 1000, self.size * self.bin, self.max_lag * self.bin * 1000)

    @staticmethod
    def _series(store: dict, start: int, end: int):
        """Valores por cada 10 ms; los huecos (relojes de las tarjetas) se rellenan con el anterior."""
        values, last, missing = [], None, 0
        for k in range(start, end):
            v = store.get(k)
            if v is None:
                missing += 1
                v = last if last is not None else 1e-7
            values.append(v)
            last = v
        return None if missing > (end - start) // 4 else np.array(values)

    def measure(self, now: float) -> float | None:
        if self.t0 is None or now - self.last < 2.0:
            return self.value_ms
        self.last = now
        end = int((now - self.t0) / self.bin) - 2
        start = end - self.size
        for store in (self.inp, self.out):
            for k in [k for k in store if k < start - self.max_lag]:
                del store[k]
        if start - self.max_lag < 0:
            return self.value_ms
        a = self._series(self.inp, start - self.max_lag, end)
        b = self._series(self.out, start, end)
        if a is None or b is None:
            return self.value_ms
        la, lb = np.log(a), np.log(b)
        if np.std(lb) < 0.5 or np.std(la) < 0.5:  # casi silencio: no hay voz para medir
            return self.value_ms
        lb = (lb - lb.mean()) / lb.std()
        best, best_lag = -1.0, 0
        for lag in range(0, self.max_lag + 1):
            seg = la[self.max_lag - lag:self.max_lag - lag + self.size]
            seg = (seg - seg.mean()) / (seg.std() + 1e-9)
            c = float(np.mean(seg * lb))
            if c > best:
                best, best_lag = c, lag
        if best < 0.6:
            return self.value_ms
        self.results = (self.results + [best_lag * self.bin * 1000])[-7:]
        self.value_ms = float(np.median(self.results))
        return self.value_ms
