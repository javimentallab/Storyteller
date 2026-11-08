# -*- coding: utf-8 -*-
"""
Storyteller — servidor de voz: tu XTTS, con tus voces, para quien hable la API estándar.

La app del móvil (o cualquier cliente de /v1/audio/speech) lo usa como un servicio más: se pone su
URL y su clave en «Modelos». No hace falta para nada más: es solo una forma de narrar con tu
propia tarjeta gráfica en vez de con un servicio público.

    python st_server.py              arranca y enseña la dirección y la clave
    python st_server.py --lang en    para libros en otro idioma (la API no lleva idioma)

Carga el modelo con la primera petición y libera la tarjeta tras 10 minutos sin usarlo. Con varias
peticiones a la vez abre más XTTS, hasta 3 (--workers) y solo si caben en la tarjeta: el cliente decide
cuántos con las peticiones que manda. Va sin TLS: en la red de casa viaja en claro; para sacarlo a
internet, ponlo detrás de un proxy con HTTPS.
"""
import argparse, atexit, functools, hmac, importlib.util, io, json, multiprocessing, os, re, secrets, shutil, socket, subprocess, sys, threading, time, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import array, tempfile
from urllib.parse import unquote, urlsplit

__version__ = "1.3.3"
DEFAULT_PORT = 8765
MAX_INPUT = 4096                     # como la API de OpenAI; una frase son unos 200 caracteres
MAX_BODY = 1024 * 1024
MAX_VOICE = 32 * 1024 * 1024         # una voz son unos segundos de audio
VOICE_SECONDS = 30
VOICE_MIN_SECONDS = 3
LANGUAGES = {"en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"}
IDLE_UNLOAD = 600
MAX_WORKERS = 3
# Lo que debe quedar libre para abrir otro XTTS, que ocupa 2,9 GB (medido en una GPU de 12 GB). Si no cabe no
# se abre: desbordado a la RAM va varias veces más lento que con uno menos.
WORKER_MB = 3500
MODEL_ID = "xtts-v2"
HERE = Path(__file__).resolve().parent
RATE = 24000
ENCODERS = {
    "mp3": ("audio/mpeg", ["-f", "mp3", "-b:a", "128k"]),
    "flac": ("audio/flac", ["-f", "flac"]),
    "opus": ("audio/ogg", ["-f", "ogg", "-c:a", "libopus", "-b:a", "64k"]),
    "aac": ("audio/aac", ["-f", "adts", "-b:a", "96k"]),
}
VOICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}")
BOOK_PATH = re.compile(r"/v1/storyteller/books/([^/\\\\]+)/([^/\\\\]+)")
BOOK_FILE = re.compile(r"cap\d{2,3}\.(?:mp3|m4a|json)|source\.epub")
CHAPTER_BASE = re.compile(r"cap\d{2,3}")
CONTENT_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".json": "application/json", ".epub": "application/epub+zip"}


