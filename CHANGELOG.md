# Historial de versiones

## 1.2.0 — Quitar la música que regresa por la consola
- **Corregido:** «No se pudo capturar la música de la PC: Error 0x800401f0» (faltaba inicializar COM de Windows en el hilo que copia la música).
- **Nuevo: quitar la música que regresa por la consola.** Consolas como la BOMGE F4-MP3 mandan de vuelta por el USB la música junto con tu micrófono; EC-Voice ahora la resta (filtro adaptativo, como el anti-eco de las videollamadas) para que en TikTok la música salga una sola vez y sincronizada, y para que la voz de IA no intente convertir la canción. Activado por defecto; muestra cuántos dB está quitando.
- La sincronía automática toma en cuenta los ~40 ms que agrega quitar la música.

## 1.1.1
- El cable virtual ahora se llama **📦 Audio Virtual Box** en EC-Voice: EC-Voice → **Input** ▣ caja ▣ **Output** → TikTok LIVE Studio.
- EC-Voice elige solo el **Input** de la caja cuando la encuentra y muestra «✅ conectada».

## 1.1.0 — Mezclador y sincronizador
- **EC-Voice ahora es el mezclador de tu live**, con o sin voz de IA: toma tu micrófono (la consola) y **la música que suena en la PC** (YouTube), los **sincroniza** y los manda juntos por el cable virtual a TikTok LIVE Studio. Ya no hay que configurar la captura de audio en TikTok ni pelear con el desfase.
- **Sincronía:** la música se atrasa sola lo que tarda tu voz (natural o de IA) y hay un ajuste fino de −500 a +1000 ms.
- Volúmenes de micrófono, voz y música, con medidores; «Escucharme en» opcional; «Encender al abrir EC-Voice».
- **🙂 Mi voz:** usar EC-Voice solo como mezclador, sin preparar el motor de IA.
- **Cambiar de voz sin quedarte mudo:** mientras la voz de IA carga (o si se atrasa), sale tu voz normal. El cambio entre voces de IA ya no reinicia todo el motor.
- El motor de IA ahora solo convierte audio; los dispositivos los maneja el mezclador.

## 0.1.1 — Cable virtual incluido
- El instalador trae **VB-CABLE** (de VB-Audio, www.vb-cable.com, donationware) y lo instala si lo marcas: Windows pide permiso de administrador y confirmar el controlador; después hay que reiniciar.
- Si falta el cable, EC-Voice muestra **🔌 Instalar cable virtual**.

## 0.1.0 — Base: voz en vivo
- Preparar el motor de IA desde la app (Python 3.12 con uv, PyTorch CUDA 12.8, Applio fijado a una versión).
- Elegir micrófono, salida (VB-Cable) y monitor; aviso si falta VB-Cable.
- Importar voces RVC (.pth / .index / .zip) y elegir una.
- Voz en vivo con tono, parecido, protección, retraso y volúmenes; cambios al momento.
