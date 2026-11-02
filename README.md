# Storyteller

[Versión en español](README.es.md)

<p align="center">
  <img src="docs/player.png" width="49%" alt="Player: the sentence being read is highlighted">
  <img src="docs/library.png" width="49%" alt="Library of narrated books">
</p>

Desktop app (PySide6) that turns an **EPUB into a narrated audiobook** with a neural
voice, in the language you choose (17 available with XTTS), and plays it with **synced
subtitles** (the sentence being read is highlighted; click to jump). It also **imports
existing audiobooks** and **transcribes them with Whisper** to make those subtitles, and
**translates books and documents** between 15 languages with TranslateGemma.

Everything runs **locally** (GPU or CPU) or, if you like, on **your own server** over SSH.
Narration can also use **any voice service by URL**: your own or a public one with its key.
**Models and voices are not bundled**: they are **downloaded from inside the app**.

## Features

- **Narrate an EPUB** with neural TTS, choosing the engine in *Models*:
  - **XTTS v2 multilingual** (coqui-tts): reads the whole sentence in one go and
    pronounces foreign words in the text properly (e.g. English inside a Spanish book).
    Clones the voice from a reference recording. Narrates in **17 languages**, chosen in
    *Models*: English, Spanish, French, German, Italian, Portuguese, Dutch, Polish, Czech,
    Hungarian, Turkish, Russian, Arabic, Hindi, Chinese, Japanese and Korean.
  - **F5-TTS** (Spanish or English): one model per language.
  - **A voice service by URL**: your own XTTS with `st_server.py` ([API](docs/server-api.md))
    or a public service with its key, with several **requests at a time**. There your own
    server can also receive new voices and hand finished books to other apps.
- **Pauses that follow the book**: it cuts at every comma, so the pauses after a comma, a
  full stop and a paragraph are set by the punctuation and not left to the model.
- **Parallel chunks** (1, 2 or 3): splits the chapters between several processes on the
  same GPU; with XTTS, 3 chunks narrate about 2.5 times faster.
- **Time left** in the progress bar ("~1 h 10 min left, done around 14:35"), worked out
  from each chunk's real pace.
- **Quality check (QC) with Whisper**: transcribes every narrated sentence and
  regenerates it when words are missing, text is made up or it gets stuck repeating. It also
  measures the audio, because Whisper doesn't hear babbling without words.
- **Text prepared for the voice**: numbers spelled out in the language being narrated,
  acronyms spelled letter by letter, CAPITALS, punctuation, and an editable
  **pronunciation dictionary** (`pron.json`, with a part for each language) for words that
  sound wrong.
- **Import an audiobook** (mp3/m4a/…) and transcribe it with Whisper → timed subtitles.
  Language can be chosen or detected.
- **Player** with synced subtitles, autoscroll, speed control, skips, go to
  chapter/position and a **listening bookmark** (picks up where you left off).
- **Listen while it narrates**: *Listen* in the narration bar opens the book in progress;
  the player adds new sentences and chapters by itself and, when it catches up, waits
  and carries on from the exact spot.
- **Stop and save / Resume** both narration and transcription, even with another model
  (what is already done is kept; they work in blocks and
  save progress; they carry on from the same sentence). Closing the app during a
  narration is the same as *Stop and save*. One job runs at a time: resuming another book
  asks whether to stop the current one, which stays saved, and switch to it.
- **Library**: resume half-done books, **regenerate** one from its original EPUB, and
  delete it.
- **Translate documents** (*Translate…* button) with **TranslateGemma**: EPUB, PDF, Word,
  PowerPoint, Excel, subtitles (SRT/VTT), TXT and Markdown, between the interface
  languages. Paragraphs, headings, tables and subtitle timings are kept (italics or bold
  inside a sentence are lost); a PDF becomes a translated EPUB, without its layout. On
  this computer (llama.cpp, loaded only
  while translating) or on your own server (Ollama over SSH). It can be stopped and
  resumed, even with another model, and the translated EPUB can be sent to narration
  with one click.
- **Model manager** (*Models* screen): download/delete voice models (XTTS v2, F5) and
  transcription models (faster-whisper), with a progress bar (size, speed, time left) and *Cancel*, choose the narration language, parallel chunks and QC,
  and **load voices** from a file or a URL (the reference text is **transcribed
  automatically**). Catalogue of downloadable voices.

<p align="center">
  <img src="docs/translate.png" width="49%" alt="Translate documents screen">
  <img src="docs/models.png" width="49%" alt="Models and voices screen">
</p>

## Requirements

- **Python 3.11, 3.12 or 3.13** with `PySide6` and `PySide6-Addons` (QtMultimedia, ffmpeg
  backend). The PyTorch versions below have no packages for 3.14.
- **ffmpeg**, **ffprobe** and **curl** on the `PATH`.
- F5/Whisper engine in `engine/.venv` (PyTorch, `f5-tts`, `faster-whisper`,
  `num2words`, `beautifulsoup4`, `lxml`). For NVIDIA GPUs, PyTorch with CUDA.
