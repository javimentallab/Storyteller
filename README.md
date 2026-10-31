# Storyteller

[Versión en español](README.es.md)

Desktop app (PySide6) that turns an **EPUB into a narrated audiobook** with a neural
voice and plays it with **synced subtitles** (the sentence being read is highlighted;
click to jump). It also **imports existing audiobooks** and **transcribes them with
Whisper** to make those subtitles.

Everything runs **locally** (GPU or CPU) or, if you like, on **your own server** over SSH.
**Models and voices are not bundled**: they are **downloaded from inside the app**.

## Features

- **Narrate an EPUB** with neural TTS, choosing the engine in *Models*:
  - **XTTS v2 multilingual** (coqui-tts): reads the whole sentence in one go and
    pronounces foreign words in the text properly (e.g. English inside a Spanish book).
    Clones the voice from a reference recording.
  - **F5-TTS** (Spanish or English): one model per language.
- **Parallel chunks** (1, 2 or 3): splits the chapters between several processes on the
  same GPU; with XTTS, 3 chunks narrate about 2.5 times faster.
- **Time left** in the progress bar ("~1 h 10 min left, done around 14:35"), worked out
  from each chunk's real pace.
- **Quality check (QC) with Whisper**: transcribes every narrated sentence and
  regenerates it when words are missing, text is made up or it gets stuck repeating.
- **Text prepared for the voice**: numbers spelled out in the model's language,
  acronyms spelled letter by letter, CAPITALS, punctuation, and an editable
  **pronunciation dictionary** (`pron.json`) for words that sound wrong.
- **Import an audiobook** (mp3/m4a/…) and transcribe it with Whisper → timed subtitles.
  Language can be chosen or detected.
- **Player** with synced subtitles, autoscroll, speed control, skips, go to
  chapter/position and a **listening bookmark** (picks up where you left off).
- **Stop and save / Resume** both narration and transcription (they work in blocks and
  save progress; they carry on from the same sentence). Closing the app during a
  narration is the same as *Stop and save*.
- **Library**: resume half-done books, **regenerate** one from its original EPUB, and
  delete it.
- **Model manager** (*Models* screen): download/delete voice models (XTTS v2, F5) and
  transcription models (faster-whisper), choose the language, parallel chunks and QC,
  and **load voices** from a file or a URL (the reference text is **transcribed
  automatically**). Catalogue of downloadable voices.

## Requirements

- **Python 3.11+** with `PySide6` and `PySide6-Addons` (QtMultimedia, ffmpeg backend).
- **ffmpeg**, **ffprobe** and **curl** on the `PATH`.
- F5/Whisper engine in `engine/.venv` (PyTorch, `f5-tts`, `faster-whisper`,
  `num2words`). For NVIDIA GPUs, PyTorch with CUDA.
- XTTS engine in a separate venv, `engine/.venv-xtts` (its dependencies clash with
  F5's): `torch==2.8.0` + `torchaudio==2.8.0` + `coqui-tts` + `faster-whisper` (for the
  QC), **without** `torchcodec` (torch ≥ 2.9 requires it and it fails on Windows).

Model weights and voices are downloaded from *Models* into `~/cuentacuentos/` (they are
not in the repository).

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

*Own server* mode uses an equivalent engine on the server over SSH (not included).

## Security

Continuous scanning with **Trivy** (`.github/workflows/trivy.yml`): vulnerabilities,
secrets and misconfigurations on every push/PR.

## License

MIT. See [LICENSE](LICENSE).
