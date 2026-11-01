# -*- coding: utf-8 -*-
"""
Storyteller — pantalla «Traducir documentos».

Traduce EPUB, DOCX, PPTX, XLSX, SRT/VTT, TXT y MD entre los idiomas de la app con
TranslateGemma (un PDF se convierte en un EPUB traducido, sin su maquetación), en este equipo (llama.cpp, arrancado solo mientras traduce) o en el
servidor propio (Ollama por SSH). El trabajo lo hace engine/cc_translate.py en su propio
venv (engine/.venv-translate), que esta pantalla instala la primera vez.
El EPUB traducido (también el que sale de un PDF) se puede mandar a narrar.
"""
import os, re, sys, json, time, shutil, hashlib, threading, subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox, QScrollArea,
    QFileDialog, QProgressBar, QFrame, QMessageBox,
)

import st_lang
from st_lang import _

HERE       = Path(__file__).resolve().parent
HOME_CC    = Path.home() / "cuentacuentos"
ENGINE     = HERE / "engine" / "cc_translate.py"
_VENV      = HOME_CC / "engine" / ".venv-translate"
VPY        = _VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
LLAMA_DIR  = HOME_CC / "engine" / "llama.cpp"
MODELS_DIR = HOME_CC / "models"
WORK       = HOME_CC / "translations"            # progreso guardado (reanudar) y señales de parada
SETTINGS   = HOME_CC / "translate.json"
CONFIG     = HOME_CC / "config.json"             # solo se LEE (servidor): la escribe la app
NOWIN      = 0x08000000 if os.name == "nt" else 0

# Versiones fijadas: maduras, sin CVE conocidas y con licencia permisiva (el proyecto es MIT:
# nada de GPL/AGPL, ni siquiera instalado aparte; por eso no se usa PyMuPDF).
REQS = ["beautifulsoup4==4.15.0", "lxml==6.1.3", "python-docx==1.2.0", "python-pptx==1.0.2",
        "openpyxl==3.1.5", "pdfminer.six==20260107"]

# llama.cpp fijado a una versión probada (no la última del día).
LLAMA_BUILD = "b11803"
_LB = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/"
if os.name == "nt":
    LLAMA_FILES = [f"llama-{LLAMA_BUILD}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
elif sys.platform == "darwin":
    LLAMA_FILES = [f"llama-{LLAMA_BUILD}-bin-macos-arm64.tar.gz"]
else:
    LLAMA_FILES = [f"llama-{LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz"]

_HF = "https://huggingface.co/mradermacher/"


def _sz(v):
    """Tamaño con el separador decimal del idioma ("2,5 GB" / "2.5 GB")."""
    return v.replace(",", ".") if st_lang.LANG in ("en", "ja", "ko", "zh", "hi", "th") else v


TR_MODELS = [
    {"id": "translategemma-4b", "name": "TranslateGemma 4B", "size": _sz("2,5 GB"),
     "file": "translategemma-4b-it.Q4_K_M.gguf",
     "url": _HF + "translategemma-4b-it-GGUF/resolve/main/translategemma-4b-it.Q4_K_M.gguf",
     "ollama": "translategemma:4b",
     "desc": _("Rápido. Cabe en tarjetas gráficas de 6 GB.")},
    {"id": "translategemma-12b", "name": "TranslateGemma 12B", "size": _sz("7,3 GB"),
     "file": "translategemma-12b-it.Q4_K_M.gguf",
     "url": _HF + "translategemma-12b-it-GGUF/resolve/main/translategemma-12b-it.Q4_K_M.gguf",
     "ollama": "translategemma:12b",
     "desc": _("Traduce mejor. Pide una tarjeta gráfica de 10-12 GB.")},
]

TR_LANGS = [("en", _("Inglés")), ("es", _("Español")), ("de", _("Alemán")), ("fr", _("Francés")),
            ("it", _("Italiano")), ("pt", _("Portugués")), ("nl", _("Neerlandés")), ("pl", _("Polaco")),
            ("ru", _("Ruso")), ("uk", _("Ucraniano")), ("ja", _("Japonés")), ("ko", _("Coreano")),
            ("zh", _("Chino")), ("hi", _("Hindi")), ("th", _("Tailandés")), ("ca", _("Catalán"))]

EXTS = ("epub", "pdf", "docx", "pptx", "xlsx", "srt", "vtt", "txt", "md")


def load_settings():
    tgt = st_lang.LANG if any(c == st_lang.LANG for c, _n in TR_LANGS) else "es"
    s = {"src": "en" if tgt != "en" else "es", "tgt": tgt, "where": "local", "model": TR_MODELS[0]["id"]}
    try:
        s.update(json.loads(SETTINGS.read_text("utf-8")))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(s), encoding="utf-8")
    except Exception:
        pass


