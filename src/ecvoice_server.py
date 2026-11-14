"""EC-Voice — mezclador para tus LIVEs: micrófono + música de la PC,
sincronizados y enviados por el cable virtual a TikTok LIVE Studio; y,
si quieres, tu voz convertida al timbre de otra (RVC con Applio).

Servidor local (solo 127.0.0.1) con una página sencilla. El mezclador
(ecvoice_audio.py) corre aquí mismo; el motor de voz de IA corre aparte con
su propio Python (PyTorch con CUDA), que se prepara una sola vez.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

APP_VERSION = "1.5.0"

try:  # lo genera windows/build.ps1
    from ecvoice_build import BUILD_DATE  # type: ignore[import-not-found]
except ImportError:
    BUILD_DATE = ""

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_ROOT = ROOT / "web"
ENGINE_SCRIPT = Path(os.environ.get("ECVOICE_ENGINE_SCRIPT") or ROOT / "engine" / "ecvoice_engine.py")
DEFAULT_PORT = 8793
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
IS_WINDOWS = os.name == "nt"

# Motor: Applio en una versión fija (probada), PyTorch con CUDA 12.8.
APPLIO_COMMIT = "6d5d9511b7e719b1f613362bc767e841ad72fa72"
APPLIO_ZIP = f"https://codeload.github.com/IAHispano/Applio/zip/{APPLIO_COMMIT}"
UV_ZIP = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"


def ecvoice_home() -> Path:
    override = os.environ.get("ECVOICE_HOME")
    if override:
        return Path(override)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "EC-Voice"
    return Path.home() / ".local" / "state" / "ecvoice"


HOME = ecvoice_home()
ENGINE_DIR = HOME / "engine"
APPLIO_DIR = ENGINE_DIR / "applio"
VENV_DIR = ENGINE_DIR / "venv"
# Python oficial de python.org (paquete NuGet: carpeta completa, sin instalador). No usamos el
# Python «administrado» de uv porque en algunos Windows 11 falla al crear su enlace (error 448).
PY_DIR = ENGINE_DIR / "py312"
PYTHON_VERSION = "3.12.10"
PYTHON_NUPKG = f"https://www.nuget.org/api/v2/package/python/{PYTHON_VERSION}"
VENV_PY = Path(os.environ.get("ECVOICE_ENGINE_PY") or PY_DIR / ("python.exe" if IS_WINDOWS else "bin/python3"))
UV_EXE = ENGINE_DIR / "uv" / ("uv.exe" if IS_WINDOWS else "uv")
READY_FILE = ENGINE_DIR / "ready.json"
VOICES_DIR = HOME / "voices"


def _setup_logging() -> None:
    if sys.stdout is not None and not getattr(sys, "frozen", False):
        return
    log_dir = HOME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ecvoice.log"
    with contextlib.suppress(OSError):
        if log_path.exists() and log_path.stat().st_size > 4 * 1024 * 1024:
            log_path.replace(log_dir / "ecvoice.old.log")
    stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stderr = stream


_setup_logging()


_LOG_LOCK = threading.Lock()


def log(message: str) -> None:
    with _LOG_LOCK:
        sys.stdout.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        sys.stdout.flush()


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0


def _engine_env() -> dict:
    env = dict(os.environ)
    env.update({
        "ECVOICE_APPLIO": str(APPLIO_DIR),
        "APPLIO_ROOT": str(APPLIO_DIR),
        "APPLIO_CONFIG_DIR": str(HOME / "applio-config"),
        "APPLIO_LOGS_DIR": str(HOME / "applio-logs"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": str(ENGINE_DIR / "uv-cache"),
        "UV_LINK_MODE": "copy",
    })
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
CONFIG_PATH = HOME / "config.json"
DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "input": "",        # "nombre||api" del micrófono (la consola)
    "output": "",       # "nombre||api" de la salida hacia TikTok (CABLE Input)
    "monitor": "",      # "" = no escucharme
    "music": "",        # salida de la PC donde suena YouTube ("" = sin música)
    "voice": "",        # "" = tu voz natural; si no, la carpeta de la voz de IA
    "pitch": 0,
    "index_rate": 0.5,
    "protect": 0.33,
    "chunk_ms": 250,
    "input_gain": 100,
    "voice_gain": 100,
    "music_gain": 60,
    "monitor_gain": 100,
    "sync_ms": 0,
    "on_at_start": False,
    "favorites": [],
    "mute_voice": False,  # silencios: no se guardan (al abrir, todo suena)
    "mute_music": False,    # voces favoritas (máximo 8) que se ven como botones
    "console_music": False,  # la consola ya regresa la música por el USB (p. ej. BOMGE F4-MP3)
}
CONFIG_LOCK = threading.Lock()


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            config.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    return config


def save_config() -> None:
    with CONFIG_LOCK, contextlib.suppress(OSError):
        HOME.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


CONFIG = load_config()
CONFIG["mute_voice"] = CONFIG["mute_music"] = False

NUMERIC = {"pitch": (-24, 24, int), "index_rate": (0.0, 1.0, float), "protect": (0.0, 0.5, float),
           "chunk_ms": (100, 600, int), "input_gain": (0, 400, int), "voice_gain": (0, 300, int),
           "music_gain": (0, 300, int), "monitor_gain": (0, 300, int), "sync_ms": (-500, 1000, int)}


# ---------------------------------------------------------------------------
# Preparar el motor (una sola vez)
# ---------------------------------------------------------------------------
STEPS = [
    ("uv", "Descargar el instalador de Python (uv)"),
    ("applio", "Descargar Applio (conversión de voz)"),
    ("python", "Descargar Python 3.12 (python.org)"),
    ("libs", "Instalar PyTorch con CUDA y librerías (~3.5 GB)"),
    ("models", "Descargar modelos base (tono y timbre)"),
    ("check", "Comprobar la tarjeta de video"),
]


class Setup:
    def __init__(self) -> None:
        self.state = "ready" if self.is_ready() else "idle"  # idle, running, ready, error
        self.step = ""
        self.done: list[str] = [key for key, _ in STEPS] if self.state == "ready" else []
        self.detail = ""
        self.percent: float | None = None
        self.error = ""
        self.tail: list[str] = []
        self.thread: threading.Thread | None = None

    @staticmethod
    def is_ready() -> bool:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            data = json.loads(READY_FILE.read_text(encoding="utf-8"))
            return data.get("applio") == APPLIO_COMMIT and VENV_PY.is_file() and APPLIO_DIR.is_dir()
        return False

    def public(self) -> dict:
        return {"state": self.state, "step": self.step, "done": self.done, "detail": self.detail,
                "percent": self.percent, "error": self.error, "tail": self.tail[-6:],
                "steps": [{"key": k, "label": label} for k, label in STEPS]}

    def start(self) -> None:
        if self.state == "running":
            return
        self.state, self.error, self.done, self.tail = "running", "", [], []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # ---------- utilidades ----------
    def _line(self, text: str) -> None:
        text = text.strip()
        if text:
            self.tail = (self.tail + [text[:200]])[-30:]
            self.detail = text[:160]

    def _download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": f"EC-Voice/{APP_VERSION}"})
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:  # noqa: S310
            total = int(response.headers.get("Content-Length") or 0)
            got = 0
            while chunk := response.read(256 * 1024):
                out.write(chunk)
                got += len(chunk)
                self.percent = got * 100 / total if total else None
                self.detail = f"{got / 1048576:.1f} MB" + (f" de {total / 1048576:.1f} MB" if total else "")
        tmp.replace(target)

    def _run_cmd(self, args: list[str], cwd: Path | None = None) -> None:
        log("$ " + " ".join(args))
        process = subprocess.Popen(args, cwd=str(cwd) if cwd else None, env=_engine_env(), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                   creationflags=_no_window())
        assert process.stdout is not None
        buffer = ""
        while True:
            char = process.stdout.read(1)
            if not char:
                break
            if char in "\r\n":  # tqdm y uv reescriben la línea con \r
                if buffer.strip():
                    self._line(buffer)
                    match = re.search(r"(\d{1,3})%\|", buffer)
                    self.percent = float(match.group(1)) if match else self.percent
                    if "\n" == char:
                        log("  " + buffer.strip()[:300])
                buffer = ""
            else:
                buffer += char
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"El paso terminó con código {code}: {self.detail}")

    # ---------- pasos ----------
    def _run(self) -> None:
        try:
            ENGINE_DIR.mkdir(parents=True, exist_ok=True)
            for key, label in STEPS:
                self.step, self.percent, self.detail = key, None, ""
                log(f"Motor: {label}")
                getattr(self, f"_step_{key}")()
                self.done.append(key)
            READY_FILE.write_text(json.dumps({"applio": APPLIO_COMMIT, "at": time.time(), "app": APP_VERSION}),
                                  encoding="utf-8")
            self.state, self.step, self.percent = "ready", "", None
            log("Motor listo.")
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if isinstance(exc, OSError) and not isinstance(exc, RuntimeError):
                text = f"No se pudo descargar ({exc}). Revisa tu internet y dale «Reintentar»."
            self.state, self.error = "error", text
            log(f"Motor: error en {self.step}: {exc!r}")

    def _step_uv(self) -> None:
        if UV_EXE.is_file():
            return
        if not IS_WINDOWS:
            found = shutil.which("uv")
            if not found:
                raise RuntimeError("Falta uv.")
            UV_EXE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, UV_EXE)
            return
        archive = ENGINE_DIR / "uv.zip"
        self._download(UV_ZIP, archive)
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if name.endswith(("uv.exe", "uvx.exe")):
                    UV_EXE.parent.mkdir(parents=True, exist_ok=True)
                    (UV_EXE.parent / Path(name).name).write_bytes(zf.read(name))
        archive.unlink(missing_ok=True)
        if not UV_EXE.is_file():
            raise RuntimeError("No se encontró uv.exe en la descarga.")

    def _step_applio(self) -> None:
        marker = APPLIO_DIR / ".ecvoice-commit"
        if marker.is_file() and marker.read_text().strip() == APPLIO_COMMIT:
            return
        archive = ENGINE_DIR / "applio.zip"
        self._download(APPLIO_ZIP, archive)
        self.detail, self.percent = "Descomprimiendo…", None
        staging = ENGINE_DIR / "applio-new"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)
        top = next(staging.iterdir())
        # Conservar los modelos base ya descargados (pesan cientos de MB).
        old_models = APPLIO_DIR / "rvc" / "models"
        if old_models.is_dir():
            shutil.copytree(old_models, top / "rvc" / "models", dirs_exist_ok=True)
        shutil.rmtree(APPLIO_DIR, ignore_errors=True)
        top.replace(APPLIO_DIR)
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
        marker.write_text(APPLIO_COMMIT)

    def _step_python(self) -> None:
        if VENV_PY.is_file():
            return
        # Restos de intentos anteriores con el Python de uv.
        shutil.rmtree(ENGINE_DIR / "python", ignore_errors=True)
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        archive = ENGINE_DIR / "python.nupkg"
        self._download(PYTHON_NUPKG, archive)
        self.detail, self.percent = "Descomprimiendo…", None
        staging = ENGINE_DIR / "py312-new"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if member.filename.startswith("tools/") and not member.is_dir():
                    target = staging / member.filename[len("tools/"):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))
        if not (staging / "python.exe").is_file():
            raise RuntimeError("El paquete de Python no trae python.exe.")
        shutil.rmtree(PY_DIR, ignore_errors=True)
        staging.replace(PY_DIR)
        archive.unlink(missing_ok=True)

    def _step_libs(self) -> None:
        self._run_cmd([str(UV_EXE), "pip", "install", "--python", str(VENV_PY), "--break-system-packages", "-r",
                       str(APPLIO_DIR / "requirements-engine.txt"), "--extra-index-url", TORCH_INDEX,
                       "--index-strategy", "unsafe-best-match"])
        shutil.rmtree(ENGINE_DIR / "uv-cache", ignore_errors=True)  # ~3 GB que ya no hacen falta

    def _step_models(self) -> None:
        self._run_cmd([str(VENV_PY), "core.py", "prerequisites", "--no-pretraineds-hifigan", "--models", "--exe"],
                      cwd=APPLIO_DIR)

    def _step_check(self) -> None:
        data = ENGINE.request("info", timeout=600, start_if_needed=True, force=True)
        if not data:
            raise RuntimeError("El motor no respondió.")
        if data.get("error"):
            raise RuntimeError(data["error"])
        if not data.get("cuda"):
            raise RuntimeError("PyTorch no detecta la tarjeta NVIDIA (CUDA). Actualiza el controlador de NVIDIA y "
                               "vuelve a darle «Preparar motor».")
        self.detail = f"{data.get('gpu')} · {data.get('vram_gb')} GB"


SETUP = Setup()


# ---------------------------------------------------------------------------
# Proceso del motor (Python del motor + Applio)
# ---------------------------------------------------------------------------
class Engine:
    """Proceso del motor de voz (Python del motor + Applio). Solo convierte audio."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.events: dict[str, dict] = {}
        self.event_seen: dict[str, threading.Event] = {}
        self.status: dict = {"state": "empty"}
        self.info: dict = {}
        self.port = 0
        self.loaded_voice = ""
        self.last_state = ""

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ensure(self, force: bool = False) -> None:
        with self.lock:
            if self.running():
                return
            if not force and not Setup.is_ready():
                raise RuntimeError("Primero prepara el motor de IA.")
            if not ENGINE_SCRIPT.is_file():
                raise RuntimeError("Falta el archivo del motor. Reinstala EC-Voice.")
            log("Iniciando el motor de voz…")
            self.status, self.port, self.loaded_voice = {"state": "empty"}, 0, ""
            port_seen = self.event_seen.setdefault("port", threading.Event())
            port_seen.clear()
            self.process = subprocess.Popen(
                [str(VENV_PY), "-u", str(ENGINE_SCRIPT)], cwd=str(APPLIO_DIR), env=_engine_env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=_no_window())
            threading.Thread(target=self._reader, args=(self.process,), daemon=True).start()
        if not port_seen.wait(120):
            raise RuntimeError("El motor de voz no arrancó (revisa el registro).")

    def _reader(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            if not line.startswith("@@EV "):
                if line.strip():
                    log("motor: " + line[:400])
                continue
            with contextlib.suppress(json.JSONDecodeError):
                event = json.loads(line[5:])
                kind = event.pop("type", "")
                if kind == "status":
                    if event.get("state") != self.last_state:
                        self.last_state = event.get("state")
                        log(f"motor: estado {event.get('state')} {event.get('error') or ''}".rstrip())
                    self.status = event
                elif kind == "info":
                    self.info = event
                elif kind == "port":
                    self.port = int(event.get("port") or 0)
                    with contextlib.suppress(OSError):
                        MIXER.link.connect(self.port)
                elif kind in ("error", "log"):
                    log("motor: " + json.dumps(event, ensure_ascii=False))
                self.events[kind] = event
                self.event_seen.setdefault(kind, threading.Event()).set()
        code = process.wait()
        log(f"El motor se cerró (código {code}).")
        MIXER.link.close()
        self.port, self.loaded_voice = 0, ""
        self.status = {"state": "error", "error": "El motor de voz se cerró. Vuelve a elegir la voz."} \
            if CONFIG["voice"] else {"state": "empty"}

    def send(self, message: dict) -> None:
        self.ensure()
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, cmd: str, timeout: float = 60, start_if_needed: bool = True, force: bool = False) -> dict | None:
        if start_if_needed:
            self.ensure(force=force)
        flag = self.event_seen.setdefault(cmd, threading.Event())
        flag.clear()
        self.send({"cmd": cmd})
        if flag.wait(timeout):
            return self.events.get(cmd)
        return None

    def quit(self) -> None:
        if self.running():
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                self.send({"cmd": "quit"})
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=8)  # type: ignore[union-attr]
            if self.running():
                self.process.kill()  # type: ignore[union-attr]


