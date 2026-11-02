# Storyteller

[English version](README.md)

<p align="center">
  <img src="docs/player.png" width="49%" alt="Player: the sentence being read is highlighted">
  <img src="docs/library.png" width="49%" alt="Library of narrated books">
</p>

Aplicación de escritorio (PySide6) que convierte **EPUB en audiolibro narrado** con
voz neural, en el idioma que elijas (17 disponibles con XTTS), y lo reproduce con
**subtítulos sincronizados** (la frase que suena se resalta; clic para saltar). También
**importa audiolibros existentes** y los **transcribe con Whisper** para generar esos
subtítulos, y **traduce libros y documentos** entre 15 idiomas con TranslateGemma.

Todo corre **en local** (GPU o CPU) o, si quieres, en un **servidor propio** por SSH.
La narración también puede usar **cualquier servicio de voz por URL**: el tuyo o uno público con su clave.
Los **modelos y las voces no vienen incluidos**: se **descargan desde la propia app**. Para narrar basta con XTTS
o un servicio de voz; **F5 y Whisper son opcionales** y se instalan aparte, solo si los quieres.

## Características

- **Narrar un EPUB** con TTS neural, eligiendo motor en *Modelos*:
  - **XTTS v2 multilingüe** (coqui-tts): lee la frase entera de una pasada y pronuncia
    bien las palabras en otro idioma dentro del texto (p. ej. inglés en un libro en
    español). Clona la voz a partir de un audio de referencia. Narra en **17 idiomas**,
    que se eligen en *Modelos*: español, inglés, francés, alemán, italiano, portugués,
    neerlandés, polaco, checo, húngaro, turco, ruso, árabe, hindi, chino, japonés y coreano.
  - **F5-TTS** (español o inglés, opcional, se instala aparte): un modelo por idioma.
  - **Un servicio de voz por URL**: tu propio XTTS con `st_server.py` ([API](docs/server-api.md))
    o un servicio público con su clave, con varias **peticiones a la vez**. Ahí tu servidor
    también puede recibir voces nuevas y dar libros terminados a otras apps.
- **Pausas que sigue el libro**: corta en cada coma, así que las pausas tras una coma, un
  punto y un párrafo las marca la puntuación y no se dejan al modelo.
- **Tramos en paralelo** (1, 2 o 3): reparte los capítulos entre varios procesos en la
  misma GPU; con XTTS, 3 tramos narran unas 2,5 veces más rápido.
- **Tiempo restante** en la barra de progreso («quedan ~1 h 10 min, hacia las 14:35»),
  calculado con el ritmo real de cada tramo.
- **Control de calidad (QC) con Whisper** (opcional, se instala aparte): transcribe cada frase narrada y la regenera
  si faltan palabras, se inventa texto o entra en bucle repitiendo. También mide el audio,
  porque Whisper no oye el balbuceo sin palabras. Con un servicio de voz por URL, revisa lo que llega con el
  Whisper de este equipo o con un Whisper por URL, con su propia clave; volver a pedir una frase tarda más y,
  en un servicio de pago, cuesta más.
- **Texto preparado para la voz**: números en palabras en el idioma que se narra, siglas
  deletreadas, MAYÚSCULAS, puntuación, y un **diccionario de pronunciación** editable
  (`pron.json`, con una parte por idioma) para las palabras que suenen mal.
- **Importar un audiolibro** (mp3/m4a/…) y transcribirlo con Whisper (opcional, se instala aparte) → subtítulos
  con tiempos. Idioma seleccionable o autodetección.
- **Reproductor** con subtítulos sincronizados, autoscroll, control de velocidad,
  saltos, ir a capítulo/posición y **marcador de escucha** (retoma donde lo dejaste).
- **Escuchar mientras narra**: *Escuchar* en la barra de narración abre el libro en curso;
  el reproductor añade solo las frases y capítulos nuevos y, cuando alcanza lo narrado,
  espera y sigue en el punto exacto.
- **Detener y guardar / Reanudar** tanto la narración como la transcripción, también con
  otro modelo (lo ya hecho se conserva; van por
  bloques y guardan el progreso; se continúan desde la misma frase). Cerrar la app
  durante una narración equivale a «Detener y guardar». Hay un solo trabajo a la vez: al
  reanudar otro libro, pregunta si detiene el actual, que queda guardado, y pasa a él.
- **Biblioteca**: reanudar libros a medias, **regenerar** uno desde su EPUB original y
  borrarlo.
- **Traducir documentos** (botón *Traducir…*) con **TranslateGemma**: EPUB, PDF, Word,
  PowerPoint, Excel, subtítulos (SRT/VTT), TXT y Markdown, entre los idiomas de la
  interfaz. Conserva párrafos, títulos, tablas y los tiempos de los subtítulos (las
  cursivas o negritas sueltas dentro de una frase se pierden); un PDF se convierte en un
  EPUB traducido, sin su maquetación. En este equipo (llama.cpp, cargado solo mientras traduce) o en el servidor propio
  (Ollama por SSH). Se puede detener y reanudar, también con otro modelo, y el EPUB
  traducido se manda a narrar con un clic.
