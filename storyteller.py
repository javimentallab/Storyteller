# -*- coding: utf-8 -*-
"""
Storyteller — app de escritorio (PySide6).
Biblioteca de libros narrados + reproductor con subtitulos sincronizados.
Lee un EPUB, lo manda al servidor para narrarlo con la voz neural,
se trae la grabacion + el mapa de tiempos y lo reproduce (frase que suena
resaltada, clic para saltar). La generacion corre en el servidor (GPU) por SSH.
"""
import sys, os, re, json, shutil, subprocess, time, tempfile, threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer, QTranslator, QLibraryInfo
from PySide6.QtGui import QShortcut, QKeySequence, QIcon, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QStackedWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSlider, QScrollArea, QFileDialog, QProgressBar,
    QFrame, QMessageBox, QInputDialog, QLineEdit, QCheckBox, QProgressDialog, QSizePolicy, QGridLayout,
    QDialog, QPlainTextEdit,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices

from st_lang import _, LANG  # interface texts in the user's language
from st_translate import TranslatePage, _sz  # pantalla «Traducir documentos»
from st_core import (ACTIVE_GEN, EtaTracker, _atomic_write, book_resumable, chapter_is_partial, install_incoming,
                     live_manifest, merge_stream_manifests, read_gen_state, recover_interrupted, slug,
                     split_ranges, snapshot_chapter as core_snapshot_chapter, venv_has, BASE_PACKAGES, F5_PACKAGES,
                     XTTS_PACKAGES, install_commands, qc_can_run, whisper_target, whisper_venv, VENV_XTTS)

__version__ = "1.2.1"

MINT       = ""                      # servidor propio opcional (user@host); se configura en Modelos
REMOTE_DIR = "epub-tts"                # relativa al home del usuario del servidor
HOME_CC    = Path.home() / "cuentacuentos"
LOCAL_BOOKS = HOME_CC / "books"
ENGINE_DIR = HOME_CC / "engine"
# Ruta del Python del motor: Windows usa Scripts/, Linux/macOS usan bin/.
# engine/.venv: F5, o solo leer EPUB y Whisper si no se narra en este equipo. Lo tiene quien lo pida.
_VENV      = HOME_CC / "engine" / ".venv"
VPY        = _VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CC_ENGINE  = HOME_CC / "engine" / "cc_engine.py"
# Motor XTTS (multilingüe) en su propio venv, aislado del de F5.
_VENV_XTTS = HOME_CC / "engine" / ".venv-xtts"
VPY_XTTS   = _VENV_XTTS / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CC_XTTS    = HOME_CC / "engine" / "cc_xtts.py"
CC_SERVICE = HOME_CC / "engine" / "cc_service.py"   # narrar con un servicio por URL
SERVICE_KEY = HOME_CC / "service.key"
SERVICE_QC_KEY = HOME_CC / "service_qc.key"   # la del Whisper que revisa: puede ser de otro
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
    {"id": "f5-spanish", "name": _("F5 Español"), "backend": "f5", "repo": "jpgallegoar/F5-Spanish",
     "ckpt": "model_1250000.safetensors", "vocab": "vocab.txt", "arch": "F5TTS_Base",
     "lang": "es", "size": "~1,3 GB", "desc": _("Castellano peninsular, alta calidad.")},
    {"id": "f5-english", "name": _("F5 Inglés (oficial)"), "backend": "f5", "repo": "SWivid/F5-TTS",
     "ckpt": "F5TTS_v1_Base/model_1250000.safetensors", "vocab": "F5TTS_v1_Base/vocab.txt",
     "arch": "F5TTS_v1_Base", "lang": "en", "size": "~1,3 GB",
     "desc": _("Inglés (modelo base oficial F5-TTS). Mejor con una voz de referencia en inglés.")},
    {"id": "xtts-v2", "name": _("XTTS v2 (multilingüe)"), "backend": "xtts", "lang": "es",
     "repo": "coqui/XTTS-v2", "size": "~1,8 GB",
     "desc": _("Multilingüe: pronuncia bien los términos en inglés dentro del español. "
             "Clona tu voz. Usa más VRAM que F5.")},
]

DEFAULT_MODEL = "xtts-v2"            # F5 es opcional: no puede ser el modelo de partida

# Modelos de transcripción (faster-whisper) para importar audiolibros EN LOCAL,
# sin depender de un servidor. Se descargan de Hugging Face como los de voz.
WHISPER_MODELS = [
    {"id": "whisper-small", "name": "Whisper small", "repo": "Systran/faster-whisper-small",
     "size": "~0,5 GB", "desc": _("Rápido; calidad suficiente para subtítulos de audiolibros.")},
    {"id": "whisper-medium", "name": "Whisper medium", "repo": "Systran/faster-whisper-medium",
     "size": "~1,5 GB", "desc": _("Mejor calidad, algo más lento.")},
    {"id": "whisper-large-v3", "name": "Whisper large-v3", "repo": "Systran/faster-whisper-large-v3",
     "size": "~3 GB", "desc": _("Máxima calidad; el más pesado y lento.")},
]

# Voces de referencia descargables (clips públicos). El usuario también puede
# cargar las suyas por archivo o URL (con autotranscripción del texto).
VOICE_PRESETS = [
    {"id": "en-nature", "name": _("Inglés — ejemplo F5"), "lang": "en",
     "url": "https://raw.githubusercontent.com/SWivid/F5-TTS/main/src/f5_tts/infer/examples/basic/basic_ref_en.wav",
     "text": "Some call me nature, others call me mother nature."},
]

# Idiomas para la transcripción de audiolibros (Whisper es multilingüe). "auto" = detectar.
LANGS = [("es", _("Español")), ("auto", _("Detectar automáticamente")), ("en", _("Inglés")),
         ("fr", _("Francés")), ("de", _("Alemán")), ("it", _("Italiano")), ("pt", _("Portugués")),
         ("ca", _("Catalán")), ("gl", _("Gallego")), ("eu", _("Euskera"))]

def load_config():
    cfg = {"engine": "local", "model": DEFAULT_MODEL, "voice": "narrator", "server": ""}
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
    ms = all_models(cfg)
    return next((m for m in ms if m["id"] == mid), next(m for m in ms if m["id"] == DEFAULT_MODEL))
def model_dir(mid): return MODELS_DIR / mid
def xtts_ready():
    """El venv de XTTS tiene sus paquetes instalados del todo: un venv a medias no cuenta."""
    return venv_has(_VENV_XTTS, XTTS_PACKAGES)
def xtts_hf_dir():
    """coqui-tts guarda XTTS en la caché de Hugging Face; su carpeta propia es un marcador vacío."""
    home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    return Path(home) / "hub" / "models--tts-hub--XTTS-v2"
def xtts_dirs():
    """Carpetas de instalaciones antiguas de coqui-tts (y su marcador en las actuales)."""
    name = "tts_models--multilingual--multi-dataset--xtts_v2"
    return [Path.home() / "AppData/Local/tts" / name, Path.home() / ".local/share/tts" / name]
def xtts_model_cached():
    """El modelo XTTS v2 ya está descargado en la caché de coqui-tts."""
    # Una descarga a medias no cuenta: hace falta el modelo entero.
    for m in list(xtts_hf_dir().glob("snapshots/*/model.pth")) + [d / "model.pth" for d in xtts_dirs()]:
        try:
            if m.stat().st_size > 1_500_000_000 and (m.parent / "config.json").exists():
                return True
        except OSError:
            pass
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
def qc_args_local(cfg=None, model=None):
    """Args de QC para los motores LOCALES: usan el Whisper descargado en
    «Modelos» (en CPU, sin red), NUNCA el Whisper del servidor. El remoto hace
    su QC en el propio servidor. Devuelve (args, aviso-o-None). Con aviso no se revisa."""
    cfg = cfg or load_config()
    if not cfg.get("qc_auto"):
        return [], None
    backend = (model or model_by_id(cfg.get("model"), cfg)).get("backend", "f5")
    if not qc_can_run(ENGINE_DIR, backend):
        return [], _("QC activado, pero Whisper no está en el entorno de este motor: esta narración no se revisará. "
                     "Para instalarlo ahí, ejecuta esto en una terminal:\n\n{0}").format(whisper_install_text(backend))
    w = whisper_by_id(active_whisper_id(cfg))
    if not whisper_installed(w):
        return [], (_("QC activado pero no hay modelo Whisper descargado. Descárgalo en "
                    "«Modelos» para que revise la calidad. Narro sin QC."))
    return (["--qc-whisper-dir", str(model_dir(w["id"])),
             "--qc-threshold", "0.85", "--qc-retries", "2"], None)
# Los idiomas de XTTS v2, cada uno escrito en el suyo: así los encuentra quien los habla.
NARRATION_LANGS = [("es", "Español"), ("en", "English"), ("fr", "Français"), ("de", "Deutsch"), ("it", "Italiano"),
                   ("pt", "Português"), ("nl", "Nederlands"), ("pl", "Polski"), ("cs", "Čeština"), ("hu", "Magyar"),
                   ("tr", "Türkçe"), ("ru", "Русский"), ("ar", "العربية"), ("hi", "हिन्दी"), ("zh-cn", "中文"),
                   ("ja", "日本語"), ("ko", "한국어")]

def narration_language(cfg=None):
    """El que ha elegido el usuario; hasta que elija, el de la interfaz si XTTS lo tiene."""
    cfg = cfg or load_config()
    known = dict(NARRATION_LANGS)
    if cfg.get("narration_lang") in known:
        return cfg["narration_lang"]
    ui = "zh-cn" if LANG == "zh" else LANG
    return ui if ui in known else "en"

def model_language(model, cfg=None):
    """F5 solo lee el idioma de su modelo; XTTS y los servicios, el que se elija."""
    return narration_language(cfg) if model.get("backend") in ("xtts", "service") else model.get("lang", "es")

def xtts_needs_text(line):
    """Lo que dice cc_xtts cuando a su entorno le falta algo para ese idioma."""
    return _("Para narrar en ese idioma falta instalar esto en el entorno de XTTS:\n\n{0}").format(
        install_text("xtts-languages"))

# Lo que escriben los motores cuando el QC no ha podido oír una frase y la dan por buena.
QC_MISSED = "sin respuesta del Whisper"

def qc_missed_suffix(missed):
    """Para la barra de progreso: que no parezca que se revisa cuando no se revisa."""
    return " — " + _("sin revisar (QC)") if missed else ""

def active_language(cfg=None):
    cfg = cfg or load_config(); return cfg.get("language", "es")
def lang_name(code):
    return dict(LANGS).get(code) or dict(NARRATION_LANGS).get(code, code or "es")
def epub_ready():
    """engine/.venv sabe leer un EPUB: basta para narrar con un servicio por URL."""
    return venv_has(_VENV, BASE_PACKAGES)
def f5_ready():
    """F5 es opcional, y va en engine/.venv."""
    return venv_has(_VENV, F5_PACKAGES)
def voice_engine_ready(model):
    """Está instalado el motor con el que narra ese modelo."""
    return xtts_ready() if model.get("backend") == "xtts" else f5_ready()
def whisper_python():
    """El Python con Whisper (opcional) para transcribir: el de engine/.venv o el de XTTS. None si no hay."""
    venv = whisper_venv(ENGINE_DIR)
    return (VPY_XTTS if venv == VENV_XTTS else VPY) if venv else None

INSTALL_HEAD = "cd ~/cuentacuentos"

def install_text(what):
    """Los comandos para pegarlos en una terminal: son los del README, desde la carpeta de la app."""
    return "\n".join([INSTALL_HEAD] + install_commands(what))
def engine_how_text(model):
    """Cómo instalar el motor que le falta a ese modelo."""
    if model.get("backend") == "xtts":
        return _("XTTS no está instalado. Para instalarlo, ejecuta esto en una terminal (tarda y ocupa varios GB; "
                 "sin GPU NVIDIA, cambia cu128 por cpu):\n\n{0}\n\n"
                 "También puedes generar en un servidor propio o con un servicio por URL.").format(install_text("xtts"))
    return _("F5 es opcional y no está instalado, así que no se puede narrar con este modelo. Para instalarlo, ejecuta "
             "esto en una terminal (tarda y ocupa varios GB; sin GPU NVIDIA, cambia cu128 por cpu):\n\n{0}\n\n"
             "También puedes elegir otro modelo en «Modelos» o un servicio por URL.").format(install_text("f5"))
def whisper_install_text(backend=None):
    return install_text(whisper_target(ENGINE_DIR, backend))
def whisper_how_text(backend=None):
    """Cómo instalar Whisper, que es opcional."""
    return _("Whisper es opcional y no está instalado. Hace falta para transcribir audiolibros y para revisar las "
             "frases (QC). Para instalarlo, ejecuta esto en una terminal:\n\n{0}").format(whisper_install_text(backend))
def next_model_id(cfg):
    """Con qué modelo seguir al quitar el activo: el primero que pueda narrar aquí o, si no, el de partida."""
    return next((m["id"] for m in all_models(cfg) if voice_engine_ready(m) and model_installed(m)), DEFAULT_MODEL)
def how_parts(text):
    """(lo de antes, los comandos, lo de después) de un aviso. Sin comandos, todo es «lo de antes»."""
    paras = text.split("\n\n")
    for i, para in enumerate(paras):
        if para.startswith(INSTALL_HEAD):
            return "\n\n".join(paras[:i]), para, "\n\n".join(paras[i + 1:])
    return text, "", ""
def how_dialog(parent, text):
    """Un aviso con comandos: van en su cuadro, sin partir las líneas, y se copian de una vez."""
    before, commands, after = how_parts(text)
    dlg = QDialog(parent); dlg.setWindowTitle("Storyteller")
    lay = QVBoxLayout(dlg)
    box = QPlainTextEdit(commands); box.setObjectName("commands"); box.setReadOnly(True)
    box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    box.setStyleSheet(f"font-family: '{fixed.family()}';")       # con setFont manda la hoja de estilo de la app
    fm = QFontMetrics(fixed); lines = commands.splitlines()
    wide = max((fm.horizontalAdvance(l) for l in lines), default=0) + 2 * fm.averageCharWidth() + 28
    # Lo que no quepa en la pantalla se ve con la barra: una línea partida no se puede pegar.
    box.setMinimumWidth(min(wide, 900))
    box.setFixedHeight(fm.lineSpacing() * (len(lines) + 1) + 34)
    for part, widget in ((before, None), (None, box), (after, None)):
        if widget:
            lay.addWidget(widget)
        elif part:
            label = QLabel(part); label.setWordWrap(True); label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(label)
    row = QHBoxLayout(); row.addStretch(1)
    copy = QPushButton(_("Copiar")); ok = QPushButton(_("Aceptar")); ok.setDefault(True)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(commands))
    ok.clicked.connect(dlg.accept)
    row.addWidget(copy); row.addWidget(ok); lay.addLayout(row)
    return dlg
def show_how(parent, text):
    """Un aviso que puede traer comandos para pegarlos en una terminal."""
    if how_parts(text)[1]:
        how_dialog(parent, text).exec()
        return
    box = QMessageBox(QMessageBox.Icon.Information, "Storyteller", text, QMessageBox.StandardButton.Ok, parent)
    box.setTextInteractionFlags(Qt.TextSelectableByMouse)
    box.exec()
def warn(parent, text, title="Storyteller"):
    """Un aviso de que algo ha fallado; si trae comandos, sin partirlos."""
    if how_parts(text)[1]:
        show_how(parent, text)
    else:
        QMessageBox.warning(parent, title, text)
