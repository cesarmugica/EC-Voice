"""«Aprender voz»: entrenar una voz nueva con Applio a partir de enlaces de
YouTube y/o archivos de audio.

Pasos: descargar (yt-dlp) / convertir (FFmpeg) → separar la voz de la música
(MDX-Net «Kim Vocal 2», opcional) → preparar (Applio preprocess) → analizar
(extract) → entrenar (train) → índice → copiar a la carpeta de voces.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma", ".webm", ".mp4", ".mkv", ".mov"}

SEPARATOR_URL = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/Kim_Vocal_2.onnx"
SEPARATOR_SHA256 = "ce74ef3b6a6024ce44211a07be9cf8bc6d87728cc852a68ab34eb8e58cde9c8b"

QUALITY = {"rapida": 150, "normal": 300, "alta": 500}
SAMPLE_RATE = "40000"

STEPS = [
    ("fuentes", "Bajar y convertir los audios"),
    ("separar", "Separar la voz de la música"),
    ("preparar", "Preparar los audios (cortar y limpiar)"),
    ("analizar", "Analizar el tono y el timbre"),
    ("entrenar", "Entrenar la voz"),
    ("guardar", "Crear el índice y guardar la voz"),
]


def slug_ascii(name: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return (text or "voz")[:40]


class LearnJob:
    """Un entrenamiento a la vez. El servidor lee `public()` para la página."""

    def __init__(self, *, home: Path, python: Path, applio: Path, bin_dirs: list[Path], env: Callable[[], dict],
                 log: Callable[[str], None], voices_dir: Path, slug: Callable[[str], str],
                 on_done: Callable[[str], None], separate_script: Path) -> None:
        self.home, self.python, self.applio = home, python, applio
        self.bin_dirs, self.env_fn, self.log = bin_dirs, env, log
        self.voices_dir, self.slug, self.on_done = voices_dir, slug, on_done
        self.separate_script = separate_script
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.cancel_requested = False
        self._reset()

    def _reset(self) -> None:
        self.state = "idle"            # idle, running, done, error, cancelled
        self.name = ""
        self.step = ""
        self.steps = {key: "pending" for key, _ in STEPS}
        self.percent: float | None = None
        self.detail = ""
        self.error = ""
        self.started = 0.0
        self.step_started = 0.0
        self.tail: list[str] = []
        self.minutes = 0.0
        self.result = ""

    # ---------- herramientas ----------
    def tool(self, name: str) -> str | None:
        for folder in self.bin_dirs:
            candidate = folder / (name + EXE)
            if candidate.is_file():
                return str(candidate)
        return shutil.which(name)

    def ytdlp(self) -> str | None:
        """yt-dlp se copia a %LOCALAPPDATA%\\EC-Voice\\bin para poder actualizarse."""
        local = self.home / "bin" / ("yt-dlp" + EXE)
        if not local.is_file():
            bundled = self.tool("yt-dlp")
            if bundled and Path(bundled).is_file():
                local.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundled, local)
        return str(local) if local.is_file() else self.tool("yt-dlp")

    # ---------- estado ----------
    def public(self) -> dict:
        now = time.time()
        return {"state": self.state, "name": self.name, "step": self.step,
                "steps": [{"key": k, "label": label, "state": self.steps.get(k, "pending")} for k, label in STEPS],
                "percent": None if self.percent is None else round(self.percent, 1), "detail": self.detail,
                "error": self.error, "elapsed": round(now - self.started) if self.started else 0,
                "step_elapsed": round(now - self.step_started) if self.step_started else 0,
                "tail": self.tail[-6:], "minutes": round(self.minutes, 1), "result": self.result}

    def running(self) -> bool:
        return self.state == "running"

    # ---------- arranque ----------
    def start(self, name: str, urls: list[str], files: list[str], separate: bool, quality: str) -> None:
        with self.lock:
            if self.running():
                raise RuntimeError("Ya hay una voz aprendiéndose.")
            name = (name or "").strip()
            if not name:
                raise RuntimeError("Ponle nombre a la voz.")
            urls = [u.strip() for u in urls if u.strip()]
            bad = [u for u in urls if not URL_RE.match(u)]
            if bad:
                raise RuntimeError(f"Este enlace no se ve bien: {bad[0]}")
            files = [f for f in files if Path(f).is_file()]
            if not urls and not files:
                raise RuntimeError("Agrega al menos un enlace de YouTube o un archivo de audio.")
            if not self.python.is_file() or not self.applio.is_dir():
                raise RuntimeError("Primero prepara el motor de IA (abajo).")
            self._reset()
            self.cancel_requested = False
            self.state, self.name, self.started = "running", name, time.time()
            epochs = QUALITY.get(quality, QUALITY["normal"])
        threading.Thread(target=self._work, args=(name, urls, files, separate, epochs), daemon=True).start()

    def cancel(self) -> None:
        self.cancel_requested = True
        process = self.process
        if process is not None and process.poll() is None:
            self.log("Aprender voz: cancelando…")
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            else:
                process.kill()

    # ---------- pasos ----------
    def _begin(self, key: str) -> None:
        if self.cancel_requested:
            raise _Cancelled
        self.step, self.steps[key] = key, "running"
        self.percent, self.detail, self.step_started = None, "", time.time()
        label = dict(STEPS)[key]
        self.log(f"Aprender voz «{self.name}»: {label}")

    def _end(self, key: str, state: str = "done") -> None:
        self.steps[key] = state
        self.percent = None

    def _work(self, name: str, urls: list[str], files: list[str], separate: bool, epochs: int) -> None:
        model = slug_ascii(name)
        work = self.home / "learn" / model
        raw, dataset = work / "raw", work / "dataset"
        marker = work / "sin-terminar.txt"
        resume = marker.is_file()  # se canceló o falló antes: el entrenamiento sigue donde se quedó
        try:
            work.mkdir(parents=True, exist_ok=True)
            if not resume:
                # Empezar de cero: sin restos de un aprendizaje anterior con el mismo nombre.
                shutil.rmtree(self._logs(model), ignore_errors=True)
            marker.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
            # 1) Fuentes
            self._begin("fuentes")
            for folder in (raw, dataset):
                if folder.exists():
                    shutil.rmtree(folder, ignore_errors=True)
                folder.mkdir(parents=True, exist_ok=True)
            total = len(urls) + len(files)
            for number, url in enumerate(urls, 1):
                self.detail = f"Enlace {number} de {total}"
                self._download(url, raw, number)
            for number, path in enumerate(files, len(urls) + 1):
                self.detail = f"Archivo {number} de {total}: {Path(path).name}"
                self._convert(Path(path), raw / f"{number:03d}.wav")
            wavs = sorted(raw.glob("*.wav"))
            if not wavs:
                raise RuntimeError("No se pudo sacar audio de ningún enlace o archivo.")
            self.minutes = sum(_wav_seconds(p) for p in wavs) / 60
            self.log(f"Aprender voz: {len(wavs)} audios, {self.minutes:.1f} min en total")
            self._end("fuentes")

            # 2) Separar la voz
            if separate:
                self._begin("separar")
                model_path = self._separator_model()
                self._run_python([str(self.separate_script), str(model_path), str(raw), str(dataset)],
                                 parse=self._parse_separate)
                self._end("separar")
            else:
                self.steps["separar"] = "skipped"
                for wav in wavs:
                    shutil.copy2(wav, dataset / wav.name)
            cores = str(max(1, min(16, (os.cpu_count() or 4) - 1)))

            # 3) Preparar
            self._begin("preparar")
            self._run_python(["core.py", "preprocess", "--model-name", model, "--dataset-path", str(dataset),
                              "--sample-rate", SAMPLE_RATE, "--cpu-cores", cores, "--cut-preprocess", "Automatic",
                              "--process-effects", "--normalization-mode", "post"], fail_text="failed")
            self._end("preparar")

            # 4) Analizar
            self._begin("analizar")
            self._run_python(["core.py", "extract", "--model-name", model, "--f0-method", "rmvpe", "--gpu", "0",
                              "--sample-rate", SAMPLE_RATE, "--cpu-cores", cores], fail_text="failed")
            self._end("analizar")

            # 5) Entrenar
            self._begin("entrenar")
            self.total_epochs = epochs
            self.detail = f"Vuelta 0 de {epochs}" + (" · sigue donde se quedó" if resume else "")
            if resume:
                self.log("Aprender voz: se sigue el entrenamiento anterior de esta voz.")
            self._run_python(["core.py", "train", "--model-name", model, "--save-every-epoch", "10",
                              "--save-only-latest", "--total-epoch", str(epochs), "--sample-rate", SAMPLE_RATE,
                              "--batch-size", "8", "--gpu", "0", "--index-algorithm", "Auto",
                              *([] if resume else ["--cleanup"])],
                             parse=self._parse_train, fail_text="Training failed")
            self._end("entrenar")

            # 6) Guardar
            self._begin("guardar")
            self.result = self._save(model, name)
            self._end("guardar")
            marker.unlink(missing_ok=True)
            for folder in (raw, dataset):  # los WAV de trabajo ocupan mucho; la voz ya quedó guardada
                shutil.rmtree(folder, ignore_errors=True)
            self.state = "done"
            self.log(f"Aprender voz: «{name}» lista en {(time.time() - self.started) / 60:.0f} min")
            self.on_done(self.result)
        except _Cancelled:
            self.state = "cancelled"
            if self.step:
                self.steps[self.step] = "cancelled"
            self.log("Aprender voz: cancelado.")
        except Exception as exc:  # noqa: BLE001
            self.state, self.error = "error", str(exc)
            if self.step:
                self.steps[self.step] = "error"
            self.log(f"Aprender voz: error en «{self.step}»: {exc}")
        finally:
            self.process = None
            self.percent = None

    # ---------- descargas y conversiones ----------
    def _download(self, url: str, raw: Path, number: int) -> None:
        ytdlp, ffmpeg, deno = self.ytdlp(), self.tool("ffmpeg"), self.tool("deno")
        if not ytdlp or not ffmpeg:
            raise RuntimeError("Faltan yt-dlp o FFmpeg. Reinstala EC-Voice.")
        if number == 1:
            with contextlib.suppress(Exception):
                self._run([ytdlp, "-U"], timeout=120)  # YouTube cambia seguido: yt-dlp al día
        downloaded = raw / f"dl_{number:03d}.wav"
        args = [ytdlp, url, "--no-playlist", "-f", "bestaudio/best", "-x", "--audio-format", "wav",
                "--ffmpeg-location", str(Path(ffmpeg).parent), "-o", str(raw / f"dl_{number:03d}.%(ext)s"),
                "--newline", "--no-part", "--no-mtime"]
        if deno:
            args += ["--js-runtimes", f"deno:{deno}"]
        self._run(args, parse=self._parse_ytdlp, check_wav=downloaded)
        self._convert(downloaded, raw / f"{number:03d}.wav")  # 44.1 kHz estéreo para el separador
        downloaded.unlink(missing_ok=True)

    def _convert(self, src: Path, dst: Path) -> None:
        ffmpeg = self.tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("Falta FFmpeg. Reinstala EC-Voice.")
        self._run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ar", "44100", "-ac", "2",
                   str(dst)], check_wav=dst)

    def _separator_model(self) -> Path:
        target = self.home / "engine" / "separator" / "Kim_Vocal_2.onnx"
        if target.is_file() and target.stat().st_size > 10_000_000:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        self.detail = "Descargando el separador de voz (una sola vez, ~65 MB)…"
        tmp = target.with_suffix(".part")
        request = urllib.request.Request(SEPARATOR_URL, headers={"User-Agent": "EC-Voice"})
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:  # noqa: S310
            size = int(response.headers.get("Content-Length") or 0)
            got = 0
            while chunk := response.read(256 * 1024):
                if self.cancel_requested:
                    raise _Cancelled
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                self.percent = got * 100 / size if size else None
        if digest.hexdigest() != SEPARATOR_SHA256:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("El separador de voz se descargó mal. Vuelve a intentar.")
        tmp.replace(target)
        self.percent = None
        return target

    # ---------- procesos ----------
    def _run_python(self, args: list[str], parse: Callable[[str], None] | None = None,
                    fail_text: str | None = None) -> None:
        self._run([str(self.python), "-u", *args], cwd=self.applio, parse=parse, fail_text=fail_text, python=True)

    def _run(self, args: list[str], cwd: Path | None = None, parse: Callable[[str], None] | None = None,
             fail_text: str | None = None, check_wav: Path | None = None, python: bool = False,
             timeout: float | None = None) -> None:
        if self.cancel_requested:
            raise _Cancelled
        self.log("$ " + " ".join(f'"{a}"' if " " in a else a for a in args))
        kwargs: dict = {}
        if IS_WINDOWS:
            # Ventana oculta (no CREATE_NO_WINDOW): Applio abre procesos hijos para entrenar y
            # necesitan una consola; sin ella se quedan atorados o pierden su salida.
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = info
            if not python:
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        env = self.env_fn() if python else dict(os.environ)
        process = subprocess.Popen(args, cwd=str(cwd) if cwd else None, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                   errors="replace", **kwargs)
        self.process = process
        failed_line = ""
        assert process.stdout is not None
        start = time.time()
        buffer = ""
        while True:
            char = process.stdout.read(1)
            if not char:
                break
            if char not in "\r\n":
                buffer += char
                continue
            line, buffer = buffer.strip(), ""
            if not line:
                continue
            if parse:
                with contextlib.suppress(Exception):
                    parse(line)
            if not line.startswith("@@") and not line.startswith("[download]"):
                self.tail.append(line[:300])
                del self.tail[:-40]
                self.log("  " + line[:400])
            if fail_text and fail_text.lower() in line.lower():
                failed_line = line
            if timeout and time.time() - start > timeout:
                process.kill()
        code = process.wait()
        self.process = None
        if self.cancel_requested:
            raise _Cancelled
        if code != 0 or failed_line:
            last = failed_line or (self.tail[-1] if self.tail else f"código {code}")
            raise RuntimeError(f"{dict(STEPS).get(self.step, self.step)} falló: {last}")
        if check_wav is not None and not check_wav.is_file():
            raise RuntimeError(f"No se pudo sacar el audio ({check_wav.name}).")

    # ---------- lectores de progreso ----------
    def _parse_ytdlp(self, line: str) -> None:
        match = re.search(r"\[download\]\s+([\d.]+)%", line)
        if match:
            self.percent = float(match.group(1))

    def _parse_separate(self, line: str) -> None:
        if line.startswith("@@P "):
            _, number, total, fname = line.split(" ", 3)
            self.percent = (int(number) - 1) * 100 / int(total)
            self.detail = f"Audio {number} de {total}"

    def _parse_train(self, line: str) -> None:
        match = re.search(r"epoch=(\d+)", line)
        if match:
            epoch = int(match.group(1))
            total = getattr(self, "total_epochs", 0) or 1
            self.percent = min(100.0, epoch * 100 / total)
            elapsed = time.time() - self.step_started
            left = elapsed / max(epoch, 1) * max(total - epoch, 0)
            self.detail = f"Vuelta {epoch} de {total} · faltan ~{_human(left)}"
        elif "Downloading" in line and "pretrain" in line.lower():
            self.detail = "Descargando el modelo base para entrenar (una sola vez)…"

    def _logs(self, model: str) -> Path:
        return Path(self.env_fn().get("APPLIO_LOGS_DIR") or self.applio / "logs") / model

    # ---------- guardar ----------
    def _save(self, model: str, name: str) -> str:
        logs = self._logs(model)
        weights = []
        for path in logs.glob(f"{model}_*e_*s.pth"):
            match = re.search(r"_(\d+)e_(\d+)s\.pth$", path.name)
            if match:
                weights.append((int(match.group(1)), int(match.group(2)), path))
        if not weights:
            raise RuntimeError("El entrenamiento no dejó ningún modelo (.pth).")
        weights.sort()
        pth = weights[-1][2]
        index = logs / f"{model}.index"
        folder = self.voices_dir / self.slug(name)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)  # volver a aprender la misma voz la reemplaza
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pth, folder / "model.pth")
        if index.is_file():
            shutil.copy2(index, folder / "model.index")
        (folder / "voice.json").write_text(json.dumps({
            "name": name, "learned": time.strftime("%Y-%m-%d %H:%M"), "epochs": weights[-1][0],
            "minutes": round(self.minutes, 1)}, ensure_ascii=False, indent=1), encoding="utf-8")
        self.log(f"Aprender voz: guardada en {folder} (de {pth.name})")
        # Las copias intermedias (~55 MB cada 10 vueltas) ya no hacen falta; G_/D_ se quedan para poder seguir.
        for _epoch, _step, path in weights[:-1]:
            with contextlib.suppress(OSError):
                path.unlink()
        return folder.name


class _Cancelled(Exception):
    pass


def _wav_seconds(path: Path) -> float:
    import wave

    with contextlib.suppress(Exception), wave.open(str(path)) as wav:
        return wav.getnframes() / float(wav.getframerate())
    # WAV de 32 bits flotante u otros que `wave` no abre: estimar por tamaño (44.1 kHz, estéreo, 16 bits)
    return max(0.0, (path.stat().st_size - 44) / (44100 * 2 * 2))


def _human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"
