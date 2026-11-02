#!/usr/bin/env bash
# Lanzador para Linux/macOS. Requiere Python 3.11+ con PySide6 (y PySide6-Addons),
# además de ffmpeg/ffprobe y curl en el PATH. Los motores viven en engine/.venv-xtts y engine/.venv.
cd "$(dirname "$0")"
exec python3 storyteller.py "$@"