class VoiceProblem(Exception):
    """Esa voz no se puede guardar. status: el código HTTP con el que se contesta."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class EngineBusy(Exception):
    """Sin memoria en la tarjeta: que el cliente vuelva a intentarlo en un rato."""


class XttsEngine:
    """El mismo camino que la narración del escritorio: texto preparado, frase entera, limpieza.
    Vive dentro de un proceso de trabajo y hace un encargo cada vez."""

    def __init__(self, pron=None):
        self.pron = pron
        self.tts = None

    def _load(self):
        import torch
        from TTS.api import TTS
        import cc_xtts as X
        X.use_soundfile()                 # la voz, con soundfile: torchaudio.load necesita torchcodec
        os.environ.setdefault("COQUI_TOS_AGREED", "1")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self.tts = X.new_xtts(TTS).to(device)          # sin rellenar al azar lo que carga el checkpoint
        except Exception as e:
            if "out of memory" in str(e).lower():
                raise EngineBusy(str(e)) from e
            if device != "cuda":
                raise
            print(f"la GPU falló al cargar ({type(e).__name__}): sigo en CPU, irá lento", flush=True)
            self.tts = X.new_xtts(TTS).to("cpu")
            device = "cpu"
        print(f"XTTS cargado en {device}", flush=True)

    def synth(self, text, voice_wav, lang):
        """(muestras PCM de 16 bits, frecuencia)."""
        import numpy as np
        sys.path.insert(0, str(HERE / "engine"))
        import cc_engine as E
        import cc_xtts as X
        trim = getattr(E, "trim_clip", None) or getattr(X, "_trim_clip", None) or (lambda w, sr=RATE, text=None: w)
        cap = getattr(E, "cap_silences", None)
        if self.tts is None:
            if self.pron and Path(self.pron).exists():
                E.load_pron(str(self.pron))
            self._load()
        E.NUM_LANG = lang
        t = E.with_stop(E.normalize_text(text))
        if not t:
            return b"", RATE
        try:
            # el trozo ya es una frase: sin split_sentences=False coqui la parte y mete silencios
            w = self.tts.tts(text=t, speaker_wav=str(voice_wav), language=lang, split_sentences=False)
        except RuntimeError as e:
            if "out of memory" in str(e).lower():
                raise EngineBusy(str(e)) from e
            raise
        w = trim(X.finish_voice(w), RATE, text=t)
        if cap:
            w = cap(w, RATE)
        return (np.clip(w, -1, 1) * 32767).astype("<i2").tobytes(), RATE


def worker_main(conn, make_engine):
    """Un proceso, un motor. Si el servidor desaparece, el canal se cierra y este proceso también."""
    engine = make_engine()
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        try:
            conn.send(("ok", engine.synth(*job)))
        except EngineBusy as e:
            conn.send(("busy", str(e)))
            return                               # sin memoria: que este proceso la devuelva entera
        except Exception as e:
            conn.send(("error", f"{type(e).__name__}: {e}"))


class Worker:
    """Sin daemon=True: coqui abre un proceso ayudante en Windows, y un daemon no puede tener hijos."""

    def __init__(self, make_engine):
        ctx = multiprocessing.get_context("spawn")
        self.conn, there = ctx.Pipe()
        self.proc = ctx.Process(target=worker_main, args=(there, make_engine))
        self.proc.start()
        there.close()
        self.loading = True                      # hasta su primera respuesta no se sabe lo que ocupa
        self.last_use = time.monotonic()

    def ask(self, job):
        self.conn.send(job)
        return self.conn.recv()

    def close(self):
        try:
            self.conn.send(None)
        except OSError:
            pass
        self.proc.join(15)
        if self.proc.is_alive():
            self.proc.terminate()
        self.conn.close()


def free_gpu_mb():
    """Memoria libre de la tarjeta, o None si no se puede saber."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class WorkerPool:
    """Hasta `size` XTTS a la vez, cada uno en su proceso: en hilos se estorbarían, porque el bucle
    del modelo es Python. Se abre otro solo si hay encargos esperando y cabe en la tarjeta. Se reutiliza
    primero el último usado: así, si el cliente baja el ritmo, los demás quedan sin uso y se cierran."""

    def __init__(self, make_engine, size=MAX_WORKERS, idle=IDLE_UNLOAD, start=Worker, free_mb=free_gpu_mb, reaper=True):
        self.make_engine, self.size, self.idle, self.start, self.free_mb = make_engine, size, idle, start, free_mb
        self.ceiling = size                      # baja si la tarjeta se queda sin memoria
        self.cond = threading.Condition()
        self.workers = []
        self.waiting = []                        # los libres, el último usado al final
        self.checked = (0.0, None)
        if reaper:
            threading.Thread(target=self._reaper, daemon=True).start()

    def _free_now(self):
        at, value = self.checked
        if not at or time.monotonic() - at > 15:  # mirarlo cuesta, y se mira con el cerrojo puesto
            value = self.free_mb()
            self.checked = (time.monotonic(), value)
        return value

    def _can_open(self):
        n = len(self.workers)
        if n == 0:
            return True
        if n >= self.ceiling or any(w.loading for w in self.workers):
            return False
        free = self._free_now()
        return free is not None and free >= WORKER_MB

    def _take(self):
        with self.cond:
            while True:
                if self.waiting:
                    return self.waiting.pop()
                if self._can_open():
                    w = self.start(self.make_engine)
                    self.workers.append(w)
                    self.checked = (0.0, None)   # lo que ocupe este aún no se ve: mirar de nuevo después
                    print(f"abro un XTTS: {len(self.workers)} a la vez", flush=True)
                    return w
                self.cond.wait(5)                # al rato puede haber memoria: se vuelve a mirar

    def _give_back(self, w):
        with self.cond:
            if w.loading:
                self.checked = (0.0, None)       # ya cargado: ahora sí se ve lo que ocupa
            w.loading = False
            w.last_use = time.monotonic()
            self.waiting.append(w)
            self.cond.notify()

    def _forget(self, w, no_memory=False):
        """Lo saca de la lista y dice si queda alguno."""
        with self.cond:
            self.workers.remove(w)
            if no_memory:
                self.ceiling = max(1, len(self.workers))
            if not self.workers:
                self.ceiling = self.size
            self.cond.notify_all()
            return bool(self.workers)

    def synth(self, text, voice_wav, lang):
        job = (text, str(voice_wav), lang)
        while True:
            w = self._take()
            try:
                kind, answer = w.ask(job)
            except (EOFError, OSError) as e:     # el cliente reintentará y le tocará otro proceso
                self._forget(w)
                raise RuntimeError("se ha caído el proceso del motor") from e
            if kind == "busy":
                others = self._forget(w, no_memory=True)
                w.close()
                print(f"sin memoria en la tarjeta: me quedo con {self.ceiling} XTTS", flush=True)
                if not others:
                    raise EngineBusy(answer)
                continue                         # el encargo pasa a uno de los que caben
            self._give_back(w)
            if kind == "error":
                raise RuntimeError(answer)
            return answer

    def release_idle(self):
        """Que no ocupe la tarjeta si nadie narra: se puede estar jugando."""
        now = time.monotonic()
        with self.cond:
            old = [w for w in self.waiting if now - w.last_use > self.idle]
            for w in old:
                self.waiting.remove(w)
                self.workers.remove(w)
            if not self.workers:
                self.ceiling = self.size
            left = len(self.workers)
        for w in old:
            w.close()
        if old:
            print(f"cierro {len(old)} XTTS sin uso: quedan {left}", flush=True)

    def _reaper(self):
        while True:
            time.sleep(30)
            self.release_idle()

    def close(self):
        with self.cond:
            all_of_them, self.workers, self.waiting = self.workers, [], []
        for w in all_of_them:
            w.close()


