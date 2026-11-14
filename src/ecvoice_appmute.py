"""Silenciar la música «en su origen»: el volumen por aplicación de Windows.

Con una consola que regresa la música por el USB (BOMGE), la música ya viene
revuelta con tu voz y no se puede quitar del todo. Lo limpio es callar a las
apps que suenan (Edge, Chrome, Spotify…) en la salida de la PC: así la música
ni siquiera llega a la consola. Usa pycaw (sesiones de audio de Windows).
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Callable

# Apps que NO se callan: TikTok (su monitor) y EC-Voice.
SKIP = ("tiktok", "mediasdk", "ecvoice")


class AppMuter:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.muted: dict[int, str] = {}  # pid → nombre, solo las que callamos nosotros
        self.active = False              # el silencio de música se está haciendo en el origen
        self.lock = threading.Lock()

    def _in_com(self, fn):
        """pycaw usa COM: correrlo en un hilo propio con COM inicializado."""
        result: dict = {}

        def worker() -> None:
            try:
                import comtypes  # type: ignore[import-not-found]

                comtypes.CoInitialize()
                try:
                    result["value"] = fn()
                finally:
                    comtypes.CoUninitialize()
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(10)
        if "error" in result:
            raise result["error"]
        return result.get("value")

    @staticmethod
    def _sessions():
        from pycaw.pycaw import AudioUtilities  # type: ignore[import-not-found]

        for session in AudioUtilities.GetAllSessions():  # salida predeterminada de Windows
            process = session.Process
            if process is None:
                continue  # sonidos del sistema
            with contextlib.suppress(Exception):
                name = process.name()
                if process.pid == os.getpid() or any(word in name.lower() for word in SKIP):
                    continue
                yield process.pid, name, session.SimpleAudioVolume

    def mute(self) -> list[str]:
        def run() -> list[str]:
            names = []
            for pid, name, volume in self._sessions():
                if volume.GetMute():
                    continue  # ya estaba callada por el usuario: no la tocamos al regresar
                volume.SetMute(1, None)
                self.muted[pid] = name
                names.append(name)
            return names

        with self.lock:
            names = self._in_com(run) or []
            self.active = True
        self.log("Música silenciada en: " + (", ".join(sorted(set(names))) or "ninguna app sonando"))
        return names

    def unmute(self) -> None:
        self.active = False
        if not self.muted:
            return

        def run() -> None:
            for pid, _name, volume in self._sessions():
                if pid in self.muted:
                    volume.SetMute(0, None)

        with self.lock:
            try:
                self._in_com(run)
            finally:
                self.log("Música de nuevo con sonido: " + ", ".join(sorted(set(self.muted.values()))))
                self.muted.clear()

    def refresh(self) -> None:
        """Mientras siga activo el silencio, callar también apps que empezaron a sonar después."""
        def run() -> None:
            for pid, name, volume in self._sessions():
                if pid not in self.muted and not volume.GetMute():
                    volume.SetMute(1, None)
                    self.muted[pid] = name

        with self.lock:
            self._in_com(run)
