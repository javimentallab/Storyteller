# Changelog

## 1.3.1 (2026-11-07)

### Security
- XTTS and F5 move from PyTorch 2.8.0 to 2.10.0, with torchaudio 2.10.0. Of those listed in 1.1 as known and not
  fixed, this fixes CVE-2026-24747 (8.8), CVE-2025-55551 (7.5), CVE-2025-55552 (7.5), CVE-2025-55554,
  CVE-2025-2999 and CVE-2025-3001.
- Still known and not fixed in PyTorch 2.10.0:
  - CVE-2026-4538 (7.8), when loading a pt2 archive, which neither Storyteller nor its engines do. OSV gives 2.10.0
    as the last version affected, but PyTorch 2.11 requires setuptools older than 82, and going back would reopen
    CVE-2026-59890 and GHSA-xmj3-9gf7-h52w, fixed in 1.1.
  - CVE-2025-3000, rated low, fixed in PyTorch 2.13. Builds of PyTorch 2.12 and later are not published for CUDA
    12.8, the one in the README.
- To get this on an existing installation, run the XTTS or F5 commands of the README again.

### Changed
- From PyTorch 2.9, torchaudio reads audio with torchcodec, which on Windows needs the shared libraries of FFmpeg.
  XTTS and F5 now read the reference voice with soundfile instead. torchcodec is still installed, because coqui-tts
  does not start without it, but nothing in Storyteller uses it.

## 1.3.0 (2026-11-07)

### Added
- **Check for updates**, under the Storyteller name in the library. It asks GitHub for the latest release only
  when you press it and compares versions properly (1.10 is newer than 1.9). After asking, it downloads the
  release and replaces only the program files: settings, books, the progress of each narration, voices, models
  and engines stay as they are and are not downloaded again. A git clone is updated with git instead: the release
  tag is fetched from the official repository and the clone moves forward to it with a fast-forward only. A clone
  with uncommitted changes or commits of its own is left untouched, and the app offers the release page. It does
  not update while a narration, transcription, translation or download is in progress. See
  [Updating](README.md#updating).
- **The page you are on.** The player shows the page being narrated and how much of the book is behind you, for
  example 'Page 84 of 453 · 19 % of the book'. When the EPUB carries the page numbers of the printed book, those
  are used, Roman numerals included; otherwise pages of a fixed length are counted and marked as approximate.
- **The text follows the voice.** If you scroll away by hand, the player stops following the narration and goes
  back to the line being read after five seconds without touching it.

### Changed
- **Narration reads on instead of line by line.** Each sentence now goes to the voice whole, and XTTS makes
  the pauses at its commas; before, every comma was a separate piece with its own closing tone and a fixed
  silence. The pauses after a full stop (about 0.63 s) and a paragraph (about 0.98 s) still come from the book.
  A sentence longer than the model's limit (220 characters in Spanish, about 92 % of what XTTS takes in each
  language) is split at the comma nearest that limit. The subtitles show a sentence a line.
- XTTS no longer splits a piece again on its own, which added 0.42 s of silence inside it, and each piece now
  fades in and out over a few milliseconds instead of stopping dead against the silence. A short last word after
  a comma («…, dijo él») is no longer taken for a made-up ending and cut.
- **Books started before**: finished chapters stay as they are. A chapter left halfway is narrated again from its
  start with the new pieces, so that no chapter changes its way of reading in the middle.
- **XTTS voice without the noise filter and without squashed peaks.** The output is no longer run through
  noise reduction: it lowered the voice by about 8 dB, unevenly from one piece to the next, and dulled the high
  notes. XTTS ends in a `tanh` that flattens the loudest moments; it is undone and the level lowered so that
  emphasis does not sound saturated. Narration, `st_server` and the server engine sound the same. `noisereduce` is
  no longer installed.
- In Spanish, a piece no longer goes to XTTS with a final full stop: with it, more endings came with a made-up tail
  (6 in 80 pieces against 1 without it). Other languages keep it.
- With quality control on, **a made-up tail after the last word is cut** at the silence before it. Only at a real
  silence: where Whisper says the last word ends is up to half a second early, and cutting there would take half
  of the word. If the voice runs on to the end, nothing is cut and quality control decides as before.

### Fixed
- **Resuming no longer throws away chapters that were already narrated.** When a book had finished chapters further
  on (narrated in parallel streams, or on your own server) and you stopped before the narration reached them, they
  dropped out of the book's index; the next time you resumed, their audio was deleted and narrated again.
- **Changing the number of parallel streams before resuming keeps what is narrated.** Going from one stream to
  several treated the whole book as new, and index files left over from an earlier run could overwrite newer
  progress, also when the book went on on your own server.
