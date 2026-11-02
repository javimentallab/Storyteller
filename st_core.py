# -*- coding: utf-8 -*-
"""
Storyteller — lógica de la biblioteca compartida por la app de escritorio y el servidor.
Sin Qt ni textos de interfaz: el formato de los libros en disco y si el motor está instalado.
"""
import json, os, re, shutil, time, unicodedata, wave
from pathlib import Path


def slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "libro"


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


# Carpetas con una narración EN CURSO en esta app: la biblioteca no las "repara".
ACTIVE_GEN = set()


def _atomic_write(path, text):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for i in range(60):                                   # Windows: «Acceso denegado» si alguien lo lee justo ahora
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 59:
                raise
            time.sleep(0.05)


def _stream_parts(d):
    """{nº de tramo: ruta} de los manifest.pN.json de la carpeta."""
    out = {}
    for p in Path(d).glob("manifest.p*.json"):
        m = re.fullmatch(r"manifest\.p(\d+)\.json", p.name)
        if m:
            out[int(m.group(1))] = p
    return out


def merge_stream_manifests(dest, streams, n, complete_ok=False):
    """Une los manifest.pN.json de los tramos en manifest.json (ordenado por id) y
    escribe gen_state.json. complete solo si complete_ok y están los n capítulos.
    Devuelve la lista fusionada (vacía = nada que fusionar, no escribe)."""
    dest = Path(dest)
    best = {}
    for idx in range(1, streams + 1):
        try:
            part = json.loads((dest / f"manifest.p{idx}.json").read_text("utf-8"))
        except Exception:
            continue
        for e in part if isinstance(part, list) else []:
            if not (isinstance(e, dict) and "id" in e):
                continue
            old = best.get(e["id"])                       # duplicado: gana el más avanzado
            if old is None or (bool(e.get("done")), int(e.get("n") or 0)) > \
                              (bool(old.get("done")), int(old.get("n") or 0)):
                best[e["id"]] = e
    merged = [best[k] for k in sorted(best)]
    if not merged:
        return merged
    complete = complete_ok and sum(1 for e in merged if e.get("done")) >= n
    _atomic_write(dest / "manifest.json", json.dumps(merged, ensure_ascii=False, indent=2))
    _atomic_write(dest / "gen_state.json",
                  json.dumps({"total": n, "complete": bool(complete), "streams": streams},
                             ensure_ascii=False))
    if complete:                                          # ya no hacen falta los parciales
        for idx in range(1, streams + 1):
            try:
                (dest / f"manifest.p{idx}.json").unlink()
            except Exception:
                pass
    return merged


def recover_interrupted(d):
    """Repara un libro narrado en tramos que se cortó de golpe (cuelgue, apagón,
    proceso matado): los tramos dejaron su manifest.pN.json pero no se llegó a
    fusionar, así que no salía en la biblioteca o salía con menos capítulos.
    Lo deja a medias y reanudable. True si ha reparado algo."""
    d = Path(d)
    if str(d) in ACTIVE_GEN:
        return False
    parts = _stream_parts(d)
    if not parts:
        return False
    st = read_gen_state(d) or {}
    if st.get("complete"):
        return False
    mf = d / "manifest.json"
    try:
        newest = max(p.stat().st_mtime for p in parts.values())
        if mf.exists() and mf.stat().st_mtime >= newest and st:
            return False                                  # ya al día
    except OSError:
        return False
    log = ""
    try:
        log = (d / "gen.log").read_text("utf-8", errors="replace")
    except Exception:
        pass
    # Nº de tramos: el gen_state (se escribe al empezar) o, en libros antiguos, el log.
    streams = int(st.get("streams") or 0)
    if not streams:
        ts = [int(x) for x in re.findall(r"^\[t(\d+)\]", log, re.M)]
        streams = max(ts + list(parts))
    n = int(st.get("total") or 0)
    if not n:
        m = re.search(r"capitulos:\s*(\d+)", log)
        n = int(m.group(1)) if m else 0
    # Sin total conocido da igual: complete_ok=False y al reanudar se recuenta del EPUB.
    return bool(merge_stream_manifests(d, streams, n, complete_ok=False))