ENGINE = Engine()

from ecvoice_audio import Mixer  # noqa: E402

MIXER = Mixer(log=log)


# ---------------------------------------------------------------------------
# Voces
# ---------------------------------------------------------------------------
def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", slug)[:60] or "Voz"


def _tidy_voices() -> None:
    """Acomodar voces copiadas a mano en la carpeta «voices».

    - Un .pth suelto en «voices» → carpeta con su nombre (y el .index que se le parezca).
    - Una subcarpeta con un .pth de otro nombre → se renombra a model.pth / model.index.
    """
    if not VOICES_DIR.is_dir():
        return
    def is_checkpoint(p: Path) -> bool:
        return bool(re.match(r"^[GD]_\d+", p.name))
    loose_pth = [p for p in VOICES_DIR.glob("*.pth") if p.is_file() and not is_checkpoint(p)]
    loose_idx = [p for p in VOICES_DIR.glob("*.index") if p.is_file()]
    for pth in loose_pth:
        stem = pth.stem
        # el .index que comparta más texto con el nombre del .pth (o el único que haya)
        match = None
        for idx in loose_idx:
            if stem.lower() in idx.stem.lower() or idx.stem.lower().replace("added_", "").startswith(stem.lower()[:6]):
                match = idx
                break
        if match is None and len(loose_pth) == 1 and len(loose_idx) == 1:
            match = loose_idx[0]
        folder = VOICES_DIR / _slug(stem)
        n = 2
        while folder.exists():
            folder = VOICES_DIR / f"{_slug(stem)} {n}"
            n += 1
        try:
            folder.mkdir(parents=True)
            shutil.move(str(pth), folder / "model.pth")
            if match is not None and match.exists():
                shutil.move(str(match), folder / "model.index")
                loose_idx.remove(match)
            (folder / "voice.json").write_text(json.dumps({"name": stem, "created": time.time()}, ensure_ascii=False),
                                               encoding="utf-8")
            log(f"Voz acomodada desde la carpeta: {folder.name}")
        except OSError as exc:  # p. ej. el archivo todavía se está copiando
            log(f"No se pudo acomodar {pth.name}: {exc}")
    for folder in VOICES_DIR.iterdir():
        if not folder.is_dir() or (folder / "model.pth").is_file():
            continue
        pths = sorted((p for p in folder.glob("*.pth") if not is_checkpoint(p)), key=lambda p: p.stat().st_size, reverse=True)
        if not pths:
            continue
        with contextlib.suppress(OSError):
            pths[0].rename(folder / "model.pth")
            idxs = sorted(folder.glob("*.index"), key=lambda p: p.stat().st_size, reverse=True)
            if idxs and not (folder / "model.index").exists():
                idxs[0].rename(folder / "model.index")
            log(f"Voz acomodada: {folder.name}")


