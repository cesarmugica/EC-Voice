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
import warnings
import socket
import struct
import threading
import time
from typing import Callable

# soundcard avisa en cada hueco de la captura de música; no hace falta llenar el registro.
warnings.filterwarnings("ignore", message="data discontinuity")

import numpy as np

from ecvoice_echo import EchoRemover, LatencyMeter, SelfCancel, VoiceGate

RATE = 48000
ECHO_BLOCK = 512
RVC_ALGO_MS = 55  # retraso propio de la conversión (fundido de 50 ms + búsqueda SOLA de 10 ms)
ECHO_MARGIN = 0  # el quitador mide el retraso real y alinea la referencia con su propio historial


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
        self.monitor_direct = False   # «Escucharme en»: tu micrófono directo (para cantar) en vez de la voz procesada
        self.monitor_on_music = False # el monitor suena en la misma salida donde se captura la música
        self.sync_ms = 0
        self.ai_wanted = False      # hay una voz de IA elegida
        self.chunk_ms = 250
        self.mute_voice = False     # silenciar tu voz (sale solo la música)
        self.mute_music = False     # silenciar la música (sale solo tu voz)
        self.music_muted_at_source = False
        self.tap: list | None = None     # «Probar ajustes»: copia de tu voz limpia mientras graba
        self.test_hold = False           # durante la prueba, el live no usa el motor (sale tu voz normal)  # las apps de música están calladas en Windows (no llega a la consola)
        self.console_music = False  # la consola ya regresa la música por el USB (BOMGE F4-MP3)
        self.echo = EchoRemover(block=ECHO_BLOCK)
        self.gate = VoiceGate(rate=RATE, block=ECHO_BLOCK)  # a la IA solo pasa tu voz, no la música que queda
        self.gate_enabled = True
        self.latency = LatencyMeter()   # cuánto tarda tu voz de verdad (entra → sale)
        self.latency_key = None
        self.pc_roundtrip_ms = 40.0     # cuánto después que la PC oyes tú la música + lo que tarda en entrar tu voz
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
        self.monitor_played = Ring(2, 1)  # lo que EC-Voice sonó en «Escucharme en» (para no capturarlo como música)
        self.music_ring = Ring(6, 2)
        self.voice_primed = False
        self.music_primed = False

    # ---------- retrasos ----------
    def echo_active(self) -> bool:
        """Quitar la música del micrófono: solo con voz de IA y si la consola la regresa."""
        # También si silencias la música: hay que quitarla del micrófono para que quede solo tu voz.
        return self.console_music and self.music_ok and (
            self.ai_wanted or (self.mute_music and not self.mute_voice and not self.music_muted_at_source))

    def add_music(self) -> bool:
        """Mezclar la música de la PC: si la consola no la trae, o si se quitó para la voz de IA."""
        # Con tu voz silenciada se usa la música de la PC (la de la consola viene revuelta con tu voz).
        return self.music_ok and (not self.console_music or self.ai_wanted or self.mute_voice)

    def voice_latency_ms(self) -> float:
        """Cuánto tarda tu voz en salir (sin contar el ajuste de sincronía)."""
        if self.latency.value_ms is not None:
            return self.latency.value_ms  # medido de verdad mientras hablas o cantas
        extra = (ECHO_MARGIN + ECHO_BLOCK) * 1000 / RATE if self.echo_active() else 0
        if self.ai_wanted:
            return self.chunk_ms + self.link.perf_ms + 60 + RVC_ALGO_MS + extra
        return 30.0 + extra

    def console_lag_ms(self) -> float:
        """Con la consola que regresa la música: cuánto después de la PC te llega la música a los audífonos.

        Tú cantas al ritmo de lo que oyes en la consola, que va atrasado respecto a la música de la PC
        (lo mide el quitador de música). La música que se agrega hay que atrasarla también eso.
        """
        if self.console_music and self.echo_active():
            return min(300.0, max(0.0, self.echo.delay * 1000 / RATE))
        if not self.add_music():
            return 0.0
        # Micrófono limpio (sin música que medir): tú igual cantas con la música que oyes, que te llega
        # después que a la PC, y tu voz tarda en entrar. Usar lo último medido con la consola (o ~40 ms).
        return self.pc_roundtrip_ms

    def _targets(self) -> tuple[int, int]:
        """(relleno de la voz, retraso de la música) en muestras."""
        jitter = 60 if self.ai_wanted else 20
        total = self.voice_latency_ms() + self.console_lag_ms() + self.sync_ms
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
            self.latency.add_in(time.monotonic(), block, RATE)
            self.levels["mic"] = _rms_db(block)
            if self.monitor_direct:
                self.monitor_ring.write(block)  # oírte al instante, sin pasar por la IA

        def on_out(outdata, frames, _time, status) -> None:
            outdata[:] = self._mix(frames, outdata.shape[1])
            diag = self.diag
            if diag is not None:
                diag["final"].append(outdata[:, 0].copy())  # lo que sale a la caja (lo que oye TikTok)

        mic_stream = sd.InputStream(device=mic, channels=mic_channels, samplerate=RATE, blocksize=FRAME,
                                    dtype="float32", latency="low", callback=on_mic, extra_settings=wasapi(mic))
        out_stream = sd.OutputStream(device=output, channels=out_channels, samplerate=RATE, blocksize=FRAME,
                                     dtype="float32", latency="low", callback=on_out, extra_settings=wasapi(output))
        self.streams = [mic_stream, out_stream]
        if monitor is not None:
            mon_channels = max(1, min(2, int(sd.query_devices(monitor)["max_output_channels"])))

            def on_monitor(outdata, frames, _time, status) -> None:
                if self.monitor_direct:
                    excess = self.monitor_ring.available() - 3 * frames  # relojes distintos: no dejar que crezca el retraso
                    if excess > 0:
                        self.monitor_ring.drop(excess)
                block = self.monitor_ring.read(frames)[:, 0] * self.monitor_gain
                outdata[:] = np.repeat(block[:, None], outdata.shape[1], axis=1)
                if self.monitor_on_music:
                    self.monitor_played.write(block)

            self.streams.append(sd.OutputStream(device=monitor, channels=mon_channels, samplerate=RATE,
                                                blocksize=FRAME, dtype="float32", latency="low",
                                                callback=on_monitor, extra_settings=wasapi(monitor)))
        self.running = True
        self.echo.reset()
        self.gate.reset()
        self.latency.reset()
        self.threads = [threading.Thread(target=self._clean_loop, daemon=True),
                        threading.Thread(target=self._voice_loop, daemon=True),
                        threading.Thread(target=self._latency_loop, daemon=True)]
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
            if self.ai_wanted and self.link.sock is not None and not self.test_hold:
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

    def _latency_loop(self) -> None:
        """Medir cada 2 s cuánto tarda tu voz (al cambiar de voz o de bloque, empezar de nuevo)."""
        while not self.stop_event.is_set():
            key = (self.ai_wanted, self.chunk_ms, self.echo_active())
            if key != self.latency_key:
                self.latency_key = key
                self.latency.reset()
            with contextlib.suppress(Exception):
                self.latency.measure(time.monotonic())
            time.sleep(0.5)

    def _clean_loop(self) -> None:
        """Micrófono → (quitar la música que regresa por la consola) → voz."""
        while not self.stop_event.is_set():
            if not self.echo_active():
                available = self.mic_ring.available()
                if available < FRAME:
                    time.sleep(0.002)
                    continue
                block = self.mic_ring.read(available)[:, 0]
                self._clean_out(block)
                if self.diag is not None:
                    # Diagnóstico sin quitar música (micrófono limpio, p. ej. AUX SEND): guardar la música a la par.
                    backlog = self.ref_ring.available() - (block.size + RATE // 20)
                    if backlog > 0:
                        self.ref_ring.drop(backlog)
                    self._diag_add(block, self.ref_ring.read(block.size)[:, 0], block, self.ref_ring.available())
                else:
                    self.ref_ring.clear()
                continue
            if self.mic_ring.available() < ECHO_BLOCK + ECHO_MARGIN:
                time.sleep(0.002)
                continue
            # Mantener la referencia «fresca»: si se acumuló (otra tarjeta, otro reloj), recortar.
            # (la captura llega en tandas de hasta 50 ms: dejar ese margen antes de recortar)
            backlog = self.ref_ring.available() - (ECHO_BLOCK * 3 + RATE // 20)
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
            out = np.asarray(out, dtype=np.float32)
            if self.ai_wanted and self.gate_enabled:
                out = self.gate.process(mic, ref, self.echo.delay, out)
            else:
                self.gate.open = True
            self._clean_out(out)
            if self.diag is not None:
                self._diag_add(mic, ref, out, ref_fill)

    def _diag_add(self, mic: np.ndarray, ref: np.ndarray, out: np.ndarray, fill: int) -> None:
        diag = self.diag
        if diag is None:
            return
        diag["mic"].append(mic.copy()); diag["ref"].append(ref.copy()); diag["out"].append(np.asarray(out).copy())
        diag["fill"].append(fill)
        diag["gate"].append(1.0 if self.gate.open else 0.0)
        diag["got"] = diag.get("got", 0) + mic.size
        if diag["got"] >= diag["samples"]:
            self.diag = None
            threading.Thread(target=self._save_diag, args=(diag,), daemon=True).start()

    def _clean_out(self, block: np.ndarray) -> None:
        self.clean_ring.write(block)
        tap = self.tap
        if tap is not None:
            tap.append(block.copy())

    def start_diag(self, path: str, seconds: float = 35) -> None:
        self.ref_drops = 0
        self.diag = {"path": path, "samples": int(seconds * RATE), "mic": [], "ref": [], "out": [], "final": [], "fill": [], "gate": [],
                     "drops0": self.ref_drops, "started": time.time()}

    def _save_diag(self, diag: dict) -> None:
        import wave

        mic, ref, out = (np.concatenate(diag[k]) for k in ("mic", "ref", "out"))
        final = np.concatenate(diag["final"]) if diag["final"] else np.zeros(0, np.float32)
        final = np.pad(final, (0, max(0, mic.size - final.size)))[:mic.size]
        # Canales: 1 micrófono · 2 música de la PC · 3 micrófono sin música · 4 lo que sale a TikTok
        data = np.stack([mic, ref, out, final], axis=1)
        pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
        with wave.open(diag["path"], "wb") as wav:
            wav.setnchannels(4)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(pcm.tobytes())
        fill = np.array(diag["fill"])
        self.log(f"Diagnóstico guardado: {diag['path']} · referencia: llenado min {fill.min()} / prom {fill.mean():.0f} / "
                 f"max {fill.max()} muestras · recortes {self.ref_drops} · quita {self.echo.erle_db:.1f} dB · "
                 f"duró {time.time() - diag['started']:.1f} s · voz {self.voice_latency_ms():.0f} ms · consola "
                 f"{self.console_lag_ms():.0f} ms · música atrasada {self._targets()[1] * 1000 / RATE:.0f} ms · "
                 f"puerta de voz abierta {100 * np.mean(diag['gate']) if diag['gate'] else 0:.0f} %")
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

            with contextlib.suppress(Exception):
                import soundcard.mediafoundation as mf

                # soundcard pone sus avisos en «siempre»: callarlos aquí, después de importarlo.
                warnings.filterwarnings("ignore", category=mf.SoundcardRuntimeWarning)
            source = sc.get_microphone(speaker_name, include_loopback=True)
            # Búfer de 50 ms: con uno de 10 ms la captura de la música se cortaba (huecos de 1–4 ms).
            self_echo = SelfCancel(rate=RATE)
            with source.recorder(samplerate=RATE, channels=2, blocksize=RATE // 20) as recorder:
                self.music_ok = True
                while not self.stop_event.is_set():
                    block = recorder.record(numframes=ECHO_BLOCK)
                    if block.ndim == 1:
                        block = np.repeat(block[:, None], 2, axis=1)
                    block = block[:, :2].astype(np.float32)
                    if self.monitor_on_music and block.shape[0] == ECHO_BLOCK:
                        # Tus audífonos están en la misma salida que la música: la captura también trae lo que
                        # EC-Voice sonó ahí (tu micrófono). Quitarlo, o se mandaría al live como eco de tu voz.
                        played = self.monitor_played.read(ECHO_BLOCK)[:, 0]
                        mono = block.mean(axis=1)
                        with contextlib.suppress(Exception):
                            cleaned = self_echo.process(played, mono)
                            block = block - (mono - np.asarray(cleaned, dtype=np.float32))[:, None]
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
                self.latency.add_out(time.monotonic(), voice, RATE)
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
            drift = available - (music_delay + frames)
            if drift < -(RATE // 25):
                # Falta atraso (cambió la voz, el ajuste o lo que tarda la consola): esperar a juntarlo.
                # Antes solo se recortaba cuando sobraba, y la música se quedaba con el atraso del arranque.
                self.music_primed = False
            elif available >= frames:
                if drift > RATE // 10:  # reloj de la tarjeta adelantado o cambió el ajuste: alcanzar
                    self.music_ring.drop(drift)
                music = self.music_ring.read(frames)
            else:
                self.music_primed = False
        if not self.add_music():
            music[:] = 0  # con «Mi voz» y consola que regresa la música, la mezcla ya viene en el micrófono
        if self.mute_music:
            music[:] = 0
        if self.mute_voice:
            voice[:] = 0
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
                "voice_latency_ms": round(self.voice_latency_ms()), "latency_measured": self.latency.value_ms is not None, "console_lag_ms": round(self.console_lag_ms()), "music_delay_ms": round(music_delay * 1000 / RATE),
                "engine_ms": round(self.link.perf_ms), "music_ok": self.music_ok,
                "echo": self.echo_active(), "music_muted_at_source": self.music_muted_at_source, "adds_music": self.add_music(), "echo_db": round(float(self.echo.erle_db), 1), "gate_open": bool(self.gate.open),
                "diag": self.diag is not None, "diag_done": getattr(self, "diag_done", "")}


def list_devices(rescan: bool = False) -> dict:
    """Micrófonos, salidas y fuentes de música (salidas de la PC)."""
    import sounddevice as sd

    if rescan:
        # PortAudio guarda la lista de aparatos al arrancar: sin esto, un cable recién conectado
        # o una entrada recién habilitada en Windows no aparecía hasta reiniciar EC-Voice.
        with contextlib.suppress(Exception):
            sd._terminate()
            sd._initialize()

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
