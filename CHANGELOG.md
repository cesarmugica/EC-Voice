# Historial de versiones

## 1.5.0 — Silenciar voz o música
- En «Mezcla y sincronía»: **🎤 Silenciar mi voz** y **🎵 Silenciar la música**, para que en el live se oiga solo la música o solo tu voz. Funciona con «Mi voz» y con voces de IA.
- Con la consola que regresa la música (BOMGE): al silenciar la música con «Mi voz», EC-Voice la quita de tu micrófono (puede quedar un poco de fondo); al silenciar tu voz, manda la música de la PC.
- Los silencios no se guardan: al abrir EC-Voice todo suena.

## 1.4.3
- El motor carga las librerías de la voz (torch, Applio…) al arrancar, en su hilo principal y una por una, con el tiempo de cada una en el registro. Cargarlas desde otro hilo se quedaba atorado.
- Ya no se llena el registro con avisos de «data discontinuity» de la captura de música.

## 1.4.2
- **Arreglado: la voz de IA se quedaba «cargando» para siempre.** El proceso aparte que abría Applio para la voz se quedaba atorado en Windows sin decir nada. Ahora la voz se carga dentro del mismo motor de EC-Voice, y cualquier error sale en el registro y en pantalla.

## 1.4.1
- Si una voz de IA no termina de cargar en 3 minutos, EC-Voice lo dice (en vez de quedarse «cargando…» para siempre) y libera la tarjeta.
- El registro ahora dice paso a paso cómo va la carga de la voz (Applio, modelo en la tarjeta, calentando, lista) para encontrar dónde se atora.

## 1.4.0 — Voces en lista y favoritas
- **Todas las voces en una lista desplegable, en orden alfabético**, para que la pantalla no se llene cuando tengas muchas.
- **⭐ Favoritas (hasta 8)** como botones, para cambiar de voz con un clic en pleno live (además de «🙂 Mi voz»). «☆ Agregar a favoritas» / «★ Quitar de favoritas» junto a la lista; las voces importadas entran a favoritas si hay lugar.
- «🗑 Borrar» ahora está junto a la lista (borra la voz elegida).

## 1.3.2
- Las voces copiadas a mano a la carpeta `voices` (un `.pth` y su `.index`, sueltos o en una subcarpeta con cualquier nombre) se acomodan solas y aparecen en la lista.
- Con «Mi consola ya regresa la música», la barra del micrófono dice «🎤 Consola (voz + música)».

## 1.3.1
- **Corregido «Preparar motor»** en algunos Windows 11: fallaba en «Preparar Python» con «untrusted mount point (os error 448)» (el Python que descarga uv crea un enlace que Windows bloquea). Ahora se usa el Python oficial de python.org (paquete NuGet), sin enlaces.
- Con «Mi consola ya regresa la música» y «Mi voz», los medidores dicen que lo que sale es la mezcla de la consola (voz + música), para que no parezca un error.

## 1.3.0 — Modo consola y quitar música ajustado a la BOMGE F4-MP3
- **«Mi consola ya regresa la música por el USB»** (en Señales). Con **🙂 Mi voz**, EC-Voice manda la mezcla de la consola tal cual: es lo mismo que escuchas al cantar, así que ya viene sincronizada (sin captura de música ni ajustes). Con una **voz de IA**, EC-Voice quita la música del micrófono, convierte tu voz y le vuelve a poner la música atrasada lo mismo.
- **Quitar la música, rehecho con el diagnóstico real de la F4-MP3:** la consola regresa la música ~80 ms después y ese retraso crece ~3.6 muestras por segundo (los relojes USB de entrada y salida no son idénticos). Ahora EC-Voice mide el retraso cada medio segundo y usa un filtro corto alineado que lo sigue: con la grabación real quita ~15-18 dB (antes ~2 dB).

## 1.2.1
- **🧪 Grabar diagnóstico (20 s):** guarda en `logs` un WAV con el micrófono crudo, la música de referencia y el resultado, para ajustar «Quitar la música que regresa por la consola» con tu consola real.

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