def _progress_key(e):
    return (bool(e.get("done")), int(e.get("n") or 0), int(e.get("frames") or 0))


def live_manifest(d):
    """Capítulos del libro tal como están AHORA en disco: manifest.json más los
    manifest.pN.json de los tramos en paralelo. Por capítulo gana el más avanzado."""
    d = Path(d)
    best = {}
    for f in [d / "manifest.json"] + [p for _k, p in sorted(_stream_parts(d).items())]:
        try:
            part = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        for e in part if isinstance(part, list) else []:
            if not (isinstance(e, dict) and "id" in e and "base" in e):
                continue
            old = best.get(e["id"])
            if old is None or _progress_key(e) > _progress_key(old):
                best[e["id"]] = e
    return [best[k] for k in sorted(best)]


def chapter_is_partial(d, e):
    """Capítulo narrado a medias: su audio aún crece (o se reescribirá al reanudar).
    Las transcripciones de audiolibros traen el audio entero desde el principio."""
    if e.get("done", True):
        return False
    return (read_gen_state(d) or {}).get("kind") != "import"


def _wav_layout(path):
    """(inicio de los datos, canales, bytes por muestra, frecuencia) de un WAV PCM.
    No se fía del tamaño que declara la cabecera: mientras se escribe no está al día."""
    with open(path, "rb") as f:
        head = f.read(65536)
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos = 12
    fmt = None
    while pos + 8 <= len(head):
        cid = head[pos:pos + 4]
        size = int.from_bytes(head[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            ch = int.from_bytes(head[pos + 10:pos + 12], "little")
            sr = int.from_bytes(head[pos + 12:pos + 16], "little")
            bits = int.from_bytes(head[pos + 22:pos + 24], "little")
            fmt = (ch, bits // 8, sr)
        elif cid == b"data":
            return ((pos + 8,) + fmt) if fmt else None
        pos += 8 + size + (size & 1)
    return None


def snapshot_chapter(d, e, dest_dir):
    """Copia reproducible, en dest_dir, de lo narrado hasta ahora de un capítulo a medias.
    Sale del WAV del motor (los tiempos cuadran al ms con los subtítulos) cortado
    en las muestras que el manifest da por guardadas; si no hay WAV (motor remoto),
    del mp3 parcial. None si todavía no hay audio."""
    d = Path(d)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    frames = int(e.get("frames") or 0)
    sr = int(e.get("sr") or 0)
    tag = f"{slug(d.name)[:40]}-{e['base']}-{frames}-{int(time.time() * 1000)}"
    src = d / f"{e['base']}.wav"
    if frames > 0 and src.exists():
        try:
            lay = _wav_layout(src)
            if lay and lay[2] in (1, 2, 3, 4) and (not sr or lay[3] == sr):
                off, ch, bps, wsr = lay
                with open(src, "rb") as f:
                    f.seek(off)
                    data = f.read(frames * ch * bps)
                data = data[:len(data) - len(data) % (ch * bps)]
                if data:
                    out = dest_dir / f"{tag}.wav"
                    with wave.open(str(out), "wb") as w:
                        w.setnchannels(ch)
                        w.setsampwidth(bps)
                        w.setframerate(wsr)
                        w.writeframes(data)
                    return out
        except Exception:
            pass
    mp3 = d / f"{e['base']}.mp3"
    try:
        if mp3.exists() and mp3.stat().st_size > 0:
            out = dest_dir / f"{tag}.mp3"
            shutil.copyfile(mp3, out)
            return out
    except Exception:
        pass
    return None


class EtaTracker:
    """Tiempo restante de una narración. Lleva las frases pendientes de CADA capítulo
    (idempotente: lo que diga el motor sobrescribe) y el ritmo reciente de cada tramo
    (1, 2 o 3 procesos; últimas WIN frases, incluye QC y cambios de capítulo). El libro
    acaba cuando acaba el tramo MÁS LENTO."""
    WIN = 40

    def __init__(self, sents, ranges, dest=None):
        self.sents = list(sents)                          # frases por capítulo (1-based → [i-1])
        self.total = sum(self.sents)
        self.left = {i: n for i, n in enumerate(self.sents, 1)}
        self.caps = {k: range(cs, ce + 1) for k, (cs, ce) in ranges.items()}
        self.cur = {}
        self.ts = {k: [] for k in ranges}
        if dest:                                          # reanudar: descontar lo ya narrado
            for f in Path(dest).glob("manifest*.json"):
                try:
                    man = json.loads(f.read_text("utf-8"))
                except Exception:
                    continue
                for e in man if isinstance(man, list) else []:
                    if isinstance(e, dict) and e.get("id") in self.left:
                        done = self.left[e["id"]] if e.get("done") else int(e.get("n") or 0)
                        self.left[e["id"]] = max(0, min(self.left[e["id"]],
                                                         self.sents[e["id"] - 1] - done))

    def line(self, k, line):
        """Actualiza con una línea del motor del tramo k."""
        m = re.search(r"\[cap (\d+)\]", line)
        if m:
            cap = int(m.group(1))
            if "ya hecho" in line:
                self.left[cap] = 0
            elif (q := re.search(r"quedan (\d+)", line)):
                self.left[cap] = int(q.group(1))
            elif (q := re.search(r"->\s*(\d+) frases", line)):
                self.left[cap] = int(q.group(1))
            else:
                return
            self.cur[k] = cap
            return
        m = re.match(r"\s*frase (\d+)/(\d+)", line)
        if m and k in self.cur:
            self.left[self.cur[k]] = int(m.group(2)) - int(m.group(1))
            ts = self.ts[k]
            ts.append(time.monotonic())
            if len(ts) > self.WIN:
                del ts[0]

    def _rem(self, k):
        return sum(self.left.get(c, 0) for c in self.caps[k])

    def pct(self):
        left = sum(self._rem(k) for k in self.caps)
        return min(99, int((self.total - left) / self.total * 100)) if self.total else -1

    def seconds(self):
        """Segundos que faltan (None = aún sin ritmo medido en algún tramo vivo)."""
        worst = 0.0
        now = time.monotonic()
        for k in self.caps:
            r = self._rem(k)
            if r <= 0:
                continue
            ts = self.ts[k]
            if len(ts) < 4 or ts[-1] <= ts[0]:
                return None
            per = (ts[-1] - ts[0]) / (len(ts) - 1)        # s por frase en este tramo
            worst = max(worst, r * per - min(now - ts[-1], per))
        return max(0.0, worst)


def split_ranges(n, streams):
    """Reparte 1..n en `streams` tramos contiguos lo más equilibrados posible."""
    base, extra = divmod(n, streams)
    ranges = []
    start = 1
    for i in range(streams):
        size = base + (1 if i < extra else 0)
        if size <= 0:
            continue
        ranges.append((start, start + size - 1))
        start += size
    return ranges


# Lo que tiene que haber en cada venv, con su nombre en pip. Los comandos, en install_commands() y en el README.
VENV_MAIN, VENV_XTTS = ".venv", ".venv-xtts"
# Leer el EPUB: lo mínimo para narrar por URL.
BASE_PACKAGES = ("num2words", "beautifulsoup4", "lxml")
XTTS_PACKAGES = BASE_PACKAGES + ("torch", "torchaudio", "coqui-tts")
# Opcional, en engine/.venv. Se instala con --no-deps, y así nadie comprueba que esté todo si la instalación se corta:
# por eso aquí va cuanto F5 importa para generar (sale de recorrer su código desde f5_tts.api; lo vigila test_requisitos).
F5_PACKAGES = BASE_PACKAGES + ("torch", "torchaudio", "f5-tts", "accelerate", "cached-path", "datasets", "ema-pytorch",
                               "huggingface-hub", "hydra-core", "librosa", "matplotlib", "numpy", "omegaconf", "pydub",
                               "pypinyin", "rjieba", "safetensors", "soundfile", "torchdiffeq", "tqdm", "transformers",
                               "vocos", "wandb", "x-transformers")
# Opcional, para transcribir audiolibros y para el QC. El QC corre dentro del proceso del motor que narra, así que
# tiene que estar en SU venv; desde ahí sirve también para transcribir.
WHISPER_PACKAGES = ("faster-whisper", "av")
TORCH_INSTALL = "torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128"


def install_commands(what, windows=os.name == "nt"):
    """Los comandos para instalar una parte, tal como están en el README (un test lo comprueba)."""
    def py(venv):
        return rf"engine\{venv}\Scripts\python" if windows else f"engine/{venv}/bin/python"

    def new(venv):
        return rf"python -m venv engine\{venv}" if windows else f"python3 -m venv engine/{venv}"

    def pip_r(venv, name, flags=""):
        path = rf"engine\deps\{name}\requirements.txt" if windows else f"engine/deps/{name}/requirements.txt"
        return f"{py(venv)} -m pip install {flags}-r {path}"

    return {"xtts": [new(VENV_XTTS), f"{py(VENV_XTTS)} -m pip install {TORCH_INSTALL}", pip_r(VENV_XTTS, "xtts")],
            "xtts-languages": [pip_r(VENV_XTTS, "xtts-languages")],
            "f5": [new(VENV_MAIN), f"{py(VENV_MAIN)} -m pip install {TORCH_INSTALL}", pip_r(VENV_MAIN, "f5", "--no-deps ")],
            "base": [new(VENV_MAIN), pip_r(VENV_MAIN, "base")],
            "whisper-xtts": [pip_r(VENV_XTTS, "whisper")],
            "whisper-f5": [pip_r(VENV_MAIN, "whisper")],
            "whisper-alone": [new(VENV_MAIN), pip_r(VENV_MAIN, "whisper")]}[what]


def whisper_venv(engine_dir, windows=os.name == "nt"):
    """El venv que tiene Whisper para transcribir, o None."""
    for venv in (VENV_MAIN, VENV_XTTS):
        if venv_has(Path(engine_dir) / venv, WHISPER_PACKAGES, windows):
            return venv
    return None


def qc_can_run(engine_dir, backend, windows=os.name == "nt"):
    """¿Puede revisar el motor que narra? El QC local corre en su proceso: mira SU venv, no otro."""
    venv = VENV_XTTS if backend == "xtts" else VENV_MAIN
    return venv_has(Path(engine_dir) / venv, WHISPER_PACKAGES, windows)


def whisper_target(engine_dir, backend=None, windows=os.name == "nt"):
    """Dónde instalar Whisper: en el venv del motor que narra, así vale para el QC y para transcribir.
    Sin motor, en engine/.venv. backend es el del modelo activo, si se sabe."""
    engine_dir = Path(engine_dir)
    xtts = venv_has(engine_dir / VENV_XTTS, XTTS_PACKAGES, windows)
    f5 = venv_has(engine_dir / VENV_MAIN, F5_PACKAGES, windows)
    if xtts and (backend == "xtts" or not f5):
        return "whisper-xtts"
    return "whisper-f5" if f5 else "whisper-alone"


def venv_has(venv, packages, windows=os.name == "nt"):
    """True si el venv tiene su Python y esos paquetes instalados del todo. Solo mira qué hay en
    site-packages, sin importar nada: la pantalla de Modelos lo pregunta en cada ficha."""
    venv = Path(venv)
    if not (venv / ("Scripts/python.exe" if windows else "bin/python")).is_file():
        return False

    def name(text):
        return re.sub(r"[-_.]+", "-", text).lower()       # f5-tts se guarda como f5_tts

    missing = {name(p) for p in packages}
    for site in [venv / "Lib" / "site-packages"] if windows else venv.glob("lib/python*/site-packages"):
        try:
            entries = os.listdir(site)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".dist-info"):
                package = name(entry[:-len(".dist-info")].rsplit("-", 1)[0])
                # pip escribe RECORD lo último: sin él, la instalación de ese paquete se cortó.
                if package in missing and (site / entry / "RECORD").is_file():
                    missing.discard(package)
    return not missing