class Service:
    def __init__(self, home, engine, lang="es"):
        self.home, self.engine, self.lang = Path(home), engine, lang
        self.settings = self.home / "server.json"
        self.key = self._load_key()

    def _load_key(self):
        try:
            k = json.loads(self.settings.read_text("utf-8")).get("token")
            if k:
                return k
        except Exception:
            pass
        k = secrets.token_urlsafe(32)
        self.home.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({"token": k}), "utf-8")
        if os.name != "nt":
            os.chmod(self.settings, 0o600)
        return k

    def authorized(self, header):
        m = re.fullmatch(r"Bearer\s+(\S+)", header or "")
        return bool(m) and hmac.compare_digest(m.group(1).encode(), self.key.encode())

    def voices(self):
        d = self.home / "engine" / "voices"
        return {w.stem: w for w in sorted(d.glob("*.wav"))} if d.is_dir() else {}

    def default_voice(self):
        try:
            return json.loads((self.home / "config.json").read_text("utf-8")).get("voice")
        except Exception:
            return None

    def add_voice(self, name, audio):
        """Guarda una voz como las demás: 24 kHz, mono, pico a −1 dBFS y nada más. Sin limpiarla:
        limpiar la referencia se come la z/c y XTTS cae en otro acento."""
        d = self.home / "engine" / "voices"
        if (d / f"{name}.wav").exists():
            raise VoiceProblem(409, "Ya hay una voz con ese nombre.")
        if not shutil.which("ffmpeg"):
            raise VoiceProblem(500, "Este servidor no tiene ffmpeg.")
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-vn", "-ac", "1", "-ar", str(RATE),
                            "-t", str(VOICE_SECONDS), "-f", "s16le", "-"], capture_output=True, timeout=120)
        pcm = r.stdout[:len(r.stdout) // 2 * 2]
        if r.returncode or not pcm:
            raise VoiceProblem(400, "Ese fichero no es un audio que se pueda leer.")
        samples = array.array("h", pcm)
        if len(samples) < VOICE_MIN_SECONDS * RATE:
            raise VoiceProblem(400, f"La voz necesita al menos {VOICE_MIN_SECONDS} segundos de audio.")
        peak = max(max(samples), -min(samples))
        if peak < 300:
            raise VoiceProblem(400, "Ese audio está en silencio.")
        gain = 29204 / peak                      # 32767 a −1 dBFS
        samples = array.array("h", (max(-32768, min(32767, int(s * gain))) for s in samples))
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{name}.wav.tmp"
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(samples.tobytes())
        os.replace(tmp, d / f"{name}.wav")
        txt = d / f"{name}.txt"                  # la app de escritorio lista las voces que lo tienen
        if not txt.exists():
            txt.write_text("", "utf-8")

    def book(self, folder):
        """Lo que se puede llevar de un libro: solo los capítulos terminados."""
        try:
            manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        done = [e for e in manifest if isinstance(e, dict) and e.get("done") and CHAPTER_BASE.fullmatch(str(e.get("base", "")))]
        if not done:
            return None
        try:
            state = json.loads((folder / "gen_state.json").read_text("utf-8"))
        except (OSError, ValueError):
            state = {}
        try:
            title = (folder / "title.txt").read_text("utf-8").strip()
        except OSError:
            title = ""
        names = [f"{e['base']}.{x}" for e in done for x in ("mp3", "m4a", "json")] + ["source.epub"]
        files = [{"name": n, "size": (folder / n).stat().st_size} for n in names if (folder / n).is_file()]
        return {"id": folder.name, "title": title or folder.name, "manifest": done, "state": state, "files": files}

    def books(self):
        d = self.home / "books"
        found = [self.book(f) for f in sorted(d.iterdir()) if f.is_dir()] if d.is_dir() else []
        return [b for b in found if b]

    def book_file(self, book, name):
        """El fichero de un libro, o None: el nombre viene de fuera y no sale de la carpeta del libro."""
        root = (self.home / "books").resolve()
        folder = (root / book).resolve()
        if folder.parent != root or not BOOK_FILE.fullmatch(name) or not (folder / name).is_file():
            return None
        return folder / name


def encode(pcm, rate, fmt):
    """(bytes, tipo). Sin ffmpeg sale WAV, que todo cliente sabe leer por su cabecera."""
    if fmt == "pcm":
        return pcm, "audio/pcm"
    if fmt in ENCODERS and shutil.which("ffmpeg"):
        ctype, args = ENCODERS[fmt]
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-"] + args + ["-"],
                           input=pcm, capture_output=True, timeout=120)
        if r.returncode == 0 and r.stdout:
            return r.stdout, ctype
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return b.getvalue(), "audio/wav"


