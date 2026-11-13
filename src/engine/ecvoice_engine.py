"""Motor de voz en vivo de EC-Voice.

Corre con el Python del motor (%LOCALAPPDATA%\\EC-Voice\\engine\\venv) dentro
de la carpeta de Applio, y usa su conversión de voz en tiempo real (RVC).
El servidor de EC-Voice lo controla por stdin/stdout con una línea JSON por
mensaje:

  entrada:  {"cmd": "info" | "devices" | "start" | "update" | "stop" | "quit", ...}
  salida:   @@EV {"type": "...", ...}

Todo lo demás que imprima Applio va al registro tal cual.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

APPLIO = os.environ.get("ECVOICE_APPLIO") or os.getcwd()
os.chdir(APPLIO)
if APPLIO not in sys.path:
    sys.path.insert(0, APPLIO)

_print_lock = threading.Lock()


def emit(kind: str, **data) -> None:
    with _print_lock:
        sys.stdout.write("@@EV " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class Live:
    """Una sesión de voz en vivo (AudioCallbacks de Applio)."""

    def __init__(self) -> None:
        self.callbacks = None
        self.audio = None
        self.settings: dict = {}
        self.state = "stopped"  # stopped, loading, warmup, running, error
        self.error = ""
        self.lock = threading.Lock()
        threading.Thread(target=self._status_loop, daemon=True).start()

    # ---------- arrancar / detener ----------
    def start(self, s: dict) -> None:
        from rvc.realtime.audio import resolve_sample_rate
        from rvc.realtime.callbacks import AudioCallbacks

        self.stop()
        with self.lock:
            self.settings = dict(s)
            self.state, self.error = "loading", ""
            emit("status", state="loading")
            sample_rate = resolve_sample_rate(int(s["input"]), False, 48000)
            block_frame = int(float(s.get("chunk_ms", 250)) * sample_rate / 1000)
            monitor = s.get("monitor")
            kwargs = dict(
                pass_through=False,
                block_frame=block_frame,
                cross_fade_overlap_size=float(s.get("crossfade", 0.05)),
                extra_convert_size=float(s.get("extra", 2.5)),
                model_path=s["model"],
                index_path=s.get("index") or "",
                f0_method="rmvpe",
                embedder_model="contentvec",
                embedder_model_custom=None,
                silent_threshold=int(s.get("silence_db", -60)),
                f0_up_key=int(s.get("pitch", 0)),
                index_rate=float(s.get("index_rate", 0.5)) if s.get("index") else 0.0,
                protect=float(s.get("protect", 0.33)),
                volume_envelope=1.0,
                input_audio_gain=float(s.get("input_gain", 100)) / 100,
                output_audio_gain=float(s.get("output_gain", 100)) / 100,
                monitor_audio_gain=float(s.get("monitor_gain", 100)) / 100,
                monitor=monitor is not None,
                vad_enabled=True,
                vad_sensitivity=3,
                vad_frame_ms=30,
                sid=0,
                formant_shifting=False,
                audio_sample_rate=sample_rate,
            )
            self.callbacks = AudioCallbacks(**kwargs)
            self.audio = self.callbacks.audio
            self.audio.start(
                input_device_id=int(s["input"]),
                output_device_id=int(s["output"]),
                output_monitor_id=int(monitor) if monitor is not None else None,
                exclusive_mode=False,
                asio_input_channel=-1,
                asio_output_channel=-1,
                asio_output_monitor_channel=-1,
                block_frame=block_frame,
                audio_sample_rate=sample_rate,
            )

    def stop(self) -> None:
        with self.lock:
            audio, callbacks = self.audio, self.callbacks
            self.audio = self.callbacks = None
            if audio is not None:
                try:
                    audio.stop()
                except Exception:  # noqa: BLE001
                    pass
            if callbacks is not None:
                try:
                    callbacks.vc.stop()
                except Exception:  # noqa: BLE001
                    pass
            if self.state != "error":
                self.state = "stopped"
        emit("status", state=self.state, error=self.error)

    def update(self, s: dict) -> None:
        """Cambios que no requieren reiniciar: tono, parecido, protección, volúmenes."""
        audio = self.audio
        if audio is None:
            return
        self.settings.update(s)
        if "pitch" in s:
            audio.f0_up_key = int(s["pitch"])
        if "index_rate" in s and self.settings.get("index"):
            audio.index_rate = float(s["index_rate"])
        if "protect" in s:
            audio.protect = float(s["protect"])
        if "input_gain" in s:
            audio.input_audio_gain = float(s["input_gain"]) / 100
        if "output_gain" in s:
            audio.output_audio_gain = float(s["output_gain"]) / 100
        if "monitor_gain" in s:
            audio.monitor_audio_gain = float(s["monitor_gain"]) / 100

    # ---------- estado ----------
    def _status_loop(self) -> None:
        while True:
            time.sleep(0.5)
            cb, audio = self.callbacks, self.audio
            if cb is None or audio is None:
                continue
            try:
                process = getattr(cb.vc, "_process", None)
                if process is not None and not process.is_alive():
                    self.state, self.error = "error", "El motor de voz se cerró inesperadamente (revisa el registro)."
                    emit("status", state="error", error=self.error)
                    threading.Thread(target=self.stop, daemon=True).start()
                    continue
                if not cb.vc.ready:
                    self.state = "loading"
                    emit("status", state="loading")
                    continue
                warmup = getattr(cb.vc.vc_model, "warmup_blocks", 0)
                self.state = "warmup" if warmup > 0 else "running"
                emit("status", state=self.state, warmup=warmup,
                     latency=round(float(getattr(audio, "latency", 0) or 0), 1),
                     volume=round(float(getattr(audio, "volume", 0) or 0), 1))
            except Exception as exc:  # noqa: BLE001
                emit("log", text=f"status: {exc}")


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


def devices() -> dict:
    from rvc.realtime.audio import list_audio_device

    inputs, outputs = list_audio_device()

    def pack(items):
        return [{"index": d.index, "name": d.name, "api": d.host_api,
                 "rate": int(d.default_samplerate or 0)} for d in items]

    return {"inputs": pack(inputs), "outputs": pack(outputs)}


def main() -> None:
    live = Live()
    emit("ready")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
            cmd = message.get("cmd")
            if cmd == "info":
                emit("info", **info())
            elif cmd == "devices":
                emit("devices", **devices())
            elif cmd == "start":
                try:
                    live.start(message.get("settings") or {})
                except Exception as exc:  # noqa: BLE001
                    live.error, live.state = str(exc), "error"
                    traceback.print_exc()
                    live.stop()
            elif cmd == "update":
                live.update(message.get("settings") or {})
            elif cmd == "stop":
                live.error = ""
                live.stop()
            elif cmd == "quit":
                live.stop()
                break
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            emit("error", error=str(exc))
    emit("bye")


if __name__ == "__main__":
    main()