def server():
    try:
        return (json.loads(CONFIG.read_text("utf-8")).get("server") or "").strip()
    except Exception:
        return ""


def model_by_id(mid):
    return next((m for m in TR_MODELS if m["id"] == mid), TR_MODELS[0])


def gguf_path(m):
    return MODELS_DIR / m["id"] / m["file"]


def find_llama_server():
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    if (LLAMA_DIR / name).exists():
        return LLAMA_DIR / name
    hits = sorted(LLAMA_DIR.rglob(name)) if LLAMA_DIR.exists() else []
    return hits[0] if hits else None


def venv_ready():
    return VPY.exists() and (_VENV / ".ready").exists()


def local_ready(m):
    return venv_ready() and find_llama_server() is not None and gguf_path(m).exists()


def job_key(path, src, tgt, model_tag):
    st = Path(path).stat()
    raw = f"{Path(path).resolve()}|{st.st_size}|{int(st.st_mtime)}|{src}|{tgt}|{model_tag}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def meta_of(cache):
    """Ficha de una traducción (documento, destino, idiomas, modelo), junto a su progreso."""
    return Path(cache).with_suffix(".meta.json")


def read_meta(cache):
    try:
        return json.loads(meta_of(cache).read_text("utf-8"))
    except Exception:
        return None


def write_meta(cache, meta):
    meta_of(cache).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def forget_job(cache):
    Path(cache).unlink(missing_ok=True)
    meta_of(cache).unlink(missing_ok=True)


def unfinished_jobs():
    """Traducciones a medias: las que tienen ficha y algo traducido, la más reciente primero."""
    jobs = []
    files = sorted(WORK.glob("*.meta.json"), key=lambda f: f.stat().st_mtime, reverse=True) if WORK.exists() else []
    for mf in files:
        cache = WORK / (mf.name[:-len(".meta.json")] + ".json")
        try:
            meta, done = json.loads(mf.read_text("utf-8")), len(json.loads(cache.read_text("utf-8")))
        except Exception:
            done, meta = 0, {}
        if done:
            jobs.append({**meta, "cache": str(cache), "done": done})
        else:
            forget_job(cache)              # sin nada traducido no hay nada que reanudar
    return jobs


def base_python():
    """Python con el que crear el venv (el de la app; python.exe en vez de pythonw.exe)."""
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def fmt_left(sec):
    sec = int(sec)
    if sec < 60:
        return _("menos de 1 min")
    if sec < 3600:
        return _("~{0} min").format(round(sec / 60))
    return _("~{0} h {1} min").format(sec // 3600, round(sec % 3600 / 60))


# ------------------------------------------------------------------- workers
class InstallWorker(QThread):
    """Instala lo que falte: venv del traductor, llama.cpp (solo local) y el modelo (solo local)."""
    progress = Signal(str, int)
    done = Signal()
    failed = Signal(str)

    def __init__(self, model, local=True):
        super().__init__()
        self.model, self.local, self.proc, self._stop = model, local, None, False

    def stop(self, save=True):
        self._stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self, cmd, label):
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", creationflags=NOWIN)
        tail = []
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail = (tail + [line])[-6:]
            if line.startswith("DL "):
                parts = line.split(" ", 2)
                pct = int(parts[1]) if parts[1].isdigit() else -1
                self.progress.emit(label + (" — " + parts[2] if len(parts) > 2 else ""), pct)
        self.proc.wait()
        if self._stop:
            raise RuntimeError(_("Cancelado."))
        if self.proc.returncode != 0:
            raise RuntimeError("\n".join(tail))

    def run(self):
        try:
            if not venv_ready():
                self.progress.emit(_("Preparando el motor de traducción (una sola vez)…"), -1)
                if not VPY.exists():
                    self._run([base_python(), "-m", "venv", str(_VENV)], "venv")
                self._run([str(VPY), "-m", "pip", "install", "--disable-pip-version-check", "-q"] + REQS,
                          "pip")
                (_VENV / ".ready").write_text("ok", encoding="utf-8")
            if self.local and find_llama_server() is None:
                for f in LLAMA_FILES:
                    dest = WORK / "dl" / f
                    self._run([str(VPY), "-I", str(ENGINE), "download", "--url", _LB + f, "--dest", str(dest)],
                              _("Descargando llama.cpp…"))
                    self._run([str(VPY), "-I", str(ENGINE), "unzip", "--zip", str(dest), "--dest",
                               str(LLAMA_DIR)], "unzip")
                    dest.unlink(missing_ok=True)
            if self.local and not gguf_path(self.model).exists():
                self._run([str(VPY), "-I", str(ENGINE), "download", "--url", self.model["url"], "--dest",
                           str(gguf_path(self.model))],
                          _("Descargando {0} ({1})…").format(self.model["name"], self.model["size"]))
            self.done.emit()
        except Exception as e:
            self.failed.emit(str(e))


