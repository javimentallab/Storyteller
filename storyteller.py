# -*- coding: utf-8 -*-
"""
Storyteller — app de escritorio (PySide6).
Biblioteca de libros narrados + reproductor con subtitulos sincronizados.
Lee un EPUB, lo manda a Mint para narrarlo con la voz neural (F5, voz A),
se trae la grabacion + el mapa de tiempos y lo reproduce (frase que suena
resaltada, clic para saltar). La generacion corre en Mint (GPU) por SSH.
"""
import sys, os, re, json, shutil, subprocess, time, unicodedata, tempfile, threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer
from PySide6.QtGui import QShortcut, QKeySequence, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QStackedWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSlider, QScrollArea, QFileDialog, QProgressBar,
    QFrame, QMessageBox, QInputDialog, QLineEdit, QCheckBox, QProgressDialog,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices

MINT       = ""                      # servidor propio opcional (user@host); se configura en Modelos
REMOTE_DIR = "/home/oem/epub-tts"
HOME_CC    = Path.home() / "cuentacuentos"
LOCAL_BOOKS = HOME_CC / "books"
# Ruta del Python del motor: Windows usa Scripts/, Linux/macOS usan bin/.
_VENV      = HOME_CC / "engine" / ".venv"
VPY        = _VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CC_ENGINE  = HOME_CC / "engine" / "cc_engine.py"
# Motor XTTS (multilingüe) en su propio venv, aislado del de F5.
_VENV_XTTS = HOME_CC / "engine" / ".venv-xtts"
VPY_XTTS   = _VENV_XTTS / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CC_XTTS    = HOME_CC / "engine" / "cc_xtts.py"
MODELS_DIR = HOME_CC / "models"
CONFIG     = HOME_CC / "config.json"
PRON_FILE  = HOME_CC / "pron.json"   # diccionario de pronunciación (para el motor)

# Semilla: siglas que se deletrean y algunos términos extranjeros frecuentes.
DEFAULT_PRON = {
    "pron": {
        "software": "sófwer", "hardware": "járgüer", "email": "ímeil",
        "online": "onláin", "streaming": "estrímin", "podcast": "pódcast",
        "marketing": "márketin", "feedback": "fídbak", "meeting": "mítin",
    },
    "say_letters": ["SQL", "HTML", "CSS", "PDF", "URL", "USB", "CPU", "GPU",
                    "HTTP", "API", "FBI", "CIA", "DVD", "USA"],
    "say_word": [],
}