def list_voices() -> list[dict]:
    voices = []
    with contextlib.suppress(Exception):
        _tidy_voices()
    if VOICES_DIR.is_dir():
        for folder in sorted(VOICES_DIR.iterdir(), key=lambda p: p.name.lower()):
            model = folder / "model.pth"
            if not model.is_file():
                continue
            meta = {}
            with contextlib.suppress(OSError, json.JSONDecodeError):
                meta = json.loads((folder / "voice.json").read_text(encoding="utf-8"))
            voices.append({"id": folder.name, "name": meta.get("name") or folder.name,
                           "index": (folder / "model.index").is_file(),
                           "size_mb": round(model.stat().st_size / 1048576, 1)})
    return voices


def import_voice(name: str, files: list[str]) -> dict:
    pth, index = None, None
    temp_dir = None
    for item in files:
        path = Path(item)
        if path.suffix.lower() == ".zip":
            temp_dir = HOME / "tmp" / uuid.uuid4().hex
            temp_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as zf:
                for member in zf.infolist():
                    lower = member.filename.lower()
                    base = Path(member.filename).name
                    if member.is_dir() or not lower.endswith((".pth", ".index")):
                        continue
                    if lower.endswith(".pth") and re.match(r"^[GD]_\d+", base):
                        continue  # puntos de entrenamiento, no el modelo final
                    target = temp_dir / f"{uuid.uuid4().hex}_{base}"
                    target.write_bytes(zf.read(member))
                    if lower.endswith(".pth") and (pth is None or target.stat().st_size > pth.stat().st_size):
                        pth = target
                    elif lower.endswith(".index") and (index is None or target.stat().st_size > index.stat().st_size):
                        index = target
        elif path.suffix.lower() == ".pth":
            pth = path
        elif path.suffix.lower() == ".index":
            index = path
    try:
        if pth is None:
            raise RuntimeError("No encontré el modelo de voz (.pth).")
        folder = VOICES_DIR / _slug(name or pth.stem)
        n = 2
        while folder.exists():
            folder = VOICES_DIR / f"{_slug(name or pth.stem)} {n}"
            n += 1
        folder.mkdir(parents=True)
        shutil.copy2(pth, folder / "model.pth")
        if index is not None:
            shutil.copy2(index, folder / "model.index")
        (folder / "voice.json").write_text(json.dumps({"name": name or pth.stem, "created": time.time()},
                                                      ensure_ascii=False), encoding="utf-8")
        log(f"Voz importada: {folder.name} (índice: {'sí' if index else 'no'})")
        return {"id": folder.name}
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def delete_voice(voice_id: str) -> None:
    folder = (VOICES_DIR / voice_id).resolve()
    if VOICES_DIR.resolve() not in folder.parents or not (folder / "model.pth").is_file():
        raise RuntimeError("No existe esa voz.")
    shutil.rmtree(folder)