class TranslateWorker(QThread):
    progress = Signal(str, int)
    done = Signal(list)
    failed = Signal(str)
    stopped = Signal(list)

    def __init__(self, args, cache, stop_file):
        super().__init__()
        self.args, self.cache, self.stop_file = args, cache, stop_file
        self.proc, self._cancel, self._stopping = None, False, False

    def stop(self, save=True):
        if save:
            self._stopping = True
            Path(self.stop_file).write_text("stop", encoding="utf-8")   # para entre trozos y guarda
            # Si sigue cargando el modelo y no llega a mirar la señal, se corta a los 2 min
            # (lo traducido ya está guardado; solo se pierde el trozo en curso).
            t = threading.Timer(120, lambda: self.proc and self.proc.poll() is None and self.proc.terminate())
            t.daemon = True
            t.start()
            return
        self._cancel = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def run(self):
        outs, err, stopped, tail = [], "", False, []
        times = []
        try:
            Path(self.stop_file).unlink(missing_ok=True)
            cmd = [str(VPY), "-I", str(ENGINE), "translate"] + self.args + \
                  ["--cache", str(self.cache), "--stop-file", str(self.stop_file)]
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", creationflags=NOWIN)
            for line in self.proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                tail = (tail + [line])[-8:]
                tag, _sp, rest = line.partition(" ")
                if tag == "INFO":
                    self.progress.emit(self._info(rest), -1)
                elif tag == "TOTAL":
                    meta = read_meta(self.cache)
                    if meta is not None:
                        write_meta(self.cache, {**meta, "total": int(rest)})
                elif tag == "PULL":
                    self.progress.emit(_("Descargando el modelo en el servidor… {0} %").format(rest), int(rest or 0))
                elif tag == "SEG":
                    i, n = map(int, rest.split())
                    now = time.time()
                    times = (times + [(now, i)])[-30:]
                    txt = _("Traduciendo… {0}/{1}").format(i, n)
                    if len(times) >= 4 and times[-1][1] > times[0][1]:
                        rate = (times[-1][1] - times[0][1]) / (times[-1][0] - times[0][0])
                        txt += " — " + _("quedan {0}").format(fmt_left((n - i) / max(rate, 1e-6)))
                    self.progress.emit(txt, int(100 * i / max(1, n)))
                elif tag == "OUT":
                    outs.append(rest)
                elif tag == "STOPPED":
                    stopped = True
                elif tag == "ERROR":
                    err = rest.replace(" | ", "\n")
            self.proc.wait()
        except Exception as e:
            err = str(e)
        if self._cancel:
            forget_job(self.cache)
            self.stopped.emit([])
        elif stopped or self._stopping:
            self.stopped.emit(outs)
        elif err or self.proc is None or self.proc.returncode != 0:
            self.failed.emit(err or "\n".join(tail))        # lo traducido queda en «a medias»
        else:
            forget_job(self.cache)
            self.done.emit(outs)
        Path(self.stop_file).unlink(missing_ok=True)

    @staticmethod
    def _info(t):
        known = {"leyendo el documento": _("Leyendo el documento…"),
                 "arrancando el modelo": _("Cargando el modelo…"),
                 "arrancando el modelo (en CPU)": _("Cargando el modelo (en CPU, irá lento)…"),
                 "guardando": _("Guardando el documento traducido…")}
        if t in known:
            return known[t]
        if t.startswith("conectando con"):
            return _("Conectando con el servidor…")
        if t.startswith("descargando"):
            return _("Descargando el modelo en el servidor…")
        return t