def ensure_pron():
    """Crea pron.json con la semilla si no existe; devuelve su ruta."""
    try:
        if not PRON_FILE.exists():
            PRON_FILE.parent.mkdir(parents=True, exist_ok=True)
            PRON_FILE.write_text(json.dumps(DEFAULT_PRON, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass
    return PRON_FILE
SSH = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
SCP = ["scp", "-q", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
NOWIN = 0x08000000 if os.name == "nt" else 0

# Modelos de voz neural que el usuario puede descargar (desde Hugging Face).
MODELS = [
    {"id": "f5-spanish", "name": "F5 Español", "backend": "f5", "repo": "jpgallegoar/F5-Spanish",
     "ckpt": "model_1250000.safetensors", "vocab": "vocab.txt", "arch": "F5TTS_Base",
     "lang": "es", "size": "~1,3 GB", "desc": "Castellano peninsular, alta calidad. Es la voz A."},
    {"id": "f5-english", "name": "F5 Inglés (oficial)", "backend": "f5", "repo": "SWivid/F5-TTS",
     "ckpt": "F5TTS_v1_Base/model_1250000.safetensors", "vocab": "F5TTS_v1_Base/vocab.txt",
     "arch": "F5TTS_v1_Base", "lang": "en", "size": "~1,3 GB",
     "desc": "Inglés (modelo base oficial F5-TTS). Mejor con una voz de referencia en inglés."},
    {"id": "xtts-v2", "name": "XTTS v2 (multilingüe)", "backend": "xtts", "lang": "es",
     "repo": "coqui/XTTS-v2", "size": "~1,8 GB",
     "desc": "Multilingüe: pronuncia bien los términos en inglés dentro del español. "
             "Clona tu voz. Usa más VRAM que F5."},
]

# Modelos de transcripción (faster-whisper) para importar audiolibros EN LOCAL,
# sin depender de un servidor. Se descargan de Hugging Face como los de voz.
WHISPER_MODELS = [
    {"id": "whisper-small", "name": "Whisper small", "repo": "Systran/faster-whisper-small",
     "size": "~0,5 GB", "desc": "Rápido; calidad suficiente para subtítulos de audiolibros."},
    {"id": "whisper-medium", "name": "Whisper medium", "repo": "Systran/faster-whisper-medium",
     "size": "~1,5 GB", "desc": "Mejor calidad, algo más lento."},
    {"id": "whisper-large-v3", "name": "Whisper large-v3", "repo": "Systran/faster-whisper-large-v3",
     "size": "~3 GB", "desc": "Máxima calidad; el más pesado y lento."},
]

# Voces de referencia descargables (clips públicos). El usuario también puede
# cargar las suyas por archivo o URL (con autotranscripción del texto).
VOICE_PRESETS = [
    {"id": "en-nature", "name": "Inglés — ejemplo F5", "lang": "en",
     "url": "https://raw.githubusercontent.com/SWivid/F5-TTS/main/src/f5_tts/infer/examples/basic/basic_ref_en.wav",
     "text": "Some call me nature, others call me mother nature."},
]

# Idiomas para la transcripción de audiolibros (Whisper es multilingüe). "auto" = detectar.
LANGS = [("es", "Español"), ("auto", "Detectar automáticamente"), ("en", "Inglés"),
         ("fr", "Francés"), ("de", "Alemán"), ("it", "Italiano"), ("pt", "Portugués"),
         ("ca", "Catalán"), ("gl", "Gallego"), ("eu", "Euskera")]

def load_config():
    cfg = {"engine": "local", "model": MODELS[0]["id"], "voice": "narrator", "server": ""}
    try: cfg.update(json.loads(CONFIG.read_text("utf-8")))
    except Exception: pass
    return cfg

def save_config(cfg):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception: pass

VOICES_DIR = HOME_CC / "engine" / "voices"

def all_models(cfg=None):
    cfg = cfg or load_config()
    return MODELS + list(cfg.get("custom_models", []))

def model_by_id(mid, cfg=None):
    ms = all_models(cfg); return next((m for m in ms if m["id"] == mid), ms[0])
def model_dir(mid): return MODELS_DIR / mid
def xtts_ready():
    """El venv de XTTS está instalado (lo crea el bootstrap/instalación de XTTS)."""
    return VPY_XTTS.exists()
def xtts_model_cached():
    """El modelo XTTS v2 ya está descargado en la caché de coqui-tts."""
    import glob
    pats = [Path.home() / "AppData/Local/tts", Path.home() / ".local/share/tts"]
    for base in pats:
        if list(base.glob("*xtts_v2*")) if base.exists() else []:
            return True
    return False
def model_installed(m):
    if m.get("backend") == "xtts":
        return xtts_ready() and xtts_model_cached()
    d = model_dir(m["id"]); return (d / m["ckpt"]).exists() and (d / m["vocab"]).exists()

def whisper_by_id(wid):
    return next((w for w in WHISPER_MODELS if w["id"] == wid), WHISPER_MODELS[0])
def active_whisper_id(cfg=None):
    cfg = cfg or load_config(); return cfg.get("whisper_model", WHISPER_MODELS[0]["id"])
def whisper_installed(w):
    return (model_dir(w["id"]) / "model.bin").exists()
def qc_args_local(cfg=None):
    """Args de QC para los motores LOCALES (4070): usan el Whisper descargado en
    «Modelos» (en CPU, sin red), NUNCA el voz-server del Mint. El remoto (Mint) hace
    su QC en el propio Mint. Devuelve (args, aviso-o-None)."""
    cfg = cfg or load_config()
    if not cfg.get("qc_auto"):
        return [], None
    w = whisper_by_id(active_whisper_id(cfg))
    if not whisper_installed(w):
        return [], ("QC activado pero no hay modelo Whisper descargado. Descárgalo en "
                    "«Modelos» para que revise la calidad. Narro sin QC.")
    return (["--qc-whisper-dir", str(model_dir(w["id"])),
             "--qc-threshold", "0.85", "--qc-retries", "2"], None)
def active_language(cfg=None):
    cfg = cfg or load_config(); return cfg.get("language", "es")
def lang_name(code):
    return dict(LANGS).get(code, (code or "es"))
def engine_ready(): return (HOME_CC / "engine" / ".ready").exists()

def voices():
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for wav in sorted(VOICES_DIR.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.exists(): continue
        vid = wav.stem
        namef = wav.with_suffix(".name")
        if namef.exists(): name = namef.read_text(encoding="utf-8").strip()
        elif vid == "narrator": name = "Voz A — grave (F5 Español)"
        else: name = vid.replace("-", " ").capitalize()
        out.append({"id": vid, "name": name, "wav": wav, "txt": txt})
    return out

def active_voice(cfg=None):
    cfg = cfg or load_config(); vs = voices()
    if not vs: return None
    return next((v for v in vs if v["id"] == cfg.get("voice", "narrator")), vs[0])

def is_http_url(u):
    """Solo http/https (evita file://, ftp:// u otros esquemas al descargar)."""
    return isinstance(u, str) and u.strip().lower().startswith(("http://", "https://"))

def slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "libro"

def fmt(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"

def read_gen_state(d):
    try:
        return json.loads((Path(d) / "gen_state.json").read_text("utf-8"))
    except Exception:
        return None

def book_resumable(d):
    """True si el libro quedó a medias (narración F5 o transcripción) y se puede reanudar."""
    st = read_gen_state(d)
    if not st or st.get("complete", True):
        return False
    kind = st.get("kind", "narrate")
    if kind == "import":
        return (Path(d) / "cap01.mp3").exists()          # la transcripción reanuda del propio mp3
    return (Path(d) / "source.epub").exists()            # la narración reanuda del EPUB origen

def list_books():
    LOCAL_BOOKS.mkdir(parents=True, exist_ok=True)
    out = []
    for d in sorted(LOCAL_BOOKS.iterdir()):
        mf = d / "manifest.json"
        if not (d.is_dir() and mf.exists()):
            continue
        try:
            man = json.loads(mf.read_text("utf-8"))
        except Exception:
            continue
        tf = d / "title.txt"
        title = tf.read_text("utf-8").strip() if tf.exists() else d.name.replace("-", " ").capitalize()
        dur = sum(c.get("duration", 0) for c in man)
        out.append({"dir": d, "title": title, "n": len(man), "dur": dur, "manifest": man,
                    "resumable": book_resumable(d)})
    return sorted(out, key=lambda b: b["title"].lower())


def chap_label(m):
    """Nombre del capítulo en las listas: «nº de sección · título del libro» («23 · 18. Encuentra…»).
    El nº es el de la sección en el EPUB (= el de la barra «capítulo X/n»), no la posición en la lista:
    con tramos en paralelo hay huecos. Las secciones sin título (el motor les pone «Capitulo N») salen «Sección N»."""
    n = m.get("id", "?")
    t = (m.get("title") or "").strip()
    if not t or re.fullmatch(r"Capitulo \d+", t):
        t = f"Sección {n}"
    return f"{n} · {t}"


# ---------------------------------------------------------------- generacion
class GenWorker(QThread):
    progress = Signal(str, int)
    done     = Signal(str)
    failed   = Signal(str)
    canceled = Signal()

    def __init__(self, epub_path, name=None, resume=False):
        super().__init__()
        self.epub = Path(epub_path)
        self.name = name or slug(self.epub.stem)
        self.host = load_config().get("server") or MINT
        self.mode = "run"        # run | stopsave | cancel
        self.proc = None
        self.resume = resume

    def _run(self, args):
        return subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=NOWIN)

    def stop(self, save):
        """Detiene la narración en Mint. save=True conserva lo hecho; False descarta."""
        self.mode = "stopsave" if save else "cancel"
        try:
            # matar solo el python de narración; run_epub.sh sigue y rearranca el voz-server
            subprocess.run(SSH + [self.host, "pkill -f epub2audio.py"],
                           capture_output=True, text=True, timeout=20, creationflags=NOWIN)
        except Exception:
            pass

    def run(self):
        tmp_epub = None
        try:
            dest = LOCAL_BOOKS / self.name
            epub = self.epub
            # Al REGENERAR, el EPUB de entrada es dest/source.epub; ponerlo a salvo en un
            # temporal antes de que el rmtree de abajo vacíe la carpeta.
            try:
                if epub.exists() and (epub.parent == dest or dest in epub.parents):
                    fd, tmp_epub = tempfile.mkstemp(suffix=".epub"); os.close(fd)
                    shutil.copyfile(epub, tmp_epub); epub = Path(tmp_epub)
            except Exception: pass
            remote_epub = f"{REMOTE_DIR}/inbox/{self.name}.epub"
            self.progress.emit("Enviando el EPUB a Mint…", -1)
            r = self._run(SCP + [str(epub), f"{self.host}:{remote_epub}"])
            if r.returncode != 0:
                self.failed.emit("No pude enviar el EPUB a Mint:\n" + r.stderr.strip()); return

            self.progress.emit("Reanudando el motor de voz…" if self.resume
                               else "Arrancando el motor de voz…", -1)
            qc = "CC_QC=1 " if load_config().get("qc_auto") else ""
            rs = "CC_RESUME=1 " if self.resume else ""
            cmd = f"cd {REMOTE_DIR} && {qc}{rs}./run_epub.sh 'inbox/{self.name}.epub' 'out/{self.name}'"
            p = subprocess.Popen(SSH + [self.host, cmd], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", creationflags=NOWIN)
            self.proc = p
            total = done = 0
            cap_cur = cap_tot = 0
            for line in p.stdout:
                line = line.rstrip()
                m = re.search(r"capitulos:\s*(\d+)", line)
                if m: cap_tot = int(m.group(1))
                m = re.search(r"\[cap (\d+)\].*->\s*(\d+) frases", line)
                if m:
                    cap_cur = int(m.group(1)); total += int(m.group(2))
                if re.match(r"\s*frase \d+/\d+", line):
                    done += 1
                    pct = int(done / total * 100) if total else -1
                    self.progress.emit(f"Narrando capítulo {cap_cur}/{cap_tot} — frase {done}", pct)
                elif "regenerando" in line:
                    self.progress.emit("Revisando calidad (QC) y regenerando una frase…", -1)
                elif "LISTO" in line:
                    self.progress.emit(f"Capítulo {cap_cur} listo", -1)
                elif "voz-server parado" in line:
                    self.progress.emit("Liberando la GPU…", -1)
            p.wait()

            if self.mode == "cancel":
                if not self.resume:            # cancelar una reanudación no borra lo ya hecho
                    self._run(SSH + [self.host, f"rm -rf '{REMOTE_DIR}/out/{self.name}'"])
                self.canceled.emit(); return

            msg = "Guardando lo generado…" if self.mode == "stopsave" else "Trayendo la grabación…"
            self.progress.emit(msg, -1)
            if dest.exists(): shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True, exist_ok=True)
            r = self._run(SCP + ["-r", f"{self.host}:{REMOTE_DIR}/out/{self.name}/.", str(dest)])
            if r.returncode != 0 or not (dest / "manifest.json").exists():
                if self.mode == "stopsave":
                    self.failed.emit("Lo detuve, pero aún no había ningún capítulo terminado que guardar.")
                else:
                    self.failed.emit("La generación terminó pero no pude traerme el audio:\n" + r.stderr.strip())
                return
            for junk in list(dest.glob("*.wav")) + list(dest.glob("*.tmp")):
                try: junk.unlink()
                except Exception: pass
            try: shutil.copyfile(epub, dest / "source.epub")   # para poder regenerar
            except Exception: pass
            self.done.emit(str(dest))
        except Exception as e:
            self.failed.emit(f"Error inesperado: {e}")
        finally:
            if tmp_epub:
                try: os.remove(tmp_epub)
                except Exception: pass


class LocalGenWorker(QThread):
    """Genera EN LOCAL con el venv del motor (GPU/CPU del propio equipo)."""
    progress = Signal(str, int)
    done     = Signal(str)
    failed   = Signal(str)
    canceled = Signal()

    def __init__(self, epub_path, model, voice=None, name=None, resume=False):
        super().__init__()
        self.epub = Path(epub_path); self.name = name or slug(self.epub.stem)
        self.model = model; self.voice = voice; self.mode = "run"; self.proc = None
        self.resume = resume
        self.procs = []                                   # procesos de los tramos (modo paralelo)
        self._plock = threading.Lock()

    def stop(self, save):
        self.mode = "stopsave" if save else "cancel"
        with self._plock:
            procs = [self.proc] + list(self.procs)
        for p in procs:
            if p and p.poll() is None:
                try: p.terminate()
                except Exception: pass

    def run(self):
        tmp_epub = None
        try:
            dest = LOCAL_BOOKS / self.name
            epub = self.epub
            # Al REGENERAR, el EPUB de entrada es el propio dest/source.epub: ponerlo a
            # salvo en un temporal ANTES de vaciar dest, o rmtree lo borraría y la
            # regeneración se quedaría sin su origen.
            try:
                if epub.exists() and (epub.parent == dest or dest in epub.parents):
                    fd, tmp_epub = tempfile.mkstemp(suffix=".epub"); os.close(fd)
                    shutil.copyfile(epub, tmp_epub); epub = Path(tmp_epub)
            except Exception: pass
            if not self.resume:                               # reanudar = NO borrar lo hecho
                if dest.exists(): shutil.rmtree(dest, ignore_errors=True)
                dest.mkdir(parents=True, exist_ok=True)
                try: shutil.copyfile(epub, dest / "source.epub")   # para poder regenerar
                except Exception: pass
            else:
                dest.mkdir(parents=True, exist_ok=True)
            pron = ensure_pron()
            if self.model.get("backend") == "xtts":
                # Motor XTTS (multilingüe), su propio venv. Lee la frase entera; la voz
                # de referencia la clona del wav de la voz activa.
                cmd = [str(VPY_XTTS), "-I", str(CC_XTTS), "generate",
                       "--epub", str(epub), "--out", str(dest),
                       "--lang", self.model.get("lang", "es")]
                if self.voice:
                    cmd += ["--voice", str(self.voice["wav"])]
                if self.resume:
                    cmd += ["--resume"]
                if pron.exists():
                    cmd += ["--pron", str(pron)]
                _qa, _note = qc_args_local()                 # QC en LOCAL = Whisper local (nunca el Mint)
                if _note: self.progress.emit(_note, -1)
                cmd += _qa
            else:
                md = model_dir(self.model["id"])
                cmd = [str(VPY), "-I", str(CC_ENGINE), "generate",
                       "--epub", str(epub), "--out", str(dest),
                       "--ckpt", str(md / self.model["ckpt"]), "--vocab", str(md / self.model["vocab"]),
                       "--arch", self.model["arch"]]
                if self.resume:
                    cmd += ["--resume"]
                cmd += ["--lang", self.model.get("lang", "es")]   # idioma para leer los números
                if pron.exists():
                    cmd += ["--pron", str(pron)]                  # diccionario de pronunciación
                if self.voice:
                    cmd += ["--voice", str(self.voice["wav"]), "--voice-text", str(self.voice["txt"])]
                _qa, _note = qc_args_local()                 # QC en LOCAL = Whisper local (nunca el Mint)
                if _note: self.progress.emit(_note, -1)
                cmd += _qa
            # Nº de tramos en paralelo: reparte los capítulos entre N procesos en la
            # misma GPU (XTTS no la satura → varios a la vez suman). 1 = camino clásico.
            streams = int(load_config().get("parallel_streams", 1) or 1)
            st0 = read_gen_state(dest) or {}
            if self.resume and st0.get("streams"):            # reanudar con el mismo reparto
                streams = int(st0["streams"] or 1)
            n = self._count_chapters(cmd) if streams > 1 else 1
            if streams > 1 and n > 1:
                self._run_parallel(cmd, dest, max(1, min(streams, n)), n)
            else:
                self._run_single(cmd, dest)
            if self.mode == "cancel":
                # Cancelar una generación DESDE CERO descarta todo; cancelar una
                # REANUDACIÓN conserva lo que ya había (no borrar el libro entero).
                if not self.resume:
                    shutil.rmtree(dest, ignore_errors=True)
                self.canceled.emit(); return
            # Al detener y guardar se conserva el WAV del capítulo en curso para poder
            # reanudar; en una generación completa no queda ninguno.
            junk = list(dest.glob("*.tmp"))
            if self.mode != "stopsave":
                junk += list(dest.glob("*.wav"))
            for p in junk:
                try: p.unlink()
                except Exception: pass
            if not (dest / "manifest.json").exists():
                if self.mode == "stopsave":
                    self.failed.emit("Lo detuve, pero aún no había ningún capítulo terminado que guardar.")
                else:
                    shutil.rmtree(dest, ignore_errors=True)
                    self.failed.emit("La narración no produjo audio. Puede faltar memoria en la GPU "
                                     "(cierra juegos u apps que la usen) o el modelo estar incompleto.")
                return
            self.done.emit(str(dest))
        except Exception as e:
            self.failed.emit(f"Error inesperado: {e}")
        finally:
            if tmp_epub:
                try: os.remove(tmp_epub)
                except Exception: pass

    # ---- un solo proceso (camino clásico) --------------------------------
    def _run_single(self, cmd, dest):
        self.progress.emit("Reanudando la narración…" if self.resume else "Cargando el modelo…", -1)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
        self.proc = p
        logf = open(dest / "gen.log", "w", encoding="utf-8", errors="replace")
        total = done = 0; cap_cur = cap_tot = 0
        for line in p.stdout:
            line = line.rstrip()
            try: logf.write(line + "\n"); logf.flush()
            except Exception: pass
            m = re.search(r"capitulos:\s*(\d+)", line)
            if m: cap_tot = int(m.group(1))
            m = re.search(r"\[cap (\d+)\].*->\s*(\d+) frases", line)
            if m: cap_cur = int(m.group(1)); total += int(m.group(2))
            if re.match(r"\s*frase \d+/\d+", line):
                done += 1; pct = int(done / total * 100) if total else -1
                self.progress.emit(f"Narrando capítulo {cap_cur}/{cap_tot} — frase {done}", pct)
            elif "regenerando" in line:
                self.progress.emit("Revisando calidad (QC) y regenerando una frase…", -1)
            elif "reintento en CPU" in line:
                self.progress.emit("La GPU estaba ocupada; generando en CPU (más lento)…", -1)
            elif "modelo cargado" in line:
                self.progress.emit("Modelo cargado, narrando…", -1)
        p.wait()
        try: logf.close()
        except Exception: pass

    # ---- varios tramos en paralelo ---------------------------------------
    def _count_chapters(self, base_cmd):
        """Nº de capítulos del EPUB, lanzando el subcomando `count` del motor."""
        try:
            i = base_cmd.index("--epub"); epub = base_cmd[i + 1]
            exe = base_cmd[0]; script = base_cmd[2]
            p = subprocess.run([exe, "-I", script, "count", "--epub", epub],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=NOWIN, timeout=180)
            m = re.search(r"CHAPTERS\s+(\d+)", p.stdout or "")
            return int(m.group(1)) if m else 1
        except Exception:
            return 1

    @staticmethod
    def _split_ranges(n, streams):
        """Reparte 1..n en `streams` tramos contiguos lo más equilibrados posible."""
        base, extra = divmod(n, streams)
        ranges = []; start = 1
        for i in range(streams):
            size = base + (1 if i < extra else 0)
            if size <= 0: continue
            ranges.append((start, start + size - 1)); start += size
        return ranges

    def _run_parallel(self, base_cmd, dest, streams, n):
        ranges = self._split_ranges(n, streams)
        streams = len(ranges)
        self._lock = threading.Lock()
        self._tot_fr = 0; self._done_fr = 0; self._caps_seen = set()
        self.procs = []
        logf = open(dest / "gen.log", "w", encoding="utf-8", errors="replace")
        self.progress.emit(f"Narrando en {streams} tramos en paralelo — cargando modelos…", -1)

        def worker(idx, rng):
            cs, ce = rng
            cmd = list(base_cmd) + ["--chap-start", str(cs), "--chap-end", str(ce),
                                    "--manifest-name", f"manifest.p{idx}.json", "--no-state"]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
            with self._plock: self.procs.append(p)
            for line in p.stdout:
                line = line.rstrip()
                with self._lock:
                    try: logf.write(f"[t{idx}] {line}\n"); logf.flush()
                    except Exception: pass
                    cm = re.search(r"\[cap (\d+)\]", line)
                    if cm: self._caps_seen.add(int(cm.group(1)))   # capítulos CON contenido (= los que salen en "ir a capítulo"); en paralelo/reanudación cuadra con la lista del reproductor
                    mm = re.search(r"\[cap \d+\].*->\s*(\d+) frases", line)
                    if mm: self._tot_fr += int(mm.group(1))
                    if re.match(r"\s*frase \d+/\d+", line):
                        self._done_fr += 1
                        pct = int(self._done_fr / self._tot_fr * 100) if self._tot_fr else -1
                        if pct > 99: pct = 99
                        self.progress.emit(
                            f"Narrando en {streams} tramos — capítulo {len(self._caps_seen)}/{n} "
                            f"— frase {self._done_fr}", pct)
                    elif "QC frase" in line and "regenerando" in line:
                        self.progress.emit("Revisando calidad (QC) y regenerando una frase…", -1)
                    elif "reintento en CPU" in line:
                        self.progress.emit("La GPU estaba ocupada; un tramo va en CPU (más lento)…", -1)
            p.wait()

        ths = [threading.Thread(target=worker, args=(i, r), daemon=True)
               for i, r in enumerate(ranges, 1)]
        for t in ths: t.start()
        for t in ths: t.join()
        try: logf.close()
        except Exception: pass
        self._merge_manifests(dest, streams, n)

    def _merge_manifests(self, dest, streams, n):
        """Une los manifest.pN.json de los tramos en un manifest.json ordenado y
        escribe gen_state.json (complete solo si están TODOS los capítulos)."""
        merged = []
        for idx in range(1, streams + 1):
            try:
                part = json.loads((dest / f"manifest.p{idx}.json").read_text("utf-8"))
                if isinstance(part, list): merged += part
            except Exception: pass
        merged = [e for e in merged if isinstance(e, dict) and "id" in e]
        merged.sort(key=lambda e: e["id"])
        if not merged:
            return                                        # nada terminado: el post-bloque decide
        complete = (self.mode == "run") and sum(1 for e in merged if e.get("done")) >= n
        def _atomic(path, text):
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: f.write(text)
            os.replace(tmp, path)
        _atomic(dest / "manifest.json", json.dumps(merged, ensure_ascii=False, indent=2))
        _atomic(dest / "gen_state.json",
                json.dumps({"total": n, "complete": bool(complete), "streams": streams},
                           ensure_ascii=False))
        if complete:                                      # ya no hacen falta los parciales
            for idx in range(1, streams + 1):
                try: (dest / f"manifest.p{idx}.json").unlink()
                except Exception: pass


class DownloadWorker(QThread):
    progress = Signal(str)
    done     = Signal()
    failed   = Signal(str)

    def __init__(self, model):
        super().__init__(); self.model = model

    def run(self):
        try:
            md = model_dir(self.model["id"]); md.mkdir(parents=True, exist_ok=True)
            cmd = [str(VPY), "-I", str(CC_ENGINE), "download",
                   "--repo", self.model["repo"], "--ckpt-name", self.model["ckpt"],
                   "--vocab-name", self.model["vocab"], "--dest", str(md)]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
            for line in p.stdout:
                line = line.rstrip()
                if line: self.progress.emit(line)
            p.wait()
            if model_installed(self.model): self.done.emit()
            else: self.failed.emit("La descarga no dejó el modelo completo.")
        except Exception as e:
            self.failed.emit(str(e))


class WhisperDownloadWorker(QThread):
    """Descarga un modelo faster-whisper para transcribir audiolibros en local."""
    progress = Signal(str)
    done     = Signal()
    failed   = Signal(str)

    def __init__(self, model):
        super().__init__(); self.model = model

    def run(self):
        try:
            md = model_dir(self.model["id"]); md.mkdir(parents=True, exist_ok=True)
            cmd = [str(VPY), "-I", str(CC_ENGINE), "whisper-download",
                   "--repo", self.model["repo"], "--dest", str(md)]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
            for line in p.stdout:
                line = line.rstrip()
                if line: self.progress.emit(line)
            p.wait()
            if whisper_installed(self.model): self.done.emit()
            else: self.failed.emit("La descarga del modelo de transcripción no se completó.")
        except Exception as e:
            self.failed.emit(str(e))


class XttsDownloadWorker(QThread):
    """Descarga el modelo XTTS v2 a la caché de coqui-tts (en el venv de XTTS)."""
    progress = Signal(str)
    done     = Signal()
    failed   = Signal(str)

    def run(self):
        try:
            code = ("import os; os.environ['COQUI_TOS_AGREED']='1'; "
                    "from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2'); "
                    "print('XTTS-DL-DONE')")
            p = subprocess.Popen([str(VPY_XTTS), "-c", code], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                 errors="replace", creationflags=NOWIN)
            for line in p.stdout:
                line = line.rstrip()
                if line and "warning" not in line.lower(): self.progress.emit(line[:120])
            p.wait()
            if xtts_model_cached(): self.done.emit()
            else: self.failed.emit("La descarga del modelo XTTS no se completó.")
        except Exception as e:
            self.failed.emit(str(e))


def voz_http_url():
    """URL HTTP del voz-server (Whisper), derivada del host de config['server']."""
    cfg = load_config()
    host = (cfg.get("server") or MINT).split("@")[-1].split(":")[0]
    return f"http://{host}:8000"


class ImportWorker(QThread):
    """Importa un audio existente: lo pasa a MP3 y lo transcribe en el voz-server
    (Whisper, verbose_json con tiempos) para generar los cues de subtítulos.
    Transcribe POR TRAMOS (guardando cada uno) para poder DETENER Y GUARDAR,
    CANCELAR y REANUDAR, igual que la narración con F5. 1 capítulo (cap01)."""
    progress = Signal(str, int)
    done     = Signal(str)
    failed   = Signal(str)
    canceled = Signal()

    CHUNK = 60.0    # segundos por tramo (= granularidad de "detener y guardar")

    def __init__(self, src=None, resume_dir=None):
        super().__init__()
        self.src = src; self.resume_dir = resume_dir
        self._dest = None; self.mode = "run"; self.proc = None

    def stop(self, save):
        self.mode = "stopsave" if save else "cancel"
        p = self.proc
        if p and p.poll() is None:
            try: p.terminate()
            except Exception: pass

    def _proc(self, args):
        """Lanza un proceso que se puede terminar desde stop(); devuelve (rc, stdout_bytes)."""
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=NOWIN)
        out, _ = self.proc.communicate()
        rc = self.proc.returncode; self.proc = None
        return rc, (out or b"")

    def _duration(self, p):
        try:
            rc, out = self._proc(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", str(p)])
            return float(out.decode("utf-8", "replace").strip() or 0)
        except Exception:
            return 0.0

    def _transcribe_chunk(self, mp3, start, length, url, language="es"):
        """Extrae [start, start+length) a wav 16k mono y lo transcribe; devuelve
        la lista de segmentos con tiempos ya desplazados, o None si falla."""
        fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        try:
            rc, _ = self._proc(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                                "-i", str(mp3), "-ar", "16000", "-ac", "1", wav])
            if rc != 0 or not os.path.exists(wav) or os.path.getsize(wav) < 1000:
                return None
            cmd = ["curl", "-s", "--connect-timeout", "10", "-m", "900",
                   "-F", f"file=@{wav}", "-F", "response_format=verbose_json"]
            if language and language != "auto":              # sin idioma = autodetectar
                cmd += ["-F", f"language={language}"]
            cmd.append(url)
            rc, out = self._proc(cmd)
            if rc != 0:
                return None
            data = json.loads(out.decode("utf-8", "replace"))
            if isinstance(data, dict) and data.get("error"):
                return None
            segs = data.get("segments") if isinstance(data, dict) else None
            out_segs = []
            for s in (segs or []):
                txt = (s.get("text") or "").strip()
                if not txt: continue
                out_segs.append({"start": round(start + float(s["start"]), 3),
                                 "end": round(start + float(s["end"]), 3), "text": txt})
            return out_segs
        except Exception:
            return None
        finally:
            try: os.remove(wav)
            except OSError: pass

    def _save(self, dest, title, cues, dur, covered, complete):
        """Vuelca cap01.json + manifest + gen_state de forma que el libro sea
        reproducible y (si no está completo) reanudable."""
        (dest / "cap01.json").write_text(json.dumps(
            {"title": title, "duration": round(dur, 3), "cues": cues},
            ensure_ascii=False, indent=1), "utf-8")
        (dest / "manifest.json").write_text(json.dumps(
            [{"id": 1, "base": "cap01", "title": title, "n": len(cues),
              "duration": round(dur, 3), "done": bool(complete)}], ensure_ascii=False, indent=2), "utf-8")
        (dest / "gen_state.json").write_text(json.dumps(
            {"total": 1, "complete": bool(complete), "kind": "import",
             "covered": round(covered, 3), "dur": round(dur, 3)}, ensure_ascii=False), "utf-8")

    def run(self):
        resume = bool(self.resume_dir)
        try:
            url = voz_http_url() + "/v1/audio/transcriptions"
            if resume:
                dest = Path(self.resume_dir); self._dest = dest
                mp3 = dest / "cap01.mp3"
                if not mp3.exists():
                    self.failed.emit("No encuentro el audio de este libro, no puedo reanudar."); return
                tf = dest / "title.txt"
                title = tf.read_text("utf-8").strip() if tf.exists() else dest.name
                try:
                    st = json.loads((dest / "gen_state.json").read_text("utf-8"))
                    covered = float(st.get("covered", 0.0)); dur = float(st.get("dur", 0.0))
                except Exception:
                    covered = 0.0; dur = 0.0
                try:
                    cues = json.loads((dest / "cap01.json").read_text("utf-8")).get("cues", [])
                except Exception:
                    cues = []
                if dur <= 0: dur = self._duration(mp3)
            else:
                src = self.src
                title = Path(src).stem.replace("_", " ").strip() or "Audio"
                sl = slug(title) or "audio"
                dest = LOCAL_BOOKS / sl; n = 1
                while dest.exists():
                    n += 1; dest = LOCAL_BOOKS / f"{sl}-{n}"
                dest.mkdir(parents=True, exist_ok=True); self._dest = dest
                self.progress.emit("Convirtiendo audio…", -1)
                mp3 = dest / "cap01.mp3"
                rc, _ = self._proc(["ffmpeg", "-y", "-i", str(src), "-codec:a", "libmp3lame",
                                    "-b:a", "128k", str(mp3)])
                if self.mode == "cancel":
                    shutil.rmtree(dest, ignore_errors=True); self.canceled.emit(); return
                if rc != 0 or not mp3.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                    self.failed.emit("No se pudo convertir el audio (¿formato soportado? ¿ffmpeg instalado?).")
                    return
                dur = self._duration(mp3); cues = []; covered = 0.0
                (dest / "title.txt").write_text(title, "utf-8")
                self._save(dest, title, cues, dur, covered, False)   # ya reanudable aunque se pare enseguida

            if dur <= 0:
                if not resume: shutil.rmtree(dest, ignore_errors=True)
                self.failed.emit("No pude leer la duración del audio."); return

            # LOCAL = Whisper en este equipo (sin servidor); REMOTO = voz-server por SSH.
            lang = active_language()
            if load_config().get("engine", "local") == "local":
                ok, msg, covered = self._transcribe_local(dest, mp3, title, dur, cues, covered, lang)
            else:
                ok, msg, covered = self._transcribe_remote(dest, mp3, title, dur, cues, covered, lang)

            if self.mode == "cancel":
                shutil.rmtree(dest, ignore_errors=True); self.canceled.emit(); return
            if not ok:
                self._save(dest, title, cues, dur, covered, False)   # conservar lo hecho (reanudable)
                self.failed.emit(msg); return
            complete = covered >= dur - 0.05
            self._save(dest, title, cues, dur, covered, complete)
            self.done.emit(str(dest))
        except Exception as e:
            # nunca borrar un parcial reanudable por un error inesperado
            self.failed.emit(f"Error inesperado al importar: {e}")

    def _transcribe_remote(self, dest, mp3, title, dur, cues, covered, language="es"):
        """Transcribe por tramos contra el Whisper del voz-server (SSH/HTTP)."""
        url = voz_http_url() + "/v1/audio/transcriptions"
        start = covered
        while start < dur - 0.05:
            if self.mode == "cancel":
                break
            length = min(self.CHUNK, dur - start)
            self.progress.emit(
                f"Transcribiendo (servidor)… {fmt(start*1000)}–{fmt((start+length)*1000)} "
                f"de {fmt(dur*1000)}  ·  guardado {int(covered//60)}/{int((dur+59)//60)} min", -1)
            segs = self._transcribe_chunk(mp3, start, length, url, language)
            if self.mode == "cancel":
                break
            if segs is None:
                if self.mode == "stopsave":
                    break
                return (False, "Se cortó la transcripción (¿servidor de voz caído?). "
                               "Lo transcrito se ha guardado; puedes Reanudar.", covered)
            for s in segs:
                cues.append({"i": len(cues), "start": s["start"], "end": s["end"], "text": s["text"]})
            covered = round(start + length, 3)
            self._save(dest, title, cues, dur, covered, False)
            if self.mode == "stopsave":
                break
            start = covered
        return (True, "", covered)

    def _transcribe_local(self, dest, mp3, title, dur, cues, covered, language="es"):
        """Transcribe con faster-whisper EN LOCAL (motor), en streaming de segmentos.
        Al reanudar transcribe solo el tramo que falta ([covered, fin]) y lo desplaza."""
        wm = whisper_by_id(active_whisper_id()); mdir = model_dir(wm["id"])
        if not (mdir / "model.bin").exists():
            return (False, "Antes descarga un modelo de transcripción (Whisper) en «Modelos».", covered)
        audio = str(mp3); offset = 0.0; clip = None
        if covered > 0.5:                                    # reanudar: solo lo que falta
            fd, clip = tempfile.mkstemp(suffix=".wav"); os.close(fd)
            rc, _ = self._proc(["ffmpeg", "-y", "-ss", f"{covered:.3f}", "-i", str(mp3),
                                "-ar", "16000", "-ac", "1", clip])
            if self.mode == "cancel":
                try: os.remove(clip)
                except OSError: pass
                return (True, "", covered)
            if rc == 0 and os.path.exists(clip):
                audio = clip; offset = covered
            else:
                cues.clear(); covered = 0.0                  # no se pudo recortar: rehacer entero
        self.progress.emit("Cargando el modelo de transcripción…", -1)
        cmd = [str(VPY), "-I", str(CC_ENGINE), "whisper", "--audio", audio,
               "--model-dir", str(mdir), "--device", "auto", "--offset", f"{offset:.3f}",
               "--language", language or "es"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
        done_seen = False; got = False; last = 0.0
        for line in self.proc.stdout:
            line = line.rstrip()
            if line.startswith("SEG "):
                try: s = json.loads(line[4:])
                except Exception: continue
                got = True
                cues.append({"i": len(cues), "start": s["start"], "end": s["end"], "text": s["text"]})
                covered = max(covered, float(s["end"]))
                self.progress.emit(f"Transcribiendo con Whisper… {fmt(covered*1000)} / {fmt(dur*1000)}",
                                   int(min(100, covered / dur * 100)) if dur else -1)
                if time.monotonic() - last > 4:
                    last = time.monotonic(); self._save(dest, title, cues, dur, covered, False)
            elif "WH-DONE" in line:
                done_seen = True
            elif "whisper cargado" in line:
                self.progress.emit("Transcribiendo con Whisper…", -1)
            if self.mode in ("stopsave", "cancel"):
                try: self.proc.terminate()
                except Exception: pass
                break
        try: self.proc.wait()
        except Exception: pass
        self.proc = None
        if clip:
            try: os.remove(clip)
            except OSError: pass
        if self.mode == "cancel":
            return (True, "", covered)
        if self.mode == "stopsave":
            self._save(dest, title, cues, dur, covered, False)
            return (True, "", covered)
        if not done_seen and not got:
            return (False, "No se pudo transcribir en local (¿modelo Whisper incompleto o falta memoria?).", covered)
        covered = dur                                        # terminó: cubre todo el audio
        self._save(dest, title, cues, dur, covered, False)
        return (True, "", covered)


# ------------------------------------------------------------------- widgets
class Cue(QLabel):
    clicked = Signal(float)

    def __init__(self, text, start):
        super().__init__(text)
        self.start = start
        self.setWordWrap(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("active", False)
        self.setObjectName("cue")

    def mousePressEvent(self, e):
        self.clicked.emit(self.start)


class BookCard(QFrame):
    opened = Signal(str)
    deleted = Signal(str)
    resumed = Signal(str)

    def __init__(self, book):
        super().__init__()
        self.dir = str(book["dir"])
        self.title = book["title"]
        self.setObjectName("bookcard")
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self); row.setContentsMargins(16, 12, 12, 12); row.setSpacing(10)
        col = QVBoxLayout(); col.setSpacing(3)
        t = QLabel(book["title"]); t.setObjectName("booktitle"); t.setWordWrap(True)
        mins = round(book["dur"] / 60)
        extra = " · ⏸ a medias" if book.get("resumable") else ""
        sub = QLabel(f"{book['n']} capítulos · {mins} min{extra}"); sub.setObjectName("booksub")
        col.addWidget(t); col.addWidget(sub)
        row.addLayout(col, 1)
        if book.get("resumable"):
            self.resumeBtn = QPushButton("▸ Reanudar"); self.resumeBtn.setObjectName("tb")
            self.resumeBtn.setToolTip("Seguir narrando este libro desde donde se detuvo")
            self.resumeBtn.setCursor(Qt.PointingHandCursor)
            self.resumeBtn.clicked.connect(lambda: self.resumed.emit(self.dir))
            row.addWidget(self.resumeBtn, 0, Qt.AlignTop)
        self.delBtn = QPushButton("🗑"); self.delBtn.setObjectName("trash")
        self.delBtn.setToolTip("Eliminar este libro"); self.delBtn.setCursor(Qt.PointingHandCursor)
        self.delBtn.clicked.connect(lambda: self.deleted.emit(self.dir))
        row.addWidget(self.delBtn, 0, Qt.AlignTop)

    def mousePressEvent(self, e):
        self.opened.emit(self.dir)


# ------------------------------------------------------------------- biblioteca
class LibraryPage(QWidget):
    openBook = Signal(str)
    openEpub = Signal()
    openModels = Signal()
    importAudio = Signal()
    resumeBook = Signal(str)

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        head = QWidget(); head.setObjectName("head")
        hb = QHBoxLayout(head); hb.setContentsMargins(16, 12, 16, 12); hb.setSpacing(12)
        brand = QVBoxLayout(); brand.setSpacing(2)
        w = QLabel("Storyteller"); w.setObjectName("word")
        self.tag = QLabel("tu biblioteca narrada"); self.tag.setObjectName("tag")
        brand.addWidget(w); brand.addWidget(self.tag)
        hb.addLayout(brand); hb.addStretch(1)
        self.refreshBtn = QPushButton("↻ Actualizar"); self.refreshBtn.setObjectName("tb")
        self.refreshBtn.setToolTip("Volver a leer la carpeta de libros")
        self.refreshBtn.setCursor(Qt.PointingHandCursor)
        self.refreshBtn.clicked.connect(self.refresh)
        self.voicesBtn = QPushButton("Modelos"); self.voicesBtn.setObjectName("tb")
        self.voicesBtn.setCursor(Qt.PointingHandCursor); self.voicesBtn.clicked.connect(self.openModels.emit)
        self.importBtn = QPushButton("Importar audiolibro…"); self.importBtn.setObjectName("open")
        self.importBtn.setCursor(Qt.PointingHandCursor)
        self.importBtn.setToolTip("Añadir un audiolibro/audio existente y transcribirlo (Whisper) para subtítulos")
        self.importBtn.clicked.connect(self.importAudio.emit)
        self.openBtn = QPushButton("Abrir EPUB…"); self.openBtn.setObjectName("open")
        self.openBtn.setCursor(Qt.PointingHandCursor)
        self.openBtn.clicked.connect(self.openEpub.emit)
        hb.addWidget(self.refreshBtn); hb.addWidget(self.voicesBtn)
        hb.addWidget(self.importBtn); hb.addWidget(self.openBtn)
        root.addWidget(head)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setObjectName("reader")
        inner = QWidget(); self.grid = QVBoxLayout(inner)
        self.grid.setContentsMargins(16, 16, 16, 16); self.grid.setSpacing(12)
        self.empty = QLabel("Todavía no hay libros.\nPulsa «Abrir EPUB…» y Storyteller lo narrará con la voz A.")
        self.empty.setObjectName("hint"); self.empty.setWordWrap(True); self.empty.setAlignment(Qt.AlignCenter)
        self.grid.addWidget(self.empty)
        self.grid.addStretch(1)
        self.scroll.setWidget(inner)
        root.addWidget(self.scroll, 1)

        self.progWrap = QWidget(); self.progWrap.setObjectName("progwrap")
        pv = QVBoxLayout(self.progWrap); pv.setContentsMargins(16, 10, 16, 12); pv.setSpacing(7)
        self.progLabel = QLabel(""); self.progLabel.setObjectName("proglabel")
        self.prog = QProgressBar(); self.prog.setTextVisible(False); self.prog.setFixedHeight(6)
        pv.addWidget(self.progLabel); pv.addWidget(self.prog)
        btns = QHBoxLayout(); btns.setSpacing(8); btns.addStretch(1)
        self.stopBtn = QPushButton("Detener y guardar"); self.stopBtn.setObjectName("tb")
        self.cancelBtn = QPushButton("Cancelar"); self.cancelBtn.setObjectName("tb")
        for b in (self.stopBtn, self.cancelBtn): b.setCursor(Qt.PointingHandCursor)
        btns.addWidget(self.stopBtn); btns.addWidget(self.cancelBtn)
        pv.addLayout(btns)
        self.progWrap.hide(); root.addWidget(self.progWrap)

        self.setStyleSheet(STYLE)
        self.refresh()

    def refresh(self):
        # vacia las tarjetas (deja empty + stretch al final)
        while self.grid.count() > 2:
            it = self.grid.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        books = list_books()
        self.empty.setVisible(not books)
        for b in books:
            card = BookCard(b); card.opened.connect(self.openBook.emit)
            card.deleted.connect(self._delete_book)
            card.resumed.connect(self.resumeBook.emit)
            self.grid.insertWidget(self.grid.count() - 2, card)

    def _delete_book(self, folder):
        d = Path(folder)
        tf = d / "title.txt"
        name = tf.read_text(encoding="utf-8").strip() if tf.exists() else d.name
        r = QMessageBox.question(self, "Eliminar libro",
                                 f"¿Eliminar «{name}» y su grabación? No se puede deshacer.")
        if r == QMessageBox.StandardButton.Yes:
            shutil.rmtree(folder, ignore_errors=True)
            self.refresh()

    def set_generating(self, on, text="", pct=-1):
        self.progWrap.setVisible(on)
        self.openBtn.setEnabled(not on); self.importBtn.setEnabled(not on)
        if on:
            self.stopBtn.setVisible(True); self.cancelBtn.setVisible(True)
            self.stopBtn.setEnabled(True); self.cancelBtn.setEnabled(True)
            self.progLabel.setText(text)
            if pct < 0: self.prog.setRange(0, 0)
            else: self.prog.setRange(0, 100); self.prog.setValue(pct)

    def set_busy(self, on, text=""):
        """Progreso sin botones de detener (import: no hay parcial que guardar)."""
        self.progWrap.setVisible(on)
        self.openBtn.setEnabled(not on); self.importBtn.setEnabled(not on)
        self.stopBtn.setVisible(False); self.cancelBtn.setVisible(False)
        self.prog.setRange(0, 0); self.progLabel.setText(text)

    def set_stopping(self, text):
        """Mientras se detiene: deshabilita los botones y muestra el mensaje."""
        self.stopBtn.setEnabled(False); self.cancelBtn.setEnabled(False)
        self.prog.setRange(0, 0); self.progLabel.setText(text)


# ------------------------------------------------------------------- modelos
class ModelsScreen(QWidget):
    back = Signal()

    def __init__(self):
        super().__init__()
        self.dl = None
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        head = QWidget(); head.setObjectName("head")
        hb = QHBoxLayout(head); hb.setContentsMargins(10, 8, 16, 8); hb.setSpacing(10)
        self.backBtn = QPushButton("‹ Biblioteca"); self.backBtn.setObjectName("tb")
        self.backBtn.setCursor(Qt.PointingHandCursor); self.backBtn.clicked.connect(self.back.emit)
        ttl = QLabel("Modelos y voces"); ttl.setObjectName("word2")
        hb.addWidget(self.backBtn); hb.addWidget(ttl); hb.addStretch(1)
        root.addWidget(head)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setObjectName("reader")
        inner = QWidget(); self.col = QVBoxLayout(inner)
        self.col.setContentsMargins(16, 16, 16, 16); self.col.setSpacing(10)

        erow = QHBoxLayout(); erow.setSpacing(10)
        el = QLabel("Generar en:"); el.setObjectName("booksub")
        self.engineSel = QComboBox()
        self.engineSel.addItem("Este equipo (local)", "local")
        self.engineSel.addItem("Servidor propio", "remote")
        self.engineSel.currentIndexChanged.connect(self._engine_changed)
        erow.addWidget(el); erow.addWidget(self.engineSel); erow.addStretch(1)
        self.col.addLayout(erow)

        self.serverRow = QWidget()
        sr = QHBoxLayout(self.serverRow); sr.setContentsMargins(0, 0, 0, 0); sr.setSpacing(10)
        slb = QLabel("Servidor:"); slb.setObjectName("booksub")
        self.serverEdit = QLineEdit(); self.serverEdit.setPlaceholderText("usuario@host de tu servidor (SSH)")
        self.serverEdit.editingFinished.connect(self._server_changed)
        sr.addWidget(slb); sr.addWidget(self.serverEdit, 1)
        self.col.addWidget(self.serverRow)

        self.qcChk = QCheckBox("Revisar y regenerar frases que suenen mal (QC Whisper)")
        self.qcChk.setToolTip("Tras narrar cada frase, la transcribe con Whisper y, si no coincide con el "
                              "texto, la regenera. Más lento pero caza alucinaciones. Usa el Whisper del "
                              "sitio donde narras: en LOCAL el modelo descargado en «Modelos»; en el Mint, "
                              "el del Mint. (En local hace falta un modelo Whisper descargado.)")
        self.qcChk.setCursor(Qt.PointingHandCursor)
        self.qcChk.toggled.connect(self._qc_changed)
        self.col.addWidget(self.qcChk)

        self.parRow = QWidget()
        pr2 = QHBoxLayout(self.parRow); pr2.setContentsMargins(0, 0, 0, 0); pr2.setSpacing(10)
        plb = QLabel("Velocidad / uso de GPU:"); plb.setObjectName("booksub")
        self.streamsSel = QComboBox()
        self.streamsSel.addItem("Normal — 1 tramo (no satura la GPU, más lento)", 1)
        self.streamsSel.addItem("Rápido — 2 tramos (≈1,8×)", 2)
        self.streamsSel.addItem("Máximo — 3 tramos (≈2,6×, exprime la GPU)", 3)
        self.streamsSel.setToolTip("Cómo narrar en este equipo:\n"
                                   "• Normal: un solo proceso, como siempre. No satura la GPU "
                                   "(puedes seguir usándola), pero es lo más lento.\n"
                                   "• Rápido / Máximo: reparte los capítulos entre 2 o 3 procesos "
                                   "a la vez. Mucho más rápido, pero la GPU queda muy cargada "
                                   "(con 3, al límite: no juegues mientras narra). Si diera error "
                                   "de memoria de la GPU, baja de 3 a 2.")
        self.streamsSel.currentIndexChanged.connect(self._streams_changed)
        pr2.addWidget(plb); pr2.addWidget(self.streamsSel); pr2.addStretch(1)
        self.col.addWidget(self.parRow)

        self.note = QLabel(""); self.note.setObjectName("hint"); self.note.setWordWrap(True)
        self.col.addWidget(self.note)

        h1 = QLabel("Modelos de voz (narrar EPUB)"); h1.setObjectName("sec"); self.col.addWidget(h1)
        self.cards = QVBoxLayout(); self.cards.setSpacing(10); self.col.addLayout(self.cards)
        self.addModelBtn = QPushButton("+ Añadir modelo por URL / repo…"); self.addModelBtn.setObjectName("tb")
        self.addModelBtn.setCursor(Qt.PointingHandCursor); self.addModelBtn.clicked.connect(self._add_model)
        amrow = QHBoxLayout(); amrow.addStretch(1); amrow.addWidget(self.addModelBtn); self.col.addLayout(amrow)

        hw = QLabel("Transcripción de audiolibros (Whisper)"); hw.setObjectName("sec"); self.col.addWidget(hw)
        lrow = QHBoxLayout(); lrow.setSpacing(10)
        llb = QLabel("Idioma del audio:"); llb.setObjectName("booksub")
        self.langSel = QComboBox()
        for code, name in LANGS: self.langSel.addItem(name, code)
        self.langSel.currentIndexChanged.connect(self._lang_changed)
        lrow.addWidget(llb); lrow.addWidget(self.langSel); lrow.addStretch(1)
        self.col.addLayout(lrow)
        self.whisperCards = QVBoxLayout(); self.whisperCards.setSpacing(10); self.col.addLayout(self.whisperCards)

        h2 = QLabel("Voces de referencia"); h2.setObjectName("sec"); self.col.addWidget(h2)
        self.voiceRows = QVBoxLayout(); self.voiceRows.setSpacing(10); self.col.addLayout(self.voiceRows)
        self.addVoiceBtn = QPushButton("+ Añadir voz (archivo)…"); self.addVoiceBtn.setObjectName("tb")
        self.addVoiceBtn.setCursor(Qt.PointingHandCursor); self.addVoiceBtn.clicked.connect(self._add_voice)
        self.addVoiceUrlBtn = QPushButton("+ Cargar voz por URL…"); self.addVoiceUrlBtn.setObjectName("tb")
        self.addVoiceUrlBtn.setCursor(Qt.PointingHandCursor); self.addVoiceUrlBtn.clicked.connect(self._add_voice_url)
        avrow = QHBoxLayout(); avrow.addStretch(1)
        avrow.addWidget(self.addVoiceBtn); avrow.addWidget(self.addVoiceUrlBtn); self.col.addLayout(avrow)
        vhint = QLabel("Al añadir una voz, el texto de referencia se transcribe solo (Whisper); "
                       "puedes corregirlo."); vhint.setObjectName("booksub"); vhint.setWordWrap(True)
        self.col.addWidget(vhint)

        hv = QLabel("Voces descargables"); hv.setObjectName("sec"); self.col.addWidget(hv)
        self.presetRows = QVBoxLayout(); self.presetRows.setSpacing(10); self.col.addLayout(self.presetRows)

        hp = QLabel("Pronunciación"); hp.setObjectName("sec"); self.col.addWidget(hp)
        phint = QLabel("Cómo lee la voz ciertas palabras: términos extranjeros, siglas y "
                       "palabras concretas que suenen mal. No cambia los subtítulos.")
        phint.setObjectName("booksub"); phint.setWordWrap(True); self.col.addWidget(phint)
        self.editPronBtn = QPushButton("Editar diccionario de pronunciación…"); self.editPronBtn.setObjectName("tb")
        self.editPronBtn.setCursor(Qt.PointingHandCursor); self.editPronBtn.clicked.connect(self._edit_pron)
        prow = QHBoxLayout(); prow.addStretch(1); prow.addWidget(self.editPronBtn); self.col.addLayout(prow)

        self.progLabel = QLabel(""); self.progLabel.setObjectName("proglabel"); self.progLabel.setWordWrap(True)
        self.col.addWidget(self.progLabel)
        self.col.addStretch(1)
        self.scroll.setWidget(inner); root.addWidget(self.scroll, 1)
        self.setStyleSheet(STYLE)
        self.refresh()

    def _apply_engine_view(self, cfg):
        """Parte de la pantalla que depende de local/remoto. Se puede llamar sola
        (al cambiar el motor) sin reconstruir la lista de modelos/voces ni perder scroll."""
        self.serverRow.setVisible(cfg["engine"] != "local")
        self.parRow.setVisible(cfg["engine"] == "local")   # los tramos paralelos son solo locales
        if cfg["engine"] == "local" and not engine_ready():
            self.note.setText("El motor local se está instalando (PyTorch + F5). Mientras tanto puedes generar en un servidor propio.")
        elif cfg["engine"] == "local":
            self.note.setText("Se genera en este equipo con la GPU. Elige modelo y voz; descarga el modelo si hace falta.")
        else:
            self.note.setText("Se genera por SSH en el servidor que indiques (debe tener el mismo motor instalado). El modelo y la voz de aquí se usan en el modo local.")

    def refresh(self):
        cfg = load_config()
        self.engineSel.blockSignals(True)
        self.engineSel.setCurrentIndex(0 if cfg["engine"] == "local" else 1)
        self.engineSel.blockSignals(False)
        self.serverEdit.setText(cfg.get("server", ""))
        self.qcChk.blockSignals(True); self.qcChk.setChecked(bool(cfg.get("qc_auto"))); self.qcChk.blockSignals(False)
        self.streamsSel.blockSignals(True)
        si = {1: 0, 2: 1, 3: 2}.get(int(cfg.get("parallel_streams", 1) or 1), 0)
        self.streamsSel.setCurrentIndex(si); self.streamsSel.blockSignals(False)
        self.langSel.blockSignals(True)
        li = next((i for i, (c, _n) in enumerate(LANGS) if c == active_language(cfg)), 0)
        self.langSel.setCurrentIndex(li); self.langSel.blockSignals(False)
        self._apply_engine_view(cfg)
        for lay in (self.cards, self.whisperCards, self.voiceRows, self.presetRows):
            while lay.count():
                it = lay.takeAt(0)
                if it.widget(): it.widget().deleteLater()
        for mdl in all_models(cfg):
            self.cards.addWidget(self._card(mdl, cfg))
        for wm in WHISPER_MODELS:
            self.whisperCards.addWidget(self._whisper_card(wm, cfg))
        for v in voices():
            self.voiceRows.addWidget(self._voice_row(v, cfg))
        for pr in VOICE_PRESETS:
            self.presetRows.addWidget(self._preset_card(pr))

    def _card(self, mdl, cfg):
        f = QFrame(); f.setObjectName("bookcard")
        lay = QVBoxLayout(f); lay.setContentsMargins(16, 12, 16, 12); lay.setSpacing(6)
        top = QHBoxLayout()
        name = QLabel(mdl["name"]); name.setObjectName("booktitle"); top.addWidget(name)
        if cfg["model"] == mdl["id"]:
            chip = QLabel("activo"); chip.setObjectName("chip"); top.addWidget(chip)
        top.addStretch(1)
        state = QLabel("✓ Instalado" if model_installed(mdl) else f"Sin descargar · {mdl.get('size','?')}")
        state.setObjectName("booksub"); top.addWidget(state)
        lay.addLayout(top)
        desc = QLabel(f"{mdl.get('desc','')}  · Idioma: {lang_name(mdl.get('lang','es'))}  ({mdl['repo']})")
        desc.setObjectName("booksub"); desc.setWordWrap(True)
        lay.addWidget(desc)
        is_xtts = mdl.get("backend") == "xtts"
        btns = QHBoxLayout(); btns.setSpacing(8); btns.addStretch(1)
        if model_installed(mdl):
            use = QPushButton("Usar"); use.setObjectName("tb"); use.setEnabled(cfg["model"] != mdl["id"])
            use.clicked.connect(lambda _, i=mdl["id"]: self._use_model(i))
            dele = QPushButton("Borrar"); dele.setObjectName("tb")
            dele.clicked.connect((lambda _, m=mdl: self._delete_xtts(m)) if is_xtts
                                 else (lambda _, m=mdl: self._delete_model(m)))
            btns.addWidget(use); btns.addWidget(dele)
        else:
            dl = QPushButton(f"Descargar  ({mdl.get('size','?')})"); dl.setObjectName("open")
            ready = xtts_ready() if is_xtts else engine_ready()
            dl.setEnabled(ready)
            if not ready:
                dl.setToolTip("El motor XTTS aún se está instalando" if is_xtts
                              else "Espera a que termine de instalarse el motor local")
            dl.clicked.connect((lambda _, m=mdl: self._download_xtts(m)) if is_xtts
                               else (lambda _, m=mdl: self._download(m)))
            btns.addWidget(dl)
        if mdl.get("custom"):
            rm = QPushButton("Quitar"); rm.setObjectName("tb")
            rm.clicked.connect(lambda _, i=mdl["id"]: self._remove_custom(i)); btns.addWidget(rm)
        for i in range(btns.count()):
            w = btns.itemAt(i).widget()
            if w: w.setCursor(Qt.PointingHandCursor)
        lay.addLayout(btns)
        return f

    def _whisper_card(self, wm, cfg):
        f = QFrame(); f.setObjectName("bookcard")
        lay = QVBoxLayout(f); lay.setContentsMargins(16, 12, 16, 12); lay.setSpacing(6)
        top = QHBoxLayout()
        name = QLabel(wm["name"]); name.setObjectName("booktitle"); top.addWidget(name)
        if active_whisper_id(cfg) == wm["id"]:
            chip = QLabel("activo"); chip.setObjectName("chip"); top.addWidget(chip)
        top.addStretch(1)
        state = QLabel("✓ Instalado" if whisper_installed(wm) else f"Sin descargar · {wm.get('size','?')}")
        state.setObjectName("booksub"); top.addWidget(state)
        lay.addLayout(top)
        desc = QLabel(f"{wm.get('desc','')}  ({wm['repo']})"); desc.setObjectName("booksub"); desc.setWordWrap(True)
        lay.addWidget(desc)
        btns = QHBoxLayout(); btns.setSpacing(8); btns.addStretch(1)
        if whisper_installed(wm):
            use = QPushButton("Usar"); use.setObjectName("tb"); use.setEnabled(active_whisper_id(cfg) != wm["id"])
            use.clicked.connect(lambda _, i=wm["id"]: self._use_whisper(i))
            dele = QPushButton("Borrar"); dele.setObjectName("tb")
            dele.clicked.connect(lambda _, m=wm: self._delete_whisper(m))
            btns.addWidget(use); btns.addWidget(dele)
        else:
            dl = QPushButton(f"Descargar  ({wm.get('size','?')})"); dl.setObjectName("open")
            dl.setEnabled(engine_ready())
            if not engine_ready(): dl.setToolTip("Espera a que termine de instalarse el motor local")
            dl.clicked.connect(lambda _, m=wm: self._download_whisper(m))
            btns.addWidget(dl)
        for i in range(btns.count()):
            w = btns.itemAt(i).widget()
            if w: w.setCursor(Qt.PointingHandCursor)
        lay.addLayout(btns)
        return f

    def _voice_row(self, v, cfg):
        f = QFrame(); f.setObjectName("bookcard")
        row = QHBoxLayout(f); row.setContentsMargins(16, 10, 16, 10); row.setSpacing(10)
        name = QLabel(v["name"]); name.setObjectName("booktitle"); row.addWidget(name)
        if cfg.get("voice", "narrator") == v["id"]:
            chip = QLabel("activa"); chip.setObjectName("chip"); row.addWidget(chip)
        row.addStretch(1)
        use = QPushButton("Usar"); use.setObjectName("tb"); use.setCursor(Qt.PointingHandCursor)
        use.setEnabled(cfg.get("voice", "narrator") != v["id"])
        use.clicked.connect(lambda _, i=v["id"]: self._use_voice(i)); row.addWidget(use)
        if v["id"] != "narrator":
            rm = QPushButton("Borrar"); rm.setObjectName("tb"); rm.setCursor(Qt.PointingHandCursor)
            rm.clicked.connect(lambda _, vv=v: self._delete_voice(vv)); row.addWidget(rm)
        return f

    # --- modelos
    def _engine_changed(self, _):
        # Solo actualiza lo que cambia (fila de servidor + texto): no reconstruye
        # la lista de modelos/voces, así no parpadea ni salta el scroll.
        cfg = load_config(); cfg["engine"] = self.engineSel.currentData(); save_config(cfg)
        self._apply_engine_view(cfg)

    def _server_changed(self):
        cfg = load_config(); cfg["server"] = self.serverEdit.text().strip(); save_config(cfg)

    def _qc_changed(self, on):
        cfg = load_config(); cfg["qc_auto"] = bool(on); save_config(cfg)

    def _streams_changed(self, _):
        cfg = load_config(); cfg["parallel_streams"] = int(self.streamsSel.currentData() or 1); save_config(cfg)

    def _lang_changed(self, _):
        cfg = load_config(); cfg["language"] = self.langSel.currentData(); save_config(cfg)

    def _edit_pron(self):
        """Editor sencillo del diccionario de pronunciación (JSON validado)."""
        p = ensure_pron()
        try: cur = p.read_text("utf-8")
        except Exception: cur = json.dumps(DEFAULT_PRON, ensure_ascii=False, indent=2)
        txt, ok = QInputDialog.getMultiLineText(self, "Diccionario de pronunciación",
            "pron: palabra → cómo se dice · say_letters: siglas deletreadas · "
            "say_word: siglas como palabra", cur)
        if not ok: return
        try:
            data = json.loads(txt)
            assert isinstance(data, dict)
        except Exception:
            QMessageBox.warning(self, "Pronunciación", "El JSON no es válido; no se guardó."); return
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        self.progLabel.setText("Diccionario de pronunciación guardado.")

    def _use_model(self, mid):
        cfg = load_config(); cfg["model"] = mid; save_config(cfg); self.refresh()

    def _delete_model(self, mdl):
        d = model_dir(mdl["id"])
        if d.exists(): shutil.rmtree(d, ignore_errors=True)
        self.progLabel.setText(f"Modelo «{mdl['name']}» borrado (archivos)."); self.refresh()

    def _download_xtts(self, mdl):
        if self.dl and self.dl.isRunning(): return
        self.progLabel.setText("Descargando XTTS v2… (puede tardar, ~1,8 GB)")
        self.dl = XttsDownloadWorker()
        self.dl.progress.connect(lambda t: self.progLabel.setText(t))
        self.dl.done.connect(lambda: (self.progLabel.setText("XTTS v2 listo."), self.refresh()))
        self.dl.failed.connect(lambda e: (self.progLabel.setText("Error descargando XTTS: " + e), self.refresh()))
        self.dl.start(); self.refresh()

    def _delete_xtts(self, mdl):
        """Borra el modelo XTTS de la caché de coqui-tts (deja el venv)."""
        import glob
        removed = False
        for base in (Path.home() / "AppData/Local/tts", Path.home() / ".local/share/tts"):
            if base.exists():
                for d in base.glob("*xtts_v2*"):
                    shutil.rmtree(d, ignore_errors=True); removed = True
        cfg = load_config()
        if cfg.get("model") == mdl["id"]: cfg["model"] = MODELS[0]["id"]; save_config(cfg)
        self.progLabel.setText("Modelo XTTS borrado (caché)." if removed else "No había caché de XTTS.")
        self.refresh()

    def _remove_custom(self, mid):
        cfg = load_config()
        cfg["custom_models"] = [m for m in cfg.get("custom_models", []) if m["id"] != mid]
        if cfg.get("model") == mid: cfg["model"] = MODELS[0]["id"]
        save_config(cfg); self.refresh()

    def _add_model(self):
        repo, ok = QInputDialog.getText(self, "Añadir modelo",
            "Repo o URL de Hugging Face\n(p. ej. usuario/modelo o https://huggingface.co/usuario/modelo):")
        if not ok or not repo.strip(): return
        repo = repo.strip()
        if "huggingface.co/" in repo:
            repo = repo.split("huggingface.co/", 1)[1].strip("/")
            repo = "/".join(repo.split("/")[:2])
        ckpt, ok = QInputDialog.getText(self, "Añadir modelo",
            "Nombre del checkpoint dentro del repo:", text="model_last.safetensors")
        if not ok or not ckpt.strip(): return
        vocab, ok = QInputDialog.getText(self, "Añadir modelo",
            "Nombre del vocabulario dentro del repo:", text="vocab.txt")
        if not ok or not vocab.strip(): return
        arch, ok = QInputDialog.getText(self, "Añadir modelo",
            "Arquitectura (F5TTS_Base para la mayoría; F5TTS_v1_Base para el oficial v1):", text="F5TTS_Base")
        if not ok or not arch.strip(): return
        lang, ok = QInputDialog.getText(self, "Añadir modelo",
            "Idioma del modelo (código: es, en, fr, de, it, pt…):", text="es")
        if not ok or not lang.strip(): return
        name, ok = QInputDialog.getText(self, "Añadir modelo", "Nombre para mostrar:", text=repo.split("/")[-1])
        if not ok or not name.strip(): return
        cfg = load_config()
        customs = cfg.get("custom_models", [])
        mid = "u-" + slug(repo)
        customs = [m for m in customs if m["id"] != mid]
        customs.append({"id": mid, "name": name.strip(), "repo": repo,
                        "ckpt": ckpt.strip(), "vocab": vocab.strip(), "arch": arch.strip(),
                        "lang": lang.strip().lower(), "size": "?", "desc": "Añadido por ti", "custom": True})
        cfg["custom_models"] = customs; save_config(cfg)
        self.progLabel.setText(f"Modelo «{name.strip()}» añadido. Pulsa Descargar.")
        self.refresh()

    def _download(self, mdl):
        if self.dl and self.dl.isRunning(): return
        self.progLabel.setText(f"Descargando «{mdl['name']}»…")
        self.dl = DownloadWorker(mdl)
        self.dl.progress.connect(lambda t: self.progLabel.setText(t))
        self.dl.done.connect(lambda: (self.progLabel.setText("Descarga completada."), self.refresh()))
        self.dl.failed.connect(lambda e: (self.progLabel.setText("Error en la descarga: " + e), self.refresh()))
        self.dl.start(); self.refresh()

    # --- whisper (transcripción de audiolibros)
    def _use_whisper(self, wid):
        cfg = load_config(); cfg["whisper_model"] = wid; save_config(cfg); self.refresh()

    def _delete_whisper(self, wm):
        d = model_dir(wm["id"])
        if d.exists(): shutil.rmtree(d, ignore_errors=True)
        self.progLabel.setText(f"Modelo de transcripción «{wm['name']}» borrado."); self.refresh()

    def _download_whisper(self, wm):
        if self.dl and self.dl.isRunning(): return
        self.progLabel.setText(f"Descargando «{wm['name']}»…")
        self.dl = WhisperDownloadWorker(wm)
        self.dl.progress.connect(lambda t: self.progLabel.setText(t))
        self.dl.done.connect(lambda i=wm["id"]: (self._use_whisper(i),
                             self.progLabel.setText("Modelo de transcripción listo.")))
        self.dl.failed.connect(lambda e: (self.progLabel.setText("Error en la descarga: " + e), self.refresh()))
        self.dl.start(); self.refresh()

    # --- voces
    def _use_voice(self, vid):
        cfg = load_config(); cfg["voice"] = vid; save_config(cfg); self.refresh()

    def _delete_voice(self, v):
        for p in (v["wav"], v["txt"], v["wav"].with_suffix(".name")):
            try: p.unlink()
            except Exception: pass
        cfg = load_config()
        if cfg.get("voice") == v["id"]: cfg["voice"] = "narrator"
        save_config(cfg); self.refresh()

    def _autotranscribe(self, audio):
        """Transcribe un clip corto para rellenar el texto de referencia de la voz.
        Usa el Whisper local (si hay modelo) o el voz-server; devuelve texto o None."""
        cfg = load_config()
        try:
            if cfg.get("engine", "local") == "local":
                mdir = model_dir(active_whisper_id(cfg))
                if not (mdir / "model.bin").exists():
                    return None
                p = subprocess.run([str(VPY), "-I", str(CC_ENGINE), "whisper", "--audio", str(audio),
                                    "--model-dir", str(mdir), "--device", "auto",
                                    "--language", active_language(cfg) or "es"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", creationflags=NOWIN, timeout=240)
                segs = [json.loads(l[4:]).get("text", "") for l in p.stdout.splitlines()
                        if l.startswith("SEG ")]
                return " ".join(segs).strip() or None
            else:
                url = voz_http_url() + "/v1/audio/transcriptions"
                lang = active_language(cfg)
                cmd = ["curl", "-s", "--connect-timeout", "10", "-m", "240",
                       "-F", f"file=@{audio}", "-F", "response_format=json"]
                if lang and lang != "auto": cmd += ["-F", f"language={lang}"]
                cmd.append(url)
                p = subprocess.run(cmd, capture_output=True, creationflags=NOWIN, timeout=260)
                d = json.loads((p.stdout or b"").decode("utf-8", "replace"))
                return (d.get("text") or "").strip() or None
        except Exception:
            return None

    def _voice_save(self, audio, name, text):
        """Guarda la voz: reencoda a WAV 24k mono + .txt (referencia) + .name."""
        vid = slug(name); VOICES_DIR.mkdir(parents=True, exist_ok=True)
        dest_wav = VOICES_DIR / f"{vid}.wav"
        subprocess.run(["ffmpeg", "-y", "-i", str(audio), "-ar", "24000", "-ac", "1", str(dest_wav)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NOWIN)
        if not dest_wav.exists():
            raise RuntimeError("no se pudo convertir el audio (¿ffmpeg?)")
        (VOICES_DIR / f"{vid}.txt").write_text(text.strip(), encoding="utf-8")
        (VOICES_DIR / f"{vid}.name").write_text(name.strip(), encoding="utf-8")
        return vid

    def _voice_wizard(self, audio, cleanup=None):
        """Flujo común: autotranscribe el texto (editable) + nombre + guardar."""
        self.progLabel.setText("Transcribiendo la voz con Whisper…"); QApplication.processEvents()
        guess = self._autotranscribe(audio) or ""
        self.progLabel.setText("")
        txt, ok = QInputDialog.getMultiLineText(self, "Texto de la voz",
            "Texto que dice el audio (autodetectado; corrígelo si hace falta):", guess)
        if ok and txt.strip():
            name, ok = QInputDialog.getText(self, "Añadir voz", "Nombre para la voz:", text="Mi voz")
            if ok and name.strip():
                try:
                    vid = self._voice_save(audio, name, txt)
                    self._use_voice(vid)                 # activar + refrescar
                    self.progLabel.setText(f"Voz «{name.strip()}» añadida y activada.")
                except Exception as e:
                    self.progLabel.setText("No pude añadir la voz: " + str(e)); self.refresh()
        if cleanup:
            try: os.remove(cleanup)
            except OSError: pass

    def _add_voice(self):
        wav, _ = QFileDialog.getOpenFileName(self, "Audio de la voz (5-12 s, limpio)",
                                             str(Path.home()), "Audio (*.wav *.mp3 *.flac *.m4a *.ogg *.opus)")
        if wav: self._voice_wizard(wav)

    def _add_voice_url(self):
        url, ok = QInputDialog.getText(self, "Cargar voz por URL",
                                       "Enlace al audio de la voz (http/https, wav/mp3, 5-15 s):")
        if not ok or not url.strip(): return
        if not is_http_url(url):
            self.progLabel.setText("La URL debe empezar por http:// o https://"); return
        fd, tmp = tempfile.mkstemp(suffix=".audio"); os.close(fd)
        self.progLabel.setText("Descargando la voz…"); QApplication.processEvents()
        rc = subprocess.run(["curl", "-s", "-L", "--max-time", "120", "-o", tmp, url.strip()],
                            creationflags=NOWIN).returncode
        if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 2000:
            self.progLabel.setText("No se pudo descargar la voz desde esa URL.")
            try: os.remove(tmp)
            except OSError: pass
            return
        self._voice_wizard(tmp, cleanup=tmp)

    def _download_preset(self, pr):
        if not is_http_url(pr.get("url", "")):
            self.progLabel.setText("URL de voz no válida."); return
        fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        self.progLabel.setText(f"Descargando «{pr['name']}»…"); QApplication.processEvents()
        rc = subprocess.run(["curl", "-s", "-L", "--max-time", "120", "-o", tmp, pr["url"]],
                            creationflags=NOWIN).returncode
        if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 2000:
            self.progLabel.setText("No se pudo descargar la voz.")
            try: os.remove(tmp)
            except OSError: pass
            return
        text = pr.get("text") or (self._autotranscribe(tmp) or "")
        try:
            self._voice_save(tmp, pr["name"], text)
            self.progLabel.setText(f"Voz «{pr['name']}» descargada.")
        except Exception as e:
            self.progLabel.setText("Error al guardar la voz: " + str(e))
        try: os.remove(tmp)
        except OSError: pass
        self.refresh()

    def _preset_card(self, pr):
        f = QFrame(); f.setObjectName("bookcard")
        row = QHBoxLayout(f); row.setContentsMargins(16, 10, 16, 10); row.setSpacing(10)
        name = QLabel(f"{pr['name']}  · Idioma: {lang_name(pr.get('lang','es'))}")
        name.setObjectName("booktitle"); row.addWidget(name); row.addStretch(1)
        has = (VOICES_DIR / f"{slug(pr['name'])}.wav").exists()
        if has:
            lbl = QLabel("✓ Descargada"); lbl.setObjectName("booksub"); row.addWidget(lbl)
        else:
            dl = QPushButton("Descargar"); dl.setObjectName("open"); dl.setCursor(Qt.PointingHandCursor)
            dl.clicked.connect(lambda _, p=pr: self._download_preset(p)); row.addWidget(dl)
        return f


# ------------------------------------------------------------------- reproductor
class PlayerPage(QWidget):
    back = Signal()
    regen = Signal(str)
    resume = Signal(str)

    def __init__(self):
        super().__init__()
        self.manifest = []; self.cues = []; self.cap = 0
        self.book_dir = None; self._autoscroll = False; self._pause_until = 0.0
        self._restore_pos = None; self._pb_last_save = 0.0; self._last_pos = 0
        self._manual_scroll = False   # si el usuario paginó a mano, no recolocar solo

        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self._vol = max(0, min(100, int(load_config().get("volume", 100) or 100)))
        self.audio.setVolume(self._vol / 100); self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self.on_pos)
        self.player.durationChanged.connect(self.on_dur)
        self.player.playbackStateChanged.connect(self.on_state)
        self.player.mediaStatusChanged.connect(self.on_media_status)

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        head = QWidget(); head.setObjectName("head")
        hb = QHBoxLayout(head); hb.setContentsMargins(10, 8, 16, 8); hb.setSpacing(10)
        self.backBtn = QPushButton("‹ Biblioteca"); self.backBtn.setObjectName("tb")
        self.backBtn.setCursor(Qt.PointingHandCursor); self.backBtn.clicked.connect(self.back.emit)
        self.booklbl = QLabel(""); self.booklbl.setObjectName("word2")
        self.regenBtn = QPushButton("↻ Regenerar"); self.regenBtn.setObjectName("tb")
        self.regenBtn.setCursor(Qt.PointingHandCursor)
        self.regenBtn.setToolTip("Volver a narrar el libro entero desde su EPUB")
        self.regenBtn.clicked.connect(self._do_regen); self.regenBtn.hide()
        self.resumeBtn = QPushButton("▸ Reanudar"); self.resumeBtn.setObjectName("tb")
        self.resumeBtn.setCursor(Qt.PointingHandCursor)
        self.resumeBtn.setToolTip("Seguir narrando desde donde se detuvo")
        self.resumeBtn.clicked.connect(self._do_resume); self.resumeBtn.hide()
        self.exportBtn = QPushButton("⤓ Exportar"); self.exportBtn.setObjectName("tb")
        self.exportBtn.setCursor(Qt.PointingHandCursor)
        self.exportBtn.setToolTip("Guardar el audio narrado como MP3")
        self.exportBtn.clicked.connect(self._export); self.exportBtn.hide()
        hb.addWidget(self.backBtn); hb.addWidget(self.booklbl); hb.addWidget(self.resumeBtn)
        hb.addWidget(self.regenBtn); hb.addWidget(self.exportBtn); hb.addStretch(1)
        spk = QLabel("🔊"); spk.setObjectName("time"); hb.addWidget(spk)
        self.outSel = QComboBox(); self.outSel.setMaximumWidth(190); self.outSel.setToolTip("Salida de audio")
        self.outSel.currentIndexChanged.connect(self._out_changed)
        hb.addWidget(self.outSel)
        self.volSld = QSlider(Qt.Horizontal); self.volSld.setRange(0, 100)
        self.volSld.setValue(self._vol); self.volSld.setFixedWidth(90)
        self.volSld.setToolTip("Volumen")
        self.volSld.valueChanged.connect(self._vol_changed)
        hb.addWidget(self.volSld)
        self.chap = QComboBox(); self.chap.setMinimumWidth(150)
        self.chap.currentIndexChanged.connect(self.on_chap_change)
        hb.addWidget(self.chap)
        root.addWidget(head)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setObjectName("reader")
        self.scroll.verticalScrollBar().valueChanged.connect(self.on_user_scroll)
        inner = QWidget(); self.reader = QVBoxLayout(inner)
        self.reader.setContentsMargins(28, 26, 28, 320); self.reader.setSpacing(2)
        self.chtitle = QLabel(""); self.chtitle.setObjectName("chtitle"); self.chtitle.setWordWrap(True)
        self.reader.addWidget(self.chtitle)
        self.flow = QVBoxLayout(); self.flow.setSpacing(0); self.reader.addLayout(self.flow)
        self.reader.addStretch(1)
        self.scroll.setWidget(inner)
        root.addWidget(self.scroll, 1)

        bar = QWidget(); bar.setObjectName("bar")
        bv = QVBoxLayout(bar); bv.setContentsMargins(16, 10, 16, 12); bv.setSpacing(8)
        ctr = QHBoxLayout(); ctr.setSpacing(8)
        self.prevBtn = QPushButton("⏮"); self.back10 = QPushButton("⟲ 10")
        self.playBtn = QPushButton("▶"); self.playBtn.setObjectName("play")
        self.fwd10 = QPushButton("10 ⟳"); self.nextBtn = QPushButton("⏭")
        for b in (self.prevBtn, self.back10, self.playBtn, self.fwd10, self.nextBtn):
            if b.objectName() != "play": b.setObjectName("tb")
            b.setCursor(Qt.PointingHandCursor)
        self.prevBtn.clicked.connect(lambda: self.load_chap(self.cap - 1, True))
        self.nextBtn.clicked.connect(lambda: self.load_chap(self.cap + 1, True))
        self.back10.clicked.connect(lambda: self.player.setPosition(max(0, self.player.position() - 10000)))
        self.fwd10.clicked.connect(lambda: self.player.setPosition(self.player.position() + 10000))
        self.playBtn.clicked.connect(self.toggle)
        self.tcur = QLabel("0:00"); self.tcur.setObjectName("time")
        self.speed = QComboBox()
        self._speeds = [round(0.75 + 0.05 * i, 2) for i in range(26)]  # 0,75 .. 2,00 en pasos de 0,05
        for v in self._speeds:
            lbl = f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")
            self.speed.addItem(f"{lbl}×")
        self.speed.setCurrentIndex(self._speeds.index(1.0))
        self.speed.currentIndexChanged.connect(self.on_speed)
        spd = QLabel("Velocidad"); spd.setObjectName("time")
        ctr.addWidget(self.prevBtn); ctr.addWidget(self.back10); ctr.addWidget(self.playBtn)
        ctr.addWidget(self.fwd10); ctr.addWidget(self.nextBtn); ctr.addSpacing(10); ctr.addWidget(self.tcur)
        ctr.addStretch(1); ctr.addWidget(spd); ctr.addWidget(self.speed)
        bv.addLayout(ctr)

        # --- fila de navegación: páginas de texto, ir a posición e ir a capítulo
        nav = QHBoxLayout(); nav.setSpacing(8)
        self.pageUpBtn = QPushButton("▲ Página"); self.pageDnBtn = QPushButton("Página ▼")
        self.pageUpBtn.setToolTip("Retroceder una página del texto")
        self.pageDnBtn.setToolTip("Avanzar una página del texto")
        self.gotoPosBtn = QPushButton("⤓ Ir a posición…")
        self.gotoPosBtn.setToolTip("Saltar a un minuto:segundo del capítulo")
        self.gotoChapBtn = QPushButton("≡ Ir a capítulo…")
        self.gotoChapBtn.setToolTip("Elegir un capítulo de la lista")
        for b in (self.pageUpBtn, self.pageDnBtn, self.gotoPosBtn, self.gotoChapBtn):
            b.setObjectName("tb"); b.setCursor(Qt.PointingHandCursor)
        self.pageUpBtn.clicked.connect(lambda: self._page(-1))
        self.pageDnBtn.clicked.connect(lambda: self._page(1))
        self.gotoPosBtn.clicked.connect(self._goto_position)
        self.gotoChapBtn.clicked.connect(self._goto_chapter)
        nav.addWidget(self.pageUpBtn); nav.addWidget(self.pageDnBtn)
        nav.addStretch(1)
        nav.addWidget(self.gotoPosBtn); nav.addWidget(self.gotoChapBtn)
        bv.addLayout(nav)

        sl = QHBoxLayout(); sl.setSpacing(10)
        self.seek = QSlider(Qt.Horizontal); self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.seek.sliderReleased.connect(lambda: self.player.setPosition(self.seek.value()))
        self.tdur = QLabel("0:00"); self.tdur.setObjectName("time")
        sl.addWidget(self.seek, 1); sl.addWidget(self.tdur)
        bv.addLayout(sl)
        root.addWidget(bar)

        self.setStyleSheet(STYLE)
        self._shortcuts()
        self._populate_outputs()

    def _shortcuts(self):
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle)
        QShortcut(QKeySequence(Qt.Key_Right), self,
                  activated=lambda: self.player.setPosition(self.player.position() + 10000))
        QShortcut(QKeySequence(Qt.Key_Left), self,
                  activated=lambda: self.player.setPosition(max(0, self.player.position() - 10000)))
        QShortcut(QKeySequence(Qt.Key_PageUp), self, activated=lambda: self._page(-1))
        QShortcut(QKeySequence(Qt.Key_PageDown), self, activated=lambda: self._page(1))

    def _page(self, direction):
        """Desplaza el texto una página (viewport) arriba/abajo."""
        sb = self.scroll.verticalScrollBar()
        step = sb.pageStep() or self.scroll.viewport().height()
        sb.setValue(sb.value() + direction * int(step * 0.9))

    def _goto_position(self):
        """Pide minuto:segundo (o segundos) y salta ahí dentro del capítulo."""
        dur = self.player.duration()
        txt, ok = QInputDialog.getText(self, "Ir a posición",
                                       f"Minuto:segundo (capítulo de {fmt(dur)}):",
                                       QLineEdit.Normal, fmt(self.player.position()))
        if not ok or not txt.strip():
            return
        s = txt.strip().replace(",", ":")
        try:
            if ":" in s:
                mm, ss = s.split(":", 1); secs = int(mm) * 60 + float(ss)
            else:
                secs = float(s)
        except ValueError:
            QMessageBox.warning(self, "Ir a posición", "Escribe el tiempo como «minutos:segundos», p. ej. 3:40.")
            return
        pos = max(0, int(secs * 1000))
        if dur > 0:
            pos = min(pos, dur)
        self._manual_scroll = False      # ir a posición: vuelve a seguir la narración
        self.player.setPosition(pos); self.player.play()

    def _goto_chapter(self):
        """Lista los capítulos y salta al elegido."""
        if not self.manifest:
            return
        if len(self.manifest) == 1:
            QMessageBox.information(self, "Ir a capítulo", "Este título tiene un solo capítulo.")
            return
        items = [chap_label(m) for m in self.manifest]
        cur = self.cap if 0 <= self.cap < len(items) else 0
        item, ok = QInputDialog.getItem(self, "Ir a capítulo", "Capítulo:", items, cur, False)
        if ok and item in items:
            self.load_chap(items.index(item),
                           self.player.playbackState() == QMediaPlayer.PlayingState)

    def _populate_outputs(self):
        self._devs = QMediaDevices.audioOutputs()
        cfg = load_config(); want = cfg.get("audio_device"); sel = 0
        self.outSel.blockSignals(True); self.outSel.clear()
        for i, d in enumerate(self._devs):
            self.outSel.addItem(d.description())
            if want and d.description() == want: sel = i
        if self.outSel.count(): self.outSel.setCurrentIndex(sel)
        self.outSel.blockSignals(False)
        self._apply_output()

    def _apply_output(self):
        i = self.outSel.currentIndex()
        if 0 <= i < len(getattr(self, "_devs", [])):
            self.audio.setDevice(self._devs[i])

    def _out_changed(self, _):
        self._apply_output()
        i = self.outSel.currentIndex()
        if 0 <= i < len(getattr(self, "_devs", [])):
            cfg = load_config(); cfg["audio_device"] = self._devs[i].description(); save_config(cfg)

    def _vol_changed(self, v):
        v = max(0, min(100, int(v)))
        self._vol = v; self.audio.setVolume(v / 100)
        cfg = load_config(); cfg["volume"] = v; save_config(cfg)

    def stop(self):
        self._save_playback()                               # recordar el punto al salir
        self.player.stop()

    def _do_regen(self):
        src = self.book_dir / "source.epub"
        if not src.exists(): return
        r = QMessageBox.question(self, "Regenerar",
                                 "¿Volver a narrar el libro entero desde cero? Se sobrescribe lo actual.")
        if r == QMessageBox.StandardButton.Yes:
            self.regen.emit(str(src))

    def _do_resume(self):
        if self.book_dir:
            self.resume.emit(str(self.book_dir))

    def _export(self):
        if not self.book_dir or not self.manifest:
            return
        mp3s = [self.book_dir / f"{m['base']}.mp3" for m in self.manifest]
        mp3s = [p for p in mp3s if p.exists()]
        if not mp3s:
            QMessageBox.warning(self, "Exportar", "No hay audio generado para exportar.")
            return
        base = (self.booklbl.text().strip() or self.book_dir.name) or "narracion"
        safe = re.sub(r'[\\/:*?"<>|]+', " ", base).strip() or "narracion"
        dst, _ = QFileDialog.getSaveFileName(self, "Guardar narración como MP3",
                                             str(Path.home() / f"{safe}.mp3"), "Audio MP3 (*.mp3)")
        if not dst:
            return
        dst = Path(dst)
        if dst.suffix.lower() != ".mp3":
            dst = dst.with_suffix(".mp3")
        try:
            if len(mp3s) == 1:
                shutil.copyfile(mp3s[0], dst)
            else:
                # Los capN.mp3 son todos CBR 128k -> se concatenan sin recodificar.
                fd, lst = tempfile.mkstemp(suffix=".txt"); os.close(fd)
                with open(lst, "w", encoding="utf-8") as f:
                    for p in mp3s:
                        f.write("file '%s'\n" % str(p).replace("'", "'\\''"))
                tmp = str(dst) + ".tmp.mp3"
                r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                                    "-c", "copy", tmp],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=NOWIN)
                try: os.remove(lst)
                except OSError: pass
                if r.returncode != 0 or not os.path.exists(tmp):
                    raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[-400:] or "ffmpeg falló")
                os.replace(tmp, dst)
        except FileNotFoundError:
            QMessageBox.critical(self, "Exportar",
                                 "No se encontró ffmpeg. Hace falta para unir los capítulos.")
            return
        except Exception as e:
            QMessageBox.critical(self, "Exportar", f"No se pudo exportar:\n{e}")
            return
        QMessageBox.information(self, "Exportar",
                                f"Guardado:\n{dst}\n\n{len(mp3s)} capítulo(s).")

    def load_book(self, folder):
        self.book_dir = Path(folder)
        self.regenBtn.setVisible((self.book_dir / "source.epub").exists())
        self.resumeBtn.setVisible(book_resumable(self.book_dir))
        self.exportBtn.setVisible(True)
        self.manifest = json.loads((self.book_dir / "manifest.json").read_text("utf-8"))
        tf = self.book_dir / "title.txt"
        self.booklbl.setText(tf.read_text("utf-8").strip() if tf.exists()
                             else self.book_dir.name.replace("-", " ").capitalize())
        self.chap.blockSignals(True); self.chap.clear()
        for i, m in enumerate(self.manifest):
            self.chap.addItem(chap_label(m))
        self.chap.setVisible(len(self.manifest) > 1)
        self.chap.blockSignals(False)
        self.gotoChapBtn.setEnabled(len(self.manifest) > 1)
        self.prevBtn.setEnabled(len(self.manifest) > 1)
        self.nextBtn.setEnabled(len(self.manifest) > 1)
        pb = self._read_playback()                          # retomar por donde se escuchaba
        start_cap = pb["cap"] if pb else 0
        self._restore_pos = pb["pos"] if pb else None
        self._pb_last_save = time.monotonic()
        self.load_chap(start_cap, False)

    def _read_playback(self):
        """Lee el punto de escucha guardado (capítulo + ms), validado."""
        try:
            pb = json.loads((self.book_dir / "playback.json").read_text("utf-8"))
            cap = int(pb.get("cap", 0)); pos = int(pb.get("pos", 0))
            if 0 <= cap < len(self.manifest) and pos >= 0:
                return {"cap": cap, "pos": pos}
        except Exception:
            pass
        return None

    def _save_playback(self, pos=None):
        """Guarda el punto de escucha actual en el propio libro. Usa la última
        posición REAL vista (self._last_pos): al salir, la posición en vivo ya
        puede haberse reseteado a 0, y escribiríamos 0 por error."""
        if not self.book_dir:
            return
        try:
            p = self._last_pos if pos is None else pos
            (self.book_dir / "playback.json").write_text(
                json.dumps({"cap": self.cap, "pos": int(max(0, p))}), "utf-8")
        except Exception:
            pass

    def load_chap(self, i, autoplay):
        if not self.manifest or i < 0 or i >= len(self.manifest): return
        self.cap = i
        self.chap.blockSignals(True); self.chap.setCurrentIndex(i); self.chap.blockSignals(False)
        m = self.manifest[i]
        data = json.loads((self.book_dir / f"{m['base']}.json").read_text("utf-8"))
        self.cues = data.get("cues", [])
        self.chtitle.setText(data.get("title") or m["title"])
        while self.flow.count():
            wdg = self.flow.takeAt(0).widget()
            if wdg: wdg.deleteLater()
        self.cue_widgets = []
        for c in self.cues:
            cue = Cue(c["text"], float(c["start"])); cue.clicked.connect(self.seek_to)
            self.flow.addWidget(cue); self.cue_widgets.append((c, cue))
        self._last_pos = 0
        self.player.setSource(QUrl.fromLocalFile(str(self.book_dir / f"{m['base']}.mp3")))
        self.on_speed(self.speed.currentIndex())
        self.scroll.verticalScrollBar().setValue(0)
        self._manual_scroll = False      # capítulo nuevo: vuelve a seguir la narración
        if autoplay: self.player.play()

    def seek_to(self, sec):
        self._manual_scroll = False      # saltar a una frase: vuelve a seguir la narración
        self.player.setPosition(int(sec * 1000) + 10); self.player.play()

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState: self.player.pause()
        else: self.player.play()

    def on_state(self, st):
        self.playBtn.setText("⏸" if st == QMediaPlayer.PlayingState else "▶")
        if st == QMediaPlayer.PausedState:
            self._save_playback()

    def on_media_status(self, st):
        if st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._apply_restore()
        if st == QMediaPlayer.EndOfMedia and self.cap < len(self.manifest) - 1:
            self.load_chap(self.cap + 1, True)

    def _apply_restore(self):
        """Salta al punto de escucha guardado en cuanto se conoce la duración."""
        if self._restore_pos is None:
            return
        d = self.player.duration()
        if d > 0:
            self.player.setPosition(min(self._restore_pos, max(0, d - 500)))
            self._last_pos = self.player.position()
            self._restore_pos = None

    def on_dur(self, d):
        self.seek.setRange(0, d); self.tdur.setText(fmt(d))
        self._apply_restore()

    def on_pos(self, p):
        if not self.seek.isSliderDown(): self.seek.setValue(p)
        self.tcur.setText(fmt(p)); self.highlight(p / 1000.0)
        if p > 0:
            self._last_pos = p                      # última posición real conocida
            if time.monotonic() - self._pb_last_save > 5:
                self._pb_last_save = time.monotonic(); self._save_playback()

    def highlight(self, t):
        active = None
        for c, w in getattr(self, "cue_widgets", []):
            on = c["start"] <= t < c["end"]
            if w.property("active") != on:
                w.setProperty("active", on); w.style().unpolish(w); w.style().polish(w)
            if on: active = w
        if active and not self._manual_scroll and time.monotonic() > self._pause_until:
            self._autoscroll = True
            self.scroll.ensureWidgetVisible(active, 0, 140)
            QTimer.singleShot(0, lambda: setattr(self, "_autoscroll", False))

    def on_user_scroll(self, _):
        if not self._autoscroll:
            self._manual_scroll = True            # desplazó a mano: no volver solo
            self._pause_until = time.monotonic() + 4.5

    def on_speed(self, idx):
        if 0 <= idx < len(self._speeds):
            self.player.setPlaybackRate(self._speeds[idx])

    def on_chap_change(self, i):
        self.load_chap(i, self.player.playbackState() == QMediaPlayer.PlayingState)


# ------------------------------------------------------------------- ventana
class Cuentacuentos(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Storyteller")
        self.resize(820, 720)
        self.setMinimumSize(560, 520)      # no romper el layout al encoger
        self.stack = QStackedWidget(); self.setCentralWidget(self.stack)
        self.library = LibraryPage(); self.player = PlayerPage(); self.models = ModelsScreen()
        self.stack.addWidget(self.library); self.stack.addWidget(self.player); self.stack.addWidget(self.models)
        self.library.openEpub.connect(self.open_epub)
        self.library.openBook.connect(self.open_book)
        self.library.openModels.connect(self.show_models)
        self.library.importAudio.connect(self.import_audio)
        self.library.resumeBook.connect(self._resume_gen)
        self.player.back.connect(self.show_library)
        self.player.regen.connect(self._regen)
        self.player.resume.connect(self._resume_gen)
        self.models.back.connect(self.show_library)
        self.setStyleSheet(STYLE)

    def show_library(self):
        self.player.stop()
        self.library.refresh()
        self.stack.setCurrentWidget(self.library)

    def show_models(self):
        self.models.refresh()
        self.stack.setCurrentWidget(self.models)

    def open_book(self, folder):
        self.player.load_book(folder)
        self.stack.setCurrentWidget(self.player)

    def open_epub(self):
        path, _ = QFileDialog.getOpenFileName(self, "Elige un EPUB", str(Path.home()),
                                              "Libros EPUB (*.epub)")
        if path: self._generate(path)

    def import_audio(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Elige un audiolibro para importar", str(Path.home()),
            "Audio (*.mp3 *.m4a *.aac *.ogg *.opus *.flac *.wav *.wma);;Todos (*.*)")
        if not path:
            return
        self._start_import(ImportWorker(src=path))

    def _start_import(self, worker, resume=False):
        self.imp = worker
        self.library.set_generating(True, "Reanudando transcripción…" if resume else "Preparando…", -1)
        self.imp.progress.connect(lambda t, p: self.library.set_generating(True, t, p))
        self.imp.failed.connect(self._import_fail)
        self.imp.done.connect(self._import_done)
        self.imp.canceled.connect(self._import_canceled)
        self.library.stopBtn.clicked.connect(self._imp_stop)
        self.library.cancelBtn.clicked.connect(self._imp_cancel)
        self.imp.start()

    def _imp_stop(self):
        self.library.set_stopping("Deteniendo y guardando lo transcrito…")
        self.imp.stop(True)

    def _imp_cancel(self):
        self.library.set_stopping("Cancelando…")
        self.imp.stop(False)

    def _import_fail(self, msg):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        QMessageBox.warning(self, "Importar audiolibro", msg)

    def _import_canceled(self):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()

    def _import_done(self, folder):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        self.open_book(folder)

    def _regen(self, epubpath):
        self.show_library()
        self._generate(epubpath, name=Path(epubpath).parent.name)

    def _resume_gen(self, folder):
        st = read_gen_state(folder) or {}
        if st.get("kind") == "import":                       # reanudar una transcripción Whisper
            self.show_library()
            self._start_import(ImportWorker(resume_dir=folder), resume=True)
            return
        src = Path(folder) / "source.epub"
        if not src.exists():
            QMessageBox.warning(self, "Reanudar",
                                "No encuentro el EPUB original de este libro, no puedo reanudar.")
            return
        self.show_library()
        self._generate(str(src), name=Path(folder).name, resume=True)

    def _generate(self, path, name=None, resume=False):
        if not path or not os.path.exists(path): return
        # Candado SÍNCRONO: no permitir una segunda narración mientras otra está en
        # marcha. Un doble disparo (p. ej. doble clic) lanzaba procesos duplicados que
        # se pisaban en la GPU. Usamos una bandera y no worker.isRunning(), porque el
        # hilo tarda un instante en figurar como "running" tras start() y el 2º disparo
        # se colaba en ese hueco. La bandera se marca aquí, antes de arrancar nada.
        if getattr(self, "_gen_busy", False):
            QMessageBox.information(self, "Storyteller",
                "Ya hay una narración en marcha. Espera a que termine o deténla "
                "antes de empezar otra.")
            return
        cfg = load_config()
        if cfg["engine"] == "local":
            m = model_by_id(cfg["model"], cfg)
            ready = xtts_ready() if m.get("backend") == "xtts" else engine_ready()
            if not ready:
                QMessageBox.information(self, "Storyteller",
                    "El motor todavía se está instalando. Prueba dentro de un rato, "
                    "o cambia a «Servidor propio» en Modelos.")
                return
            if not model_installed(m):
                QMessageBox.information(self, "Storyteller",
                    f"Antes descarga el modelo «{m['name']}» en Modelos.")
                self.show_models(); return
            self.worker = LocalGenWorker(path, m, active_voice(cfg), name=name, resume=resume)
        else:
            self.worker = GenWorker(path, name=name, resume=resume)
        self._gen_busy = True                             # ya comprometidos: bloquea un 2º disparo
        self.library.set_generating(True, "Reanudando…" if resume else "Preparando…", -1)
        self.worker.progress.connect(lambda t, p: self.library.set_generating(True, t, p))
        self.worker.failed.connect(self._gen_fail)
        self.worker.done.connect(self._gen_done)
        self.worker.canceled.connect(self._gen_canceled)
        self.library.stopBtn.clicked.connect(self._on_stop)
        self.library.cancelBtn.clicked.connect(self._on_cancel)
        self.worker.start()

    def _on_stop(self):
        self.library.set_stopping("Deteniendo y guardando lo generado…")
        self.worker.stop(True)

    def _on_cancel(self):
        self.library.set_stopping("Cancelando…")
        self.worker.stop(False)

    def _cleanup_gen_conns(self):
        self._gen_busy = False                            # libera el candado de narración
        for btn in (self.library.stopBtn, self.library.cancelBtn):
            try: btn.clicked.disconnect()
            except Exception: pass

    def _gen_fail(self, msg):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        QMessageBox.warning(self, "Storyteller", msg)

    def _gen_canceled(self):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()

    def _gen_done(self, folder):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        self.open_book(folder)

    # ---- cierre de la aplicación -----------------------------------------
    def _stop_worker_and_wait(self, worker, label):
        """Al cerrar la app: detiene y GUARDA el trabajo en curso, esperando a que el
        worker vuelque su estado a disco (manifest.json + gen_state.json). Sin esta
        espera, matar la ventana corta el QThread de golpe y el libro queda en limbo
        (sin manifest → ni aparece en la biblioteca ni es reanudable)."""
        if not worker or not worker.isRunning():
            return
        try: worker.stop(True)                       # equivale a «Detener y guardar»
        except Exception: pass
        dlg = QProgressDialog(label, "", 0, 0, self)     # barra indeterminada, sin cancelar
        dlg.setWindowTitle("Storyteller")
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()
        deadline = time.monotonic() + 45             # tope de seguridad: el cierre nunca cuelga
        while worker.isRunning() and time.monotonic() < deadline:
            worker.wait(150)
            QApplication.processEvents()
        try: dlg.close()
        except Exception: pass

    def closeEvent(self, event):
        # Guardar el marcador de escucha del reproductor (stop() lo persiste antes de parar).
        try: self.player.stop()
        except Exception: pass
        # Detener y GUARDAR cualquier narración / transcripción en marcha, esperando a que
        # el estado quede escrito (→ el libro aparece «a medias» y se puede reanudar).
        try: self._stop_worker_and_wait(getattr(self, "worker", None),
                                         "Guardando la narración en curso…")
        except Exception: pass
        try: self._stop_worker_and_wait(getattr(self, "imp", None),
                                         "Guardando la transcripción en curso…")
        except Exception: pass
        event.accept()


STYLE = """
* { font-family: "Segoe UI", sans-serif; color: #ece2d0; }
QStackedWidget, QWidget { background: #16120d; }
#head { background: #211b13; border-bottom: 1px solid #352d22; }
#word { font-family: "Georgia", serif; font-style: italic; font-size: 21px; font-weight: 600; }
#word2 { font-family: "Georgia", serif; font-style: italic; font-size: 16px; font-weight: 600; }
#tag { font-size: 10px; color: #e3a94f; letter-spacing: 1px; }
#open { background: #e3a94f; color: #1a140c; border: none; border-radius: 8px; padding: 8px 14px; font-weight: 600; }
#open:disabled { background: #4a3f2c; color: #8a7d64; }
QComboBox { background: #16120d; border: 1px solid #352d22; border-radius: 8px; padding: 6px 10px; }
QComboBox QAbstractItemView { background: #211b13; selection-background-color: #e3a94f; selection-color: #1a140c; }
#reader { background: #16120d; border: none; }
#bookcard { background: #211b13; border: 1px solid #352d22; border-radius: 12px; }
#bookcard:hover { border-color: #e3a94f; }
#booktitle { font-family: "Georgia", serif; font-size: 19px; font-weight: 600; }
#booksub { color: #a2957f; font-size: 13px; }
#chip { background: #e3a94f; color: #1a140c; border-radius: 8px; padding: 1px 7px; font-size: 10px; font-weight: 600; }
#sec { font-family: "Georgia", serif; font-size: 16px; font-weight: 600; color: #ece2d0; margin-top: 6px; }
#trash { background: transparent; border: none; font-size: 15px; padding: 2px 6px; }
#trash:hover { color: #ff8a80; }
#chtitle { font-family: "Georgia", serif; font-size: 30px; font-weight: 600; margin-bottom: 18px; }
#hint { color: #a2957f; font-size: 15px; }
QLabel#cue { font-family: "Georgia", serif; font-size: 21px; color: rgba(236,226,208,0.40);
    padding: 4px 6px; border-radius: 6px; }
QLabel#cue[active="true"] { color: #ece2d0; background: rgba(227,169,79,0.17); }
#bar { background: #211b13; border-top: 1px solid #352d22; }
#progwrap { background: #211b13; border-top: 1px solid #352d22; }
#proglabel { color: #a2957f; font-size: 12px; }
QProgressBar { background: #16120d; border: none; border-radius: 3px; }
QProgressBar::chunk { background: #e3a94f; border-radius: 3px; }
#time { color: #a2957f; font-size: 12px; }
QPushButton#tb { background: #16120d; border: 1px solid #352d22; border-radius: 8px; padding: 7px 11px; }
QPushButton#tb:hover { border-color: #e3a94f; color: #e3a94f; }
#play { background: #e3a94f; color: #1a140c; border: none; border-radius: 8px; font-size: 16px; min-width: 46px; padding: 7px 11px; }
QSlider::groove:horizontal { height: 4px; background: #352d22; border-radius: 2px; }
QSlider::handle:horizontal { background: #e3a94f; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }
QSlider::sub-page:horizontal { background: #e3a94f; border-radius: 2px; }
QScrollBar:vertical { background: #16120d; width: 10px; }
QScrollBar::handle:vertical { background: #352d22; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


def main():
    app = QApplication(sys.argv)
    _ico = HOME_CC / "storyteller.ico"
    if _ico.exists(): app.setWindowIcon(QIcon(str(_ico)))
    win = Cuentacuentos(); win.show()
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "--book":
        QTimer.singleShot(200, lambda: win.open_book(a[1]))
    elif a and a[0].lower().endswith(".epub"):
        QTimer.singleShot(200, lambda: win._generate(a[0]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
