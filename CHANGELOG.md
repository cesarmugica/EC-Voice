# Historial de versiones

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
