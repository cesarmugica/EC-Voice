"""Mezclador de EC-Voice: junta tu micrófono y la música de la PC, los
sincroniza y los manda por el cable virtual hacia TikTok LIVE Studio.

Funciona solo (tu voz natural) o con una voz de IA: en ese caso cada bloque
del micrófono va al motor de voz (otro proceso, por un socket local) y regresa
convertido. Mientras la voz de IA carga o si el motor no responde a tiempo,
sale tu voz natural, así nunca te quedas mudo en el live.

Todo a 48 kHz. Voz en mono, música en estéreo, salida en estéreo.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
import time
from typing import Callable

import numpy as np

from ecvoice_echo import EchoRemover

RATE = 48000
ECHO_BLOCK = 512
ECHO_MARGIN = int(0.030 * RATE)  # el micrófono espera 30 ms para que la música de referencia llegue antes


def _com_init() -> None:
    """soundcard usa COM de Windows: cada hilo que lo use debe inicializarlo (error 0x800401f0)."""
    import os

    if os.name == "nt":
        import ctypes

        with contextlib.suppress(Exception):
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # type: ignore[attr-defined]
FRAME = 480  # 10 ms por bloque de salida


class Ring:
    """Búfer circular de audio (muestras × canales), seguro entre hilos."""

    def __init__(self, seconds: float, channels: int) -> None:
        self.size = int(RATE * seconds)
        self.data = np.zeros((self.size, channels), dtype=np.float32)
        self.channels = channels
        self.start = 0
        self.count = 0
        self.lock = threading.Lock()

    def available(self) -> int:
        return self.count

    def clear(self) -> None:
        with self.lock:
            self.start = self.count = 0

    def write(self, block: np.ndarray) -> None:
        if block.ndim == 1:
            block = block[:, None]
        if block.shape[1] != self.channels:
            block = np.repeat(block.mean(axis=1, keepdims=True), self.channels, axis=1) if self.channels > 1 else block.mean(axis=1, keepdims=True)
        n = len(block)
        if n == 0:
            return
        with self.lock:
            if n >= self.size:
                block, n = block[-self.size:], self.size
            overflow = self.count + n - self.size
            if overflow > 0:  # tirar lo más viejo
                self.start = (self.start + overflow) % self.size
                self.count -= overflow
            end = (self.start + self.count) % self.size
            first = min(n, self.size - end)
            self.data[end:end + first] = block[:first]
            if n > first:
                self.data[:n - first] = block[first:]
            self.count += n

    def read(self, n: int) -> np.ndarray:
        """Lee n muestras; si faltan, rellena con silencio al final."""
        out = np.zeros((n, self.channels), dtype=np.float32)
        with self.lock:
            take = min(n, self.count)
            first = min(take, self.size - self.start)
            out[:first] = self.data[self.start:self.start + first]
            if take > first:
                out[first:take] = self.data[:take - first]
            self.start = (self.start + take) % self.size
            self.count -= take
        return out

    def drop(self, n: int) -> None:
        with self.lock:
            n = min(n, self.count)
            self.start = (self.start + n) % self.size
            self.count -= n


def _rms_db(block: np.ndarray) -> float:
    if block.size == 0:
        return -90.0
    value = float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))
    return max(-90.0, 20 * np.log10(value + 1e-9))


class EngineLink:
    """Conexión con el motor de voz: manda un bloque y recibe el convertido."""

    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.port = 0
        self.lock = threading.Lock()
        self.perf_ms = 0.0

    def connect(self, port: int) -> None:
        self.close()
        self.port = port
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = sock

    def close(self) -> None:
        with contextlib.suppress(OSError):
            if self.sock:
                self.sock.close()
        self.sock = None

    def _recv(self, n: int) -> bytes:
        assert self.sock is not None
        chunks, got = [], 0
        while got < n:
            chunk = self.sock.recv(n - got)
            if not chunk:
                raise ConnectionError("el motor cerró la conexión")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def convert(self, block: np.ndarray, timeout: float) -> tuple[np.ndarray | None, bool]:
        """Devuelve (audio, listo). audio=None si el motor no contestó a tiempo."""
        with self.lock:
            if self.sock is None:
                return None, False
            payload = block.astype(np.float32).tobytes()
            try:
                self.sock.settimeout(timeout)
                self.sock.sendall(struct.pack("<cI", b"B", len(payload)) + payload)
                kind, length, perf, ready = struct.unpack("<cIfB", self._recv(10))
                data = self._recv(length)
            except (OSError, ConnectionError, struct.error):
                self.close()  # se reconecta cuando el motor vuelva a avisar su puerto
                return None, False
            self.perf_ms = 0.8 * self.perf_ms + 0.2 * float(perf)
            if kind != b"R" or length == 0:
                return None, bool(ready)
            return np.frombuffer(data, dtype=np.float32).copy(), bool(ready)


class Mixer:
    def __init__(self, log: Callable[[str], None] = print) -> None:
        self.log = log
        self.running = False
        self.error = ""
        self.lock = threading.Lock()
        self.streams: list = []
        self.threads: list[threading.Thread] = []
        self.stop_event = threading.Event()
        self.link = EngineLink()
        # Ajustes (los cambia el servidor en caliente)
        self.voice_gain = 1.0
        self.music_gain = 0.6
        self.input_gain = 1.0
        self.monitor_gain = 1.0
        self.sync_ms = 0
        self.ai_wanted = False      # hay una voz de IA elegida
        self.chunk_ms = 250
        self.echo_cancel = True     # quitar la música que regresa por la consola
        self.echo = EchoRemover(block=ECHO_BLOCK)
        self.ref_drops = 0          # cuántas veces se recortó la referencia (diagnóstico)
        self.diag: dict | None = None  # grabación de diagnóstico en curso
        # Estado para la interfaz
        self.ai_active = False      # la voz de IA está saliendo de verdad
        self.levels = {"mic": -90.0, "voice": -90.0, "music": -90.0}
        self.music_ok = False
        self._reset_buffers()

    def _reset_buffers(self) -> None:
        self.mic_ring = Ring(4, 1)
        self.clean_ring = Ring(4, 1)    # micrófono ya sin la música que regresa
        self.ref_ring = Ring(2, 1)      # la música exacta que se mandó a la consola
        self.voice_ring = Ring(4, 1)
        self.monitor_ring = Ring(2, 1)
        self.music_ring = Ring(6, 2)
        self.voice_primed = False
        self.music_primed = False

    # ---------- retrasos ----------
    def echo_active(self) -> bool:
        return self.echo_cancel and self.music_ok

    def voice_latency_ms(self) -> float:
        """Cuánto tarda tu voz en salir (sin contar el ajuste de sincronía)."""
        extra = (ECHO_MARGIN + ECHO_BLOCK) * 1000 / RATE if self.echo_active() else 0
        if self.ai_wanted:
            return self.chunk_ms + self.link.perf_ms + 60 + extra
        return 30.0 + extra

    def _targets(self) -> tuple[int, int]:
        """(relleno de la voz, retraso de la música) en muestras."""
        jitter = 60 if self.ai_wanted else 20
        total = self.voice_latency_ms() + self.sync_ms
        music = max(0.0, total)
        extra_voice = max(0.0, -total)  # sincronía negativa: retrasar la voz
        return int((jitter + extra_voice) * RATE / 1000), int(music * RATE / 1000)

    # ---------- arranque ----------
    def start(self, mic: int, output: int, monitor: int | None, music: str | None) -> None:
        import sounddevice as sd

        self.stop()
        self.error = ""
        self._reset_buffers()
        self.stop_event.clear()

        def wasapi(device: int):
            try:
                api = sd.query_hostapis(sd.query_devices(device)["hostapi"])["name"]
            except Exception:  # noqa: BLE001
                return None
            return sd.WasapiSettings(auto_convert=True) if "WASAPI" in api else None

        mic_channels = max(1, min(2, int(sd.query_devices(mic)["max_input_channels"])))
        out_channels = max(1, min(2, int(sd.query_devices(output)["max_output_channels"])))

        def on_mic(indata, frames, _time, status) -> None:
            block = indata.mean(axis=1) * self.input_gain
            self.mic_ring.write(block)
            self.levels["mic"] = _rms_db(block)

        def on_out(outdata, frames, _time, status) -> None:
            outdata[:] = self._mix(frames, outdata.shape[1])

        mic_stream = sd.InputStream(device=mic, channels=mic_channels, samplerate=RATE, blocksize=FRAME,
                                    dtype="float32", latency="low", callback=on_mic, extra_settings=wasapi(mic))
        out_stream = sd.OutputStream(device=output, channels=out_channels, samplerate=RATE, blocksize=FRAME,
                                     dtype="float32", latency="low", callback=on_out, extra_settings=wasapi(output))
        self.streams = [mic_stream, out_stream]
        if monitor is not None:
            mon_channels = max(1, min(2, int(sd.query_devices(monitor)["max_output_channels"])))

            def on_monitor(outdata, frames, _time, status) -> None:
                block = self.monitor_ring.read(frames)[:, 0] * self.monitor_gain
                outdata[:] = np.repeat(block[:, None], outdata.shape[1], axis=1)

            self.streams.append(sd.OutputStream(device=monitor, channels=mon_channels, samplerate=RATE,
                                                blocksize=FRAME, dtype="float32", latency="low",
                                                callback=on_monitor, extra_settings=wasapi(monitor)))
        self.running = True
        self.echo.reset()
        self.threads = [threading.Thread(target=self._clean_loop, daemon=True),
                        threading.Thread(target=self._voice_loop, daemon=True)]
        if music:
            self.threads.append(threading.Thread(target=self._music_loop, args=(music,), daemon=True))
        for thread in self.threads:
            thread.start()
        for stream in self.streams:
            stream.start()
        self.log(f"Mezclador encendido (mic {mic}, salida {output}, monitor {monitor}, música {music or 'no'}).")

    def stop(self) -> None:
        self.stop_event.set()
        for stream in self.streams:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
        self.streams = []
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads = []
        if self.running:
            self.log("Mezclador apagado.")
        self.running = False
        self.ai_active = False
        self.levels = {"mic": -90.0, "voice": -90.0, "music": -90.0}

    # ---------- voz ----------
    def _voice_loop(self) -> None:
        """Pasa el micrófono a la voz: directo, o por bloques al motor de IA."""
        while not self.stop_event.is_set():
            if self.ai_wanted and self.link.sock is not None:
                block_len = int(self.chunk_ms * RATE / 1000)
                if self.clean_ring.available() < block_len:
                    time.sleep(0.003)
                    continue
                block = self.clean_ring.read(block_len)[:, 0]
                converted, ready = self.link.convert(block, timeout=self.chunk_ms / 1000 * 1.6)
                if converted is not None and ready and len(converted) == block_len:
                    self.ai_active = True
                    self._emit_voice(converted)
                else:
                    self.ai_active = False
                    self._emit_voice(block)  # tu voz normal mientras la IA carga o si se atrasa
            else:
                self.ai_active = False
                available = self.clean_ring.available()
                if available < FRAME:
                    time.sleep(0.003)
                    continue
                self._emit_voice(self.clean_ring.read(available)[:, 0])

    def _clean_loop(self) -> None:
        """Micrófono → (quitar la música que regresa por la consola) → voz."""
        while not self.stop_event.is_set():
            if not self.echo_active():
                self.ref_ring.clear()
                available = self.mic_ring.available()
                if available < FRAME:
                    time.sleep(0.002)
                    continue
                self.clean_ring.write(self.mic_ring.read(available)[:, 0])
                continue
            if self.mic_ring.available() < ECHO_BLOCK + ECHO_MARGIN:
                time.sleep(0.002)
                continue
            # Mantener la referencia «fresca»: si se acumuló (otra tarjeta, otro reloj), recortar.
            backlog = self.ref_ring.available() - ECHO_BLOCK * 3
            if backlog > 0:
                self.ref_ring.drop(backlog)
                self.ref_drops += 1
            mic = self.mic_ring.read(ECHO_BLOCK)[:, 0]
            ref_fill = self.ref_ring.available()
            ref = self.ref_ring.read(ECHO_BLOCK)[:, 0]
            try:
                out = self.echo.process(ref, mic)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Quitar música del micrófono falló: {exc}")
                self.echo.reset()
                out = mic
            self.clean_ring.write(out)
            diag = self.diag
            if diag is not None:
                diag["mic"].append(mic.copy()); diag["ref"].append(ref.copy()); diag["out"].append(np.asarray(out).copy())
                diag["fill"].append(ref_fill)
                if len(diag["mic"]) * ECHO_BLOCK >= diag["samples"]:
                    self.diag = None
                    threading.Thread(target=self._save_diag, args=(diag,), daemon=True).start()

    def start_diag(self, path: str, seconds: float = 20) -> None:
        self.ref_drops = 0
        self.diag = {"path": path, "samples": int(seconds * RATE), "mic": [], "ref": [], "out": [], "fill": [],
                     "drops0": self.ref_drops, "started": time.time()}

    def _save_diag(self, diag: dict) -> None:
        import wave

        mic, ref, out = (np.concatenate(diag[k]) for k in ("mic", "ref", "out"))
        data = np.stack([mic, ref, out], axis=1)
        pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
        with wave.open(diag["path"], "wb") as wav:
            wav.setnchannels(3)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(pcm.tobytes())
        fill = np.array(diag["fill"])
        self.log(f"Diagnóstico guardado: {diag['path']} · referencia: llenado min {fill.min()} / prom {fill.mean():.0f} / "
                 f"max {fill.max()} muestras · recortes {self.ref_drops} · quita {self.echo.erle_db:.1f} dB · "
                 f"duró {time.time() - diag['started']:.1f} s")
        self.diag_done = diag["path"]

    def _emit_voice(self, block: np.ndarray) -> None:
        block = np.clip(block * self.voice_gain, -1.0, 1.0).astype(np.float32)
        self.voice_ring.write(block)
        self.monitor_ring.write(block)
        self.levels["voice"] = _rms_db(block)

    # ---------- música (lo que suena en la PC) ----------
    def _music_loop(self, speaker_name: str) -> None:
        try:
            _com_init()
            import soundcard as sc

            source = sc.get_microphone(speaker_name, include_loopback=True)
            with source.recorder(samplerate=RATE, channels=2, blocksize=FRAME) as recorder:
                self.music_ok = True
                while not self.stop_event.is_set():
                    block = recorder.record(numframes=FRAME)
                    if block.ndim == 1:
                        block = np.repeat(block[:, None], 2, axis=1)
                    block = block[:, :2].astype(np.float32)
                    self.music_ring.write(block)
                    self.ref_ring.write(block.mean(axis=1))
                    self.levels["music"] = _rms_db(block)
        except Exception as exc:  # noqa: BLE001
            self.music_ok = False
            self.error = f"No se pudo capturar la música de la PC: {exc}"
            self.log(self.error)
        finally:
            self.music_ok = False

    # ---------- mezcla (callback de salida) ----------
    def _mix(self, frames: int, channels: int) -> np.ndarray:
        voice_target, music_delay = self._targets()
        # Voz: esperar un pequeño colchón para no cortarse entre bloques.
        voice = np.zeros(frames, dtype=np.float32)
        if not self.voice_primed and self.voice_ring.available() >= voice_target + frames:
            self.voice_primed = True
        if self.voice_primed:
            if self.voice_ring.available() >= frames:
                voice = self.voice_ring.read(frames)[:, 0]
                block = int(self.chunk_ms * RATE / 1000) if self.ai_wanted else 0
                excess = self.voice_ring.available() - (voice_target + block + RATE // 10)
                if excess > 0:  # se acumuló retraso: recortarlo
                    self.voice_ring.drop(excess)
            else:
                self.voice_primed = False  # se vació: volver a llenar el colchón
        # Música: mantenerla atrasada lo que tarda tu voz (más el ajuste de sincronía).
        music = np.zeros((frames, 2), dtype=np.float32)
        available = self.music_ring.available()
        if not self.music_primed and available >= music_delay + frames:
            self.music_primed = True
        if self.music_primed:
            if available >= frames:
                drift = available - (music_delay + frames)
                if drift > RATE // 10:  # reloj de la tarjeta adelantado o cambió el ajuste: alcanzar
                    self.music_ring.drop(drift)
                music = self.music_ring.read(frames)
            else:
                self.music_primed = False
        mixed = music * self.music_gain + voice[:, None]
        out = np.clip(mixed, -1.0, 1.0).astype(np.float32)
        if channels == 1:
            return out.mean(axis=1, keepdims=True)
        if channels > 2:
            return np.pad(out, ((0, 0), (0, channels - 2)))
        return out

    def public(self) -> dict:
        _, music_delay = self._targets()
        return {"running": self.running, "error": self.error, "ai_active": self.ai_active,
                "levels": {k: round(v, 1) for k, v in self.levels.items()},
                "voice_latency_ms": round(self.voice_latency_ms()), "music_delay_ms": round(music_delay * 1000 / RATE),
                "engine_ms": round(self.link.perf_ms), "music_ok": self.music_ok,
                "echo": self.echo_active(), "echo_db": round(float(self.echo.erle_db), 1),
                "diag": self.diag is not None, "diag_done": getattr(self, "diag_done", "")}


def list_devices() -> dict:
    """Micrófonos, salidas y fuentes de música (salidas de la PC)."""
    import sounddevice as sd

    apis = sd.query_hostapis()
    inputs, outputs = [], []
    for index, device in enumerate(sd.query_devices()):
        api = apis[device["hostapi"]]["name"]
        item = {"index": index, "name": device["name"], "api": api}
        if device["max_input_channels"] > 0:
            inputs.append(item)
        if device["max_output_channels"] > 0:
            outputs.append(item)
    music = []
    with contextlib.suppress(Exception):
        _com_init()
        import soundcard as sc

        default = sc.default_speaker().name
        music = [{"name": s.name, "default": s.name == default} for s in sc.all_speakers()]
    return {"inputs": inputs, "outputs": outputs, "music": music}
