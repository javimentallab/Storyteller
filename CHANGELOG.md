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

### Fixed
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
  `CONTRIBUTING.md`. No known vulnerability was fixed in this version.

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