- XTTS engine in a separate venv, `engine/.venv-xtts` (its dependencies clash with
  F5's): `torch==2.8.0` + `torchaudio==2.8.0` + `coqui-tts` + `faster-whisper` (for the
  QC) + `beautifulsoup4` + `lxml` (to read the EPUB), **without** `torchcodec` (torch ≥ 2.9
  requires it and it fails on Windows). To
  narrate in Japanese or Chinese it also needs `cutlet fugashi unidic-lite` or `pypinyin`.
- Translation: installs itself from its own screen (`engine/.venv-translate` and
  `engine/llama.cpp`). In server mode, Ollama on the server.

Model weights and voices are downloaded from *Models* into `~/cuentacuentos/` (they are
not in the repository).

## Installation

The app always looks for its engines, models and books in `~/cuentacuentos`, so the clone
has to go in that folder. In a terminal (PowerShell on Windows):

```sh
cd ~
git clone https://github.com/javimentallab/Storyteller.git cuentacuentos
cd cuentacuentos
python -m pip install PySide6-Essentials PySide6-Addons
```

On Linux/macOS use `python3`. If Linux refuses with `externally-managed-environment`, install
PySide6 in a venv of its own and start the app from the terminal where it is active:

```sh
python3 -m venv ~/storyteller-app
. ~/storyteller-app/bin/activate
python3 -m pip install PySide6-Essentials PySide6-Addons
```

Then the engines, one or both; each takes several GB. Install PyTorch first, as shown, so
that pip does not bring a newer version. Without an NVIDIA GPU, change the `--index-url` to
`https://download.pytorch.org/whl/cpu`.

**F5/Whisper** (F5 voices and transcribing audiobooks). Windows:

```powershell
python -m venv engine\.venv
engine\.venv\Scripts\python -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
engine\.venv\Scripts\python -m pip install f5-tts faster-whisper num2words beautifulsoup4 lxml
```

Linux/macOS:

```sh
python3 -m venv engine/.venv
engine/.venv/bin/python -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
engine/.venv/bin/python -m pip install f5-tts faster-whisper num2words beautifulsoup4 lxml
```

**XTTS** (the multilingual voice). Windows:

```powershell
python -m venv engine\.venv-xtts
engine\.venv-xtts\Scripts\python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
engine\.venv-xtts\Scripts\python -m pip install coqui-tts faster-whisper beautifulsoup4 lxml noisereduce
```

Linux/macOS:

```sh
python3 -m venv engine/.venv-xtts
engine/.venv-xtts/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
engine/.venv-xtts/bin/python -m pip install coqui-tts faster-whisper beautifulsoup4 lxml noisereduce
```

To narrate in Japanese or Chinese, add `cutlet fugashi unidic-lite` or `pypinyin` to that last
command. `noisereduce` cleans the noise from each clip; without it they stay as they come out.

Then start the app and download the models from *Models*. If an engine is missing, that screen
says which one, and it notices a new engine the next time you open it, without restarting.

## Usage

- **Windows:** `Storyteller.bat` (or `pythonw storyteller.py`).
- **Linux/macOS:** `./storyteller.sh` (or `python3 storyteller.py`).

Arguments: `storyteller.py <book.epub>` generates, `--book <folder>` opens a finished one.

**Interface languages (15):** English, Spanish, Portuguese, French, German, Italian,
Dutch, Polish, Russian, Ukrainian, Chinese, Japanese, Korean, Thai and Hindi. It follows
the system language (English if it is not in the list); to force one, use
`--lang xx` (e.g. `--lang de`) or the `STORYTELLER_LANG` environment variable.

## Layout

- `storyteller.py` — the app (library + player + model manager).
- `st_lang.py` + `i18n/<lang>.txt` — interface translations (one `NN|text` line per
  text; checked on load: all texts present and the same `{n}` placeholders).
- `engine/cc_engine.py` — local engine: `download` / `generate` (F5), `count`
  (chapters and sentences, to split chunks and estimate the time), `whisper-download` /
  `whisper` (streaming transcription) and the text preparation shared by the engines.
- `engine/cc_xtts.py` — XTTS v2 engine: `generate` / `count`, same output format
  (mp3 + subtitles + manifest, incremental and resumable).
- `engine/cc_service.py` — narrates by asking a voice service for each sentence, through the
  standard API, same output format. No graphics card or voice model needed on this computer.
- `st_server.py` — that service with your own XTTS and voices, for this app on another computer,
  the mobile app or any other client ([API](docs/server-api.md)).
- `st_translate.py` + `engine/cc_translate.py` — document translation screen and engine
  (one reader/writer per format; saves progress and resumes).

*Own server* mode uses an equivalent engine on the server over SSH (not included).

## Security

Continuous scanning with **Trivy** (`.github/workflows/trivy.yml`): vulnerabilities,
secrets and misconfigurations on every push/PR and every week. **CodeQL** analyzes the code,
the tests run in CI, and every Action is pinned by commit.

## Contributing

See [CONTRIBUTING](CONTRIBUTING.md). To report a security problem privately, see [SECURITY](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE). Components and models that are downloaded separately keep
their own licence: see [NOTICE](NOTICE). Version history: [CHANGELOG](CHANGELOG.md).
