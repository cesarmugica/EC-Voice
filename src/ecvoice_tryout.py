"""«Probar ajustes»: grabar una frase una vez y oírla con la voz de IA y los
ajustes del momento, cuantas veces quieras, sin volver a grabar.

La frase se convierte con el mismo motor del live (bloque por bloque, más
rápido que en tiempo real) y se oye por la salida de la música (tus audífonos
en la consola).
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable

import numpy as np

from ecvoice_audio import RATE, EngineLink, _com_init

MAX_SECONDS = 15


class TryOut:
    def __init__(self, mixer, log: Callable[[str], None]) -> None:
        self.mixer, self.log = mixer, log
        self.state = "empty"   # empty, recording, ready, converting, playing, error
        self.error = ""
        self.clip: np.ndarray | None = None
        self.seconds = 0.0
        self.lock = threading.Lock()
        self.stop_flag = False

    def public(self) -> dict:
        return {"state": self.state, "error": self.error, "seconds": round(self.seconds, 1)}

    # ---------- grabar ----------
    def record(self, seconds: float, mic_index: int | None) -> None:
        with self.lock:
            if self.state in ("recording", "converting", "playing"):
                raise RuntimeError("Espera a que termine la prueba anterior.")
            self.state, self.error, self.stop_flag = "recording", "", False
        seconds = max(2.0, min(MAX_SECONDS, seconds))
        threading.Thread(target=self._record, args=(seconds, mic_index), daemon=True).start()

    def _record(self, seconds: float, mic_index: int | None) -> None:
        try:
            if self.mixer.running:
                # Tomar tu voz ya limpia del mezclador (sin la música que regresa la consola, si se está quitando).
                self.mixer.tap = []
                end = time.time() + seconds
                while time.time() < end and not self.stop_flag:
                    time.sleep(0.05)
                parts, self.mixer.tap = self.mixer.tap, None
                clip = np.concatenate(parts) if parts else np.zeros(0, np.float32)
            else:
                import sounddevice as sd

                if mic_index is None:
                    raise RuntimeError("Elige tu micrófono en «Señales».")
                data = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="float32", device=mic_index)
                sd.wait()
                clip = data[:, 0] * self.mixer.input_gain
            if clip.size < RATE:
                raise RuntimeError("No se grabó audio. ¿Está encendido el micrófono?")
            if float(np.abs(clip).max()) < 0.01:
                raise RuntimeError("Se grabó casi puro silencio. Acércate al micrófono o súbele.")
            self.clip = clip.astype(np.float32)
            self.seconds = clip.size / RATE
            self.state = "ready"
            self.log(f"Probar ajustes: frase grabada ({self.seconds:.1f} s)")
        except Exception as exc:  # noqa: BLE001
            self.mixer.tap = None
            self.state, self.error = ("ready" if self.clip is not None else "error"), str(exc)

    # ---------- escuchar ----------
    def play(self, original: bool, engine_port: int, chunk_ms: int, speaker: str) -> None:
        with self.lock:
            if self.clip is None:
                raise RuntimeError("Primero graba una frase.")
            if self.state in ("recording", "converting", "playing"):
                raise RuntimeError("Espera a que termine la prueba anterior.")
            if not original and not engine_port:
                raise RuntimeError("Elige una voz de IA y espera a que cargue.")
            if not speaker:
                raise RuntimeError("Elige en «Señales» dónde suena la música (ahí la vas a oír).")
            self.state, self.error, self.stop_flag = ("playing" if original else "converting"), "", False
        threading.Thread(target=self._play, args=(original, engine_port, chunk_ms, speaker), daemon=True).start()

    def stop(self) -> None:
        self.stop_flag = True

    def _convert(self, port: int, chunk_ms: int) -> np.ndarray:
        assert self.clip is not None
        block = int(chunk_ms * RATE / 1000)
        link = EngineLink()
        link.connect(port)
        self.mixer.test_hold = True  # que el live no use el motor al mismo tiempo (cambiaría su memoria)
        try:
            time.sleep(0.15)
            # Medio segundo de silencio antes (el motor arranca suave) y uno después (para que salga todo).
            padded = np.concatenate([np.zeros(RATE // 2, np.float32), self.clip, np.zeros(RATE, np.float32)])
            padded = np.pad(padded, (0, (-padded.size) % block))
            out = []
            for start in range(0, padded.size, block):
                if self.stop_flag:
                    break
                audio, ready = link.convert(padded[start:start + block], timeout=max(2.0, chunk_ms / 1000 * 8))
                if audio is None or not ready or audio.size != block:
                    if not out:
                        raise RuntimeError("La voz todavía no está lista. Espera a que diga «voz de IA» arriba.")
                    audio = np.zeros(block, np.float32)
                out.append(audio)
            return np.concatenate(out) if out else np.zeros(0, np.float32)
        finally:
            link.close()
            self.mixer.test_hold = False

    def _play(self, original: bool, port: int, chunk_ms: int, speaker: str) -> None:
        try:
            audio = self.clip if original else self._convert(port, chunk_ms)
            if self.stop_flag or audio is None or audio.size == 0:
                self.state = "ready"
                return
            if not original:
                audio = audio * self.mixer.voice_gain
            self.state = "playing"
            _com_init()
            import soundcard as sc

            out = sc.get_speaker(speaker)
            stereo = np.repeat(np.clip(audio, -1, 1)[:, None], 2, axis=1).astype(np.float32)
            with out.player(samplerate=RATE, channels=2, blocksize=2048) as player:
                step = RATE // 10
                for start in range(0, stereo.shape[0], step):
                    if self.stop_flag:
                        break
                    player.play(stereo[start:start + step])
            self.state = "ready"
        except Exception as exc:  # noqa: BLE001
            self.state, self.error = "ready", str(exc)
            self.log(f"Probar ajustes: {exc}")
        finally:
            with contextlib.suppress(Exception):
                self.mixer.test_hold = False