class Api(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Storyteller/" + __version__
    sys_version = ""
    timeout = 120

    @property
    def svc(self):
        return self.server.service

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send_bytes(self, status, data, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200, extra=None):
        self.send_bytes(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def send_file(self, path):
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
        except ConnectionError:                  # el cliente se ha ido: otra vez será
            self.close_connection = True

    def error(self, status, message, extra=None):
        self.send_json({"error": {"message": message}}, status, extra)

    def guarded(self, discard=0):
        """¿Trae la clave? Si no, contesta 401. Con discard, antes lee y tira hasta ese tope del cuerpo."""
        if self.svc.authorized(self.headers.get("Authorization")):
            return True
        if discard:
            self.discard_body(discard)
        self.error(401, "Falta la clave de acceso, o no es esa.")
        return False

    def do_GET(self):
        self.close_connection = "Content-Length" in self.headers
        if not self.guarded():
            return
        path = unquote(urlsplit(self.path).path)
        book_file = BOOK_PATH.fullmatch(path)
        if path == "/v1/models":
            self.send_json({"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "storyteller"}]})
        elif path == "/v1/audio/voices":
            self.send_json({"voices": list(self.svc.voices())})
        elif path == "/v1/storyteller/books":
            self.send_json({"books": self.svc.books()})
        elif book_file:
            found = self.svc.book_file(*book_file.groups())
            if found:
                self.send_file(found)
            else:
                self.error(404, "No tengo ese fichero.")
        else:
            self.error(404, "No existe esa ruta.")

    def body_length(self, limit):
        """Lo que dice que va a mandar, o None si ya se ha contestado que es demasiado."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if 0 <= length <= limit:
            return length
        self.close_connection = True
        self.drain(max(length, 0), 16 * limit)
        self.error(413, "Petición demasiado grande.")
        return None

    def discard_body(self, limit):
        """Tira el cuerpo sin guardarlo. Si no cabe en el tope, esta conexión ya no sirve para otra petición."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= limit:
            self.close_connection = True
        self.drain(max(length, 0), limit)

    def drain(self, length, limit):
        """Lo leído se tira, hasta un tope: si se cierra sin leer, el cliente no ve la respuesta."""
        left = min(length, limit)
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                break
            left -= len(chunk)

    def do_PUT(self):
        if not self.guarded(discard=MAX_VOICE):         # sin clave, ni un byte a disco
            return
        length = self.body_length(MAX_VOICE)
        if length is None:
            return
        m = re.fullmatch(r"/v1/audio/voices/([^/]+)", unquote(urlsplit(self.path).path))
        fd, tmp = tempfile.mkstemp(suffix=".voice")
        try:
            with os.fdopen(fd, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if not m:
                return self.error(404, "No existe esa ruta.")
            name = m.group(1)
            if not VOICE_ID.fullmatch(name):
                return self.error(400, "Ese nombre no vale: solo letras, números, punto, guion y guion bajo.")
            if not length:
                return self.error(400, "Falta el audio.")
            try:
                self.svc.add_voice(name, tmp)
            except VoiceProblem as e:
                return self.error(e.status, str(e))
            self.send_json({"id": name}, 201)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def do_POST(self):
        length = self.body_length(MAX_BODY)
        if length is None:
            return
        body = self.rfile.read(length)
        if not self.guarded():
            return
        if self.path != "/v1/audio/speech":
            return self.error(404, "No existe esa ruta.")
        try:
            q = json.loads(body.decode("utf-8"))
            if not isinstance(q, dict):
                raise ValueError
        except ValueError:
            return self.error(400, "El cuerpo no es JSON.")
        self.speech(q)

    def speech(self, q):
        text = q.get("input")
        if not isinstance(text, str) or not text.strip():
            return self.error(400, "Falta «input».")
        if len(text) > MAX_INPUT:
            return self.error(400, f"«input» pasa de {MAX_INPUT} caracteres.")
        model = q.get("model") or MODEL_ID
        if model != MODEL_ID:
            return self.error(404, f"Este servidor solo tiene el modelo {MODEL_ID}.")
        fmt = q.get("response_format") or "mp3"
        if fmt != "wav" and fmt != "pcm" and fmt not in ENCODERS:
            return self.error(400, f"Formato desconocido: {fmt}.")
        voices = self.svc.voices()
        if not voices:
            return self.error(400, "Este servidor no tiene ninguna voz.")
        name = q.get("voice") or self.svc.default_voice() or next(iter(voices))
        if not isinstance(name, str) or not VOICE_ID.fullmatch(name) or name not in voices:
            return self.error(400, "No tengo esa voz. Tengo: " + ", ".join(voices))
        lang = q.get("language") or self.svc.lang
        if q.get("language") and lang not in LANGUAGES:
            return self.error(400, f"No conozco ese idioma. Conozco: {', '.join(sorted(LANGUAGES))}.")
        try:
            pcm, rate = self.server.service.engine.synth(text, voices[name], lang)
        except EngineBusy:
            return self.error(503, "La tarjeta gráfica no tiene memoria libre ahora mismo.", {"Retry-After": "30"})
        except Exception as e:
            print(f"fallo al narrar: {type(e).__name__}: {e}", flush=True)
            return self.error(500, f"El motor ha fallado: {type(e).__name__}.")
        if not pcm:
            return self.error(400, "Ese texto no tiene nada que leer.")
        data, ctype = encode(pcm, rate, fmt)
        self.send_bytes(200, data, ctype)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # En Windows SO_REUSEADDR deja a otro proceso quedarse con un puerto en uso.
    allow_reuse_address = os.name != "nt"


def make_server(service, host="0.0.0.0", port=DEFAULT_PORT, verbose=False):
    srv = Server((host, port), Api)
    srv.service = service
    srv.verbose = verbose
    return srv


def lan_addresses():
    out = set()
    try:
        for _f, _t, _p, _c, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if sa[0].startswith(("192.168.", "10.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", sa[0]):
                out.add(sa[0])
    except OSError:
        pass
    return sorted(out) or ["127.0.0.1"]


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Storyteller voice server (OpenAI-style /v1/audio/speech with XTTS)")
    ap.add_argument("--home", default=str(Path.home() / "cuentacuentos"), help="data folder, with engine/voices")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--lang", default="es", help="language of the text when the request doesn't say")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS,
                    help="most XTTS at once; each takes about 3 GB and only opens if it fits (default: %(default)s)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    home = Path(a.home)
    if importlib.util.find_spec("TTS") is None:          # XTTS vive en su propio entorno, aislado
        py = home / "engine" / ".venv-xtts" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not py.exists():
            sys.exit(f"Falta el entorno de XTTS: {py}")
        sys.exit(subprocess.call([str(py), "-I", str(Path(__file__).resolve())] + sys.argv[1:]))
    pool = WorkerPool(functools.partial(XttsEngine, pron=home / "pron.json"), max(1, a.workers))
    atexit.register(pool.close)
    service = Service(home, pool, a.lang)
    srv = make_server(service, a.host, a.port, a.verbose)
    print(f"Storyteller, servidor de voz {__version__}  ·  voces: {', '.join(service.voices()) or 'ninguna'}")
    for ip in lan_addresses():
        print(f"  URL:   http://{ip}:{a.port}")
    print(f"  Clave: {service.key}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