# ------------------------------------------------------------------- pantalla
class TranslatePage(QWidget):
    back = Signal()
    narrate = Signal(str)          # ruta de un EPUB para narrarlo

    def __init__(self):
        super().__init__()
        self.s = load_settings()
        self.inp = ""
        self.worker = None
        self.result = []
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        head = QWidget(); head.setObjectName("head")
        hb = QHBoxLayout(head); hb.setContentsMargins(10, 8, 16, 8); hb.setSpacing(10)
        self.backBtn = QPushButton(_("‹ Biblioteca")); self.backBtn.setObjectName("tb")
        self.backBtn.setCursor(Qt.PointingHandCursor); self.backBtn.clicked.connect(self._back)
        ttl = QLabel(_("Traducir documentos")); ttl.setObjectName("word2")
        hb.addWidget(self.backBtn); hb.addWidget(ttl); hb.addStretch(1)
        root.addWidget(head)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setObjectName("reader")
        inner = QWidget(); col = QVBoxLayout(inner)
        col.setContentsMargins(16, 16, 16, 16); col.setSpacing(10)

        # traducciones a medias (sobreviven a cerrar la app)
        self.jobsTitle = QLabel(_("Traducciones a medias")); self.jobsTitle.setObjectName("sec")
        col.addWidget(self.jobsTitle)
        self.jobsBox = QVBoxLayout(); self.jobsBox.setSpacing(8)
        col.addLayout(self.jobsBox)

        # documento
        sec = QLabel(_("Documento")); sec.setObjectName("sec"); col.addWidget(sec)
        frow = QHBoxLayout(); frow.setSpacing(10)
        self.pickBtn = QPushButton(_("Elegir archivo…")); self.pickBtn.setObjectName("tb")
        self.pickBtn.setCursor(Qt.PointingHandCursor); self.pickBtn.clicked.connect(self._pick)
        self.fileLbl = QLabel(_("EPUB, PDF, Word, PowerPoint, Excel, subtítulos (SRT/VTT), TXT o Markdown"))
        self.fileLbl.setObjectName("booksub"); self.fileLbl.setWordWrap(True)
        frow.addWidget(self.pickBtn); frow.addWidget(self.fileLbl, 1)
        col.addLayout(frow)

        # idiomas
        sec = QLabel(_("Idiomas")); sec.setObjectName("sec"); col.addWidget(sec)
        lrow = QHBoxLayout(); lrow.setSpacing(10)
        self.srcSel, self.tgtSel = QComboBox(), QComboBox()
        for cb in (self.srcSel, self.tgtSel):
            for code, name in TR_LANGS:
                cb.addItem(name, code)
        self.srcSel.setCurrentIndex(max(0, self.srcSel.findData(self.s["src"])))
        self.tgtSel.setCurrentIndex(max(0, self.tgtSel.findData(self.s["tgt"])))
        swap = QPushButton("⇄"); swap.setObjectName("tb"); swap.setCursor(Qt.PointingHandCursor)
        swap.setToolTip(_("Intercambiar idiomas")); swap.clicked.connect(self._swap)
        l1 = QLabel(_("De:")); l1.setObjectName("booksub"); l2 = QLabel(_("A:")); l2.setObjectName("booksub")
        lrow.addWidget(l1); lrow.addWidget(self.srcSel); lrow.addWidget(swap)
        lrow.addWidget(l2); lrow.addWidget(self.tgtSel); lrow.addStretch(1)
        col.addLayout(lrow)
        self.srcSel.currentIndexChanged.connect(self._opts_changed)
        self.tgtSel.currentIndexChanged.connect(self._opts_changed)

        # dónde y con qué modelo
        sec = QLabel(_("Modelo")); sec.setObjectName("sec"); col.addWidget(sec)
        wrow = QHBoxLayout(); wrow.setSpacing(10)
        wl = QLabel(_("Traducir en:")); wl.setObjectName("booksub")
        self.whereSel = QComboBox()
        self.whereSel.addItem(_("Este equipo (local)"), "local")
        self.whereSel.addItem(_("Servidor propio"), "remote")
        self.whereSel.setCurrentIndex(max(0, self.whereSel.findData(self.s["where"])))
        self.modelSel = QComboBox()
        for m in TR_MODELS:
            self.modelSel.addItem(f'{m["name"]} ({m["size"]})', m["id"])
        self.modelSel.setCurrentIndex(max(0, self.modelSel.findData(self.s["model"])))
        wrow.addWidget(wl); wrow.addWidget(self.whereSel); wrow.addWidget(self.modelSel); wrow.addStretch(1)
        col.addLayout(wrow)
        self.whereSel.currentIndexChanged.connect(self._opts_changed)
        self.modelSel.currentIndexChanged.connect(self._opts_changed)

        card = QFrame(); card.setObjectName("bookcard")
        cv = QVBoxLayout(card); cv.setContentsMargins(14, 10, 14, 10); cv.setSpacing(6)
        self.modelInfo = QLabel(""); self.modelInfo.setObjectName("booksub"); self.modelInfo.setWordWrap(True)
        self.modelInfo.setStyleSheet("background: transparent;")
        mrow = QHBoxLayout(); mrow.setSpacing(8)
        self.dlBtn = QPushButton(_("Descargar")); self.dlBtn.setObjectName("tb")
        self.delBtn = QPushButton(_("Borrar")); self.delBtn.setObjectName("tb")
        for b in (self.dlBtn, self.delBtn):
            b.setCursor(Qt.PointingHandCursor)
        self.dlBtn.clicked.connect(self._install)
        self.delBtn.clicked.connect(self._delete_model)
        mrow.addWidget(self.modelInfo, 1); mrow.addWidget(self.dlBtn); mrow.addWidget(self.delBtn)
        cv.addLayout(mrow)
        col.addWidget(card)

        note = QLabel(_("Se conservan párrafos, títulos, tablas y listas; las cursivas o negritas sueltas "
                        "dentro de una frase se pierden. Un PDF se convierte en un EPUB traducido, sin su "
                        "maquetación, listo para narrar."))
        note.setObjectName("booksub"); note.setWordWrap(True)
        col.addWidget(note)

        brow = QHBoxLayout(); brow.addStretch(1)
        self.goBtn = QPushButton(_("Traducir")); self.goBtn.setObjectName("open")
        self.goBtn.setCursor(Qt.PointingHandCursor); self.goBtn.clicked.connect(self._go)
        brow.addWidget(self.goBtn)
        col.addLayout(brow)

        # resultado
        self.resCard = QFrame(); self.resCard.setObjectName("bookcard")
        rv = QVBoxLayout(self.resCard); rv.setContentsMargins(14, 10, 14, 10); rv.setSpacing(8)
        self.resLbl = QLabel(""); self.resLbl.setWordWrap(True); self.resLbl.setObjectName("booksub")
        self.resLbl.setStyleSheet("background: transparent;")
        self.resLbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rrow = QHBoxLayout(); rrow.setSpacing(8); rrow.addStretch(1)
        self.folderBtn = QPushButton(_("Abrir carpeta")); self.folderBtn.setObjectName("tb")
        self.narrBtn = QPushButton(_("Narrar este libro")); self.narrBtn.setObjectName("open")
        for b in (self.folderBtn, self.narrBtn):
            b.setCursor(Qt.PointingHandCursor)
        self.folderBtn.clicked.connect(self._open_folder)
        self.narrBtn.clicked.connect(self._narrate)
        rrow.addWidget(self.folderBtn); rrow.addWidget(self.narrBtn)
        rv.addWidget(self.resLbl); rv.addLayout(rrow)
        self.resCard.hide()
        col.addWidget(self.resCard)
        col.addStretch(1)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        # progreso
        self.progWrap = QWidget(); self.progWrap.setObjectName("progwrap")
        pv = QVBoxLayout(self.progWrap); pv.setContentsMargins(16, 10, 16, 12); pv.setSpacing(7)
        self.progLabel = QLabel(""); self.progLabel.setObjectName("proglabel"); self.progLabel.setWordWrap(True)
        self.prog = QProgressBar(); self.prog.setTextVisible(False); self.prog.setFixedHeight(6)
        pv.addWidget(self.progLabel); pv.addWidget(self.prog)
        pb = QHBoxLayout(); pb.setSpacing(8); pb.addStretch(1)
        self.stopBtn = QPushButton(_("Detener y guardar")); self.stopBtn.setObjectName("tb")
        self.cancelBtn = QPushButton(_("Cancelar")); self.cancelBtn.setObjectName("tb")
        for b in (self.stopBtn, self.cancelBtn):
            b.setCursor(Qt.PointingHandCursor)
        self.stopBtn.clicked.connect(lambda: self._stop(True))
        self.cancelBtn.clicked.connect(lambda: self._stop(False))
        pb.addWidget(self.stopBtn); pb.addWidget(self.cancelBtn)
        pv.addLayout(pb)
        self.progWrap.hide()
        root.addWidget(self.progWrap)
        self.refresh()

    # ---- estado
    def _model(self):
        return model_by_id(self.modelSel.currentData())

    def _remote(self):
        return self.whereSel.currentData() == "remote"

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def refresh(self):
        m, srv = self._model(), server()
        if self._remote():
            if srv:
                info = _("Se usa {0} en el servidor {1} (Ollama). Si no lo tiene, lo descarga allí la "
                         "primera vez.").format(m["ollama"], srv)
            else:
                info = _("No hay servidor configurado. Ponlo en «Modelos» (usuario@host).")
            self.dlBtn.setVisible(not venv_ready())
            self.dlBtn.setText(_("Instalar motor"))
            self.delBtn.hide()
        else:
            if local_ready(m):
                info = m["name"] + " — " + m["desc"] + "  " + _("Descargado.")
            else:
                info = m["name"] + " — " + m["desc"] + "  " + _("No descargado ({0}).").format(m["size"])
            self.dlBtn.setText(_("Descargar"))
            self.dlBtn.setVisible(not local_ready(m))
            self.delBtn.setVisible(gguf_path(m).exists())
        self.modelInfo.setText(info)
        ready = bool(self.inp) and (venv_ready() and bool(srv) if self._remote() else local_ready(m))
        self.goBtn.setEnabled(ready and not self.busy())
        self.goBtn.setText(_("Reanudar traducción") if self.inp and self._cache().exists() else _("Traducir"))
        self._refresh_jobs()

    def _clear_jobs(self):
        while self.jobsBox.count():
            it = self.jobsBox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.jobsTitle.hide()

    def _refresh_jobs(self):
        self._clear_jobs()
        jobs = [] if self.busy() else unfinished_jobs()
        self.jobsTitle.setVisible(bool(jobs))
        for j in jobs:
            self.jobsBox.addWidget(self._job_card(j))

    def _job_card(self, j):
        names = dict(TR_LANGS)
        f = QFrame(); f.setObjectName("bookcard")
        row = QHBoxLayout(f); row.setContentsMargins(14, 10, 10, 10); row.setSpacing(10)
        tx = QVBoxLayout(); tx.setSpacing(2)
        name = QLabel(Path(j.get("inp", "?")).name); name.setWordWrap(True)
        name.setStyleSheet("background: transparent;")
        total, done = j.get("total") or 0, j["done"]
        count = _("{0} de {1} trozos ({2} %)").format(done, total, round(100 * done / total)) if total \
            else _("{0} trozos").format(done)
        sub = QLabel(f'{names.get(j.get("src"), "?")} → {names.get(j.get("tgt"), "?")} · '
                     f'{model_by_id(j.get("model")).get("name")} · {count}')
        sub.setObjectName("booksub"); sub.setWordWrap(True); sub.setStyleSheet("background: transparent;")
        tx.addWidget(name); tx.addWidget(sub)
        row.addLayout(tx, 1)
        go = QPushButton(_("▸ Reanudar")); go.setObjectName("tb"); go.setCursor(Qt.PointingHandCursor)
        go.clicked.connect(lambda _c=False, j=j: self._resume_job(j))
        bin_ = QPushButton("🗑"); bin_.setObjectName("trash"); bin_.setCursor(Qt.PointingHandCursor)
        bin_.setToolTip(_("Descartar lo traducido hasta ahora"))
        bin_.clicked.connect(lambda _c=False, j=j: self._discard_job(j))
        row.addWidget(go); row.addWidget(bin_)
        return f

    def _resume_job(self, j):
        if self.busy():
            return
        if not os.path.exists(j.get("inp", "")):
            QMessageBox.warning(self, _("Traducir"), _("No encuentro el archivo original:\n{0}").format(j.get("inp", "")))
            return
        for sel, key in ((self.srcSel, "src"), (self.tgtSel, "tgt"), (self.whereSel, "where"), (self.modelSel, "model")):
            sel.setCurrentIndex(max(0, sel.findData(j.get(key))))
        self.inp = j["inp"]
        self.fileLbl.setText(Path(self.inp).name)
        self.resCard.hide()
        self.refresh()
        if self.goBtn.isEnabled():         # si no (modelo sin descargar), la tarjeta del modelo lo indica
            self._go(job=j)

    def _discard_job(self, j):
        r = QMessageBox.question(self, _("Descartar traducción"),
                                 _("¿Descartar lo traducido de «{0}»? El archivo que ya se guardó no se borra.")
                                 .format(Path(j.get("inp", "?")).name))
        if r == QMessageBox.StandardButton.Yes:
            forget_job(j["cache"])
            self.refresh()

    def _opts_changed(self, *_a):
        self.s.update(src=self.srcSel.currentData(), tgt=self.tgtSel.currentData(),
                      where=self.whereSel.currentData(), model=self.modelSel.currentData())
        save_settings(self.s)
        self.refresh()

    def _swap(self):
        a, b = self.srcSel.currentIndex(), self.tgtSel.currentIndex()
        self.srcSel.setCurrentIndex(b); self.tgtSel.setCurrentIndex(a)

    def _model_tag(self):
        m = self._model()
        return ("ollama:" + m["ollama"]) if self._remote() else ("gguf:" + m["file"])

    def _cache(self):
        if not self.inp or not os.path.exists(self.inp):
            return WORK / "_none.json"
        return WORK / (job_key(self.inp, self.srcSel.currentData(), self.tgtSel.currentData(),
                               self._model_tag()) + ".json")

    # ---- acciones
    def _pick(self):
        flt = _("Documentos") + " (" + " ".join("*." + e for e in EXTS) + ")"
        path, _f = QFileDialog.getOpenFileName(self, _("Elige un documento para traducir"),
                                               str(Path(self.inp).parent if self.inp else Path.home()), flt)
        if not path:
            return
        self.inp = path
        self.fileLbl.setText(Path(path).name)
        self.resCard.hide()
        self.refresh()

    def _install(self):
        if self.busy():
            return
        self._start(InstallWorker(self._model(), local=not self._remote()), install=True)

    def _delete_model(self):
        m = self._model()
        r = QMessageBox.question(self, _("Borrar modelo"),
                                 _("¿Borrar {0} ({1}) de este equipo?").format(m["name"], m["size"]))
        if r == QMessageBox.StandardButton.Yes:
            shutil.rmtree(MODELS_DIR / m["id"], ignore_errors=True)
            self.refresh()

    def _go(self, _checked=False, job=None):
        if self.busy() or not self.inp:
            return
        if self.srcSel.currentData() == self.tgtSel.currentData():
            QMessageBox.information(self, _("Traducir"), _("El idioma de origen y el de destino son el mismo."))
            return
        src_p = Path(self.inp)
        tgt = self.tgtSel.currentData()
        stem = re.sub(r"\s*\((?:[a-z]{2})\)$", "", src_p.stem)
        is_pdf = src_p.suffix.lower() == ".pdf"
        ext = ".epub" if is_pdf else src_p.suffix         # un PDF sale como EPUB
        WORK.mkdir(parents=True, exist_ok=True)
        cache = Path(job["cache"]) if job else self._cache()
        meta = read_meta(cache) or {}
        out = meta.get("out")              # al reanudar, el mismo archivo: no se vuelve a preguntar
        if not out:
            sug = src_p.with_name(f"{stem} ({tgt}){ext}")
            out, _f = QFileDialog.getSaveFileName(self, _("Guardar la traducción como"), str(sug), f"*{ext}")
            if not out:
                return
            if not out.lower().endswith(ext.lower()):
                out += ext
        if Path(out).resolve() == src_p.resolve():
            QMessageBox.warning(self, _("Traducir"), _("Elige otro nombre: no voy a sobrescribir el original."))
            return
        m = self._model()
        args = ["--in", str(src_p), "--out", out, "--src", self.srcSel.currentData(), "--tgt", tgt]
        if self._remote():
            args += ["--ssh", server(), "--ollama-model", m["ollama"]]
        else:
            args += ["--llama-dir", str(find_llama_server().parent), "--gguf", str(gguf_path(m)), "--workers", "2"]
        if is_pdf:
            args += ["--title", stem]
        write_meta(cache, {"inp": str(src_p), "out": out, "src": self.srcSel.currentData(), "tgt": tgt,
                           "where": self.whereSel.currentData(), "model": m["id"], "total": meta.get("total")})
        self._start(TranslateWorker(args, cache, WORK / (cache.stem + ".stop")))

    def _start(self, w, install=False):
        self.worker = w
        self.resCard.hide()
        self.progWrap.show()
        self.stopBtn.setVisible(not install)
        self.stopBtn.setEnabled(True); self.cancelBtn.setEnabled(True)
        self._set_prog(_("Preparando…"), -1)
        w.progress.connect(self._set_prog)
        w.failed.connect(self._failed)
        if install:
            w.done.connect(self._installed)
        else:
            w.done.connect(self._done)
            w.stopped.connect(self._stopped)
        for wd in (self.pickBtn, self.srcSel, self.tgtSel, self.whereSel, self.modelSel, self.dlBtn, self.delBtn):
            wd.setEnabled(False)
        self.goBtn.setEnabled(False)
        w.finished.connect(self.refresh)   # al terminar el hilo: botones y lista de «a medias» al día
        self._clear_jobs()                 # sin leer el disco: la ficha nueva aún no tiene progreso
        w.start()

    def _finish(self):
        self.progWrap.hide()
        for wd in (self.pickBtn, self.srcSel, self.tgtSel, self.whereSel, self.modelSel, self.dlBtn, self.delBtn):
            wd.setEnabled(True)
        self.refresh()

    def _set_prog(self, text, pct):
        self.progLabel.setText(text)
        if pct < 0:
            self.prog.setRange(0, 0)
        else:
            self.prog.setRange(0, 100); self.prog.setValue(pct)

    def _stop(self, save):
        if not self.worker:
            return
        self.stopBtn.setEnabled(False); self.cancelBtn.setEnabled(False)
        self._set_prog(_("Deteniendo y guardando lo traducido…") if save else _("Cancelando…"), -1)
        if isinstance(self.worker, InstallWorker):
            self.worker.stop()
        else:
            self.worker.stop(save)

    def _installed(self):
        self._finish()

    def _failed(self, msg):
        self._finish()
        QMessageBox.warning(self, _("Traducir"), msg[-1500:])

    def _stopped(self, outs):
        self._finish()
        self.result = outs
        if outs:
            self.resLbl.setText(_("Detenido. Lo traducido está guardado en:\n{0}\nLo que falta sigue en el idioma "
                                  "original. Pulsa «Reanudar traducción» para seguir donde lo dejaste.").format(outs[0]))
        elif Path(self.worker.cache).exists():
            self.resLbl.setText(_("Detenido. Todavía no había nada traducido."))
        else:
            self.resLbl.setText(_("Cancelado."))
        self.folderBtn.setVisible(bool(outs)); self.narrBtn.hide()
        self.resCard.show()

    def _done(self, outs):
        self._finish()
        self.result = outs
        self.resLbl.setText(_("Listo:") + "\n" + "\n".join(outs))
        self.folderBtn.setVisible(bool(outs))
        self.narrBtn.setVisible(any(o.lower().endswith(".epub") for o in outs))
        self.resCard.show()

    def _open_folder(self):
        if self.result:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.result[0]).parent)))

    def _narrate(self):
        ep = next((o for o in self.result if o.lower().endswith(".epub")), None)
        if ep:
            self.narrate.emit(ep)

    def _back(self):
        self.back.emit()