def fit_popup(combo):
    """La lista desplegable, con los nombres enteros aunque el cuadro cerrado sea más estrecho."""
    fm = combo.fontMetrics()
    wide = max((fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
    combo.view().setTextElideMode(Qt.TextElideMode.ElideNone)
    combo.view().setMinimumWidth(wide + 48)

def voices():
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for wav in sorted(VOICES_DIR.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.exists(): continue
        vid = wav.stem
        namef = wav.with_suffix(".name")
        if namef.exists(): name = namef.read_text(encoding="utf-8").strip()
        elif vid == "narrator": name = _("Narrador — grave (F5 Español)")
        else: name = vid.replace("-", " ").capitalize()
        out.append({"id": vid, "name": name, "wav": wav, "txt": txt})
    return out

def active_voice(cfg=None):
    cfg = cfg or load_config(); vs = voices()
    if not vs: return None
    return next((v for v in vs if v["id"] == cfg.get("voice", "narrator")), vs[0])

# ---- narrar con un servicio por URL (engine/cc_service.py)
_service_mod = None

def service_module():
    """cc_service sin tocar sys.path. Solo usa la biblioteca estándar: se carga también aquí,
    en la interfaz, para probar un servicio y entender lo que se escribe como dirección."""
    global _service_mod
    if _service_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("cc_service", CC_SERVICE)
        _service_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_service_mod)
    return _service_mod

def service_cfg(cfg=None):
    s = (cfg or load_config()).get("service") or {}
    return {"url": s.get("url", ""), "model": s.get("model", ""), "voice": s.get("voice", ""),
            "parallel": max(1, min(int(s.get("parallel", 1) or 1), 4)),
            "qc_url": s.get("qc_url", ""), "qc_model": s.get("qc_model", "")}

def service_key(path=SERVICE_KEY):
    try: return path.read_text("utf-8").strip()
    except OSError: return ""

def save_service_key(key, path=SERVICE_KEY):
    """Aparte de config.json y legible solo por el usuario."""
    try:
        if key:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(key)
        else:
            path.unlink(missing_ok=True)
    except OSError: pass

# Lo que necesita el Whisper local dentro del proceso de cc_service (cc_engine.qc_transcribe_local).
LOCAL_QC_PACKAGES = ("numpy", "soundfile", "faster-whisper")

def python_has(py, packages):
    """¿Tiene el venv de ese Python estos paquetes? Sin importar nada, como venv_has."""
    return venv_has(Path(py).parent.parent, packages)

def service_python(cfg=None):
    """El Python que lee el EPUB para el servicio: el de un motor instalado. Si se va a revisar con el
    Whisper de este equipo, que corre en ese proceso, mejor el que lo tenga."""
    cfg = cfg or load_config()
    envs = [py for py, ready in ((VPY, epub_ready()), (VPY_XTTS, xtts_ready())) if ready]
    if cfg.get("qc_auto") and not service_cfg(cfg)["qc_url"]:
        envs.sort(key=lambda py: not python_has(py, LOCAL_QC_PACKAGES))
    return envs[0] if envs else None

def service_qc(cfg=None):
    """Cómo revisar lo que llega del servicio: (args de cc_service, clave de ese Whisper, aviso). Con aviso
    no se revisa. Una URL escrita manda sobre el Whisper de este equipo."""
    cfg = cfg or load_config()
    if not cfg.get("qc_auto"):
        return [], "", None
    s = service_cfg(cfg)
    tune = ["--qc-threshold", "0.85", "--qc-retries", "2"]
    if s["qc_url"]:
        model = ["--qc-model", s["qc_model"]] if s["qc_model"] else []
        return ["--qc-url", s["qc_url"]] + model + tune, service_key(SERVICE_QC_KEY), None
    py = service_python(cfg)
    if not py or not python_has(py, LOCAL_QC_PACKAGES):
        return [], "", _("QC activado, pero no hay con qué revisar: ni URL de Whisper ni Whisper en este equipo. "
                         "Esta narración no se revisará.")
    w = whisper_by_id(active_whisper_id(cfg))
    if not whisper_installed(w):
        return [], "", (_("QC activado pero no hay modelo Whisper descargado. Descárgalo en "
                          "«Modelos» para que revise la calidad. Narro sin QC."))
    return ["--qc-whisper-dir", str(model_dir(w["id"]))] + tune, "", None

def service_qc_hint(cfg):
    """(lo que hay que saber del QC con un servicio, ¿ofrecer instalar Whisper?). Sin prometer más de lo que hará."""
    s = service_cfg(cfg); py = service_python(cfg)
    if s["qc_url"]:
        if py and not python_has(py, ("numpy",)):
            return _("Con este entorno, el QC compara lo que oye Whisper con el texto, pero no mide la voz de más "
                     "al final de cada frase, porque falta numpy."), False
        return "", False
    if py and python_has(py, LOCAL_QC_PACKAGES):
        if whisper_installed(whisper_by_id(active_whisper_id(cfg))):
            return "", False
        return _("El QC necesita un modelo Whisper: descárgalo más abajo."), False
    # Sin soundfile, instalar Whisper no bastaría: solo hay un entorno para leer el EPUB.
    return (_("Sin URL de Whisper, el QC necesita Whisper en el entorno de un motor de este equipo, y no está."),
            bool(py and python_has(py, ("soundfile",))))

def service_command(model, epub, dest, pron=None, resume=False):
    """La orden de cc_service. Las claves no van aquí: se verían en la lista de procesos."""
    cmd = [str(model["python"]), "-I", str(CC_SERVICE), "generate",
           "--epub", str(epub), "--out", str(dest), "--url", model["url"],
           "--model", model["model"], "--voice", model["voice"],
           "--lang", model.get("lang", "es"), "--parallel", str(model.get("parallel", 1))]
    if resume:
        cmd += ["--resume"]
    if pron and pron.exists():
        cmd += ["--pron", str(pron)]
    return cmd + service_qc()[0]

def service_error_text(kind, detail=""):
    """Lo que le pasa al servicio, para el usuario. El mensaje del propio servicio, si dio alguno, detrás."""
    text = {"url": _("Esa dirección no es válida."),
            "auth": _("El servicio rechazó la clave de acceso."),
            "notfound": _("El servicio no encuentra esa dirección o ese modelo. Revisa la URL y el modelo."),
            "refused": _("El servicio rechazó la petición."),
            "busy": _("El servicio sigue ocupado tras varios intentos. Prueba más tarde."),
            "noaudio": _("El servicio no devolvió un audio que se pueda leer."),
            }.get(kind, _("No hay respuesta del servicio. Comprueba la URL y que esté encendido."))
    return f"{text}\n\n{detail}" if detail and kind in ("auth", "notfound", "refused", "busy") else text

def is_http_url(u):
    """Solo http/https (evita file://, ftp:// u otros esquemas al descargar)."""
    return isinstance(u, str) and u.strip().lower().startswith(("http://", "https://"))

def fmt(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"

def fmt_eta(sec):
    """«quedan ~1 h 10 min (hacia las 14:35)» para la barra de narración."""
    if sec is None: return _("calculando tiempo restante…")
    m = int(round(sec / 60))
    if m < 1: dur = _("menos de 1 min")
    elif m < 60: dur = _('~{0} min').format(m)
    else: dur = _('~{0} h {1} min').format(m // 60, format(m % 60, '02d'))
    end = time.localtime(time.time() + sec); now = time.localtime()
    days = (time.mktime((end.tm_year, end.tm_mon, end.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))) // 86400
    when = time.strftime("%H:%M", end)
    when = when if days <= 0 else (_('mañana {0}').format(when) if days == 1 else _('en {0} días, {1}').format(int(days), when))
    return _('quedan {0} (hacia las {1})').format(dur, when) if days <= 0 else _('quedan {0} ({1})').format(dur, when)

def list_books():
    LOCAL_BOOKS.mkdir(parents=True, exist_ok=True)
    out = []
    for d in sorted(LOCAL_BOOKS.iterdir()):
        if d.is_dir():
            try: recover_interrupted(d)                   # narración cortada de golpe → reanudable
            except Exception: pass
        if not d.is_dir():
            continue
        active = is_generating(d)
        # Narrándose ahora: lo que hay en disco en este momento (los tramos en
        # paralelo van por delante de la fusión periódica del manifest.json).
        man = live_manifest(d) if active else None
        if not man:
            mf = d / "manifest.json"
            if not mf.exists():
                continue
            try:
                man = json.loads(mf.read_text("utf-8"))
            except Exception:
                continue
        tf = d / "title.txt"
        title = tf.read_text("utf-8").strip() if tf.exists() else d.name.replace("-", " ").capitalize()
        dur = sum(c.get("duration", 0) for c in man)
        out.append({"dir": d, "title": title, "n": len(man), "dur": dur, "manifest": man,
                    "resumable": book_resumable(d), "active": active})
    return sorted(out, key=lambda b: b["title"].lower())


# ------------------------------------------------- escuchar mientras se narra
# El capítulo que el motor está narrando no se reproduce desde sus ficheros: el
# reproductor los dejaría abiertos y en Windows el motor no podría reemplazar el
# mp3 al cerrar el capítulo (ni borrarlo al reanudar). Suena una COPIA de lo
# narrado hasta ahora en esta carpeta temporal, que se renueva al llegar al final.
LISTEN_DIR = Path(tempfile.gettempdir()) / "storyteller-listen"

def resume_model_name(d):
    """Con qué modelo seguiría un libro a medias: el activo ahora, lo ya hecho se conserva."""
    cfg = load_config()
    if cfg.get("engine", "local") == "remote":
        return _("Servidor propio")
    if (read_gen_state(d) or {}).get("kind") == "import":
        return whisper_by_id(active_whisper_id(cfg))["name"]
    if cfg.get("engine") == "service":
        return service_module().host_of(service_cfg(cfg)["url"]) or _("Un servicio por URL")
    return model_by_id(cfg["model"], cfg)["name"]


def is_generating(d):
    """True si esta app está narrando ahora mismo el libro de la carpeta d."""
    return d is not None and str(Path(d)) in ACTIVE_GEN

def clean_listen_dir(keep=None):
    """Borra las copias viejas (la que está sonando sigue abierta y se salta sola)."""
    try:
        for p in LISTEN_DIR.iterdir():
            if keep is None or p != keep:
                try: p.unlink()
                except OSError: pass
    except OSError:
        pass

def snapshot_chapter(d, e):
    return core_snapshot_chapter(d, e, LISTEN_DIR)


def chap_label(m):
    """Nombre del capítulo en las listas: «nº de sección · título del libro» («23 · 18. Encuentra…»).
    El nº es el de la sección en el EPUB (= el de la barra «capítulo X/n»), no la posición en la lista:
    con tramos en paralelo hay huecos. Las secciones sin título (el motor les pone «Capitulo N») salen «Sección N»."""
    n = m.get("id", "?")
    t = (m.get("title") or "").strip()
    if not t or re.fullmatch(r"Capitulo \d+", t):
        t = _('Sección {0}').format(n)
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
        self.qc_missed = 0                                # frases que el QC del servidor no pudo revisar
        cfg = load_config()
        self.host = cfg.get("server") or MINT
        self.streams = max(1, int(cfg.get("parallel_streams", 1) or 1))
        self.model = model_by_id(cfg.get("model"), cfg)
        self.xtts = self.model.get("backend") == "xtts"
        self.voice = active_voice(cfg)
        self.qc_model = active_whisper_id(cfg).replace("whisper-", "") if cfg.get("qc_auto") else ""
        self.mode = "run"        # run | stopsave | cancel
        self.proc = None
        self.resume = resume

    def _run(self, args):
        return subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=NOWIN)

    def stop(self, save):
        """Detiene la narración en el servidor. save=True conserva lo hecho; False descarta."""
        self.mode = "stopsave" if save else "cancel"
        try:
            # matar solo el python de narración; el script sigue y rearranca su servicio de voz
            subprocess.run(SSH + [self.host, "pkill -f 'epub2audio.py|xtts_tramos.py|cc_xtts.py'"],
                           capture_output=True, text=True, timeout=20, creationflags=NOWIN)
        except Exception:
            pass

    def _narrated(self, text):
        """Frases narradas según un manifest.json; -1 si no se puede leer."""
        try:
            return sum(int(e.get("n") or 0) for e in json.loads(text))
        except Exception:
            return -1

    def _send_xtts(self, dest):
        """Deja en el servidor el motor, la voz y el diccionario de este equipo, para que suene
        igual que aquí; al reanudar, también lo narrado si aquí va más avanzado."""
        rdir = f"{REMOTE_DIR}/out/{self.name}"
        r = self._run(SSH + [self.host, f"mkdir -p {REMOTE_DIR}/engine-xtts {REMOTE_DIR}/voices-xtts '{rdir}'"])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        for src, dst in ((CC_ENGINE, "engine-xtts/cc_engine.py"), (CC_XTTS, "engine-xtts/cc_xtts.py"),
                         (ensure_pron(), "engine-xtts/pron.json"),
                         (self.voice["wav"], f"voices-xtts/{self.voice['id']}.wav")):
            r = self._run(SCP + [str(src), f"{self.host}:{REMOTE_DIR}/{dst}"])
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())
        if not self.resume:                        # desde cero: que no quede nada de otra tirada
            self._run(SSH + [self.host, f"rm -rf '{rdir}' && mkdir -p '{rdir}'"])
            return
        try:
            here = self._narrated((dest / "manifest.json").read_text("utf-8"))
        except OSError:
            here = -1
        r = self._run(SSH + [self.host, f"cat '{rdir}/manifest.json'"])
        if r.returncode == 255:                    # 255 = no hay conexión (1 = aún no existe)
            raise RuntimeError(r.stderr.strip())
        there = self._narrated(r.stdout) if r.returncode == 0 else -1
        if here < 0 or there > here:               # el servidor siguió narrando y no llegó a traerse
            return
        self.progress.emit(_("Enviando lo narrado al servidor…"), -1)
        files = [str(p) for p in dest.iterdir() if p.is_file() and p.suffix != ".tmp"]
        self._run(SSH + [self.host, f"rm -rf '{rdir}' && mkdir -p '{rdir}'"])
        r = self._run(SCP + files + [f"{self.host}:{rdir}/"])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())

    def run(self):
        tmp_epub = None
        try:
            dest = LOCAL_BOOKS / self.name
            epub = self.epub
            if self.xtts:
                r = self._run(SSH + [self.host, f"test -x {REMOTE_DIR}/.venv-xtts/bin/python && test -x {REMOTE_DIR}/run_xtts.sh"])
                if r.returncode == 255:                    # 255 = no hay conexión
                    self.failed.emit(_("No pude enviar el EPUB al servidor:\n") + r.stderr.strip())
                    return
                if r.returncode != 0 or not self.voice:
                    self.failed.emit(_("El servidor no tiene instalado XTTS. Elige otro modelo en «Modelos» o instálalo en el servidor."))
                    return
            # Reanudar con F5 en el servidor un libro empezado en este equipo: su motor es otro y allí
            # no está lo narrado, empezaría de cero y al traerlo se perdería lo de aquí.
            elif self.resume and self._run(SSH + [self.host, f"test -f '{REMOTE_DIR}/out/{self.name}/manifest.json'"]).returncode == 1:
                self.failed.emit(_("Este libro se empezó en este equipo y el servidor no tiene lo narrado: si siguiera "
                                   "allí, se perdería. Cámbialo a este equipo en «Modelos» para reanudarlo."))
                return
            # Al REGENERAR, el EPUB de entrada es dest/source.epub; ponerlo a salvo en un
            # temporal antes de que el rmtree de abajo vacíe la carpeta.
            try:
                if epub.exists() and (epub.parent == dest or dest in epub.parents):
                    fd, tmp_epub = tempfile.mkstemp(suffix=".epub"); os.close(fd)
                    shutil.copyfile(epub, tmp_epub); epub = Path(tmp_epub)
            except Exception: pass
            remote_epub = f"{REMOTE_DIR}/inbox/{self.name}.epub"
            self.progress.emit(_("Enviando el EPUB al servidor…"), -1)
            r = self._run(SCP + [str(epub), f"{self.host}:{remote_epub}"])
            if r.returncode != 0:
                self.failed.emit(_("No pude enviar el EPUB al servidor:\n") + r.stderr.strip()); return
            if self.xtts:
                try:
                    self._send_xtts(dest)
                except RuntimeError as e:
                    self.failed.emit(_("No pude enviar el EPUB al servidor:\n") + str(e)); return

            self.progress.emit(_("Reanudando el motor de voz…") if self.resume
                               else _("Arrancando el motor de voz…"), -1)
            rs = "CC_RESUME=1 " if self.resume else ""
            st = f"CC_STREAMS={self.streams} " if self.streams > 1 else ""
            if self.xtts:
                qc = f"CC_QC_MODEL={self.qc_model} " if self.qc_model else ""
                cmd = (f"cd {REMOTE_DIR} && {qc}{rs}{st}./run_xtts.sh 'inbox/{self.name}.epub' 'out/{self.name}' "
                       f"'voices-xtts/{self.voice['id']}.wav' '{model_language(self.model)}'")
            else:
                qc = "CC_QC=1 " if load_config().get("qc_auto") else ""
                cmd = f"cd {REMOTE_DIR} && {qc}{rs}{st}./run_epub.sh 'inbox/{self.name}.epub' 'out/{self.name}'"
            p = subprocess.Popen(SSH + [self.host, cmd], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", creationflags=NOWIN)
            self.proc = p
            total = done = 0
            cap_cur = cap_tot = 0
            tramos = 1                 # los que de verdad lanza el servidor (lo dice él)
            caps_seen = set()          # en tramos no hay un único capítulo actual
            tramo_failed = False
            finished = False           # el script del servidor acaba siempre con RUN-RC
            for line in p.stdout:
                line = line.rstrip()
                if "RUN-RC=" in line: finished = True
                m = re.search(r"capitulos:\s*(\d+)", line)
                if m: cap_tot = int(m.group(1))
                m = re.match(r"tramos:\s*(\d+)", line)
                if m:
                    tramos = int(m.group(1))
                    if tramos > 1:
                        self.progress.emit(_('Narrando en {0} tramos en paralelo — cargando modelos…').format(tramos), -1)
                    else:
                        self.progress.emit(_("Cargando el modelo…"), -1)
                if "TRAMO-ERROR" in line: tramo_failed = True
                m = re.search(r"\[cap (\d+)\]", line)
                if m: caps_seen.add(int(m.group(1)))
                m = re.search(r"\[cap (\d+)\].*->\s*(\d+) frases", line)
                if m:
                    cap_cur = int(m.group(1)); total += int(m.group(2))
                m = re.search(r"\[cap (\d+)\].*quedan (\d+)", line)
                if m:
                    cap_cur = int(m.group(1)); total += int(m.group(2))
                if QC_MISSED in line:
                    self.qc_missed += 1
                # search y no match: con varios tramos, la barra de progreso a medias de
                # uno puede quedar pegada delante de la línea de otro.
                if re.search(r"(?<!\S)frase \d+/\d+\s+[\d.]+s", line):
                    done += 1
                    pct = min(99, int(done / total * 100)) if total else -1
                    if tramos > 1:
                        txt = _('Narrando en {0} tramos — capítulo {1}/{2} — frase {3}').format(tramos, len(caps_seen), cap_tot, done)
                    else:
                        txt = _('Narrando capítulo {0}/{1} — frase {2}').format(cap_cur, cap_tot, done)
                    self.progress.emit(txt + qc_missed_suffix(self.qc_missed), pct)
                elif "regenerando" in line:
                    self.progress.emit(_("Revisando calidad (QC) y regenerando una frase…"), -1)
                elif "LISTO" in line:
                    self.progress.emit(_('Capítulo {0} listo').format(cap_cur), -1)
                elif "voz-server parado" in line:
                    self.progress.emit(_("Liberando la GPU…"), -1)
                elif "reintento en CPU" in line:
                    self.progress.emit(_("La GPU estaba ocupada; un tramo va en CPU (más lento)…"), -1)
                elif "modelo cargado" in line and tramos == 1:
                    self.progress.emit(_("Modelo cargado, narrando…"), -1)
            p.wait()

            if self.mode == "cancel":
                if not self.resume:            # cancelar una reanudación no borra lo ya hecho
                    self._run(SSH + [self.host, f"rm -rf '{REMOTE_DIR}/out/{self.name}'"])
                self.canceled.emit(); return

            # Sin RUN-RC se ha cortado la conexión: no ha terminado, aunque lo parezca. El servidor
            # conserva lo narrado y al reanudar se sigue desde allí, así que lo de aquí no se toca.
            lost = not finished and self.mode == "run"
            lost_msg = _("Se perdió la conexión con el servidor. Lo narrado allí se conserva: pulsa Reanudar para seguir.")
            if lost and (dest / "manifest.json").exists():
                self.failed.emit(lost_msg)
                return
            tramo_msg = _("Un tramo se cortó en el servidor; puede faltar memoria en su GPU. Lo narrado "
                          "está guardado: baja el número de tramos en «Modelos» y pulsa Reanudar.")
            # Si ningún tramo llegó a narrar, en el servidor no hay nada que traer: no tocar lo de aquí.
            if tramo_failed and self._run(SSH + [self.host, f"test -f '{REMOTE_DIR}/out/{self.name}/manifest.json'"]).returncode != 0:
                self.failed.emit(_("El servidor no pudo arrancar tantos tramos; puede faltar memoria en su GPU. "
                                   "Baja el número de tramos en «Modelos» y vuelve a intentarlo."))
                return
            msg = _("Guardando lo generado…") if self.mode == "stopsave" else _("Trayendo la grabación…")
            self.progress.emit(msg, -1)
            # Traerlo a una carpeta aparte y cambiarla solo si ha llegado bien: si falla la
            # copia, lo que ya había aquí sigue intacto.
            incoming = LOCAL_BOOKS.parent / f".incoming-{self.name}"
            shutil.rmtree(incoming, ignore_errors=True)
            incoming.mkdir(parents=True, exist_ok=True)
            r = self._run(SCP + ["-r", f"{self.host}:{REMOTE_DIR}/out/{self.name}/.", str(incoming)])
            if r.returncode != 0 or not (incoming / "manifest.json").exists():
                shutil.rmtree(incoming, ignore_errors=True)
                if self.mode == "stopsave":
                    self.failed.emit(_("Lo detuve, pero aún no había ningún capítulo terminado que guardar."))
                else:
                    self.failed.emit(_("La generación terminó pero no pude traerme el audio:\n") + r.stderr.strip())
                return
            # Los WAV de capítulos a medias hacen falta para reanudar en la misma frase.
            junk = list(incoming.glob("*.tmp"))
            if self.mode == "run" and not tramo_failed and not lost:
                junk += list(incoming.glob("*.wav"))
            for f in junk:
                try: f.unlink()
                except Exception: pass
            # Sin vaciar antes la carpeta: si está escuchando este libro, el capítulo que suena no se
            # puede borrar y se quedaría sin el resto. Cancelar deja de esperar a ese fichero.
            install_incoming(incoming, dest, keep_waiting=lambda: self.mode != "cancel")
            try: shutil.copyfile(epub, dest / "source.epub")   # para poder regenerar
            except Exception: pass
            if lost:
                self.failed.emit(lost_msg)
                return
            if tramo_failed and self.mode == "run":
                self.failed.emit(tramo_msg)
                return
            self.done.emit(str(dest))
        except Exception as e:
            self.failed.emit(_('Error inesperado: {0}').format(e))
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
        self.service = model.get("backend") == "service"
        self.error = None                                 # por qué lo dejó el servicio
        self.qc_missed = 0                                # frases que el QC no pudo revisar

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
            ACTIVE_GEN.add(str(dest))                     # la biblioteca no la "repara" mientras narra
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
            if self.service:
                # La voz la hace el servicio; aquí se lee el EPUB y se guarda lo que llega.
                cmd = service_command(self.model, epub, dest, pron, self.resume)
            elif self.model.get("backend") == "xtts":
                # Motor XTTS (multilingüe), su propio venv. Lee la frase entera; la voz
                # de referencia la clona del wav de la voz activa.
                cmd = [str(VPY_XTTS), "-I", str(CC_XTTS), "generate",
                       "--epub", str(epub), "--out", str(dest),
                       "--lang", model_language(self.model)]
                if self.voice:
                    cmd += ["--voice", str(self.voice["wav"])]
                if self.resume:
                    cmd += ["--resume"]
                if pron.exists():
                    cmd += ["--pron", str(pron)]
                cmd += qc_args_local(model=self.model)[0]    # QC en LOCAL = Whisper local (nunca el servidor)
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
                cmd += qc_args_local(model=self.model)[0]    # QC en LOCAL = Whisper local (nunca el servidor)
            # Nº de tramos en paralelo: reparte los capítulos entre N procesos en la
            # misma GPU (XTTS no la satura → varios a la vez suman). 1 = camino clásico.
            streams = int(load_config().get("parallel_streams", 1) or 1)
            st0 = read_gen_state(dest) or {}
            if self.resume and st0.get("streams"):            # reanudar con el mismo reparto
                streams = int(st0["streams"] or 1)
            if self.service:                                  # un solo proceso con varias peticiones (--parallel)
                streams = 1
            self.progress.emit(_("Contando las frases del libro…"), -1)
            n, sents = self._count_chapters(cmd)          # sents = frases por capítulo (estimación)
            if streams > 1 and n > 1:
                self._run_parallel(cmd, dest, max(1, min(streams, n)), n, sents)
            else:
                self._run_single(cmd, dest, sents)
            if self.mode == "cancel":
                # Cancelar una generación DESDE CERO descarta todo; cancelar una
                # REANUDACIÓN conserva lo que ya había (no borrar el libro entero).
                if not self.resume:
                    shutil.rmtree(dest, ignore_errors=True)
                self.canceled.emit(); return
            if self.error and self.mode == "run":
                # Lo que llegó queda guardado y el libro, a medias.
                if (dest / "manifest.json").exists():
                    self.error += "\n\n" + _("Lo ya narrado está guardado; puedes reanudar el libro desde la biblioteca.")
                elif not self.resume:
                    shutil.rmtree(dest, ignore_errors=True)
                self.failed.emit(self.error); return
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
                    self.failed.emit(_("Lo detuve, pero aún no había ningún capítulo terminado que guardar."))
                else:
                    shutil.rmtree(dest, ignore_errors=True)
                    self.failed.emit(_("La narración no produjo audio. Puede faltar memoria en la GPU "
                                     "(cierra juegos u apps que la usen) o el modelo estar incompleto."))
                return
            self.done.emit(str(dest))
        except Exception as e:
            self.failed.emit(_('Error inesperado: {0}').format(e))
        finally:
            ACTIVE_GEN.discard(str(LOCAL_BOOKS / self.name))
            if tmp_epub:
                try: os.remove(tmp_epub)
                except Exception: pass

    # ---- un solo proceso (camino clásico) --------------------------------
    def _service_line(self, line):
        """Lo que cuenta cc_service de cómo le va con el servicio."""
        word, _sep, rest = line.partition(" ")
        if word == "SERVICE-RETRY":
            self.progress.emit(_('El servicio no responde; reintentando en {0} s…').format(rest), -1)
        elif word == "SERVICE-ERROR":
            e = json.loads(rest)
            self.error = service_error_text(e.get("kind"), e.get("detail", ""))

    def _run_single(self, cmd, dest, sents=None):
        self.progress.emit(_("Reanudando con {0}…").format(self.model["name"]) if self.resume
                           else _('Conectando con {0}…').format(self.model["name"]) if self.service
                           else _("Cargando el modelo…"), -1)
        eta = EtaTracker(sents, {1: (1, len(sents))}, dest if self.resume else None) if sents else None
        # La clave, nunca en la línea de órdenes: se vería en la lista de procesos.
        env = dict(os.environ, ST_SERVICE_KEY=service_key(), ST_QC_KEY=service_key(SERVICE_QC_KEY)) if self.service else None
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
        self.proc = p
        logf = open(dest / "gen.log", "w", encoding="utf-8", errors="replace")
        total = done = 0; cap_cur = cap_tot = 0
        last = ""
        for line in p.stdout:
            line = line.rstrip()
            try: logf.write(line + "\n"); logf.flush()
            except Exception: pass
            if line.startswith("SERVICE-"):
                self._service_line(line); continue
            if line.startswith("XTTS-NEEDS "):
                self.error = xtts_needs_text(line); continue
            if QC_MISSED in line:
                self.qc_missed += 1
            last = line or last
            m = re.search(r"capitulos:\s*(\d+)", line)
            if m: cap_tot = int(m.group(1))
            m = re.search(r"\[cap (\d+)\].*->\s*(\d+) frases", line)
            if m: cap_cur = int(m.group(1)); total += int(m.group(2))
            if eta: eta.line(1, line)
            if re.match(r"\s*frase \d+/\d+", line):
                done += 1; pct = int(done / total * 100) if total else -1
                txt = _('Narrando capítulo {0}/{1} — frase {2}').format(cap_cur, cap_tot, done)
                if eta: pct = eta.pct(); txt += " — " + fmt_eta(eta.seconds())
                self.progress.emit(txt + qc_missed_suffix(self.qc_missed), pct)
            elif "regenerando" in line:
                self.progress.emit(_("Revisando calidad (QC) y regenerando una frase…"), -1)
            elif "reintento en CPU" in line:
                self.progress.emit(_("La GPU estaba ocupada; generando en CPU (más lento)…"), -1)
            elif "modelo cargado" in line:
                self.progress.emit(_("Modelo cargado, narrando…"), -1)
        p.wait()
        try: logf.close()
        except Exception: pass
        if self.service and p.returncode and not self.error:   # se cayó sin llegar a contar por qué
            self.error = _('Error inesperado: {0}').format(last)

    # ---- varios tramos en paralelo ---------------------------------------
    def _count_chapters(self, base_cmd):
        """(nº de capítulos, frases por capítulo) del EPUB, con el subcomando `count`
        del motor. Las frases salen [] si el motor no las da (sin estimación)."""
        try:
            i = base_cmd.index("--epub"); epub = base_cmd[i + 1]
            exe = base_cmd[0]; script = base_cmd[2]
            p = subprocess.run([exe, "-I", script, "count", "--epub", epub],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=NOWIN, timeout=180)
            m = re.search(r"CHAPTERS\s+(\d+)", p.stdout or "")
            ms = re.search(r"^SENTS((?: \d+)*)\s*$", p.stdout or "", re.M)
            sents = [int(x) for x in ms.group(1).split()] if ms else []
            n = int(m.group(1)) if m else 1
            return n, (sents if len(sents) == n and sum(sents) else [])
        except Exception:
            return 1, []

    @staticmethod
    def _split_ranges(n, streams):
        return split_ranges(n, streams)

    def _run_parallel(self, base_cmd, dest, streams, n, sents=None):
        ranges = self._split_ranges(n, streams)
        streams = len(ranges)
        eta = EtaTracker(sents, dict(enumerate(ranges, 1)), dest if self.resume else None) if sents else None
        self._lock = threading.Lock()
        self._tot_fr = 0; self._done_fr = 0; self._caps_seen = set()
        self.procs = []
        # Guardar YA el reparto y el total: si el PC se cuelga, la biblioteca sabe
        # reconstruir el libro y reanudar con los mismos tramos (recover_interrupted).
        st0 = read_gen_state(dest) or {}
        if not st0.get("streams"):
            try:
                _atomic_write(dest / "gen_state.json", json.dumps(
                    {"total": n, "complete": False, "streams": streams}, ensure_ascii=False))
            except Exception: pass
        self._last_merge = time.monotonic()
        logf = open(dest / "gen.log", "w", encoding="utf-8", errors="replace")
        self.progress.emit(_('Narrando en {0} tramos en paralelo — cargando modelos…').format(streams), -1)

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
                    if line.startswith("XTTS-NEEDS "): self.error = xtts_needs_text(line)
                    if QC_MISSED in line: self.qc_missed += 1
                    cm = re.search(r"\[cap (\d+)\]", line)
                    if cm: self._caps_seen.add(int(cm.group(1)))   # capítulos CON contenido (= los que salen en "ir a capítulo"); en paralelo/reanudación cuadra con la lista del reproductor
                    mm = re.search(r"\[cap \d+\].*->\s*(\d+) frases", line)
                    if mm: self._tot_fr += int(mm.group(1))
                    if eta: eta.line(idx, line)
                    if re.match(r"\s*frase \d+/\d+", line):
                        self._done_fr += 1
                        pct = int(self._done_fr / self._tot_fr * 100) if self._tot_fr else -1
                        if pct > 99: pct = 99
                        txt = (_('Narrando en {0} tramos — capítulo {1}/{2} — frase {3}').format(streams, len(self._caps_seen), n, self._done_fr))
                        if eta: pct = eta.pct(); txt += " — " + fmt_eta(eta.seconds())
                        self.progress.emit(txt + qc_missed_suffix(self.qc_missed), pct)
                    elif "QC frase" in line and "regenerando" in line:
                        self.progress.emit(_("Revisando calidad (QC) y regenerando una frase…"), -1)
                    elif "reintento en CPU" in line:
                        self.progress.emit(_("La GPU estaba ocupada; un tramo va en CPU (más lento)…"), -1)
                    # Fusión periódica: ante un cuelgue el libro queda en la biblioteca
                    # (a medias, reanudable) con lo narrado hasta hace unos segundos.
                    if time.monotonic() - self._last_merge > 20:
                        self._last_merge = time.monotonic()
                        try: merge_stream_manifests(dest, streams, n, complete_ok=False)
                        except Exception: pass
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
        escribe gen_state.json (complete solo si están TODOS los capítulos).
        Si no hay nada terminado no escribe nada: el post-bloque decide."""
        merge_stream_manifests(dest, streams, n, complete_ok=(self.mode == "run"))


def hf_repo_bytes(repo, keep):
    """Bytes a descargar de un repo de Hugging Face (para dar un porcentaje). None si no se sabe."""
    import urllib.request
    try:
        req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}/tree/main?recursive=1",
                                     headers={"User-Agent": "Storyteller"})
        with urllib.request.urlopen(req, timeout=15) as r:
            items = json.load(r)
        return sum(i.get("size", 0) for i in items if i.get("type") == "file" and keep(i["path"])) or None
    except Exception:
        return None


def folder_bytes(d):
    """Lo que ocupa una carpeta, incluidos los ficheros a medias de la descarga."""
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))     # lstat: un enlace no cuenta dos veces
                total += st.st_size
            except OSError:
                pass
    return total


class ModelDownload(QThread):
    """Descarga un modelo en un proceso aparte. El avance se mide por lo que ocupa en disco: la
    salida de las librerías de descarga no sirve sin terminal y no da porcentaje ni velocidad."""
    progress = Signal(str, int)       # texto, % (-1 = no se sabe)
    done     = Signal()
    failed   = Signal(str)
    canceled = Signal()

    def __init__(self, name):
        super().__init__()
        self.name, self.proc, self._cancel = name, None, False

    def command(self): raise NotImplementedError
    def folder(self): raise NotImplementedError
    def total_bytes(self): return None
    def installed(self): raise NotImplementedError
    def fail_text(self): return _("La descarga no dejó el modelo completo.")

    @staticmethod
    def _remove_partials(folder):
        """huggingface_hub no reanuda: cada intento usa un temporal nuevo y, si se corta, el anterior
        se queda ocupando disco e inflaría el porcentaje."""
        for _try in range(4):
            left = False
            for f in Path(folder).rglob("*.incomplete"):
                try:
                    f.unlink()
                except OSError:
                    left = True      # el proceso cortado aún lo tiene abierto un instante
            if not left:
                return
            time.sleep(0.4)

    def stop(self):
        self._cancel = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _text(self, got, total, samples):
        num = lambda v: _sz(f"{v:.2f}".replace(".", ","))
        parts = [_('Descargando «{0}»…').format(self.name)]
        if total:
            parts[0] += " " + _("{0} de {1} GB ({2} %)").format(num(got / 1e9), num(total / 1e9), self._pct(got, total))
        elif got:
            parts[0] += " " + _("{0} GB").format(num(got / 1e9))
        dt = samples[-1][0] - samples[0][0]
        rate = (samples[-1][1] - samples[0][1]) / dt if dt >= 2 else 0
        if rate > 0:
            parts.append(_("{0} MB/s").format(_sz(f"{rate / 1e6:.1f}".replace(".", ","))))
            if total and total > got:
                parts.append(fmt_eta((total - got) / rate))
        return " · ".join(parts)

    @staticmethod
    def _pct(got, total):
        return min(99, int(got * 100 / total))

    def run(self):
        try:
            self.progress.emit(_('Descargando «{0}»…').format(self.name), -1)
            total = self.total_bytes()
            folder = self.folder()
            self._remove_partials(folder)
            self.proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
            tail = []

            def drain():
                for line in self.proc.stdout:
                    if line.strip():
                        tail[:] = (tail + [line.strip()])[-4:]
            th = threading.Thread(target=drain, daemon=True); th.start()
            samples = []
            while self.proc.poll() is None:
                got = folder_bytes(folder)
                samples = (samples + [(time.monotonic(), got)])[-20:]        # los últimos ~10 s
                if got:
                    self.progress.emit(self._text(got, total, samples), self._pct(got, total) if total else -1)
                time.sleep(0.5)
            th.join(2)
            if self._cancel:
                self._remove_partials(folder); self.canceled.emit()
            elif self.installed():
                self.done.emit()
            else:
                self.failed.emit(self.fail_text() + ("\n\n" + tail[-1][:300] if tail else ""))
        except Exception as e:
            self.failed.emit(str(e))


class DownloadWorker(ModelDownload):
    def __init__(self, model):
        super().__init__(model["name"]); self.model = model

    def folder(self):
        md = model_dir(self.model["id"]); md.mkdir(parents=True, exist_ok=True); return md

    def command(self):
        return [str(VPY), "-I", str(CC_ENGINE), "download", "--repo", self.model["repo"],
                "--ckpt-name", self.model["ckpt"], "--vocab-name", self.model["vocab"], "--dest", str(self.folder())]

    def total_bytes(self):
        names = (self.model["ckpt"], self.model["vocab"])
        return hf_repo_bytes(self.model["repo"], lambda p: p in names)

    def installed(self):
        return model_installed(self.model)


class WhisperDownloadWorker(ModelDownload):
    """Descarga un modelo faster-whisper para transcribir audiolibros en local."""
    def __init__(self, model):
        super().__init__(model["name"]); self.model = model

    def folder(self):
        md = model_dir(self.model["id"]); md.mkdir(parents=True, exist_ok=True); return md

    def command(self):
        return [str(whisper_python() or VPY), "-I", str(CC_ENGINE), "whisper-download", "--repo", self.model["repo"],
                "--dest", str(self.folder())]

    def total_bytes(self):
        return hf_repo_bytes(self.model["repo"], lambda p: p.endswith((".bin", ".json", ".txt", ".model")))

    def installed(self):
        return whisper_installed(self.model)

    def fail_text(self):
        return _("La descarga del modelo de transcripción no se completó.")


class XttsDownloadWorker(ModelDownload):
    """Descarga el modelo XTTS v2 con coqui-tts (en el venv de XTTS)."""
    def __init__(self, name="XTTS v2"):
        super().__init__(name)

    def folder(self):
        return xtts_hf_dir()

    def command(self):
        code = ("import os; os.environ['COQUI_TOS_AGREED']='1'; "
                "from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2'); "
                "print('XTTS-DL-DONE')")
        return [str(VPY_XTTS), "-c", code]

    def total_bytes(self):
        names = ("model.pth", "config.json", "vocab.json", "hash.md5", "speakers_xtts.pth", "mel_stats.pth")
        return hf_repo_bytes("tts-hub/XTTS-v2", lambda p: p in names)

    def installed(self):
        return xtts_model_cached()

    def fail_text(self):
        return _("La descarga del modelo XTTS no se completó.")


def voz_http_url():
    """URL HTTP del Whisper del servidor, derivada del host de config['server']."""
    cfg = load_config()
    host = (cfg.get("server") or MINT).split("@")[-1].split(":")[0]
    return f"http://{host}:8000"


class ImportWorker(QThread):
    """Importa un audio existente: lo pasa a MP3 y lo transcribe en el Whisper del servidor
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
        out, _err = self.proc.communicate()
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
            rc, _out = self._proc(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
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
                    self.failed.emit(_("No encuentro el audio de este libro, no puedo reanudar.")); return
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
                self.progress.emit(_("Convirtiendo audio…"), -1)
                mp3 = dest / "cap01.mp3"
                rc, _out = self._proc(["ffmpeg", "-y", "-i", str(src), "-codec:a", "libmp3lame",
                                    "-b:a", "128k", str(mp3)])
                if self.mode == "cancel":
                    shutil.rmtree(dest, ignore_errors=True); self.canceled.emit(); return
                if rc != 0 or not mp3.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                    self.failed.emit(_("No se pudo convertir el audio (¿formato soportado? ¿ffmpeg instalado?)."))
                    return
                dur = self._duration(mp3); cues = []; covered = 0.0
                (dest / "title.txt").write_text(title, "utf-8")
                self._save(dest, title, cues, dur, covered, False)   # ya reanudable aunque se pare enseguida

            if dur <= 0:
                if not resume: shutil.rmtree(dest, ignore_errors=True)
                self.failed.emit(_("No pude leer la duración del audio.")); return

            # LOCAL = Whisper en este equipo (sin servidor); REMOTO = Whisper del servidor, por SSH.
            lang = active_language()
            if load_config().get("engine", "local") != "remote":
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
            self.failed.emit(_('Error inesperado al importar: {0}').format(e))

    def _transcribe_remote(self, dest, mp3, title, dur, cues, covered, language="es"):
        """Transcribe por tramos contra el Whisper del servidor (SSH/HTTP)."""
        url = voz_http_url() + "/v1/audio/transcriptions"
        start = covered
        while start < dur - 0.05:
            if self.mode == "cancel":
                break
            length = min(self.CHUNK, dur - start)
            self.progress.emit(
                _('Transcribiendo (servidor)… {0}–{1} de {2}  ·  guardado {3}/{4} min').format(fmt(start*1000), fmt((start+length)*1000), fmt(dur*1000), int(covered//60), int((dur+59)//60)), -1)
            segs = self._transcribe_chunk(mp3, start, length, url, language)
            if self.mode == "cancel":
                break
            if segs is None:
                if self.mode == "stopsave":
                    break
                return (False, _("Se cortó la transcripción (¿servidor de voz caído?). "
                               "Lo transcrito se ha guardado; puedes Reanudar."), covered)
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
        py = whisper_python()
        if not py:
            return (False, whisper_how_text(), covered)
        wm = whisper_by_id(active_whisper_id()); mdir = model_dir(wm["id"])
        if not (mdir / "model.bin").exists():
            return (False, _("Antes descarga un modelo de transcripción (Whisper) en «Modelos»."), covered)
        audio = str(mp3); offset = 0.0; clip = None
        if covered > 0.5:                                    # reanudar: solo lo que falta
            fd, clip = tempfile.mkstemp(suffix=".wav"); os.close(fd)
            rc, _out = self._proc(["ffmpeg", "-y", "-ss", f"{covered:.3f}", "-i", str(mp3),
                                "-ar", "16000", "-ac", "1", clip])
            if self.mode == "cancel":
                try: os.remove(clip)
                except OSError: pass
                return (True, "", covered)
            if rc == 0 and os.path.exists(clip):
                audio = clip; offset = covered
            else:
                cues.clear(); covered = 0.0                  # no se pudo recortar: rehacer entero
        self.progress.emit(_("Cargando el modelo de transcripción…"), -1)
        cmd = [str(py), "-I", str(CC_ENGINE), "whisper", "--audio", audio,
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
                self.progress.emit(_('Transcribiendo con Whisper… {0} / {1}').format(fmt(covered*1000), fmt(dur*1000)),
                                   int(min(100, covered / dur * 100)) if dur else -1)
                if time.monotonic() - last > 4:
                    last = time.monotonic(); self._save(dest, title, cues, dur, covered, False)
            elif "WH-DONE" in line:
                done_seen = True
            elif "whisper cargado" in line:
                self.progress.emit(_("Transcribiendo con Whisper…"), -1)
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
            return (False, _("No se pudo transcribir en local (¿modelo Whisper incompleto o falta memoria?)."), covered)
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
        active = book.get("active")                       # narrándose ahora: se puede abrir y escuchar
        extra = (_(" · ● narrándose ahora") if active
                 else _(" · ⏸ a medias") if book.get("resumable") else "")
        sub = QLabel(_('{0} capítulos · {1} min{2}').format(book['n'], mins, extra)); sub.setObjectName("booksub")
        col.addWidget(t); col.addWidget(sub)
        row.addLayout(col, 1)
        if active:                                        # ni reanudar ni borrar mientras narra
            return
        if book.get("resumable"):
            self.resumeBtn = QPushButton(_("▸ Reanudar")); self.resumeBtn.setObjectName("tb")
            self.resumeBtn.setToolTip(_("Seguir desde donde se detuvo con {0}. Si cambias de modelo en «Modelos», "
                                        "lo ya hecho se conserva.").format(resume_model_name(book["dir"])))
            self.resumeBtn.setCursor(Qt.PointingHandCursor)
            self.resumeBtn.clicked.connect(lambda: self.resumed.emit(self.dir))
            row.addWidget(self.resumeBtn, 0, Qt.AlignTop)
        self.delBtn = QPushButton("🗑"); self.delBtn.setObjectName("trash")
        self.delBtn.setToolTip(_("Eliminar este libro")); self.delBtn.setCursor(Qt.PointingHandCursor)
        self.delBtn.clicked.connect(lambda: self.deleted.emit(self.dir))
        row.addWidget(self.delBtn, 0, Qt.AlignTop)

    def mousePressEvent(self, e):
        self.opened.emit(self.dir)


# ------------------------------------------------------------------- biblioteca
class ElidedLabel(QLabel):
    """One-line label that cuts long text with "…" instead of widening the window.
    text() returns the full text; the tooltip shows it too."""

    def __init__(self, text=""):
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(60)
        self.setText(text)

    def text(self):
        return self._full

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def _elide(self):
        w = max(0, self.contentsRect().width())
        QLabel.setText(self, self.fontMetrics().elidedText(self._full, Qt.ElideRight, w))

    def resizeEvent(self, e):
        super().resizeEvent(e); self._elide()

    def changeEvent(self, e):
        super().changeEvent(e); self._elide()


class LibraryPage(QWidget):
    openBook = Signal(str)
    openEpub = Signal()
    openModels = Signal()
    importAudio = Signal()
    resumeBook = Signal(str)
    listenGen = Signal()                                  # abrir el libro que se está narrando
    openTranslate = Signal()

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        head = QWidget(); head.setObjectName("head")
        hb = QHBoxLayout(head); hb.setContentsMargins(16, 12, 16, 12); hb.setSpacing(12)
        brand = QVBoxLayout(); brand.setSpacing(2)
        w = QLabel("Storyteller"); w.setObjectName("word")
        self.tag = QLabel(_("tu biblioteca narrada")); self.tag.setObjectName("tag")
        brand.addWidget(w); brand.addWidget(self.tag)
        hb.addLayout(brand); hb.addStretch(1)
        self.refreshBtn = QPushButton(_("↻ Actualizar")); self.refreshBtn.setObjectName("tb")
        self.refreshBtn.setToolTip(_("Volver a leer la carpeta de libros"))
        self.refreshBtn.setCursor(Qt.PointingHandCursor)
        self.refreshBtn.clicked.connect(self.refresh)
        self.voicesBtn = QPushButton(_("Modelos")); self.voicesBtn.setObjectName("tb")
        self.voicesBtn.setCursor(Qt.PointingHandCursor); self.voicesBtn.clicked.connect(self.openModels.emit)
        self.importBtn = QPushButton(_("Importar audiolibro…")); self.importBtn.setObjectName("open")
        self.importBtn.setCursor(Qt.PointingHandCursor)
        self.importBtn.setToolTip(_("Añadir un audiolibro/audio existente y transcribirlo (Whisper) para subtítulos"))
        self.importBtn.clicked.connect(self.importAudio.emit)
        self.openBtn = QPushButton(_("Abrir EPUB…")); self.openBtn.setObjectName("open")
        self.openBtn.setCursor(Qt.PointingHandCursor)
        self.openBtn.clicked.connect(self.openEpub.emit)
        self.translateBtn = QPushButton(_("Traducir…")); self.translateBtn.setObjectName("tb")
        self.translateBtn.setCursor(Qt.PointingHandCursor)
        self.translateBtn.setToolTip(_("Traducir un libro o documento a otro idioma (EPUB, PDF, Word, subtítulos…)"))
        self.translateBtn.clicked.connect(self.openTranslate.emit)
        hb.addWidget(self.refreshBtn); hb.addWidget(self.voicesBtn); hb.addWidget(self.translateBtn)
        hb.addWidget(self.importBtn); hb.addWidget(self.openBtn)
        root.addWidget(head)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setObjectName("reader")
        inner = QWidget(); self.grid = QVBoxLayout(inner)
        self.grid.setContentsMargins(16, 16, 16, 16); self.grid.setSpacing(12)
        self.empty = QLabel(_("Todavía no hay libros.\nPulsa «Abrir EPUB…» y Storyteller lo narrará."))
        self.empty.setObjectName("hint"); self.empty.setWordWrap(True); self.empty.setAlignment(Qt.AlignCenter)
        self.grid.addWidget(self.empty)
        self.grid.addStretch(1)
        self.scroll.setWidget(inner)
        root.addWidget(self.scroll, 1)

        self.progWrap = QWidget(); self.progWrap.setObjectName("progwrap")
        pv = QVBoxLayout(self.progWrap); pv.setContentsMargins(16, 10, 16, 12); pv.setSpacing(7)
        self.progLabel = QLabel(""); self.progLabel.setObjectName("proglabel")
        self.progLabel.setWordWrap(True)                  # con el tiempo restante la línea es larga
        self.prog = QProgressBar(); self.prog.setTextVisible(False); self.prog.setFixedHeight(6)
        pv.addWidget(self.progLabel); pv.addWidget(self.prog)
        btns = QHBoxLayout(); btns.setSpacing(8); btns.addStretch(1)
        self.listenBtn = QPushButton(_("▶ Escuchar")); self.listenBtn.setObjectName("tb")
        self.listenBtn.setToolTip(_("Escuchar lo que ya está narrado mientras sigue narrando"))
        self.listenBtn.clicked.connect(self.listenGen.emit); self.listenBtn.hide()
        self.stopBtn = QPushButton(_("Detener y guardar")); self.stopBtn.setObjectName("tb")
        self.cancelBtn = QPushButton(_("Cancelar")); self.cancelBtn.setObjectName("tb")
        for b in (self.listenBtn, self.stopBtn, self.cancelBtn): b.setCursor(Qt.PointingHandCursor)
        btns.addWidget(self.listenBtn); btns.addWidget(self.stopBtn); btns.addWidget(self.cancelBtn)
        pv.addLayout(btns)
        self.progWrap.hide(); root.addWidget(self.progWrap)

        self._sig = None                                  # resumen de lo que muestran las tarjetas
        self.setStyleSheet(STYLE)
        self.refresh()

    @staticmethod
    def _signature(books):
        return [(str(b["dir"]), b["title"], b["n"], round(b["dur"] / 60), b["resumable"], b["active"])
                for b in books]

    def refresh_if_changed(self):
        """Mientras narra: rehace las tarjetas solo si algo cambió (libro nuevo, más
        capítulos o minutos), para no parpadear ni perder el desplazamiento."""
        books = list_books()
        if self._signature(books) != self._sig:
            self.refresh(books)

    def refresh(self, books=None):
        # vacia las tarjetas (deja empty + stretch al final)
        while self.grid.count() > 2:
            it = self.grid.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        books = list_books() if books is None else books
        self._sig = self._signature(books)
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
        r = QMessageBox.question(self, _("Eliminar libro"),
                                 _('¿Eliminar «{0}» y su grabación? No se puede deshacer.').format(name))
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
    dlState = Signal(str)             # % de la descarga en curso para la biblioteca ("" = ninguna)
    serviceTested = Signal(object)    # lo que dijo la prueba del servicio: dict si fue bien, texto si no

    def __init__(self):
        super().__init__()
        self.dl = None
        self._dl_key = None           # modelo que se está descargando (candado síncrono)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        head = QWidget(); head.setObjectName("head")
        hb = QHBoxLayout(head); hb.setContentsMargins(10, 8, 16, 8); hb.setSpacing(10)
        self.backBtn = QPushButton(_("‹ Biblioteca")); self.backBtn.setObjectName("tb")
        self.backBtn.setCursor(Qt.PointingHandCursor); self.backBtn.clicked.connect(self.back.emit)
        ttl = QLabel(_("Modelos y voces")); ttl.setObjectName("word2")
        hb.addWidget(self.backBtn); hb.addWidget(ttl); hb.addStretch(1)
        root.addWidget(head)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setObjectName("reader")
        inner = QWidget(); self.col = QVBoxLayout(inner)
        self.col.setContentsMargins(16, 16, 16, 16); self.col.setSpacing(10)

        erow = QHBoxLayout(); erow.setSpacing(10)
        el = QLabel(_("Generar en:")); el.setObjectName("booksub")
        self.engineSel = QComboBox()
        self.engineSel.addItem(_("Este equipo (local)"), "local")
        self.engineSel.addItem(_("Servidor propio"), "remote")
        self.engineSel.addItem(_("Un servicio por URL"), "service")
        self.engineSel.currentIndexChanged.connect(self._engine_changed)
        erow.addWidget(el); erow.addWidget(self.engineSel); erow.addStretch(1)
        self.col.addLayout(erow)

        self.serverRow = QWidget()
        sr = QHBoxLayout(self.serverRow); sr.setContentsMargins(0, 0, 0, 0); sr.setSpacing(10)
        slb = QLabel(_("Servidor:")); slb.setObjectName("booksub")
        self.serverEdit = QLineEdit(); self.serverEdit.setPlaceholderText(_("usuario@host de tu servidor (SSH)"))
        self.serverEdit.editingFinished.connect(self._server_changed)
        sr.addWidget(slb); sr.addWidget(self.serverEdit, 1)
        self.col.addWidget(self.serverRow)
        self.serviceBox = self._service_box()
        self.col.addWidget(self.serviceBox)

        self.narrLangRow = QWidget()
        nl = QHBoxLayout(self.narrLangRow); nl.setContentsMargins(0, 0, 0, 0); nl.setSpacing(10)
        nlb = QLabel(_("Idioma de la narración:")); nlb.setObjectName("booksub")
        self.narrLangSel = QComboBox()
        for code, name in NARRATION_LANGS: self.narrLangSel.addItem(name, code)
        self.narrLangSel.setToolTip(_("El idioma en el que se lee el libro. Elige también una voz que suene bien en él. "
                                      "Los modelos F5 solo leen el suyo."))
        self.narrLangSel.currentIndexChanged.connect(self._narr_lang_changed)
        nl.addWidget(nlb); nl.addWidget(self.narrLangSel); nl.addStretch(1)
        self.col.addWidget(self.narrLangRow)

        self.qcChk = QCheckBox(_("Revisar y regenerar frases que suenen mal (QC Whisper)"))
        self.qcChk.setToolTip(_("Tras narrar cada frase, la transcribe con Whisper y, si no coincide con el "
                              "texto, la regenera. Más lento pero caza alucinaciones. Usa el Whisper del "
                              "sitio donde narras: en LOCAL el modelo descargado en «Modelos»; en el servidor, "
                              "el del servidor. (En local hace falta un modelo Whisper descargado.)"))
        self.qcChk.setCursor(Qt.PointingHandCursor)
        self.qcChk.toggled.connect(self._qc_changed)
        self.col.addWidget(self.qcChk)
        # Si el QC no puede revisar en este equipo, se dice aquí y la casilla no engaña.
        self.qcRow = QWidget()
        qr = QHBoxLayout(self.qcRow); qr.setContentsMargins(0, 0, 0, 0); qr.setSpacing(10)
        self.qcHint = QLabel(""); self.qcHint.setObjectName("hint"); self.qcHint.setWordWrap(True)
        self.qcHow = QPushButton(_("Cómo instalarlo")); self.qcHow.setObjectName("tb")
        self.qcHow.setCursor(Qt.PointingHandCursor); self.qcHow.clicked.connect(self._qc_how)
        qr.addWidget(self.qcHint, 1); qr.addWidget(self.qcHow)
        self.col.addWidget(self.qcRow)

        self.parRow = QWidget()
        pr2 = QHBoxLayout(self.parRow); pr2.setContentsMargins(0, 0, 0, 0); pr2.setSpacing(10)
        plb = QLabel(_("Velocidad / uso de GPU:")); plb.setObjectName("booksub")
        self.streamsSel = QComboBox()
        for n in (1, 2, 3):
            self.streamsSel.addItem("", n)
        self._streams_texts(True)
        self.streamsSel.currentIndexChanged.connect(self._streams_changed)
        pr2.addWidget(plb); pr2.addWidget(self.streamsSel); pr2.addStretch(1)
        self.col.addWidget(self.parRow)

        self.note = QLabel(""); self.note.setObjectName("hint"); self.note.setWordWrap(True)
        self.col.addWidget(self.note)

        h1 = QLabel(_("Modelos de voz (narrar EPUB)")); h1.setObjectName("sec"); self.col.addWidget(h1)
        self.cards = QVBoxLayout(); self.cards.setSpacing(10); self.col.addLayout(self.cards)
        self.addModelBtn = QPushButton(_("+ Añadir modelo por URL / repo…")); self.addModelBtn.setObjectName("tb")
        self.addModelBtn.setCursor(Qt.PointingHandCursor); self.addModelBtn.clicked.connect(self._add_model)
        amrow = QHBoxLayout(); amrow.addStretch(1); amrow.addWidget(self.addModelBtn); self.col.addLayout(amrow)

        hw = QLabel(_("Transcripción de audiolibros (Whisper)")); hw.setObjectName("sec"); self.col.addWidget(hw)
        lrow = QHBoxLayout(); lrow.setSpacing(10)
        llb = QLabel(_("Idioma del audio:")); llb.setObjectName("booksub")
        self.langSel = QComboBox()
        for code, name in LANGS: self.langSel.addItem(name, code)
        self.langSel.currentIndexChanged.connect(self._lang_changed)
        lrow.addWidget(llb); lrow.addWidget(self.langSel); lrow.addStretch(1)
        self.col.addLayout(lrow)
        self.whisperNote = QLabel(_("Whisper es opcional y no está instalado. Hace falta para transcribir audiolibros "
                                    "y para revisar las frases (QC)."))
        self.whisperNote.setObjectName("hint"); self.whisperNote.setWordWrap(True); self.col.addWidget(self.whisperNote)
        self.whisperCards = QVBoxLayout(); self.whisperCards.setSpacing(10); self.col.addLayout(self.whisperCards)

        h2 = QLabel(_("Voces de referencia")); h2.setObjectName("sec"); self.col.addWidget(h2)
        self.voiceRows = QVBoxLayout(); self.voiceRows.setSpacing(10); self.col.addLayout(self.voiceRows)
        self.addVoiceBtn = QPushButton(_("+ Añadir voz (archivo)…")); self.addVoiceBtn.setObjectName("tb")
        self.addVoiceBtn.setCursor(Qt.PointingHandCursor); self.addVoiceBtn.clicked.connect(self._add_voice)
        self.addVoiceUrlBtn = QPushButton(_("+ Cargar voz por URL…")); self.addVoiceUrlBtn.setObjectName("tb")
        self.addVoiceUrlBtn.setCursor(Qt.PointingHandCursor); self.addVoiceUrlBtn.clicked.connect(self._add_voice_url)
        avrow = QHBoxLayout(); avrow.addStretch(1)
        avrow.addWidget(self.addVoiceBtn); avrow.addWidget(self.addVoiceUrlBtn); self.col.addLayout(avrow)
        self.vhint = QLabel(""); self.vhint.setObjectName("booksub"); self.vhint.setWordWrap(True)
        self.col.addWidget(self.vhint)

        hv = QLabel(_("Voces descargables")); hv.setObjectName("sec"); self.col.addWidget(hv)
        self.presetRows = QVBoxLayout(); self.presetRows.setSpacing(10); self.col.addLayout(self.presetRows)

        hp = QLabel(_("Pronunciación")); hp.setObjectName("sec"); self.col.addWidget(hp)
        phint = QLabel(_("Cómo lee la voz ciertas palabras: términos extranjeros, siglas y "
                       "palabras concretas que suenen mal. No cambia los subtítulos."))
        phint.setObjectName("booksub"); phint.setWordWrap(True); self.col.addWidget(phint)
        self.editPronBtn = QPushButton(_("Editar diccionario de pronunciación…")); self.editPronBtn.setObjectName("tb")
        self.editPronBtn.setCursor(Qt.PointingHandCursor); self.editPronBtn.clicked.connect(self._edit_pron)
        prow = QHBoxLayout(); prow.addStretch(1); prow.addWidget(self.editPronBtn); self.col.addLayout(prow)

        self.progLabel = QLabel(""); self.progLabel.setObjectName("proglabel"); self.progLabel.setWordWrap(True)
        self.col.addWidget(self.progLabel)
        self.col.addStretch(1)
        self.scroll.setWidget(inner); root.addWidget(self.scroll, 1)

        # Avance de la descarga: fijo abajo, a la vista aunque se haya bajado por la pantalla.
        self.dlWrap = QWidget(); self.dlWrap.setObjectName("progwrap")
        pv = QVBoxLayout(self.dlWrap); pv.setContentsMargins(16, 10, 16, 12); pv.setSpacing(7)
        self.dlLabel = QLabel(""); self.dlLabel.setObjectName("proglabel"); self.dlLabel.setWordWrap(True)
        self.dlLabel.setTextFormat(Qt.PlainText)
        self.dlBar = QProgressBar(); self.dlBar.setTextVisible(False); self.dlBar.setFixedHeight(6)
        pv.addWidget(self.dlLabel); pv.addWidget(self.dlBar)
        drow = QHBoxLayout(); drow.addStretch(1)
        self.dlCancel = QPushButton(_("Cancelar")); self.dlCancel.setObjectName("tb")
        self.dlCancel.setCursor(Qt.PointingHandCursor); self.dlCancel.clicked.connect(self._dl_cancel)
        drow.addWidget(self.dlCancel); pv.addLayout(drow)
        self.dlWrap.hide(); root.addWidget(self.dlWrap)
        self.setStyleSheet(STYLE)
        self.refresh()

    # --- descargas
    def _start_download(self, worker, key, ok_text, after=None):
        if self._dl_key is not None:
            return
        self._dl_key, self.dl = key, worker
        self.dlCancel.show(); self.dlCancel.setEnabled(True)
        self._dl_progress(_('Descargando «{0}»…').format(worker.name), -1)
        self.dlWrap.show()
        worker.progress.connect(self._dl_progress)
        worker.done.connect(lambda: self._dl_end(ok_text, after))
        worker.failed.connect(self._dl_failed)
        worker.canceled.connect(self._dl_canceled)
        worker.start()
        self.refresh()

    def _dl_progress(self, text, pct):
        self.dlLabel.setText(text)
        if pct < 0:
            self.dlBar.setRange(0, 0)
        else:
            self.dlBar.setRange(0, 100); self.dlBar.setValue(pct)
        self.dlState.emit(f"{pct} %" if pct >= 0 else "…")

    def _dl_finish(self, text):
        self._dl_key = None
        self.dlCancel.hide()
        self.dlBar.setRange(0, 100)
        self.dlLabel.setText(text)
        self.dlState.emit("")
        self.refresh()
        QTimer.singleShot(8000, self._dl_hide)

    def _dl_hide(self):
        if self._dl_key is None:
            self.dlWrap.hide()

    def _dl_end(self, text, after):
        self.dlBar.setValue(100)
        if after:
            after()
        self._dl_finish(text)

    def _dl_failed(self, e):
        self.dlBar.setValue(0)
        self._dl_finish(_("Error en la descarga: ") + e.split("\n")[0])
        QMessageBox.warning(self, "Storyteller", _("Error en la descarga: ") + e)

    def _dl_canceled(self):
        self.dlBar.setValue(0)
        self._dl_finish(_("Descarga cancelada. Si vuelves a descargar, empezará de cero."))

    def _dl_cancel(self):
        self.dlCancel.setEnabled(False)
        self.dlLabel.setText(_("Cancelando…"))
        self.dl.stop()

    def stop_download(self, wait_ms=4000):
        """Al cerrar la app: que no se quede una descarga huérfana en segundo plano."""
        if self.dl is not None and self.dl.isRunning():
            self.dl.stop()
            self.dl.wait(wait_ms)

    def _mark_dl_button(self, btn, key):
        if self._dl_key is None:
            return
        btn.setEnabled(False)
        if key == self._dl_key:
            btn.setText(_("Descargando…"))
        else:
            btn.setToolTip(_("Ya hay una descarga en marcha."))

    def _streams_texts(self, local):
        """Las velocidades del selector se midieron con XTTS en este equipo; en un servidor
        la GPU y el motor son otros, así que allí solo se dice el número de tramos."""
        if local:
            texts = (_("Normal — 1 tramo (no satura la GPU, más lento)"),
                     _("Rápido — 2 tramos (≈1,8×)"),
                     _("Máximo — 3 tramos (≈2,6×, exprime la GPU)"))
            tip = _("Cómo narrar en este equipo:\n"
                    "• Normal: un solo proceso, como siempre. No satura la GPU "
                    "(puedes seguir usándola), pero es lo más lento.\n"
                    "• Rápido / Máximo: reparte los capítulos entre 2 o 3 procesos "
                    "a la vez. Mucho más rápido, pero la GPU queda muy cargada "
                    "(con 3, al límite: no juegues mientras narra). Si diera error "
                    "de memoria de la GPU, baja de 3 a 2.")
        else:
            texts = (_("1 tramo"), _("2 tramos"), _("3 tramos"))
            tip = _("Cuántos tramos narran a la vez en la GPU del servidor. Cada uno ocupa unos 2 GB de "
                    "su memoria: si no caben, la narración avisa y hay que bajar el número. Más tramos "
                    "no siempre es más rápido: con F5 en una RTX 2060 se tarda lo mismo con 1 que con 2.")
        for i, t in enumerate(texts):
            self.streamsSel.setItemText(i, t)
        self.streamsSel.setToolTip(tip)

    def _apply_engine_view(self, cfg):
        """Parte de la pantalla que depende de local/remoto. Se puede llamar sola
        (al cambiar el motor) sin reconstruir la lista de modelos/voces ni perder scroll."""
        self.serverRow.setVisible(cfg["engine"] == "remote")
        self.serviceBox.setVisible(cfg["engine"] == "service")
        self._apply_qc_view(cfg)
        self.parRow.setVisible(cfg["engine"] != "service")  # el servicio lleva su selector en serviceBox
        self.narrLangRow.setVisible(cfg["engine"] == "service" or model_by_id(cfg.get("model"), cfg).get("backend") == "xtts")
        self._streams_texts(cfg["engine"] == "local")
        if cfg["engine"] == "service":
            self.note.setText(_("Cada frase se pide al servicio de esa URL: el tuyo (st_server.py) o uno público con su clave. Aquí no hace falta GPU ni modelo de voz. La transcripción de audiolibros sigue en este equipo."))
        elif cfg["engine"] == "local" and not (xtts_ready() or f5_ready()):
            self.note.setText(_("Para narrar en este equipo falta un motor de voz: XTTS (engine/.venv-xtts) o F5, que es opcional. "
                                "Los comandos están en su ficha y en el README. Mientras tanto puedes generar en un servidor "
                                "propio o con un servicio por URL. Whisper (transcribir audiolibros y QC) es opcional y se "
                                "instala aparte."))
        elif cfg["engine"] == "local" and not voice_engine_ready(model_by_id(cfg.get("model"), cfg)):
            self.note.setText(_("El modelo activo necesita un motor que no está instalado. Elige otro modelo o instálalo: "
                                "los comandos están en su ficha y en el README."))
        elif cfg["engine"] == "local":
            self.note.setText(_("Se genera en este equipo con la GPU. Elige modelo y voz; descarga el modelo si hace falta."))
        else:
            self.note.setText(_("Se genera por SSH en el servidor que indiques. Con XTTS usa el motor y la voz de este equipo, y el servidor necesita tener XTTS instalado. Con F5, el motor y la voz son los del servidor."))

    def _apply_qc_view(self, cfg):
        """En local, la casilla del QC solo se puede marcar si de verdad va a revisar. En un servidor, su Whisper
        no es cosa de este equipo. Con un servicio por URL siempre se puede marcar, porque al marcarla se puede
        escribir la URL de un Whisper; lo que falte se dice debajo y al narrar."""
        why = ""
        info = ""
        no_whisper = False
        if cfg["engine"] == "local":
            no_whisper = not qc_can_run(ENGINE_DIR, model_by_id(cfg.get("model"), cfg).get("backend", "f5"))
            if no_whisper:
                why = _("El QC necesita Whisper en el entorno del motor que narra, y no está.")
            elif not whisper_installed(whisper_by_id(active_whisper_id(cfg))):
                why = _("El QC necesita un modelo Whisper: descárgalo más abajo.")
        elif cfg["engine"] == "service" and cfg.get("qc_auto"):
            info, no_whisper = service_qc_hint(cfg)
        for widget in self.svcQcWidgets:
            widget.setVisible(cfg["engine"] == "service" and bool(cfg.get("qc_auto")))
        self.qcChk.setEnabled(not why)
        self.qcChk.blockSignals(True)
        self.qcChk.setChecked(bool(cfg.get("qc_auto")) and not why)
        self.qcChk.blockSignals(False)
        self.qcHint.setText(why or info)
        self.qcHow.setVisible(no_whisper)
        self.qcRow.setVisible(bool(why or info))

    def _qc_how(self):
        cfg = load_config()
        backend = model_by_id(cfg.get("model"), cfg).get("backend", "f5")
        if cfg.get("engine") == "service":
            backend = "xtts" if service_python(cfg) == VPY_XTTS else "f5"
        show_how(self, _("Para revisar las frases (QC), Whisper tiene que estar en el entorno del motor que narra, y no está. "
                         "Para instalarlo ahí, ejecuta esto en una terminal:\n\n{0}").format(whisper_install_text(backend)))

    def refresh(self):
        cfg = load_config()
        self.engineSel.blockSignals(True)
        self.engineSel.setCurrentIndex(max(0, self.engineSel.findData(cfg["engine"])))
        self.engineSel.blockSignals(False)
        self.serverEdit.setText(cfg.get("server", ""))
        svc = service_cfg(cfg)
        self.svcUrl.setText(svc["url"]); self.svcKey.setText(service_key())
        self.svcQcUrl.setText(svc["qc_url"]); self.svcQcModel.setText(svc["qc_model"])
        self.svcQcKey.setText(service_key(SERVICE_QC_KEY))
        self.svcModel.setCurrentText(svc["model"]); self.svcVoice.setCurrentText(svc["voice"])
        self.svcParallel.blockSignals(True)
        self.svcParallel.setCurrentIndex(max(0, self.svcParallel.findData(svc["parallel"])))
        self.svcParallel.blockSignals(False)
        self._service_warn()
        self.streamsSel.blockSignals(True)
        si = {1: 0, 2: 1, 3: 2}.get(int(cfg.get("parallel_streams", 1) or 1), 0)
        self.streamsSel.setCurrentIndex(si); self.streamsSel.blockSignals(False)
        self.narrLangSel.blockSignals(True)
        self.narrLangSel.setCurrentIndex(max(0, self.narrLangSel.findData(narration_language(cfg))))
        self.narrLangSel.blockSignals(False)
        self.langSel.blockSignals(True)
        li = next((i for i, (c, _n) in enumerate(LANGS) if c == active_language(cfg)), 0)
        self.langSel.setCurrentIndex(li); self.langSel.blockSignals(False)
        self._apply_engine_view(cfg)
        whisper = bool(whisper_python())
        self.whisperNote.setVisible(not whisper)
        self.vhint.setText(_("Al añadir una voz, el texto de referencia se transcribe solo (Whisper); puedes corregirlo.")
                           if whisper else _("Sin Whisper instalado, el texto de referencia de una voz lo escribes tú."))
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
            chip = QLabel(_("activo")); chip.setObjectName("chip"); top.addWidget(chip)
        top.addStretch(1)
        engine_ok = voice_engine_ready(mdl)
        state = QLabel(_("Motor sin instalar") if not engine_ok else _("✓ Instalado") if model_installed(mdl)
                       else _('Sin descargar · {0}').format(mdl.get('size','?')))
        state.setObjectName("booksub"); top.addWidget(state)
        lay.addLayout(top)
        desc = QLabel(_('{0}  · Idioma: {1}  ({2})').format(mdl.get('desc',''), lang_name(model_language(mdl, cfg)), mdl['repo']))
        desc.setObjectName("booksub"); desc.setWordWrap(True)
        lay.addWidget(desc)
        is_xtts = mdl.get("backend") == "xtts"
        if not is_xtts and not engine_ok:
            hint = QLabel(_("F5 es opcional y no está instalado: no se puede narrar con este modelo hasta instalarlo."))
            hint.setObjectName("hint"); hint.setWordWrap(True); lay.addWidget(hint)
        btns = QHBoxLayout(); btns.setSpacing(8); btns.addStretch(1)
        if not engine_ok:
            how = QPushButton(_("Cómo instalarlo")); how.setObjectName("open")
            if is_xtts:
                how.setToolTip(_("Falta instalar el motor XTTS en engine/.venv-xtts: mira el README"))
            how.clicked.connect(lambda _checked, m=mdl: show_how(self, engine_how_text(m)))
            btns.addWidget(how)
            if model_installed(mdl):                     # los pesos ya descargados se pueden borrar sin el motor
                dele = QPushButton(_("Borrar")); dele.setObjectName("tb")
                dele.clicked.connect(lambda _checked, m=mdl: self._delete_model(m))
                btns.addWidget(dele)
        elif model_installed(mdl):
            use = QPushButton(_("Usar")); use.setObjectName("tb"); use.setEnabled(cfg["model"] != mdl["id"])
            use.clicked.connect(lambda _checked, i=mdl["id"]: self._use_model(i))
            dele = QPushButton(_("Borrar")); dele.setObjectName("tb")
            dele.clicked.connect((lambda _checked, m=mdl: self._delete_xtts(m)) if is_xtts
                                 else (lambda _checked, m=mdl: self._delete_model(m)))
            btns.addWidget(use); btns.addWidget(dele)
        else:
            dl = QPushButton(_('Descargar  ({0})').format(mdl.get('size','?'))); dl.setObjectName("open")
            dl.clicked.connect((lambda _checked, m=mdl: self._download_xtts(m)) if is_xtts
                               else (lambda _checked, m=mdl: self._download(m)))
            self._mark_dl_button(dl, mdl["id"])
            btns.addWidget(dl)
        if mdl.get("custom"):
            rm = QPushButton(_("Quitar")); rm.setObjectName("tb")
            rm.clicked.connect(lambda _checked, i=mdl["id"]: self._remove_custom(i)); btns.addWidget(rm)
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
            chip = QLabel(_("activo")); chip.setObjectName("chip"); top.addWidget(chip)
        top.addStretch(1)
        engine_ok = bool(whisper_python())
        state = QLabel(_("Motor sin instalar") if not engine_ok else _("✓ Instalado") if whisper_installed(wm)
                       else _('Sin descargar · {0}').format(wm.get('size','?')))
        state.setObjectName("booksub"); top.addWidget(state)
        lay.addLayout(top)
        desc = QLabel(f"{wm.get('desc','')}  ({wm['repo']})"); desc.setObjectName("booksub"); desc.setWordWrap(True)
        lay.addWidget(desc)
        btns = QHBoxLayout(); btns.setSpacing(8); btns.addStretch(1)
        if not engine_ok:
            how = QPushButton(_("Cómo instalarlo")); how.setObjectName("open")
            how.clicked.connect(lambda _checked: show_how(self, whisper_how_text()))
            btns.addWidget(how)
            if whisper_installed(wm):
                dele = QPushButton(_("Borrar")); dele.setObjectName("tb")
                dele.clicked.connect(lambda _checked, m=wm: self._delete_whisper(m))
                btns.addWidget(dele)
        elif whisper_installed(wm):
            use = QPushButton(_("Usar")); use.setObjectName("tb"); use.setEnabled(active_whisper_id(cfg) != wm["id"])
            use.clicked.connect(lambda _checked, i=wm["id"]: self._use_whisper(i))
            dele = QPushButton(_("Borrar")); dele.setObjectName("tb")
            dele.clicked.connect(lambda _checked, m=wm: self._delete_whisper(m))
            btns.addWidget(use); btns.addWidget(dele)
        else:
            dl = QPushButton(_('Descargar  ({0})').format(wm.get('size','?'))); dl.setObjectName("open")
            dl.clicked.connect(lambda _checked, m=wm: self._download_whisper(m))
            self._mark_dl_button(dl, wm["id"])
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
        use = QPushButton(_("Usar")); use.setObjectName("tb"); use.setCursor(Qt.PointingHandCursor)
        use.setEnabled(cfg.get("voice", "narrator") != v["id"])
        use.clicked.connect(lambda _checked, i=v["id"]: self._use_voice(i)); row.addWidget(use)
        if v["id"] != "narrator":
            rm = QPushButton(_("Borrar")); rm.setObjectName("tb"); rm.setCursor(Qt.PointingHandCursor)
            rm.clicked.connect(lambda _checked, vv=v: self._delete_voice(vv)); row.addWidget(rm)
        return f

    # --- modelos
    def _engine_changed(self, _arg):
        # Solo actualiza lo que cambia (fila de servidor + texto): no reconstruye
        # la lista de modelos/voces, así no parpadea ni salta el scroll.
        cfg = load_config(); cfg["engine"] = self.engineSel.currentData(); save_config(cfg)
        self._apply_engine_view(cfg)

    def _server_changed(self):
        cfg = load_config(); cfg["server"] = self.serverEdit.text().strip(); save_config(cfg)

    # --- servicio por URL
    def _service_box(self):
        box = QWidget()
        g = QGridLayout(box); g.setContentsMargins(0, 0, 0, 0); g.setHorizontalSpacing(10); g.setVerticalSpacing(8)
        self.svcUrl = QLineEdit(); self.svcUrl.setPlaceholderText("https://…  ·  192.168.1.5:8765")
        self.svcKey = QLineEdit(); self.svcKey.setPlaceholderText(_("Vacía si el servicio no la pide"))
        self.svcKey.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.svcModel = QComboBox(); self.svcVoice = QComboBox()
        for combo in (self.svcModel, self.svcVoice):           # se puede escribir: no todos dan la lista
            combo.setEditable(True); combo.setInsertPolicy(QComboBox.NoInsert)
            combo.lineEdit().setPlaceholderText(_("El que use por defecto"))
            combo.activated.connect(self._service_changed)
            combo.lineEdit().editingFinished.connect(self._service_changed)
        self.svcParallel = QComboBox()
        for n in range(1, 5):
            self.svcParallel.addItem(str(n), n)
        self.svcParallel.setToolTip(_("Cuántas frases se piden al servicio a la vez. Más es más rápido si el servicio "
                                      "lo permite: el tuyo (st_server.py) abre hasta 3 XTTS si caben en la tarjeta "
                                      "gráfica, y uno público puede limitar las peticiones por minuto."))
        self.svcParallel.currentIndexChanged.connect(self._service_changed)
        self.svcUrl.editingFinished.connect(self._service_changed)
        self.svcKey.editingFinished.connect(self._service_changed)
        for row, (text, field) in enumerate(((_("URL:"), self.svcUrl), (_("Clave de acceso:"), self.svcKey),
                                             (_("Modelo:"), self.svcModel), (_("Voz:"), self.svcVoice),
                                             (_("Peticiones a la vez:"), self.svcParallel))):
            lb = QLabel(text); lb.setObjectName("booksub")
            g.addWidget(lb, row, 0); g.addWidget(field, row, 1)
        self.svcTest = QPushButton(_("Probar")); self.svcTest.setObjectName("tb")
        self.svcTest.setCursor(Qt.PointingHandCursor); self.svcTest.clicked.connect(self._service_test)
        self.svcStatus = QLabel(""); self.svcStatus.setObjectName("hint"); self.svcStatus.setWordWrap(True)
        test = QHBoxLayout(); test.setSpacing(10)
        test.addWidget(self.svcTest); test.addWidget(self.svcStatus, 1)
        g.addLayout(test, 5, 1)
        # Con qué revisar lo que llega: solo se ve con la casilla del QC marcada.
        self.svcQcUrl = QLineEdit(); self.svcQcUrl.setPlaceholderText(_("Vacío: el Whisper de este equipo, en CPU"))
        self.svcQcKey = QLineEdit(); self.svcQcKey.setPlaceholderText(_("Vacía si el servicio no la pide"))
        self.svcQcKey.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.svcQcModel = QLineEdit(); self.svcQcModel.setPlaceholderText(_("El que use por defecto"))
        self.svcQcWidgets = []
        for row, (text, field) in enumerate(((_("Whisper para revisar:"), self.svcQcUrl), (_("Clave de Whisper:"), self.svcQcKey),
                                             (_("Modelo de Whisper:"), self.svcQcModel)), 6):
            lb = QLabel(text); lb.setObjectName("booksub")
            g.addWidget(lb, row, 0); g.addWidget(field, row, 1)
            field.editingFinished.connect(self._service_changed)
            self.svcQcWidgets += [lb, field]
        self.svcQcStatus = QLabel(""); self.svcQcStatus.setObjectName("hint"); self.svcQcStatus.setWordWrap(True)
        qc_cost = QLabel(_("Cada frase que llega se transcribe y, si no coincide con el texto, se pide otra vez, hasta 2 "
                           "veces más: tarda más y, en un servicio de pago, cuesta más."))
        qc_cost.setObjectName("hint"); qc_cost.setWordWrap(True)
        g.addWidget(self.svcQcStatus, 9, 1); g.addWidget(qc_cost, 10, 1)
        self.svcQcWidgets += [self.svcQcStatus, qc_cost]
        self.serviceTested.connect(self._service_tested)
        return box

    def _service_warn(self):
        svc = service_module(); url = self.svcUrl.text().strip()
        self.svcStatus.setText(_("Esa dirección no es válida.") if url and not svc.api_base(url) else
                               _("Esa dirección no va cifrada: la clave y el texto del libro viajarían a la vista. "
                                 "Fuera de tu red de casa, usa https.") if svc.is_unsafe(url) else "")
        url = self.svcQcUrl.text().strip()
        self.svcQcStatus.setText(_("Esa dirección no es válida.") if url and not svc.api_base(url) else
                                 _("Esa dirección no va cifrada: la clave y el audio viajarían a la vista. "
                                   "Fuera de tu red de casa, usa https.") if svc.is_unsafe(url) else "")

    def _service_changed(self, *_args):
        cfg = load_config()
        cfg["service"] = {"url": self.svcUrl.text().strip(), "model": self.svcModel.currentText().strip(),
                          "voice": self.svcVoice.currentText().strip(),
                          "parallel": int(self.svcParallel.currentData() or 1),
                          "qc_url": self.svcQcUrl.text().strip(), "qc_model": self.svcQcModel.text().strip()}
        save_config(cfg); save_service_key(self.svcKey.text().strip())
        save_service_key(self.svcQcKey.text().strip(), SERVICE_QC_KEY)
        self._service_warn()
        self._apply_qc_view(cfg)

    def _service_test(self):
        self._service_changed()
        s = service_cfg(); key = service_key()
        self.svcTest.setEnabled(False); self.svcStatus.setText(_("Probando…"))

        def work():                                  # hilo daemon: cerrar la app no espera a un servicio lento
            mod = service_module()
            try:
                self.serviceTested.emit(mod.probe(s["url"], key, s["model"], s["voice"]))
            except mod.ServiceError as e:
                self.serviceTested.emit(service_error_text(e.kind, e.detail))
            except Exception as e:
                self.serviceTested.emit(_('Error inesperado: {0}').format(e))
        threading.Thread(target=work, daemon=True).start()

    def _service_tested(self, answer):
        self.svcTest.setEnabled(True)
        if isinstance(answer, str):
            self.svcStatus.setText(answer); return
        for combo, names in ((self.svcModel, answer["models"]), (self.svcVoice, answer["voices"])):
            chosen = combo.currentText()
            combo.clear(); combo.addItems(names)
            combo.setCurrentText(chosen or (names[0] if len(names) == 1 or combo is self.svcVoice and names else ""))
        self._service_changed()
        self.svcStatus.setText(_('Funciona: la frase de prueba ha durado {0} s.').format(f'{answer["seconds"]:.1f}'))

    def _qc_changed(self, on):
        cfg = load_config(); cfg["qc_auto"] = bool(on); save_config(cfg)
        self._apply_qc_view(cfg)

    def _streams_changed(self, _arg):
        cfg = load_config(); cfg["parallel_streams"] = int(self.streamsSel.currentData() or 1); save_config(cfg)

    def _narr_lang_changed(self, _arg):
        cfg = load_config(); cfg["narration_lang"] = self.narrLangSel.currentData(); save_config(cfg)
        self.refresh()                               # la ficha de XTTS dice en qué idioma lee

    def _lang_changed(self, _arg):
        cfg = load_config(); cfg["language"] = self.langSel.currentData(); save_config(cfg)

    def _edit_pron(self):
        """Editor sencillo del diccionario de pronunciación (JSON validado)."""
        p = ensure_pron()
        try: cur = p.read_text("utf-8")
        except Exception: cur = json.dumps(DEFAULT_PRON, ensure_ascii=False, indent=2)
        txt, ok = QInputDialog.getMultiLineText(self, _("Diccionario de pronunciación"),
            _("pron: palabra → cómo se dice · say_letters: siglas deletreadas · "
            "say_word: siglas como palabra") + "\n" +
            _('Para otro idioma, dentro de "langs": {"en": {"pron": {"nginx": "engine x"}}}'), cur)
        if not ok: return
        try:
            data = json.loads(txt)
            assert isinstance(data, dict)
        except Exception:
            QMessageBox.warning(self, _("Pronunciación"), _("El JSON no es válido; no se guardó.")); return
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        self.progLabel.setText(_("Diccionario de pronunciación guardado."))

    def _use_model(self, mid):
        cfg = load_config(); cfg["model"] = mid; save_config(cfg); self.refresh()

    def _delete_model(self, mdl):
        d = model_dir(mdl["id"])
        if d.exists(): shutil.rmtree(d, ignore_errors=True)
        self.progLabel.setText(_('Modelo «{0}» borrado (archivos).').format(mdl['name'])); self.refresh()

    def _download_xtts(self, mdl):
        self._start_download(XttsDownloadWorker(mdl["name"]), mdl["id"], _("XTTS v2 listo."))

    def _delete_xtts(self, mdl):
        """Borra el modelo XTTS de la caché de coqui-tts (deja el venv)."""
        import glob
        removed = False
        for base in (Path.home() / "AppData/Local/tts", Path.home() / ".local/share/tts"):
            if base.exists():
                for d in base.glob("*xtts_v2*"):
                    shutil.rmtree(d, ignore_errors=True); removed = True
        if xtts_hf_dir().exists():                   # aquí está el modelo de verdad (1,8 GB)
            shutil.rmtree(xtts_hf_dir(), ignore_errors=True); removed = True
        cfg = load_config()
        if cfg.get("model") == mdl["id"]: cfg["model"] = next_model_id(cfg); save_config(cfg)
        self.progLabel.setText(_("Modelo XTTS borrado (caché).") if removed else _("No había caché de XTTS."))
        self.refresh()

    def _remove_custom(self, mid):
        cfg = load_config()
        cfg["custom_models"] = [m for m in cfg.get("custom_models", []) if m["id"] != mid]
        if cfg.get("model") == mid: cfg["model"] = next_model_id(cfg)
        save_config(cfg); self.refresh()

    def _add_model(self):
        repo, ok = QInputDialog.getText(self, _("Añadir modelo"),
            _("Repo o URL de Hugging Face\n(p. ej. usuario/modelo o https://huggingface.co/usuario/modelo):"))
        if not ok or not repo.strip(): return
        repo = repo.strip()
        if "huggingface.co/" in repo:
            repo = repo.split("huggingface.co/", 1)[1].strip("/")
            repo = "/".join(repo.split("/")[:2])
        ckpt, ok = QInputDialog.getText(self, _("Añadir modelo"),
            _("Nombre del checkpoint dentro del repo:"), text="model_last.safetensors")
        if not ok or not ckpt.strip(): return
        vocab, ok = QInputDialog.getText(self, _("Añadir modelo"),
            _("Nombre del vocabulario dentro del repo:"), text="vocab.txt")
        if not ok or not vocab.strip(): return
        arch, ok = QInputDialog.getText(self, _("Añadir modelo"),
            _("Arquitectura (F5TTS_Base para la mayoría; F5TTS_v1_Base para el oficial v1):"), text="F5TTS_Base")
        if not ok or not arch.strip(): return
        lang, ok = QInputDialog.getText(self, _("Añadir modelo"),
            _("Idioma del modelo (código: es, en, fr, de, it, pt…):"), text="es")
        if not ok or not lang.strip(): return
        name, ok = QInputDialog.getText(self, _("Añadir modelo"), _("Nombre para mostrar:"), text=repo.split("/")[-1])
        if not ok or not name.strip(): return
        cfg = load_config()
        customs = cfg.get("custom_models", [])
        mid = "u-" + slug(repo)
        customs = [m for m in customs if m["id"] != mid]
        customs.append({"id": mid, "name": name.strip(), "repo": repo,
                        "ckpt": ckpt.strip(), "vocab": vocab.strip(), "arch": arch.strip(),
                        "lang": lang.strip().lower(), "size": "?", "desc": _("Añadido por ti"), "custom": True})
        cfg["custom_models"] = customs; save_config(cfg)
        self.progLabel.setText(_('Modelo «{0}» añadido. Pulsa Descargar.').format(name.strip()))
        self.refresh()

    def _download(self, mdl):
        self._start_download(DownloadWorker(mdl), mdl["id"], _("Descarga completada."))

    # --- whisper (transcripción de audiolibros)
    def _use_whisper(self, wid):
        cfg = load_config(); cfg["whisper_model"] = wid; save_config(cfg); self.refresh()

    def _delete_whisper(self, wm):
        d = model_dir(wm["id"])
        if d.exists(): shutil.rmtree(d, ignore_errors=True)
        self.progLabel.setText(_('Modelo de transcripción «{0}» borrado.').format(wm['name'])); self.refresh()

    def _download_whisper(self, wm):
        self._start_download(WhisperDownloadWorker(wm), wm["id"], _("Modelo de transcripción listo."),
                             after=lambda i=wm["id"]: self._use_whisper(i))

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
        Usa el Whisper local (si hay modelo) o el del servidor; devuelve texto o None."""
        cfg = load_config()
        try:
            if cfg.get("engine", "local") != "remote":
                mdir = model_dir(active_whisper_id(cfg)); py = whisper_python()
                if not py or not (mdir / "model.bin").exists():
                    return None
                p = subprocess.run([str(py), "-I", str(CC_ENGINE), "whisper", "--audio", str(audio),
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
            raise RuntimeError(_("no se pudo convertir el audio (¿ffmpeg?)"))
        (VOICES_DIR / f"{vid}.txt").write_text(text.strip(), encoding="utf-8")
        (VOICES_DIR / f"{vid}.name").write_text(name.strip(), encoding="utf-8")
        return vid

    def _voice_wizard(self, audio, cleanup=None):
        """Flujo común: autotranscribe el texto (editable) + nombre + guardar."""
        self.progLabel.setText(_("Transcribiendo la voz con Whisper…")); QApplication.processEvents()
        guess = self._autotranscribe(audio) or ""
        self.progLabel.setText("")
        txt, ok = QInputDialog.getMultiLineText(self, _("Texto de la voz"),
            _("Texto que dice el audio (autodetectado; corrígelo si hace falta):"), guess)
        if ok and txt.strip():
            name, ok = QInputDialog.getText(self, _("Añadir voz"), _("Nombre para la voz:"), text=_("Mi voz"))
            if ok and name.strip():
                try:
                    vid = self._voice_save(audio, name, txt)
                    self._use_voice(vid)                 # activar + refrescar
                    self.progLabel.setText(_('Voz «{0}» añadida y activada.').format(name.strip()))
                except Exception as e:
                    self.progLabel.setText(_("No pude añadir la voz: ") + str(e)); self.refresh()
        if cleanup:
            try: os.remove(cleanup)
            except OSError: pass

    def _add_voice(self):
        wav, _filter = QFileDialog.getOpenFileName(self, _("Audio de la voz (5-12 s, limpio)"),
                                             str(Path.home()), _("Audio (*.wav *.mp3 *.flac *.m4a *.ogg *.opus)"))
        if wav: self._voice_wizard(wav)

    def _add_voice_url(self):
        url, ok = QInputDialog.getText(self, _("Cargar voz por URL"),
                                       _("Enlace al audio de la voz (http/https, wav/mp3, 5-15 s):"))
        if not ok or not url.strip(): return
        if not is_http_url(url):
            self.progLabel.setText(_("La URL debe empezar por http:// o https://")); return
        fd, tmp = tempfile.mkstemp(suffix=".audio"); os.close(fd)
        self.progLabel.setText(_("Descargando la voz…")); QApplication.processEvents()
        rc = subprocess.run(["curl", "-s", "-L", "--max-time", "120", "-o", tmp, url.strip()],
                            creationflags=NOWIN).returncode
        if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 2000:
            self.progLabel.setText(_("No se pudo descargar la voz desde esa URL."))
            try: os.remove(tmp)
            except OSError: pass
            return
        self._voice_wizard(tmp, cleanup=tmp)

    def _download_preset(self, pr):
        if not is_http_url(pr.get("url", "")):
            self.progLabel.setText(_("URL de voz no válida.")); return
        fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        self.progLabel.setText(_('Descargando «{0}»…').format(pr['name'])); QApplication.processEvents()
        rc = subprocess.run(["curl", "-s", "-L", "--max-time", "120", "-o", tmp, pr["url"]],
                            creationflags=NOWIN).returncode
        if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 2000:
            self.progLabel.setText(_("No se pudo descargar la voz."))
            try: os.remove(tmp)
            except OSError: pass
            return
        text = pr.get("text") or (self._autotranscribe(tmp) or "")
        try:
            self._voice_save(tmp, pr["name"], text)
            self.progLabel.setText(_('Voz «{0}» descargada.').format(pr['name']))
        except Exception as e:
            self.progLabel.setText(_("Error al guardar la voz: ") + str(e))
        try: os.remove(tmp)
        except OSError: pass
        self.refresh()

    def _preset_card(self, pr):
        f = QFrame(); f.setObjectName("bookcard")
        row = QHBoxLayout(f); row.setContentsMargins(16, 10, 16, 10); row.setSpacing(10)
        name = QLabel(_('{0}  · Idioma: {1}').format(pr['name'], lang_name(pr.get('lang','es'))))
        name.setObjectName("booktitle"); row.addWidget(name); row.addStretch(1)
        has = (VOICES_DIR / f"{slug(pr['name'])}.wav").exists()
        if has:
            lbl = QLabel(_("✓ Descargada")); lbl.setObjectName("booksub"); row.addWidget(lbl)
        else:
            dl = QPushButton(_("Descargar")); dl.setObjectName("open"); dl.setCursor(Qt.PointingHandCursor)
            dl.clicked.connect(lambda _checked, p=pr: self._download_preset(p)); row.addWidget(dl)
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
        # Escuchar mientras se narra (ver LISTEN_DIR):
        self._live = False            # el libro abierto se está narrando ahora
        self._partial = False         # el capítulo cargado está a medias (suena una copia)
        self._waiting = None          # {"pos": ms, "at": ms, "play": bool, "next": bool} = esperando narración
        self._cur_base = None         # capítulo cargado (por nombre: su índice cambia al llegar otros)
        self._play_after_restore = False
        self._liveTimer = QTimer(self); self._liveTimer.setInterval(2500)
        self._liveTimer.timeout.connect(self._live_tick)

        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self._vol = max(0, min(100, int(load_config().get("volume", 100) or 100)))
        self.audio.setVolume(self._vol / 100); self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self.on_pos)
        self.player.durationChanged.connect(self.on_dur)
        self.player.playbackStateChanged.connect(self.on_state)
        self.player.mediaStatusChanged.connect(self.on_media_status)

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # Two rows so long translations fit: actions on top, audio output/volume/chapter below
        head = QWidget(); head.setObjectName("head"); self.head = head
        hv = QVBoxLayout(head); hv.setContentsMargins(10, 8, 16, 8); hv.setSpacing(6)
        hb = QHBoxLayout(); hb.setSpacing(10); hv.addLayout(hb)
        hb2 = QHBoxLayout(); hb2.setSpacing(10); hv.addLayout(hb2)
        self.backBtn = QPushButton(_("‹ Biblioteca")); self.backBtn.setObjectName("tb")
        self.backBtn.setCursor(Qt.PointingHandCursor); self.backBtn.clicked.connect(self.back.emit)
        self.booklbl = ElidedLabel(""); self.booklbl.setObjectName("word2")
        self.regenBtn = QPushButton(_("↻ Regenerar")); self.regenBtn.setObjectName("tb")
        self.regenBtn.setCursor(Qt.PointingHandCursor)
        self.regenBtn.setToolTip(_("Volver a narrar el libro entero desde su EPUB"))
        self.regenBtn.clicked.connect(self._do_regen); self.regenBtn.hide()
        self.resumeBtn = QPushButton(_("▸ Reanudar")); self.resumeBtn.setObjectName("tb")
        self.resumeBtn.setCursor(Qt.PointingHandCursor)
        self.resumeBtn.setToolTip(_("Seguir narrando desde donde se detuvo"))
        self.resumeBtn.clicked.connect(self._do_resume); self.resumeBtn.hide()
        self.exportBtn = QPushButton(_("⤓ Exportar")); self.exportBtn.setObjectName("tb")
        self.exportBtn.setCursor(Qt.PointingHandCursor)
        self.exportBtn.setToolTip(_("Guardar el audio narrado como MP3"))
        self.exportBtn.clicked.connect(self._export); self.exportBtn.hide()
        hb.addWidget(self.backBtn); hb.addWidget(self.booklbl, 1); hb.addWidget(self.resumeBtn)
        hb.addWidget(self.regenBtn); hb.addWidget(self.exportBtn)
        spk = QLabel("🔊"); spk.setObjectName("time"); hb2.addWidget(spk)
        self.outSel = QComboBox(); self.outSel.setMinimumWidth(120); self.outSel.setMaximumWidth(280); self.outSel.setToolTip(_("Salida de audio"))
        self.outSel.currentIndexChanged.connect(self._out_changed)
        hb2.addWidget(self.outSel, 1)
        self.volSld = QSlider(Qt.Horizontal); self.volSld.setRange(0, 100)
        self.volSld.setValue(self._vol); self.volSld.setFixedWidth(90)
        self.volSld.setToolTip(_("Volumen"))
        self.volSld.valueChanged.connect(self._vol_changed)
        hb2.addWidget(self.volSld); hb2.addSpacing(12)
        self.chap = QComboBox(); self.chap.setMinimumWidth(150)
        self.chap.currentIndexChanged.connect(self.on_chap_change)
        hb2.addWidget(self.chap, 2)
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
        spd = QLabel(_("Velocidad")); spd.setObjectName("time")
        ctr.addWidget(self.prevBtn); ctr.addWidget(self.back10); ctr.addWidget(self.playBtn)
        ctr.addWidget(self.fwd10); ctr.addWidget(self.nextBtn); ctr.addSpacing(10); ctr.addWidget(self.tcur)
        # «● En directo…» / «Esperando a que se narre más…» (solo mientras se narra el libro)
        self.liveLbl = ElidedLabel(""); self.liveLbl.setObjectName("live"); self.liveLbl.hide()
        ctr.addSpacing(8); ctr.addWidget(self.liveLbl, 3)
        ctr.addStretch(1); ctr.addWidget(spd); ctr.addWidget(self.speed)
        bv.addLayout(ctr)

        # --- fila de navegación: páginas de texto, ir a posición e ir a capítulo
        nav = QHBoxLayout(); nav.setSpacing(8)
        self.pageUpBtn = QPushButton(_("▲ Página")); self.pageDnBtn = QPushButton(_("Página ▼"))
        self.pageUpBtn.setToolTip(_("Retroceder una página del texto"))
        self.pageDnBtn.setToolTip(_("Avanzar una página del texto"))
        self.gotoPosBtn = QPushButton(_("⤓ Ir a posición…"))
        self.gotoPosBtn.setToolTip(_("Saltar a un minuto:segundo del capítulo"))
        self.gotoChapBtn = QPushButton(_("≡ Ir a capítulo…"))
        self.gotoChapBtn.setToolTip(_("Elegir un capítulo de la lista"))
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

        # Progreso de la narración en curso, también aquí: se puede escuchar mientras narra.
        self.genWrap = QWidget(); self.genWrap.setObjectName("progwrap")
        gv = QVBoxLayout(self.genWrap); gv.setContentsMargins(16, 8, 16, 8); gv.setSpacing(5)
        self.genLbl = ElidedLabel(""); self.genLbl.setObjectName("proglabel")
        self.genBar = QProgressBar(); self.genBar.setTextVisible(False); self.genBar.setFixedHeight(4)
        gv.addWidget(self.genLbl); gv.addWidget(self.genBar)
        self.genWrap.hide()
        root.addWidget(self.genWrap)
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
        txt, ok = QInputDialog.getText(self, _("Ir a posición"),
                                       _('Minuto:segundo (capítulo de {0}):').format(fmt(dur)),
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
            QMessageBox.warning(self, _("Ir a posición"), _("Escribe el tiempo como «minutos:segundos», p. ej. 3:40."))
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
            QMessageBox.information(self, _("Ir a capítulo"), _("Este título tiene un solo capítulo."))
            return
        items = [chap_label(m) for m in self.manifest]
        cur = self.cap if 0 <= self.cap < len(items) else 0
        item, ok = QInputDialog.getItem(self, _("Ir a capítulo"), _("Capítulo:"), items, cur, False)
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
        fit_popup(self.outSel)
        self.outSel.blockSignals(False)
        self._apply_output()

    def _apply_output(self):
        i = self.outSel.currentIndex()
        if 0 <= i < len(getattr(self, "_devs", [])):
            self.audio.setDevice(self._devs[i])
            # El cuadro cerrado puede cortar el nombre.
            self.outSel.setToolTip(_("Salida de audio") + ": " + self._devs[i].description())

    def _out_changed(self, _arg):
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
        self._liveTimer.stop(); self._waiting = None
        self.player.stop()
        # Soltar el fichero: al regenerar o reanudar, el motor tiene que poder
        # borrarlo y reescribirlo (en Windows no puede si sigue abierto aquí).
        self.player.setSource(QUrl())
        clean_listen_dir()

    def set_gen_progress(self, on, text="", pct=-1):
        """Barra de la narración (o transcripción) en curso, también en el reproductor."""
        self.genWrap.setVisible(on)
        if on:
            self.genLbl.setText(text)
            if pct < 0: self.genBar.setRange(0, 0)
            else: self.genBar.setRange(0, 100); self.genBar.setValue(pct)

    def _do_regen(self):
        src = self.book_dir / "source.epub"
        if not src.exists(): return
        r = QMessageBox.question(self, _("Regenerar"),
                                 _("¿Volver a narrar el libro entero desde cero? Se sobrescribe lo actual."))
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
            QMessageBox.warning(self, _("Exportar"), _("No hay audio generado para exportar."))
            return
        base = (self.booklbl.text().strip() or self.book_dir.name) or "narracion"
        safe = re.sub(r'[\\/:*?"<>|]+', " ", base).strip() or "narracion"
        dst, _filter = QFileDialog.getSaveFileName(self, _("Guardar narración como MP3"),
                                             str(Path.home() / f"{safe}.mp3"), _("Audio MP3 (*.mp3)"))
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
                    raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[-400:] or _("ffmpeg falló"))
                os.replace(tmp, dst)
        except FileNotFoundError:
            QMessageBox.critical(self, _("Exportar"),
                                 _("No se encontró ffmpeg. Hace falta para unir los capítulos."))
            return
        except Exception as e:
            QMessageBox.critical(self, _("Exportar"), _('No se pudo exportar:\n{0}').format(e))
            return
        QMessageBox.information(self, _("Exportar"),
                                _('Guardado:\n{0}\n\n{1} capítulo(s).').format(dst, len(mp3s)))

    def load_book(self, folder):
        self.book_dir = Path(folder)
        self._live = is_generating(self.book_dir)
        self._waiting = None; self._cur_base = None; self.cap = 0
        self.manifest = live_manifest(self.book_dir)    # = manifest.json (+ tramos si se está narrando)
        tf = self.book_dir / "title.txt"
        self.booklbl.setText(tf.read_text("utf-8").strip() if tf.exists()
                             else self.book_dir.name.replace("-", " ").capitalize())
        self._fill_chapters()
        self._update_head()
        pb = self._read_playback()                          # retomar por donde se escuchaba
        start_cap = pb["cap"] if pb else 0
        self._restore_pos = pb["pos"] if pb else None
        self._pb_last_save = time.monotonic()
        self.load_chap(start_cap, False)
        self._liveTimer.start()                             # si se está narrando, lo nuevo entra solo

    def _fill_chapters(self):
        n = len(self.manifest)
        self.chap.blockSignals(True); self.chap.clear()
        for m in self.manifest:
            self.chap.addItem(chap_label(m))
        self.chap.setVisible(n > 1)
        if 0 <= self.cap < n:
            self.chap.setCurrentIndex(self.cap)
        self.chap.blockSignals(False)
        for b in (self.gotoChapBtn, self.prevBtn, self.nextBtn):
            b.setEnabled(n > 1)

    def _update_head(self):
        """Mientras se narra no se regenera, reanuda ni exporta (el motor sigue escribiendo)."""
        live = self._live
        self.regenBtn.setVisible(not live and (self.book_dir / "source.epub").exists())
        self.resumeBtn.setVisible(not live and book_resumable(self.book_dir))
        self.exportBtn.setVisible(not live)
        self._update_live_label()

    def _update_live_label(self):
        if self._live and self._waiting is not None:
            txt = _("Esperando a que se narre más…")
        elif self._live:
            txt = _("● En directo: lo nuevo se va añadiendo solo")
        else:
            txt = ""
        self.liveLbl.setText(txt); self.liveLbl.setVisible(bool(txt))

    def _sync_play_btn(self):
        playing = (self.player.playbackState() == QMediaPlayer.PlayingState
                   or (self._waiting is not None and self._waiting["play"]))
        self.playBtn.setText("⏸" if playing else "▶")

    def _read_playback(self):
        """Lee el punto de escucha guardado (capítulo + ms), validado. El capítulo va
        por su nombre (base): mientras se narra en tramos, su posición en la lista cambia."""
        try:
            pb = json.loads((self.book_dir / "playback.json").read_text("utf-8"))
            pos = int(pb.get("pos", 0))
            if pos < 0:
                return None
            if pb.get("base"):
                for i, m in enumerate(self.manifest):
                    if m.get("base") == pb["base"]:
                        return {"cap": i, "pos": pos}
                return None
            cap = int(pb.get("cap", 0))
            if 0 <= cap < len(self.manifest):
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
            m = self.manifest[self.cap] if 0 <= self.cap < len(self.manifest) else {}
            (self.book_dir / "playback.json").write_text(
                json.dumps({"cap": self.cap, "base": m.get("base"), "pos": int(max(0, p))}), "utf-8")
        except Exception:
            pass

    def load_chap(self, i, autoplay, pos=None):
        """Carga el capítulo i. Con pos (ms) se sigue en ese punto: si es el mismo
        capítulo (que ha crecido mientras se narra) no se rehace el texto."""
        if not self.manifest or i < 0 or i >= len(self.manifest): return
        m = self.manifest[i]
        same = pos is not None and i == self.cap and self._cur_base == m["base"]
        self.cap = i; self._cur_base = m["base"]
        self.chap.blockSignals(True); self.chap.setCurrentIndex(i); self.chap.blockSignals(False)
        if same:
            self._append_new_cues(m)
        else:
            try:
                data = json.loads((self.book_dir / f"{m['base']}.json").read_text("utf-8"))
            except Exception:
                data = {}
            self.cues = data.get("cues", [])
            self.chtitle.setText(data.get("title") or m.get("title", ""))
            while self.flow.count():
                wdg = self.flow.takeAt(0).widget()
                if wdg: wdg.deleteLater()
            self.cue_widgets = []
            for c in self.cues:
                cue = Cue(c["text"], float(c["start"])); cue.clicked.connect(self.seek_to)
                self.flow.addWidget(cue); self.cue_widgets.append((c, cue))
        self._waiting = None; self._play_after_restore = False
        self._last_pos = pos or 0
        # El salto pendiente se aplica DESPUÉS de cambiar de fichero: setSource avisa
        # al momento de un «cargado» falso con la duración del fichero anterior (y en
        # plena carga Qt ignora los saltos), que lo dejaría en un punto equivocado.
        restore = pos if pos is not None else self._restore_pos
        self._restore_pos = None
        src = self._chapter_source(m)
        if src is None:                                   # a medias y aún sin audio
            self.player.setSource(QUrl())
            self._restore_pos = restore
            if self._live:
                self._waiting = {"pos": pos or 0, "at": 0, "play": autoplay, "next": False}
        else:
            self.player.setSource(QUrl.fromLocalFile(str(src)))
            self._restore_pos = restore
            self.on_speed(self.speed.currentIndex())
            if pos:
                self._play_after_restore = autoplay       # arrancar ya en el punto, sin oír el principio
            elif autoplay:
                self.player.play()
        if not same:
            self.scroll.verticalScrollBar().setValue(0)
            self._manual_scroll = False  # capítulo nuevo: vuelve a seguir la narración
        self._sync_play_btn(); self._update_live_label()

    def _chapter_source(self, m):
        """Fichero que suena: el mp3 del capítulo o, si está a medias, una copia temporal."""
        if chapter_is_partial(self.book_dir, m):
            self._partial = True
            snap = snapshot_chapter(self.book_dir, m)
            clean_listen_dir(keep=snap)
            return snap
        self._partial = False
        return self.book_dir / f"{m['base']}.mp3"

    def _append_new_cues(self, m):
        """Añade al texto las frases que el motor ha narrado desde la última vez."""
        try:
            data = json.loads((self.book_dir / f"{m['base']}.json").read_text("utf-8"))
        except Exception:
            return
        cues = data.get("cues", [])
        if len(cues) <= len(self.cues):
            return
        for c in cues[len(self.cues):]:
            cue = Cue(c["text"], float(c["start"])); cue.clicked.connect(self.seek_to)
            self.flow.addWidget(cue); self.cue_widgets.append((c, cue))
        self.cues = cues

    def _next_index(self):
        """Capítulo siguiente: el de la sección siguiente. Mientras se narra no se salta
        a uno posterior (con tramos en paralelo hay capítulos sin empezar en medio)."""
        if not (0 <= self.cap < len(self.manifest)):
            return None
        try:
            want = int(self.manifest[self.cap]["id"]) + 1
        except Exception:
            want = None
        for j, e in enumerate(self.manifest):
            if want is not None and e.get("id") == want:
                return j
        if self._live:
            return None
        return self.cap + 1 if self.cap + 1 < len(self.manifest) else None

    def _refresh_manifest(self):
        """Relee el libro del disco y mete los capítulos y frases nuevos."""
        new = live_manifest(self.book_dir)
        if not new:
            return
        key = lambda man: [(e.get("id"), e.get("done"), e.get("n"), e.get("frames")) for e in man]
        if key(new) == key(self.manifest):
            return
        was_empty = not self.manifest
        labels = [chap_label(e) for e in new] != [chap_label(e) for e in self.manifest]
        self.manifest = new
        idx = next((j for j, e in enumerate(new) if e.get("base") == self._cur_base), None)
        self.cap = idx if idx is not None else min(self.cap, len(new) - 1)
        if labels:
            self._fill_chapters()
        if was_empty:
            self.load_chap(0, False)
        elif idx is not None:
            self._append_new_cues(new[idx])

    def _live_tick(self):
        """Cada 2,5 s mientras se narra el libro abierto: capítulos y frases nuevos, y
        si estaba esperando a que hubiera más audio, seguir."""
        if not self.book_dir:
            return
        live = is_generating(self.book_dir)
        if not live and not self._live:
            return
        ended = self._live and not live                    # la narración acaba de terminar o pararse
        self._live = live
        self._refresh_manifest()
        if ended:
            self._update_head()
        self._serve_waiting()
        self._sync_play_btn(); self._update_live_label()

    def _serve_waiting(self):
        """Si se estaba esperando narración: seguir donde se quedó en cuanto la haya."""
        w = self._waiting
        if w is None or not (0 <= self.cap < len(self.manifest)):
            return
        m = self.manifest[self.cap]
        if w["next"]:                                     # esperando a que empiece el capítulo siguiente
            nxt = self._next_index()
            if nxt is not None:
                self.load_chap(nxt, w["play"])
            elif not self._live:
                self._waiting = None
            return
        avail = float(m.get("duration") or 0) * 1000      # ms narrados (= lo que cubren los subtítulos)
        if m.get("done") and w["pos"] < avail - 400:
            self.load_chap(self.cap, w["play"], pos=w["pos"])   # el mp3 definitivo, en el mismo punto
        elif not m.get("done") and int(m.get("frames") or 0) > 0 and avail > w["pos"] + 300:
            self.load_chap(self.cap, w["play"], pos=w["pos"])   # copia nueva con lo último narrado
        elif m.get("done") or not self._live:             # capítulo acabado o ya no se narra: al siguiente
            nxt = self._next_index()
            if nxt is not None:
                self.load_chap(nxt, w["play"])
            elif self._live:
                self._waiting = {"pos": 0, "at": w["at"], "play": w["play"], "next": True}
            else:
                self._waiting = None

    def seek_to(self, sec):
        self._manual_scroll = False      # saltar a una frase: vuelve a seguir la narración
        ms = int(sec * 1000) + 10
        dur = self.player.duration()
        if self._partial and self._live and (dur <= 0 or ms > dur - 200):
            # Esa frase ya tiene texto pero su audio aún no está en la copia que suena:
            # se sigue ahí en cuanto llegue (unos segundos).
            at = self.player.position()
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            self._waiting = {"pos": ms, "at": at, "play": True, "next": False}
            self._sync_play_btn(); self._update_live_label()
            return
        self._waiting = None; self._update_live_label()
        self.player.setPosition(ms); self.player.play()

    def toggle(self):
        w = self._waiting
        if w is not None:                 # esperando narración: ▶/⏸ decide si sigue sonando al llegar
            w["play"] = not w["play"]; self._sync_play_btn()
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState: self.player.pause()
        else: self.player.play()

    def on_state(self, st):
        self._sync_play_btn()
        if st == QMediaPlayer.PausedState:
            self._save_playback()

    def on_media_status(self, st):
        if st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._apply_restore()
        if st == QMediaPlayer.EndOfMedia:
            self._on_end()

    def _on_end(self):
        if not (0 <= self.cap < len(self.manifest)):
            return
        end = max(self.player.duration(), self._last_pos)
        if self._partial:
            # Se acabó la copia del capítulo a medias: más audio, el mp3 definitivo,
            # el capítulo siguiente o esperar a que se narre más.
            self._waiting = {"pos": end, "at": end, "play": True, "next": False}
            self._refresh_manifest()
            self._serve_waiting()
        else:
            nxt = self._next_index()
            if nxt is not None:
                self.load_chap(nxt, True)
            elif self._live:              # el siguiente capítulo aún no ha empezado a narrarse
                self._waiting = {"pos": 0, "at": end, "play": True, "next": True}
        self._sync_play_btn(); self._update_live_label()

    def _apply_restore(self):
        """Salta al punto de escucha guardado en cuanto se conoce la duración."""
        if self._restore_pos is None:
            return
        # Solo con el fichero ya cargado: la duración llega aún «cargando» y en ese
        # estado Qt ignora el salto (sonaría desde el principio).
        if self.player.mediaStatus() not in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferingMedia,
                                             QMediaPlayer.BufferedMedia):
            return
        d = self.player.duration()
        if d > 0:
            self.player.setPosition(min(self._restore_pos, max(0, d - 500)))
            self._last_pos = self.player.position()
            self._restore_pos = None
            if self._play_after_restore:
                self._play_after_restore = False
                self.player.play()

    def on_dur(self, d):
        self.seek.setRange(0, d); self.tdur.setText(fmt(d))
        self._apply_restore()

    def on_pos(self, p):
        if not self.seek.isSliderDown(): self.seek.setValue(p)
        self.tcur.setText(fmt(p)); self.highlight(p / 1000.0)
        w = self._waiting
        if w is not None and p > 0 and abs(p - w["at"]) > 1500:
            self._waiting = None                    # se movió a otro punto: deja de esperar
            self._sync_play_btn(); self._update_live_label()
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

    def on_user_scroll(self, _arg):
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
        self.translate = TranslatePage(); self.stack.addWidget(self.translate)
        self.library.openTranslate.connect(self.show_translate)
        self.translate.back.connect(self.show_library)
        self.translate.narrate.connect(self._narrate_translated)
        self.library.openEpub.connect(self.open_epub)
        self.library.openBook.connect(self.open_book)
        self.library.openModels.connect(self.show_models)
        self.library.importAudio.connect(self.import_audio)
        self.library.resumeBook.connect(self._resume_gen)
        self.library.listenGen.connect(self._listen_gen)
        # Mientras narra, la biblioteca enseña sola el libro y lo que va creciendo.
        self._libTimer = QTimer(self); self._libTimer.setInterval(4000)
        self._libTimer.timeout.connect(self._lib_tick)
        self.player.back.connect(self.show_library)
        self.player.regen.connect(self._regen)
        self.player.resume.connect(self._resume_gen)
        self.models.back.connect(self.show_library)
        self.models.dlState.connect(self._dl_state)
        self.setStyleSheet(STYLE)
        QTimer.singleShot(0, self._fit_min_width)

    def _fit_min_width(self):
        """Window never narrower than what the pages need, so translated buttons are never cut."""
        need = max(pg.minimumSizeHint().width()
                   for pg in (self.library, self.player, self.models, self.translate))
        self.setMinimumWidth(max(560, need))

    def show_library(self):
        self.player.stop()
        self.library.refresh()
        self.stack.setCurrentWidget(self.library)
        QTimer.singleShot(0, self._fit_min_width)   # su cabecera ha podido crecer mientras estaba oculta

    def _dl_state(self, t):
        """La biblioteca también enseña cómo va la descarga."""
        self.library.voicesBtn.setText(_("Modelos") + (f" · {t}" if t else ""))
        QTimer.singleShot(0, self._fit_min_width)

    def show_models(self):
        self.models.refresh()
        self.stack.setCurrentWidget(self.models)

    def show_translate(self):
        self.translate.refresh()
        self.stack.setCurrentWidget(self.translate)

    def _narrate_translated(self, epub):
        """«Narrar este libro» en la pantalla de traducción: narración normal del EPUB traducido."""
        self.show_library()
        self._generate(epub)

    def open_book(self, folder):
        self.player.load_book(folder)
        self.stack.setCurrentWidget(self.player)
        self._fit_min_width()

    def _lib_tick(self):
        if self.stack.currentWidget() is self.library:
            self.library.refresh_if_changed()

    def _listen_gen(self):
        """«▶ Escuchar» de la barra de narración: abre el libro que se está narrando."""
        w = getattr(self, "worker", None)
        if not isinstance(w, LocalGenWorker):
            return
        d = LOCAL_BOOKS / w.name
        man = live_manifest(d) if d.exists() else []
        if not any(int(e.get("n") or 0) > 0 for e in man):
            QMessageBox.information(self, "Storyteller", _("Todavía no hay nada narrado. Espera unos "
                                    "segundos a que se guarden las primeras frases."))
            return
        self.open_book(str(d))

    def open_epub(self):
        path, _filter = QFileDialog.getOpenFileName(self, _("Elige un EPUB"), str(Path.home()),
                                              _("Libros EPUB (*.epub)"))
        if path: self._generate(path)

    def import_audio(self):
        if self._whisper_missing():
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, _("Elige un audiolibro para importar"), str(Path.home()),
            _("Audio (*.mp3 *.m4a *.aac *.ogg *.opus *.flac *.wav *.wma);;Todos (*.*)"))
        if not path:
            return
        self._start_import(ImportWorker(src=path))

    def _whisper_missing(self):
        """Transcribir en este equipo sin Whisper: dice cómo instalarlo, en vez de fallar luego."""
        if load_config().get("engine", "local") == "remote" or whisper_python():
            return False
        show_how(self, whisper_how_text())
        return True

    def _start_import(self, worker, resume=False):
        self.imp = worker
        self._imp_busy = True
        local = load_config().get("engine", "local") != "remote"
        first = _("Preparando…") if not resume else (
            _("Reanudando transcripción con {0}…").format(whisper_by_id(active_whisper_id())["name"]) if local
            else _("Reanudando transcripción…"))
        self.library.set_generating(True, first, -1)
        self.imp.progress.connect(lambda t, p: self.library.set_generating(True, t, p))
        self.imp.failed.connect(self._import_fail)
        self.imp.done.connect(self._import_done)
        self.imp.canceled.connect(self._import_canceled)
        self.library.stopBtn.clicked.connect(self._imp_stop)
        self.library.cancelBtn.clicked.connect(self._imp_cancel)
        self.imp.start()

    def _imp_stop(self):
        self.library.set_stopping(_("Deteniendo y guardando lo transcrito…"))
        self.imp.stop(True)

    def _imp_cancel(self):
        self.library.set_stopping(_("Cancelando…"))
        self.imp.stop(False)

    def _import_fail(self, msg):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        warn(self, msg, _("Importar audiolibro"))
        self._next_after_switch()

    def _import_canceled(self):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        self._next_after_switch()

    def _import_done(self, folder):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        if not self._next_after_switch():
            self.open_book(folder)

    def _regen(self, epubpath):
        self.show_library()
        self._generate(epubpath, name=Path(epubpath).parent.name)

    def _work_running(self):
        """Narrar y transcribir se reparten la barra de progreso y sus botones: uno cada vez."""
        return getattr(self, "_gen_busy", False) or getattr(self, "_imp_busy", False)

    def _next_after_switch(self):
        """Si se pidió pasar a otro libro, ahora le toca. Devuelve si era el caso."""
        target, self._switch_to = getattr(self, "_switch_to", None), None
        if target:
            QTimer.singleShot(0, lambda: self._resume_gen(target))
        return bool(target)

    def _resume_gen(self, folder):
        if self._work_running():
            try: title = (Path(folder) / "title.txt").read_text("utf-8").strip()
            except OSError: title = ""
            r = QMessageBox.question(self, _("Reanudar"), _(
                "Ya hay un libro en marcha. ¿Lo detengo y paso a «{0}»? Lo hecho queda guardado y se puede reanudar."
            ).format(title or Path(folder).name))
            if r == QMessageBox.StandardButton.Yes:
                self._switch_to = str(folder)
                self.library.stopBtn.click()     # lo mismo que «Detener y guardar»
            return
        st = read_gen_state(folder) or {}
        if st.get("kind") == "import":                       # reanudar una transcripción Whisper
            self.show_library()
            self._start_import(ImportWorker(resume_dir=folder), resume=True)
            return
        src = Path(folder) / "source.epub"
        if not src.exists():
            QMessageBox.warning(self, _("Reanudar"),
                                _("No encuentro el EPUB original de este libro, no puedo reanudar."))
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
        if self._work_running():
            QMessageBox.information(self, "Storyteller",
                _("Ya hay una narración en marcha. Espera a que termine o deténla "
                "antes de empezar otra."))
            return
        cfg = load_config()
        if cfg["engine"] == "local":
            m = model_by_id(cfg["model"], cfg)
            if not voice_engine_ready(m):
                show_how(self, engine_how_text(m))
                self.show_models(); return
            if not model_installed(m):
                QMessageBox.information(self, "Storyteller",
                    _('Antes descarga el modelo «{0}» en Modelos.').format(m['name']))
                self.show_models(); return
            qc_note = qc_args_local(cfg, m)[1]            # se narra igual, pero sin parecer que revisa
            if qc_note:
                warn(self, qc_note)
            self.worker = LocalGenWorker(path, m, active_voice(cfg), name=name, resume=resume)
        elif cfg["engine"] == "service":
            s = service_cfg(cfg); host = service_module().host_of(s["url"]); py = service_python(cfg)
            if not host:
                QMessageBox.information(self, "Storyteller", _("Antes indica la URL del servicio en Modelos."))
                self.show_models(); return
            if not py:
                show_how(self, _("Para narrar con un servicio solo hace falta poder leer el EPUB. Para instalarlo, "
                                 "ejecuta esto en una terminal:\n\n{0}").format(install_text("base")))
                return
            qc_note = service_qc(cfg)[2]                  # se narra igual, pero sin parecer que revisa
            if qc_note:
                warn(self, qc_note)
            m = {"id": "service", "name": host, "backend": "service", "python": py, "url": s["url"],
                 "model": s["model"], "voice": s["voice"], "parallel": s["parallel"], "lang": narration_language(cfg)}
            self.worker = LocalGenWorker(path, m, name=name, resume=resume)
        else:
            self.worker = GenWorker(path, name=name, resume=resume)
        self._gen_busy = True                             # ya comprometidos: bloquea un 2º disparo
        first = _("Preparando…") if not resume else (
            _("Reanudando con {0}…").format(m["name"]) if cfg["engine"] != "remote" else _("Reanudando…"))
        self.library.set_generating(True, first, -1)
        self.player.set_gen_progress(True, first, -1)
        self.worker.progress.connect(self._gen_progress)
        # Escuchar mientras narra: solo en local (el servidor propio se trae el audio al final).
        self.library.listenBtn.setVisible(isinstance(self.worker, LocalGenWorker))
        self._libTimer.start()
        self.worker.failed.connect(self._gen_fail)
        self.worker.done.connect(self._gen_done)
        self.worker.canceled.connect(self._gen_canceled)
        self.library.stopBtn.clicked.connect(self._on_stop)
        self.library.cancelBtn.clicked.connect(self._on_cancel)
        self.worker.start()

    def _on_stop(self):
        self.library.set_stopping(_("Deteniendo y guardando lo generado…"))
        self.worker.stop(True)

    def _on_cancel(self):
        self.library.set_stopping(_("Cancelando…"))
        self.worker.stop(False)

    def _gen_progress(self, text, pct):
        self.library.set_generating(True, text, pct)
        self.player.set_gen_progress(True, text, pct)

    def _cleanup_gen_conns(self):
        self._gen_busy = self._imp_busy = False           # libera el candado
        for btn in (self.library.stopBtn, self.library.cancelBtn):
            try: btn.clicked.disconnect()
            except Exception: pass
        self.library.listenBtn.hide()
        self.player.set_gen_progress(False)
        self._libTimer.stop()

    def _gen_fail(self, msg):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        warn(self, msg)
        self._next_after_switch()

    def _gen_canceled(self):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        self._next_after_switch()

    def _gen_done(self, folder):
        self._cleanup_gen_conns()
        self.library.set_generating(False)
        self.library.refresh()
        missed = getattr(self.worker, "qc_missed", 0)
        if missed:
            QMessageBox.warning(self, "Storyteller",
                _("El QC no pudo revisar {0} frases: se quedan como salieron del modelo. "
                  "El motivo está en el gen.log del libro.").format(missed))
        if self._next_after_switch():
            return
        # Si estaba escuchando (este u otro libro) no se le corta: el reproductor ya
        # recoge solo el final de la narración.
        if self.stack.currentWidget() is not self.player:
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
                                         _("Guardando la narración en curso…"))
        except Exception: pass
        try: self._stop_worker_and_wait(getattr(self, "imp", None),
                                         _("Guardando la transcripción en curso…"))
        except Exception: pass
        try: self._stop_worker_and_wait(self.translate.worker, _("Guardando la traducción en curso…"))
        except Exception: pass
        try: self.models.stop_download()
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
#live { color: #e3a94f; font-size: 12px; }
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


class StandardButtons(QTranslator):
    """Sí, No, Aceptar y Cancelar de los avisos de Qt, en el idioma de la interfaz: Qt no trae tailandés
    ni hindi, y sin traducción salen en inglés."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.texts = {"&Yes": _("Sí"), "&No": _("No"), "OK": _("Aceptar"), "&OK": _("Aceptar"),
                      "Cancel": _("Cancelar"), "&Cancel": _("Cancelar")}

    def isEmpty(self):
        return False

    def translate(self, context, source, disambiguation=None, n=-1):
        return self.texts.get(source, "") if context == "QPlatformTheme" else ""

def install_translations(app):
    """Lo que escribe la propia Qt, como el menú de copiar y pegar, y los botones de sus avisos."""
    qt = QTranslator(app)
    if qt.load("qtbase_" + {"zh": "zh_CN", "pt": "pt_BR"}.get(LANG, LANG), QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt)
    app._translators = [qt, StandardButtons(app)]        # vivos mientras dure la app
    app.installTranslator(app._translators[1])           # el último instalado es el primero que se mira

def main():
    app = QApplication(sys.argv)
    install_translations(app)
    _ico = HOME_CC / "storyteller.ico"
    if _ico.exists(): app.setWindowIcon(QIcon(str(_ico)))
    win = Cuentacuentos(); win.show()
    a = sys.argv[1:]
    if "--lang" in a:                    # handled by st_lang; drop it and its value
        i = a.index("--lang"); del a[i:i + 2]
    if len(a) >= 2 and a[0] == "--book":
        QTimer.singleShot(200, lambda: win.open_book(a[1]))
    elif a and a[0].lower().endswith(".epub"):
        QTimer.singleShot(200, lambda: win._generate(a[0]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