PICK_LOCK = threading.Lock()


def pick_voice_files() -> list[str]:
    if not IS_WINDOWS:
        return []
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; ShowInTaskbar=$false};"
        "$d = New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Title = 'Elige el modelo de voz (.pth y, si hay, su .index) o un .zip';"
        "$d.Filter = 'Modelos de voz (*.pth, *.index, *.zip)|*.pth;*.index;*.zip';"
        "$d.Multiselect = $true;"
        "if ($d.ShowDialog($owner) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $d.FileNames -join \"`n\" }"
    )
    with PICK_LOCK:
        result = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", creationflags=_no_window(), timeout=600)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Voz en vivo
# ---------------------------------------------------------------------------
def _device_key(device: dict) -> str:
    return f"{device['name']}||{device['api']}"


def _find_device(key: str, items: list[dict]) -> int | None:
    if not key:
        return None
    for device in items:
        if _device_key(device) == key:
            return device["index"]
    name = key.split("||")[0]
    for device in items:  # mismo nombre con otra API
        if device["name"] == name:
            return device["index"]
    return None


DEVICES: dict = {"inputs": [], "outputs": [], "music": []}


def refresh_devices() -> dict:
    global DEVICES
    from ecvoice_audio import list_devices

    DEVICES = list_devices()
    if not CONFIG["output"]:  # elegir sola la Audio Virtual Box (Input de VB-CABLE)
        boxes = [d for d in DEVICES["outputs"] if d["name"].lower().startswith("cable input")]
        boxes.sort(key=lambda d: "wasapi" not in d["api"].lower())
        if boxes:
            CONFIG["output"] = _device_key(boxes[0])
            save_config()
            log(f"Audio Virtual Box elegida: {boxes[0]['name']}")
    return DEVICES


