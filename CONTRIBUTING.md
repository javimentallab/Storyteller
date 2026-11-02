# Contributing

Thanks for your interest. Storyteller has a single maintainer, so replies can take a while.

## Bugs and ideas

Open an [issue](https://github.com/javimentallab/Storyteller/issues). Say what you expected, what happened, and which version and system you use. English or Spanish are both fine.

Security problems go through [SECURITY.md](SECURITY.md), not through issues.

## Changing the code

For anything big, open an issue first so we can agree on the approach.

The tests need only what reads an EPUB, in a venv of its own, not the engines or a graphics card:

```sh
python -m venv .venv-tests
.venv-tests/bin/python -m pip install -r engine/deps/base/requirements.txt
.venv-tests/bin/python -m unittest discover -s tests
```

On Windows the Python of that venv is `.venv-tests\Scripts\python`. Tests that convert audio are skipped if `ffmpeg` is
not on the `PATH`, those that narrate if there is no engine in `engine/`, and those of the *Models* screen without PySide6
(`python -m pip install -r requirements.txt`).

- **Tests:** anything important that you add comes with a test.
- **Dependencies:** a new one needs a permissive licence (MIT, BSD, Apache…). Check it before adding it.
- **Private data:** no personal paths, hostnames, IP addresses or tokens in the code, the history or the screenshots.
- **Interface text:** new text goes in every language, and has to fit on screen in all of them. Texts are in `i18n/<language>.txt`, and the source list in `st_lang.py`.
- **Licence:** your contribution is released under the project's [MIT licence](LICENSE).
