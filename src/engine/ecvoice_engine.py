"""Motor de voz de EC-Voice: convierte bloques de audio con Applio (RVC).

Corre con el Python del motor (%LOCALAPPDATA%\\EC-Voice\\engine\\venv) dentro
de la carpeta de Applio. No abre micrófono ni bocinas: el mezclador de
EC-Voice le manda cada bloque por un socket local y recibe el convertido.

Control por stdin/stdout, una línea JSON por mensaje:
  entrada:  {"cmd": "info" | "load" | "update" | "unload" | "quit", ...}
  salida:   @@EV {"type": "...", ...}

Audio por TCP (127.0.0.1, puerto que avisa con @@EV {"type": "port"}):
  pedido:    b"B" + uint32 bytes + float32[] (mono, 48 kHz, un bloque)
  respuesta: b"R" + uint32 bytes + float32 ms + uint8 listo + float32[]
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time
import traceback

APPLIO = os.environ.get("ECVOICE_APPLIO") or os.getcwd()
os.chdir(APPLIO)
if APPLIO not in sys.path:
    sys.path.insert(0, APPLIO)

RATE = 48000
_print_lock = threading.Lock()


def emit(kind: str, **data) -> None:
    with _print_lock:
        sys.stdout.write("@@EV " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class Converter:
    """Una voz cargada en la tarjeta (VoiceChangerWorker de Applio)."""

    def __init__(self) -> None:
        self.worker = None
        self.block_frame = 0
        self.model = ""
        self.params = {"f0_up_key": 0, "index_rate": 0.0, "protect": 0.33, "volume_envelope": 1.0,
                       "f0_autotune": False, "f0_autotune_strength": 1.0, "proposed_pitch": False,
                       "proposed_pitch_threshold": 155.0}
        self.lock = threading.Lock()
        self.last = None
        self.state = "empty"  # empty, loading, warmup, ready, error
        self.error = ""
        threading.Thread(target=self._status_loop, daemon=True).start()

    def load(self, s: dict) -> None:
        from rvc.realtime.worker import VoiceChangerWorker

        block_frame = int(float(s.get("chunk_ms", 250)) * RATE / 1000)
        index = s.get("index") or ""
        self.params.update(f0_up_key=int(s.get("pitch", 0)), protect=float(s.get("protect", 0.33)),
                           index_rate=float(s.get("index_rate", 0.5)) if index else 0.0)
        with self.lock:
            same_shape = self.worker is not None and self.block_frame == block_frame and self.worker._process is not None \
                and self.worker._process.is_alive()
            if same_shape:
                # Cambio de voz «en caliente»: no recarga todo el motor.
                self.state = "loading"
                self.worker.send_config({"model_path": s["model"], "index_path": index,
                                         "index_rate": self.params["index_rate"]})
                self.model = s["model"]
                self.last = None
                emit("status", state="loading", model=self.model)
                return
            self._stop_worker()
            self.state, self.error, self.model, self.block_frame, self.last = "loading", "", s["model"], block_frame, None
            emit("status", state="loading", model=self.model)
            self.worker = VoiceChangerWorker(dict(
                block_frame=block_frame,
                cross_fade_overlap_size=float(s.get("crossfade", 0.05)),
                extra_convert_size=float(s.get("extra", 2.5)),
                model_path=s["model"],
                index_path=index,
                f0_method="rmvpe",
                embedder_model="contentvec",
                embedder_model_custom=None,
                silent_threshold=int(s.get("silence_db", -60)),
                vad_enabled=True,
                vad_sensitivity=3,
                vad_frame_ms=30,
                sid=0,
                formant_shifting=False,
                audio_sample_rate=RATE,
                index_rate=self.params["index_rate"],
            ))
            self.worker.start()

    def update(self, s: dict) -> None:
        if "pitch" in s:
            self.params["f0_up_key"] = int(s["pitch"])
        if "protect" in s:
            self.params["protect"] = float(s["protect"])
        if "index_rate" in s:
            self.params["index_rate"] = float(s["index_rate"])
            if self.worker is not None:
                self.worker.send_config({"index_rate": self.params["index_rate"]})

    def unload(self) -> None:
        with self.lock:
            self._stop_worker()
            self.state, self.model, self.block_frame = "empty", "", 0
        emit("status", state="empty")

    def _stop_worker(self) -> None:
        if self.worker is not None:
            try:
                self.worker.stop()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.worker = None

    def ready(self) -> bool:
        return self.state in ("ready", "warmup")

    def convert(self, block):
        """Bloque → (audio convertido o None, ms)."""
        import numpy as np

        worker = self.worker
        if worker is None or not worker.ready or len(block) != self.block_frame:
            return None, 0.0
        worker.submit(block, dict(self.params))
        deadline = time.perf_counter() + self.block_frame / RATE * 1.4
        while time.perf_counter() < deadline:
            result = worker.retrieve()
            if result is not None:
                audio, _vol, perf_ms, warmup = result
                audio = np.asarray(audio, dtype=np.float32)
                if self.state == "loading" or (self.state == "warmup" and warmup == 0):
                    self.state = "warmup" if warmup > 0 else "ready"
                self.last = audio
                return audio, float(perf_ms)
            time.sleep(0.002)
        return None, 0.0

    def _status_loop(self) -> None:
        reported = ""
        while True:
            time.sleep(0.5)
            worker = self.worker
            if worker is None:
                continue
            process = getattr(worker, "_process", None)
            if process is not None and not process.is_alive():
                self.state, self.error = "error", "La voz no pudo cargarse (revisa el registro)."
                self.worker = None
            elif worker.ready and self.state == "loading" and self.last is None:
                self.state = "warmup"
            if self.state != reported:
                reported = self.state
                emit("status", state=self.state, error=self.error, model=self.model)


CONVERTER = Converter()


def serve_audio(server: socket.socket) -> None:
    import numpy as np

    while True:
        conn, _ = server.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=_client, args=(conn, np), daemon=True).start()


def _client(conn: socket.socket, np) -> None:
    def recv(n: int) -> bytes:
        parts, got = [], 0
        while got < n:
            chunk = conn.recv(n - got)
            if not chunk:
                raise ConnectionError
            parts.append(chunk)
            got += len(chunk)
        return b"".join(parts)

    try:
        while True:
            kind, length = struct.unpack("<cI", recv(5))
            block = np.frombuffer(recv(length), dtype=np.float32)
            audio, perf = (None, 0.0)
            if kind == b"B":
                try:
                    audio, perf = CONVERTER.convert(block)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            payload = b"" if audio is None else audio.astype(np.float32).tobytes()
            conn.sendall(struct.pack("<cIfB", b"R", len(payload), perf, 1 if CONVERTER.ready() else 0) + payload)
    except (ConnectionError, OSError, struct.error):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def info() -> dict:
    data = {"python": sys.version.split()[0]}
    try:
        import torch

        data["torch"] = torch.__version__
        data["cuda"] = bool(torch.cuda.is_available())
        if data["cuda"]:
            props = torch.cuda.get_device_properties(0)
            data["gpu"] = props.name
            data["vram_gb"] = round(props.total_memory / 1024**3, 1)
    except Exception as exc:  # noqa: BLE001
        data["error"] = str(exc)
    return data


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    threading.Thread(target=serve_audio, args=(server,), daemon=True).start()
    emit("port", port=server.getsockname()[1])
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
            cmd = message.get("cmd")
            if cmd == "info":
                emit("info", **info())
            elif cmd == "load":
                try:
                    CONVERTER.load(message.get("settings") or {})
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    CONVERTER.state, CONVERTER.error = "error", str(exc)
                    emit("status", state="error", error=str(exc))
            elif cmd == "update":
                CONVERTER.update(message.get("settings") or {})
            elif cmd == "unload":
                CONVERTER.unload()
            elif cmd == "quit":
                CONVERTER.unload()
                break
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            emit("error", error=str(exc))
    emit("bye")


if __name__ == "__main__":
    main()