- Your place in a book is no longer reset to the start just by opening and closing it while its chapter is missing
  from the index, and it is saved in one piece.

## 1.2.1 (2026-11-03)

### Fixed
- **A book no longer disappears from the library when it comes back from your own server.** With a chapter
  open in the player, Windows didn't let the app replace the book's folder: everything else had already been
  deleted, and without its index the book stopped showing up. Now the files are installed one by one and the index
  goes last, so the library always sees a whole book, and your place in it is kept. If the chapter you're listening
  to is different in what arrives, the app waits until you stop listening to it.
- If this already happened to you, the book is still in `books/.incoming-<name>`: close the app and move those files
  into `books/<name>`, keeping the `playback.json` that is there.

## 1.2 (2026-11-02)

### New
- **Quality check with a voice service by URL**: what arrives is checked with a Whisper by URL, with its own key, or
  with the Whisper in the XTTS or F5 environment on this computer, and a sentence that sounds wrong is asked for up to 2 more times. Without
  numpy in the environment, the check by URL doesn't measure extra voice at the end of a sentence, and the screen
  says so.

### Changed
- **Qt's own buttons in your language**: Yes, No, OK and Cancel follow the language of the interface in all
  15, and so does Qt's copy-and-paste menu where Qt has a translation.
- **Install commands aren't broken across lines**: they come in their own box, with a *Copy* button.
- **The list of audio outputs shows each device's whole name**, and the closed box says it in its tooltip.
- The screen no longer mentions "voice A": that voice is called *Narrator*.

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
- **F5 is installed without gradio** (`pip install --no-deps`). It is only F5's web interface, which Storyteller doesn't
  use, and together with cached-path it held back Pillow and filelock. `pip check` complains about gradio and cached-path:
  that is expected.

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
  CVE-2025-46153, CVE-2025-55553, CVE-2025-55557, CVE-2025-55558 and CVE-2025-55560.
- Fixed by moving F5's environment from Pillow 12.3.0 to 13.0.0: CVE-2026-96636, CVE-2026-96637, CVE-2026-96638,
  CVE-2026-96639, CVE-2026-96640, CVE-2026-96641, CVE-2026-96642, CVE-2026-96643, CVE-2026-96644, CVE-2026-96778,
  CVE-2026-112390, CVE-2026-112391, CVE-2026-112392, CVE-2026-112393, CVE-2026-112394, CVE-2026-112395, CVE-2026-112396,
  CVE-2026-112397, CVE-2026-112398, CVE-2026-112399, CVE-2026-112400, CVE-2026-112401, CVE-2026-112402, CVE-2026-112403,
  CVE-2026-112404, CVE-2026-112405, CVE-2026-112406, CVE-2026-112891, CVE-2026-112892, CVE-2026-112893, CVE-2026-112894,
  CVE-2026-112895, CVE-2026-112896, CVE-2026-112897, CVE-2026-112898, CVE-2026-112899, CVE-2026-112900, CVE-2026-112901,
  CVE-2026-112902, CVE-2026-114501 and GHSA-78g4-xqwv-q3g8.
- Fixed by moving F5's environment from filelock 3.32.7 to 4.2.4: CVE-2026-112907, CVE-2026-112908, CVE-2026-112909,
  CVE-2026-112910, CVE-2026-112911, CVE-2026-112912, CVE-2026-112913, CVE-2026-112915, CVE-2026-112935, CVE-2026-112936,
  CVE-2026-112938, CVE-2026-112939 and CVE-2026-112940.
- To get these on an existing F5 installation, run the F5 commands of the README again.
- Fixed by replacing the setuptools 65.5.0 that `python -m venv` leaves on Python 3.11: CVE-2022-40897,
  CVE-2024-6345, CVE-2025-47273, CVE-2026-59890 and GHSA-xmj3-9gf7-h52w.
- `st_server` wrote an uploaded voice, up to 32 MB, to a temporary file before checking the key. It now
  checks the key first: without a valid one nothing reaches the disk, and the body is read and thrown away,
  up to that limit, only so that the client sees the 401.
- **Known and not fixed: PyTorch 2.8.0**, used by XTTS and F5. The ones with a high score (CVSS, from OSV):
  - CVE-2026-24747 (8.8), fixed in PyTorch 2.10.
  - CVE-2026-4538 (7.8), with no fix published in any version.
  - CVE-2025-55551 (7.5) and CVE-2025-55552 (7.5), fixed in PyTorch 2.9.

  Also CVE-2025-55554, CVE-2025-2999, CVE-2025-3000 and CVE-2025-3001. Storyteller stays on 2.8.0 because
  PyTorch 2.9 and later require torchcodec, and that fails on Windows. Trivy does not rate these as high or
  critical, so they don't stop its check; they are listed here so that they aren't hidden.

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
