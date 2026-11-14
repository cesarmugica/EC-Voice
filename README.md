# EC-Voice

El mezclador de tus LIVEs de TikTok: junta tu micrófono y la música de la PC, los **sincroniza** y los manda por el cable virtual a TikTok LIVE Studio. Y si quieres, cambia tu voz en tiempo real por el timbre de otra persona (conversión **RVC**): tú cantas, la afinación y la interpretación son tuyas.

> Uso personal. Usa voces con permiso o como parodia, y etiqueta el live como contenido hecho con IA.

## Flujo de audio

```
Micrófono → consola → USB ─┐
                           ├─ EC-Voice (voz natural o de IA + música atrasada para ir juntas) → CABLE Input → TikTok LIVE Studio (micrófono: CABLE Output)
YouTube en la PC (bocinas) ┘  (captura de lo que suena)
```

## Cómo funciona

1. **Instala** `EC-Voice-Setup-x.y.z.exe` (pequeño).
2. **Prepara el motor** (solo la primera vez, desde la app): descarga Python 3.12, PyTorch con CUDA y [Applio](https://github.com/IAHispano/Applio) (MIT), unos 4 GB. Necesita una tarjeta **NVIDIA**.
3. **Audio:** tu micrófono → EC-Voice → **VB-CABLE** → TikTok LIVE Studio (micrófono: «CABLE Output»). Opcional: escucharte en tus audífonos. El instalador incluye VB-CABLE, de **VB-Audio** ([www.vb-cable.com](https://vb-audio.com/Cable/)), que es *donationware*: si te sirve, apóyalos.
4. **Voces:** importa un modelo RVC (`.pth` y su `.index`, o un `.zip`), o **apréndela tú** (abajo). Todas en una lista alfabética y hasta 8 favoritas como botones.
5. **Elige la voz** y ajusta tono, parecido, protección de consonantes y retraso; los cambios se aplican al momento. En «Mezcla y sincronía» puedes silenciar tu voz o la música.

## 🎓 Aprender una voz

Pon enlaces de YouTube y/o audios de tu PC con la voz (lo ideal: 10 a 30 minutos, la persona sola, sin eco). EC-Voice:

1. baja el audio (yt-dlp) o lo convierte (FFmpeg);
2. separa la voz de la música (modelo MDX-Net *Kim Vocal 2* de [UVR](https://github.com/TRvlvr/model_repo), opcional);
3. con Applio: prepara los audios, analiza tono y timbre, **entrena** (150 / 300 / 500 vueltas) y crea el índice;
4. guarda la voz en tus voces (y en favoritas si hay lugar).

En una RTX 3090, 300 vueltas con 15 minutos de audio tardan aproximadamente de 30 a 60 minutos. Puedes cerrar la ventana: sigue entrenando. Si cancelas y vuelves a empezar con el mismo nombre, el entrenamiento sigue donde se quedó.

## Cómo está hecho

- `src/ecvoice_server.py`: app local (solo `127.0.0.1:8793`), prepara el motor, maneja las voces y controla el motor.
- `src/ecvoice_audio.py`: el mezclador (sounddevice + soundcard): micrófono, captura de la música de la PC, sincronía, mezcla y salida al cable.
- `src/ecvoice_learn.py`: «Aprender voz» (descargar, separar, preprocess/extract/train/index de Applio, guardar).
- `src/engine/ecvoice_separate.py`: separa la voz de la música (ONNX Runtime con CUDA).
- `src/engine/ecvoice_engine.py`: corre con el Python del motor dentro de Applio y convierte bloques de audio (`VoiceChangerWorker`). Control por JSON en stdin/stdout; audio por un socket local.
- `src/web/index.html`: la ventana (Edge en modo aplicación).
- `windows/`: PyInstaller + Inno Setup; GitHub Actions publica un Release por cada `APP_VERSION` nueva.
- Datos: `%LOCALAPPDATA%\EC-Voice` (`engine\` ~6 GB, `voices\`, `learn\`, `applio-logs\` (entrenamientos), `config.json`, `logs\ecvoice.log`).

Applio está fijado a una versión probada (`APPLIO_COMMIT` en `src/ecvoice_server.py`).