def apply_mixer_settings() -> None:
    MIXER.input_gain = CONFIG["input_gain"] / 100
    MIXER.voice_gain = CONFIG["voice_gain"] / 100
    MIXER.music_gain = CONFIG["music_gain"] / 100
    MIXER.monitor_gain = CONFIG["monitor_gain"] / 100
    MIXER.sync_ms = CONFIG["sync_ms"]
    MIXER.chunk_ms = CONFIG["chunk_ms"]
    MIXER.ai_wanted = bool(CONFIG["voice"])
    MIXER.console_music = bool(CONFIG["console_music"])
    MIXER.mute_voice = bool(CONFIG["mute_voice"])
    MIXER.mute_music = bool(CONFIG["mute_music"])


def start_mixer() -> None:
    devices = DEVICES if DEVICES.get("inputs") else refresh_devices()
    mic = _find_device(CONFIG["input"], devices["inputs"])
    output = _find_device(CONFIG["output"], devices["outputs"])
    if mic is None:
        raise RuntimeError("Elige tu micrófono (la consola).")
    if output is None:
        raise RuntimeError("Elige el Input de la Audio Virtual Box (CABLE Input).")
    monitor = _find_device(CONFIG["monitor"], devices["outputs"]) if CONFIG["monitor"] else None
    music = CONFIG["music"] if any(m["name"] == CONFIG["music"] for m in devices.get("music", [])) else None
    apply_mixer_settings()
    MIXER.start(mic, output, monitor, music)


