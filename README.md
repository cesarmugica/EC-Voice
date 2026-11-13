# EC-Voice

Tu voz en vivo con el timbre de otra persona, para los LIVEs de TikTok. Tú cantas (la afinación y la interpretación son tuyas) y EC-Voice cambia el timbre por el de la voz elegida, en tiempo real, con conversión **RVC**.

> Uso personal. Usa voces con permiso o como parodia, y etiqueta el live como contenido hecho con IA.

## Cómo funciona

1. **Instala** `EC-Voice-Setup-x.y.z.exe` (pequeño).
2. **Prepara el motor** (solo la primera vez, desde la app): descarga Python 3.12, PyTorch con CUDA y [Applio](https://github.com/IAHispano/Applio) (MIT), unos 4 GB. Necesita una tarjeta **NVIDIA**.
3. **Audio:** tu micrófono → EC-Voice → **VB-Cable** ([vb-audio.com/Cable](https://vb-audio.com/Cable/), gratis) → TikTok LIVE Studio (micrófono: «CABLE Output»). Opcional: escucharte en tus audífonos.
4. **Voces:** importa un modelo RVC (`.pth` y su `.index`, o un `.zip`). Más adelante: crear voces desde canciones.
5. **🎤 Activar voz.** Ajusta tono, parecido, protección de consonantes y retraso; los cambios se aplican al momento.

## Cómo está hecho

- `src/ecvoice_server.py`: app local (solo `127.0.0.1:8793`), prepara el motor, maneja las voces y controla el motor.
- `src/engine/ecvoice_engine.py`: corre con el Python del motor dentro de Applio y usa su conversión en tiempo real (`rvc.realtime`). Habla con la app por JSON en stdin/stdout.
- `src/web/index.html`: la ventana (Edge en modo aplicación).
- `windows/`: PyInstaller + Inno Setup; GitHub Actions publica un Release por cada `APP_VERSION` nueva.
- Datos: `%LOCALAPPDATA%\EC-Voice` (`engine\` ~6 GB, `voices\`, `config.json`, `logs\ecvoice.log`).

Applio está fijado a una versión probada (`APPLIO_COMMIT` en `src/ecvoice_server.py`).
