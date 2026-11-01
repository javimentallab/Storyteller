# Changelog

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