def voice_settings() -> dict:
    voice = VOICES_DIR / CONFIG["voice"] / "model.pth"
    if not voice.is_file():
        raise RuntimeError("Esa voz ya no existe.")
    index = voice.with_name("model.index")
    return {"model": str(voice), "index": str(index) if index.is_file() else "",
            **{key: CONFIG[key] for key in ("pitch", "index_rate", "protect", "chunk_ms")}}


def apply_voice() -> None:
    """Cargar en el motor la voz elegida, o dejar tu voz natural."""
    apply_mixer_settings()
    if not CONFIG["voice"]:
        if ENGINE.running() and ENGINE.loaded_voice:
            ENGINE.send({"cmd": "unload"})
        ENGINE.loaded_voice = ""
        return
    ENGINE.ensure()
    if MIXER.link.sock is None and ENGINE.port:
        MIXER.link.connect(ENGINE.port)
    key = f"{CONFIG['voice']}|{CONFIG['chunk_ms']}"
    if ENGINE.loaded_voice != key:
        log(f"Cargando la voz «{CONFIG['voice']}» (bloque {CONFIG['chunk_ms']} ms)…")
        ENGINE.status = {"state": "loading"}
        ENGINE.send({"cmd": "load", "settings": voice_settings()})
        ENGINE.loaded_voice = key


# ---------------------------------------------------------------------------
# Cable virtual (VB-CABLE de VB-Audio, incluido en el instalador)
# ---------------------------------------------------------------------------
def install_cable() -> None:
    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT.parent
    setup = exe_dir / "vbcable" / "VBCABLE_Setup_x64.exe"
    if not setup.is_file():
        raise RuntimeError("No encontré el instalador del cable. Reinstala EC-Voice o descárgalo de vb-audio.com/Cable.")
    if not IS_WINDOWS:
        raise RuntimeError("Solo en Windows.")
    # Pide permiso de administrador (UAC); Windows también pide confirmar el controlador.
    import ctypes

    code = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(setup), "-i -h", str(setup.parent), 1)  # type: ignore[attr-defined]
    if code <= 32:
        raise RuntimeError("No se pudo abrir el instalador del cable (¿cancelaste el permiso?).")
    log("Instalador de VB-CABLE abierto.")


