# Changelog

## 1.1 (2026-11-02)

### New
- **Narration in 17 languages**: XTTS and voice services read in the language chosen in *Models*.
  Numbers, decimals and the length of each piece follow that language, and the pronunciation
  dictionary has a part for each one (`langs` in `pron.json`). Japanese needs
  `cutlet fugashi unidic-lite` and Chinese `pypinyin` in the XTTS environment; the app says so.
- **A voice service by URL**: narrate with your own XTTS (`st_server.py`, see
  [its API](docs/server-api.md)) or with a public service. *Requests at a time* (1 to 4) sends several
  sentences at once and writes them in order; the server opens up to 3 XTTS if they fit on the card.
- **`st_server`**: `language` on each request, uploading a voice, and handing out the finished
  chapters of the library to other apps.
- **XTTS on your own server**, not only F5.
- **Pauses that follow the punctuation**: narration cuts at every comma, and you hear about 0.30 s
  after a comma, 0.63 s after a full stop and 0.98 s after a paragraph. A half-done chapter whose
  pieces no longer match is narrated again; finished ones are kept.
- **Download progress**: a bar fixed at the bottom of the *Models* screen shows size, percentage,
  speed and time left, with a *Cancel* button, for every model. The *Models* button in the library
  shows the percentage too.

### Changed
- **F5 and Whisper are optional**: each is a separate step, installed only if you ask for it, so what
  they bring (F5: Unidecode, encodec, frozendict; Whisper: PyAV) stays out otherwise. The base is the
  interface, reading an EPUB and narrating with XTTS or a service by URL. An installation that already
  has them keeps working as it is, with nothing to reinstall.
- **XTTS is the default model** on a new installation.
- **Whisper goes in the environment of the engine that narrates**, because the QC runs in that process,
  and from there it also transcribes; without an engine, in `engine/.venv`. It uses the graphics card
  only if that environment has PyTorch.
- On *Models*, an engine that isn't installed says so on its card, with a *How to install it* button
  that gives the exact commands, instead of a disabled *Download*. Importing an audiobook and narrating
  say the same instead of failing.
- **Exact versions**: `requirements.txt` for the interface and `engine/deps/<engine>/requirements.txt`
  for each engine, also for what the app installs for translation. The README installs from them.
- F5 moves to PyTorch 2.8.0, the same as XTTS.

### Fixed
- **The QC could look as if it was checking when it wasn't**: if Whisper couldn't load, each sentence
  passed as good without a word. The box can now only be ticked if the QC can really run here (a server
  has its own Whisper, so nothing changes there). Narrating with it on but impossible warns before
  starting, and sentences it couldn't check show in the progress bar and in a warning at the end.
- faster-whisper 1.2.1 fails with `av` 19, which pip chose on Python 3.12 and later: transcribing broke
  and the QC stopped checking. `av` is now pinned to 18.1.0.
- *Models* said the engine was missing even with XTTS ready, and Whisper couldn't be downloaded without
  `engine/.venv`, so the QC never worked on an XTTS-only installation. A half-installed XTTS environment
  counted as ready.
- The tests failed without ffmpeg (`test_waits_and_tries_again`), and CI now installs it, so the tests
  that convert audio run there too. `CONTRIBUTING.md` says what to install to run them.
- Books in Russian, Chinese, Japanese, Arabic or any other non-Latin alphabet ended up with nothing
  to narrate. Chinese and Japanese are now also cut where they have no space after the sign.
- Spanish text rules (abbreviations, dollars, «igual a») were applied to every language.
- The quality check passed anything that wasn't in the Latin alphabet, and it missed babbling
  without words: it now also measures the voice after the last word and the length of the clip.
- Resuming an audiobook while something else was running started both at once, sharing one progress
  bar and one *Stop* button, and with two audiobooks the first could no longer be stopped. One job
  runs at a time now, and resuming another book asks whether to stop the current one and switch.
- On a new clone the app said the engine was being installed forever and wouldn't download Whisper.
- The translator keeps the original text when the model's answer is still broken after retrying.
- *Delete* on XTTS v2 only removed an empty folder and left the 1.8 GB model on disk.
- A cut-off XTTS download could look installed. Closing the app during a download now stops it.
- Buttons could be cut off in the library with the window at its smallest during a download.

### Security
- GitHub Actions pinned by commit, CodeQL added, Trivy with SARIF and a weekly run, `SECURITY.md` and
  `CONTRIBUTING.md`. Trivy now sees the Python dependencies, because they are in `requirements.txt` files.
- Fixed by moving F5's environment from PyTorch 2.6.0 to 2.8.0: CVE-2025-2148, CVE-2025-2149, CVE-2025-2953,
  CVE-2025-2998, CVE-2025-3730, CVE-2025-46148, CVE-2025-46149, CVE-2025-46150, CVE-2025-46152,
  CVE-2025-46153, CVE-2025-55553, CVE-2025-55557, CVE-2025-55558 and CVE-2025-55560. To get this on an
  existing installation, run the F5 commands of the README again.
- Fixed by replacing the setuptools 65.5.0 that `python -m venv` leaves on Python 3.11: CVE-2022-40897,
  CVE-2024-6345, CVE-2025-47273, CVE-2026-59890 and GHSA-xmj3-9gf7-h52w.
- Known and still open: PyTorch 2.8.0 (XTTS and F5) has CVE-2025-2999, CVE-2025-3000, CVE-2025-3001,
  CVE-2025-55551, CVE-2025-55552, CVE-2025-55554, CVE-2026-4538 and CVE-2026-24747. CVE-2026-4538 has no
  fix yet; the others need 2.9 or later, which requires torchcodec, and that fails on Windows. The optional F5 environment has
  Pillow 12.3.0 and filelock 3.32.7 with known advisories, because gradio (`pillow<13`) and cached-path
  (`filelock<4`) hold them back.

## 1.0 (2026-11-01)

First tagged version.

### New
- **Translate books and documents** with TranslateGemma: EPUB, Word, PowerPoint, Excel,
  subtitles (SRT/VTT), TXT and Markdown, between the 15 interface languages. A PDF becomes
  a translated EPUB, without its layout. It runs on this computer (llama.cpp) or on your
  own server (Ollama over SSH), and the result can be sent to narration with one click.
- **Unfinished translations** are listed on the screen, even after closing the app.
  *Stop and save* writes a document with what has been translated so far.
- **Change the model halfway**: translation, narration and transcription keep what is
  already done and carry on with the new model.
- **Listen while it narrates**: the player follows the narration as it grows.

### Fixed
- Resuming on your own server a book started on this computer started again from zero and
  deleted what had been narrated locally. It now warns you and does not start.

### Changed
- EPUB files are read without EbookLib, so the app no longer depends on that AGPL library.
- `Storyteller.bat` and the server folder no longer contain fixed paths.
