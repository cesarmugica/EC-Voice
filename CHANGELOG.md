# Historial de versiones

## 0.1.1 — Cable virtual incluido
- El instalador trae **VB-CABLE** (de VB-Audio, www.vb-cable.com, donationware) y lo instala si lo marcas: Windows pide permiso de administrador y confirmar el controlador; después hay que reiniciar.
- Si falta el cable, EC-Voice muestra **🔌 Instalar cable virtual**.

## 0.1.0 — Base: voz en vivo
- Preparar el motor de IA desde la app (Python 3.12 con uv, PyTorch CUDA 12.8, Applio fijado a una versión).
- Elegir micrófono, salida (VB-Cable) y monitor; aviso si falta VB-Cable.
- Importar voces RVC (.pth / .index / .zip) y elegir una.
- Voz en vivo con tono, parecido, protección, retraso y volúmenes; cambios al momento.