# ---------------------------------------------------------------------------
# Ventana
# ---------------------------------------------------------------------------
def _app_browser() -> str | None:
    if not IS_WINDOWS:
        return shutil.which("chromium") or shutil.which("google-chrome")
    bases = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for rel in (Path("Microsoft/Edge/Application/msedge.exe"), Path("Google/Chrome/Application/chrome.exe")):
        for base in bases:
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    return None


def open_app_window(url: str) -> None:
    browser = _app_browser()
    if browser:
        with contextlib.suppress(OSError):
            subprocess.Popen([browser, f"--app={url}", "--window-size=1100,860"], close_fds=True)
            return
    webbrowser.open(url)


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------
LAST_PING = time.time()
IDLE_EXIT_SECONDS = 90


class Handler(BaseHTTPRequestHandler):
    port = DEFAULT_PORT
    server_version = "EC-Voice"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        text = fmt % args
        if '"GET /api/state' in text or '"GET /api/ping' in text:
            return
        log("HTTP " + text)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _local_request(self) -> bool:
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return False
        except ValueError:
            return False
        host = urlsplit("//" + (self.headers.get("Host") or "")).hostname or ""
        return host.lower() in LOCAL_HOSTS

    def _same_origin_post(self) -> bool:
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parts = urlsplit(origin)
        return (parts.hostname or "").lower() in LOCAL_HOSTS and parts.port == self.port

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if path.suffix == ".html":
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ValueError("demasiado grande")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON inválido")
        return data

    def state(self) -> dict:
        return {
            "version": APP_VERSION, "build_date": BUILD_DATE, "setup": SETUP.public(),
            "engine": {"running": ENGINE.running(), "info": ENGINE.info, "status": ENGINE.status},
            "mixer": MIXER.public(), "devices": DEVICES, "voices": list_voices(),
            "settings": {k: v for k, v in CONFIG.items() if k != "port"},
        }

    def do_GET(self) -> None:  # noqa: N802
        global LAST_PING
        if not self._local_request():
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.send_file(WEB_ROOT / "index.html")
        elif path == "/api/state":
            LAST_PING = time.time()
            self.send_json(self.state())
        elif path == "/api/ping":
            LAST_PING = time.time()
            self.send_json({"ok": True})
        elif path.startswith("/img/"):
            target = (WEB_ROOT / path.lstrip("/")).resolve()
            if WEB_ROOT.resolve() in target.parents and target.is_file():
                self.send_file(target)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_request() or not self._same_origin_post():
            self.send_json({"ok": False, "error": "Solicitud rechazada."}, 403)
            return
        try:
            data = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"ok": False, "error": "Solicitud inválida."}, 400)
            return
        path = urlsplit(self.path).path
        try:
            result = self._post(path, data)
        except RuntimeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        except Exception as exc:  # noqa: BLE001
            log(f"Error en {path}: {exc!r}")
            self.send_json({"ok": False, "error": f"Algo falló: {exc}"}, 500)
            return
        if result is None:
            self.send_json({"ok": False, "error": "No encontrado."}, 404)
        else:
            self.send_json({"ok": True, **result, "state": self.state()})

    def _post(self, path: str, data: dict) -> dict | None:
        if path == "/api/setup/start":
            SETUP.start()
            return {}
        if path == "/api/devices":
            refresh_devices()
            return {}
        if path == "/api/settings":
            return self._settings(data)
        if path == "/api/mixer/start":
            start_mixer()
            if CONFIG["voice"] and Setup.is_ready():
                threading.Thread(target=self._safe_apply_voice, daemon=True).start()
            return {}
        if path == "/api/mixer/stop":
            MIXER.stop()
            return {}
        if path == "/api/engine/info":
            ENGINE.request("info", timeout=300)
            return {}
        if path == "/api/voices/import":
            files = pick_voice_files()
            if not files:
                return {"imported": None}
            imported = import_voice(str(data.get("name") or "").strip(), files)
            if len(CONFIG["favorites"]) < 8:
                CONFIG["favorites"].append(imported["id"])
                save_config()
            return {"imported": imported}
        if path == "/api/voices/delete":
            voice_id = str(data.get("id") or "")
            if CONFIG["voice"] == voice_id:
                CONFIG["voice"] = ""
                save_config()
                apply_voice()
            delete_voice(voice_id)
            if voice_id in CONFIG["favorites"]:
                CONFIG["favorites"] = [v for v in CONFIG["favorites"] if v != voice_id]
                save_config()
            return {}
        if path == "/api/cable/install":
            install_cable()
            return {}
        if path == "/api/open/voices":
            open_folder(VOICES_DIR)
            return {}
        if path == "/api/diag":
            if not MIXER.running or not MIXER.echo_active():
                raise RuntimeError("Para el diagnóstico: marca «Mi consola ya regresa la música», elige una voz de IA y enciende el mezclador con la música elegida.")
            (HOME / "logs").mkdir(parents=True, exist_ok=True)
            MIXER.start_diag(str(HOME / "logs" / f"diagnostico-{time.strftime('%Y%m%d-%H%M%S')}.wav"))
            return {}
        if path == "/api/open/logs":
            open_folder(HOME / "logs")
            return {}
        return None

    @staticmethod
    def _safe_apply_voice() -> None:
        try:
            apply_voice()
        except Exception as exc:  # noqa: BLE001
            ENGINE.status = {"state": "error", "error": str(exc)}
            log(f"No se pudo cargar la voz: {exc}")

    def _settings(self, data: dict) -> dict:
        devices_changed = False
        for key in ("input", "output", "monitor", "music"):
            if key in data and isinstance(data[key], str) and CONFIG[key] != data[key]:
                CONFIG[key] = data[key]
                devices_changed = True
        if isinstance(data.get("favorites"), list):
            known = {v["id"] for v in list_voices()}
            CONFIG["favorites"] = [str(v) for v in data["favorites"] if str(v) in known][:8]
        if isinstance(data.get("on_at_start"), bool):
            CONFIG["on_at_start"] = data["on_at_start"]
        for key in ("mute_voice", "mute_music"):
            if isinstance(data.get(key), bool):
                CONFIG[key] = data[key]
        if isinstance(data.get("console_music"), bool):
            CONFIG["console_music"] = data["console_music"]
        voice_changed = False
        if "voice" in data and isinstance(data["voice"], str) and data["voice"] != CONFIG["voice"]:
            if data["voice"] and not Setup.is_ready():
                raise RuntimeError("Para usar voces de IA, primero prepara el motor (abajo).")
            CONFIG["voice"] = data["voice"]
            voice_changed = True
        live_update = {}
        for key, (low, high, cast) in NUMERIC.items():
            if key in data:
                with contextlib.suppress(TypeError, ValueError):
                    value = cast(min(high, max(low, cast(data[key]))))
                    if key == "chunk_ms" and value != CONFIG[key]:
                        voice_changed = True
                    CONFIG[key] = value
                    live_update[key] = value
        save_config()
        apply_mixer_settings()
        if devices_changed and MIXER.running:
            start_mixer()  # reabrir los dispositivos
        if voice_changed:
            threading.Thread(target=self._safe_apply_voice, daemon=True).start()
        elif ENGINE.running() and ENGINE.loaded_voice:
            update = {k: v for k, v in live_update.items() if k in ("pitch", "index_rate", "protect")}
            if update:
                ENGINE.send({"cmd": "update", "settings": update})
        return {}