- **Gestor de modelos** (pantalla *Modelos*): descargar/borrar modelos de voz (XTTS v2,
  F5) y de transcripción (faster-whisper), con barra de progreso (tamaño, velocidad, tiempo restante) y *Cancelar*, elegir el idioma de la narración, tramos en paralelo y QC, y
  **cargar voces** desde archivo
  o por URL (el texto de referencia se **autotranscribe**). Catálogo de voces
  descargables.

<p align="center">
  <img src="docs/translate.png" width="49%" alt="Translate documents screen">
  <img src="docs/models.png" width="49%" alt="Models and voices screen">
</p>

## Requisitos

- **Python 3.11, 3.12 o 3.13**. La versión de PyTorch de abajo no tiene paquetes para la 3.14.
- **ffmpeg** y **ffprobe** y **curl** en el `PATH`.
- `PySide6` y `PySide6-Addons` (QtMultimedia, backend ffmpeg), desde `requirements.txt`.
- Los motores que quieras, cada uno en su paso de [Instalación](#instalación). Para narrar basta con XTTS o un servicio
  de voz; **F5 y Whisper son opcionales**.
- Traducción: se instala sola desde su pantalla (`engine/.venv-translate` y
  `engine/llama.cpp`). En modo servidor, Ollama en el servidor.

Los pesos de los modelos y las voces se descargan desde *Modelos* a `~/cuentacuentos/`
(no están en el repositorio).

## Instalación

La app siempre busca sus motores, modelos y libros en `~/cuentacuentos`, así que el clon tiene
que ir en esa carpeta. En una terminal (PowerShell en Windows):

```sh
cd ~
git clone https://github.com/javimentallab/Storyteller.git cuentacuentos
cd cuentacuentos
python -m pip install -r requirements.txt
```

En Linux/macOS usa `python3`. Si Linux se niega con `externally-managed-environment`, instala
PySide6 en un venv propio y abre la app desde la terminal donde esté activo:

```sh
python3 -m venv ~/storyteller-app
. ~/storyteller-app/bin/activate
python3 -m pip install -r requirements.txt
```

Después, los motores. Instala solo lo que vayas a usar:

| Quieres | Instala |
|---|---|
| Narrar en este equipo | [XTTS](#xtts) |
| Narrar y revisar cada frase (QC) | [XTTS](#xtts) y después [Whisper](#whisper-opcional) en su entorno |
| Transcribir audiolibros, sin narrar aquí | [Whisper](#whisper-opcional) por su cuenta |
| Narrar con un servicio de voz por URL | [Leer el EPUB](#leer-el-epub), o XTTS si ya lo tienes |
| Narrar en tu servidor | Nada aquí |
| Narrar con un modelo F5 | [F5](#f5-opcional) |

Los comandos se ejecutan desde la carpeta `cuentacuentos`. XTTS y F5 ocupan varios GB. Instala primero PyTorch, como se
indica, para que pip no traiga una versión más nueva. Sin GPU NVIDIA, cambia `cu128` por `cpu` en su `--index-url`.

### XTTS

La voz multilingüe, en su propio entorno. Windows:

```powershell
python -m venv engine\.venv-xtts
engine\.venv-xtts\Scripts\python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
engine\.venv-xtts\Scripts\python -m pip install -r engine\deps\xtts\requirements.txt
```

Linux/macOS:

```sh
python3 -m venv engine/.venv-xtts
engine/.venv-xtts/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
engine/.venv-xtts/bin/python -m pip install -r engine/deps/xtts/requirements.txt
```

Para narrar en japonés o chino, añade esto. Windows:

```powershell
engine\.venv-xtts\Scripts\python -m pip install -r engine\deps\xtts-languages\requirements.txt
```

Linux/macOS:

```sh
engine/.venv-xtts/bin/python -m pip install -r engine/deps/xtts-languages/requirements.txt
```

### Whisper (opcional)

Para transcribir audiolibros y para el control de calidad (QC). Va **en el entorno del motor que narra**, porque el QC
corre en ese proceso; desde ahí también transcribe. Sin Whisper se puede narrar igual, sin QC, y el texto de referencia
de una voz se escribe a mano.

Con XTTS, para el QC y para transcribir. Windows:

```powershell
engine\.venv-xtts\Scripts\python -m pip install -r engine\deps\whisper\requirements.txt
```

Linux/macOS:

```sh
engine/.venv-xtts/bin/python -m pip install -r engine/deps/whisper/requirements.txt
```

Con F5, lo mismo en su entorno. Windows:

```powershell
engine\.venv\Scripts\python -m pip install -r engine\deps\whisper\requirements.txt
```

Linux/macOS:

```sh
engine/.venv/bin/python -m pip install -r engine/deps/whisper/requirements.txt
```

Sin ningún motor, solo para transcribir. Windows:

```powershell
python -m venv engine\.venv
engine\.venv\Scripts\python -m pip install -r engine\deps\whisper\requirements.txt
```

Linux/macOS:

```sh
python3 -m venv engine/.venv
engine/.venv/bin/python -m pip install -r engine/deps/whisper/requirements.txt
```

Por su cuenta transcribe en la CPU. Usa una tarjeta gráfica NVIDIA si ese entorno tiene PyTorch con CUDA, como los de
XTTS y F5; para añadirlo a `engine/.venv`, usa el comando de PyTorch de [F5](#f5-opcional).

### Leer el EPUB

Basta para narrar con un servicio de voz por URL. XTTS y F5 ya lo traen. Windows:

```powershell
python -m venv engine\.venv
engine\.venv\Scripts\python -m pip install -r engine\deps\base\requirements.txt
```

Linux/macOS:

```sh
python3 -m venv engine/.venv
engine/.venv/bin/python -m pip install -r engine/deps/base/requirements.txt
```

### F5 (opcional)

Solo para narrar con un modelo F5. Trae paquetes con otras licencias, como una no comercial ([NOTICE](NOTICE)), que
quedan fuera si no lo instalas. Windows:

```powershell
python -m venv engine\.venv
engine\.venv\Scripts\python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
engine\.venv\Scripts\python -m pip install --no-deps -r engine\deps\f5\requirements.txt
```

Linux/macOS:

```sh
python3 -m venv engine/.venv
engine/.venv/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
engine/.venv/bin/python -m pip install --no-deps -r engine/deps/f5/requirements.txt
```

Se instala con `--no-deps` para dejar fuera `gradio`, la interfaz web de F5, que Storyteller no usa, y para saltar los topes
que `gradio` y `cached-path` ponen a `pillow` y `filelock`, cuyas versiones antiguas tienen vulnerabilidades conocidas. Por
eso `pip check` se queja de `gradio` y `cached-path`: es lo esperado, no un fallo.

Luego abre la app y descarga los modelos desde *Modelos*. Si falta un motor, su ficha lo dice y da los comandos, y la
pantalla nota el motor nuevo la próxima vez que la abras, sin reiniciar. Una instalación de una versión anterior, con
todo en `engine/.venv` y `engine/.venv-xtts`, sigue funcionando tal cual.

## Uso

- **Windows:** `Storyteller.bat` (o `pythonw storyteller.py`).
- **Linux/macOS:** `./storyteller.sh` (o `python3 storyteller.py`).

Argumentos: `storyteller.py <libro.epub>` genera, `--book <carpeta>` abre uno hecho.

**Idiomas de la interfaz (15):** inglés, español, portugués, francés, alemán, italiano,
neerlandés, polaco, ruso, ucraniano, chino, japonés, coreano, tailandés e hindi. Sigue el
idioma del sistema (inglés si no está en la lista); para forzar uno, `--lang xx`
(p. ej. `--lang de`) o la variable de entorno `STORYTELLER_LANG`.

## Estructura

- `storyteller.py` — la aplicación (biblioteca + reproductor + gestor de modelos).
- `st_lang.py` + `i18n/<idioma>.txt` — traducciones de la interfaz (una línea `NN|texto`
  por texto; se comprueban al cargar: que estén todos y con los mismos `{n}`).
- `requirements.txt` y `engine/deps/<motor>/requirements.txt` — las versiones exactas de la interfaz y de cada motor.
- `engine/cc_engine.py` — motor local: `download` / `generate` (F5), `count`
  (capítulos y frases, para repartir tramos y estimar el tiempo), `whisper-download` /
  `whisper` (transcripción en streaming) y la preparación del texto común a los motores.
- `engine/cc_xtts.py` — motor XTTS v2: `generate` / `count`, mismo formato de salida
  (mp3 + subtítulos + manifest, incremental y reanudable).
- `engine/cc_service.py` — narra pidiendo cada frase a un servicio de voz, con la API estándar y
  el mismo formato de salida. En este equipo no hace falta GPU ni modelo de voz.
- `st_server.py` — ese servicio con tu XTTS y tus voces, para esta aplicación en otro equipo, la
  app del móvil o cualquier otro cliente ([API](docs/server-api.md)).
- `st_translate.py` + `engine/cc_translate.py` — pantalla y motor de traducción de
  documentos (un lector/escritor por formato; guarda el progreso y reanuda).

El modo *Servidor propio* usa un motor equivalente en el servidor por SSH (no incluido).

## Seguridad

Escaneo continuo con **Trivy** (`.github/workflows/trivy.yml`): vulnerabilidades,
secretos y malas configuraciones en cada push/PR y cada semana, con cada `requirements.txt`. **CodeQL** analiza el código,
los tests corren en CI y cada Action está fijada por commit.

## Contribuir

Consulta [CONTRIBUTING](CONTRIBUTING.md), en inglés. Para avisar en privado de un problema de seguridad, consulta [SECURITY](SECURITY.md).

## Licencia

MIT. Ver [LICENSE](LICENSE). Los componentes y modelos que se descargan aparte conservan
su propia licencia: ver [NOTICE](NOTICE). Historial de versiones: [CHANGELOG](CHANGELOG.md).
