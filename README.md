# Storyteller

Aplicación de escritorio (PySide6) que convierte **EPUB en audiolibro narrado** con
voz neural y lo reproduce con **subtítulos sincronizados** (la frase que suena se
resalta; clic para saltar). También **importa audiolibros existentes** y los
**transcribe con Whisper** para generar esos subtítulos.

Todo corre **en local** (GPU o CPU) o, si quieres, en un **servidor propio** por SSH.
Los **modelos y las voces no vienen incluidos**: se **descargan desde la propia app**.

## Características

- **Narrar un EPUB** con TTS neural (F5-TTS). Español incluido; inglés y otros
  idiomas añadiendo su modelo.
- **Importar un audiolibro** (mp3/m4a/…) y transcribirlo con Whisper → subtítulos
  con tiempos. Idioma seleccionable o autodetección.
- **Reproductor** con subtítulos sincronizados, autoscroll, control de velocidad,
  saltos, ir a capítulo/posición y **marcador de escucha** (retoma donde lo dejaste).
- **Detener y guardar / Reanudar** tanto la narración como la transcripción (van por
  bloques y guardan el progreso; se continúan desde el punto exacto).
- **Gestor de modelos** (pantalla *Modelos*): descargar/borrar modelos de voz (F5) y
  de transcripción (faster-whisper), elegir idioma, y **cargar voces** desde archivo
  o por URL (el texto de referencia se **autotranscribe**). Catálogo de voces
  descargables.

## Requisitos

- **Python 3.11+** con `PySide6` y `PySide6-Addons` (QtMultimedia, backend ffmpeg).
- **ffmpeg** y **ffprobe** y **curl** en el `PATH`.
- Motor TTS/Whisper en `engine/.venv` (PyTorch, `f5-tts`, `faster-whisper`,
  `num2words`). Para GPU NVIDIA, PyTorch con CUDA.

Los pesos de los modelos y las voces se descargan desde *Modelos* a `~/cuentacuentos/`
(no están en el repositorio).

## Uso

- **Windows:** `Storyteller.bat` (o `pythonw storyteller.py`).
- **Linux/macOS:** `./storyteller.sh` (o `python3 storyteller.py`).

Argumentos: `storyteller.py <libro.epub>` genera, `--book <carpeta>` abre uno hecho.

## Estructura

- `storyteller.py` — la aplicación (biblioteca + reproductor + gestor de modelos).
- `engine/cc_engine.py` — motor local: `download` / `generate` (F5) y
  `whisper-download` / `whisper` (transcripción en streaming).

El modo *Servidor propio* usa un motor equivalente en el servidor por SSH (no incluido).

## Seguridad

Escaneo continuo con **Trivy** (`.github/workflows/trivy.yml`): vulnerabilidades,
secretos y malas configuraciones en cada push/PR.

## Licencia

MIT. Ver [LICENSE](LICENSE).