def _idle_watch(server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(15)
        busy = MIXER.running or SETUP.state == "running"
        if time.time() - LAST_PING > IDLE_EXIT_SECONDS and not busy:
            log("Sin ventana abierta ni voz en vivo: EC-Voice se cierra.")
            ENGINE.quit()
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=f"EC-Voice {APP_VERSION}")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stay", action="store_true", help="no cerrarse al cerrar la ventana")
    parser.add_argument("--version", action="version", version=f"EC-Voice {APP_VERSION} {BUILD_DATE}".strip())
    args = parser.parse_args()

    port = args.port or int(CONFIG.get("port") or DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}/"
    handler = type("EcVoiceHandler", (Handler,), {"port": port})
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        if not args.no_browser:
            open_app_window(url)
        return 0
    server.daemon_threads = True
    log(f"EC-Voice {APP_VERSION} ({BUILD_DATE or 'desarrollo'}) en {url} · motor {'listo' if Setup.is_ready() else 'sin preparar'}")
    if not args.stay:
        threading.Thread(target=_idle_watch, args=(server,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, open_app_window, args=(url,)).start()
    if CONFIG.get("on_at_start"):
        def _autostart() -> None:
            try:
                start_mixer()
                if CONFIG["voice"] and Setup.is_ready():
                    apply_voice()
            except Exception as exc:  # noqa: BLE001
                log(f"No se pudo encender el mezclador al abrir: {exc}")
        threading.Thread(target=_autostart, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MIXER.stop()
        ENGINE.quit()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
