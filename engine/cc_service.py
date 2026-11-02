# -*- coding: utf-8 -*-
"""
Narra un EPUB pidiendo cada frase a un servicio de voz por URL, con la API estándar
(POST /v1/audio/speech): el servidor propio (st_server.py) o uno público. No hace falta GPU
ni modelo en este equipo. Escribe lo mismo que los motores locales, así que la biblioteca,
el reproductor, «escuchar mientras narra» y reanudar funcionan igual.

  python -I cc_service.py generate --epub E --out O --url U [--model M] [--voice V]
                                   [--lang es] [--pron P] [--resume] [--parallel N]
                                   [--qc-url U [--qc-model M] | --qc-whisper-dir D]
                                   [--qc-threshold 0.85] [--qc-retries 2]
  python -I cc_service.py count --epub E

La clave va en la variable de entorno ST_SERVICE_KEY: en la línea de órdenes se vería en la
lista de procesos. La del Whisper de --qc-url, si la pide, en ST_QC_KEY: esa dirección puede ser
de otro, y no se le manda la del servicio. Este módulo solo usa la biblioteca estándar hasta que
hay que leer el EPUB (cc_engine), para que la pantalla de Modelos pueda importarlo y probar un
servicio. El QC, sin --qc-url ni --qc-whisper-dir, no se activa. Con Whisper por URL basta la
biblioteca estándar, pero sin numpy no mide la voz de más al final de la frase; con Whisper
local hacen falta numpy, soundfile y faster-whisper en este Python.
"""
import argparse, collections, concurrent.futures, http.client, io, json, math, os, re, secrets, subprocess, sys
import tempfile, threading, time, urllib.error, urllib.request, wave
from itertools import islice
from urllib.parse import urlsplit

for _s in (sys.stdout, sys.stderr):              # títulos fuera de cp1252 en la consola de Windows
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

KEY_ENV = "ST_SERVICE_KEY"
SR = 24000                                       # todo se guarda así, venga como venga del servicio
FLUSH_EVERY = 6                                  # frases entre guardados, como los motores locales
TIMEOUT = 300                                    # el primer encargo a un servidor propio carga el modelo
TRIES = 8                                        # con esperas de 2 a 60 s: unos 3 min sin respuesta
MAX_WAIT = 60
MAX_RETRY_AFTER = 120
MAX_PARALLEL = 4                                 # como «Peticiones a la vez» en la app del móvil
AHEAD = 3                                        # frases por petición a la vez que pueden esperar su turno
ENDPOINTS = ("/audio/speech", "/audio/voices", "/audio/transcriptions", "/chat/completions", "/models")
QC_KEY_ENV = "ST_QC_KEY"
QC_TIMEOUT = 60                                  # como cc_engine.qc_transcribe
# Lo que se pide al transcribir, de más a menos: un modelo que no da tiempos por palabra lo rechaza.
QC_ASKS = (("verbose_json", ("word", "segment")), ("verbose_json", ()), ("json", ()))


class ServiceError(Exception):
    """kind: url, auth, notfound, refused, busy, down, noaudio o notext. busy y down se reintentan."""

    def __init__(self, kind, detail="", retry_after=None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail, self.retry_after = kind, detail, retry_after


# ---- direcciones (lo mismo que Format.kt en la app del móvil) ---------------------------
def api_base(text):
    """La raíz de la API a partir de lo que escriba el usuario, o None si no es una dirección:
    «192.168.1.5:8765» → «http://192.168.1.5:8765/v1»; admite que peguen la ruta entera."""
    t = (text or "").strip()
    if not t or re.search(r"\s", t):
        return None
    had_scheme = t.lower().startswith(("http://", "https://"))
    if "://" in t and not had_scheme:
        return None
    u = urlsplit(t if had_scheme else "http://" + t)
    try:
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if not host or u.username or u.password or u.query or u.fragment:
        return None
    path = u.path.rstrip("/")
    for tail in ENDPOINTS:
        if path.endswith(tail):
            path = path[: -len(tail)]
    path = path.rstrip("/") or "/v1"
    scheme = "https" if u.scheme.lower() == "https" else "http"
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}" + (f":{port}" if port else "") + path


def host_of(url):
    base = api_base(url)
    return urlsplit(base).hostname if base else ""


def is_unsafe(url):
    """http más allá de la red de casa: la clave y el libro viajarían a la vista."""
    base = api_base(url)
    if not base:
        return False
    u = urlsplit(base)
    return u.scheme != "https" and not _is_private(u.hostname or "")


def _is_private(host):
    h = host.lower()
    if h == "localhost" or h.endswith((".local", ".lan", ".home.arpa")):
        return True
    p = h.split(".")
    if len(p) != 4 or not all(x.isdigit() and int(x) <= 255 for x in p):
        return False
    a, b = int(p[0]), int(p[1])
    return a in (10, 127) or (a, b) == (192, 168) or (a == 172 and 16 <= b <= 31) or \
        (a, b) == (169, 254) or (a == 100 and 64 <= b <= 127)     # lo último: Tailscale y similares


# ---- el servicio ------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Sin seguir redirecciones: la clave no debe acabar en otra dirección."""

    def redirect_request(self, *_a, **_k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _message(raw):
    """El texto de un error del servicio, en una línea. Sin él, nada: no se enseña una página HTML."""
    try:
        o = json.loads(raw)
    except ValueError:
        return ""
    e = o.get("error", o) if isinstance(o, dict) else o
    if isinstance(e, dict):
        e = e.get("message") or e.get("detail") or ""
    return re.sub(r"\s+", " ", e).strip()[:300] if isinstance(e, str) else ""


def _kind_of(status):
    if status in (401, 403):
        return "auth"
    if status == 404 or 300 <= status < 400:
        return "notfound"
    if status in (408, 425, 429) or status >= 500:
        return "busy"
    return "refused"


class Client:
    def __init__(self, url, key="", model="", voice="", tries=TRIES, sleep=time.sleep, on_wait=None, language=""):
        self.base = api_base(url)
        if not self.base:
            raise ServiceError("url")
        self.key, self.model, self.voice = key or "", model or "", voice or ""
        self.tries, self.sleep, self.on_wait = tries, sleep, on_wait
        self.format = "wav"                      # sin pérdidas; si no lo da, mp3 (ver _audio)
        self.language = language or ""           # la API estándar no lo lleva; si lo rechaza, se deja de mandar
        self.ask = 0                             # cuál de QC_ASKS contesta este servicio

    def _call(self, path, body=None, timeout=TIMEOUT, content_type=""):
        head = {"User-Agent": "Storyteller/1.1"}  # Cloudflare corta el de Python
        if self.key:
            head["Authorization"] = "Bearer " + self.key
        if content_type:                         # ya viene empaquetado
            head["Content-Type"] = content_type
        elif body is not None:
            head["Content-Type"] = "application/json"
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, headers=head)
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except OSError:
                raw = b""
            detail = _message(raw).replace(self.key, "***") if self.key else _message(raw)
            try:
                wait = float(e.headers.get("Retry-After") or 0) or None
            except ValueError:
                wait = None
            raise ServiceError(_kind_of(e.code), detail, wait) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:     # sin red, TLS, corte…
            raise ServiceError("down", type(e).__name__) from None

    def _retrying(self, work):
        for attempt in range(self.tries):
            try:
                return work()
            except ServiceError as e:
                if e.kind not in ("busy", "down") or attempt == self.tries - 1:
                    raise
                wait = int(min(e.retry_after, MAX_RETRY_AFTER) if e.retry_after else min(2 ** (attempt + 1), MAX_WAIT))
                if self.on_wait:
                    self.on_wait(wait)
                self.sleep(wait)

    def _audio(self, text):
        """Los bytes de la voz. Pide WAV y, si lo rechaza, mp3: no todos los servicios dan WAV."""
        body = {"input": text}
        if self.model:
            body["model"] = self.model
        if self.voice:
            body["voice"] = self.voice
        if self.language:
            try:
                return self._in_some_format({**body, "language": self.language})
            except ServiceError as e:
                if e.kind != "refused":
                    raise
            self.language = ""                   # el servicio saca el idioma del propio texto
        return self._in_some_format(body)

    def _in_some_format(self, body):
        try:
            return self._call("/audio/speech", {**body, "response_format": self.format})
        except ServiceError as e:
            if e.kind != "refused" or self.format == "mp3":
                raise
        audio = self._call("/audio/speech", {**body, "response_format": "mp3"})
        self.format = "mp3"
        return audio

    def say(self, text):
        """PCM de 16 bits, mono, a SR."""
        return decode(self._retrying(lambda: self._audio(text)))

    def transcribe(self, pcm, language="", prompt=""):
        """(texto, segundo en que acaba la última palabra o None) de un PCM como el de say(). Sin
        reintentos: si no contesta, no es que suene mal, y la narración no debe esperarle. Solo una
        respuesta cambia lo que se pide desde entonces: un fallo no dice nada del servicio."""
        for i in range(self.ask, len(QC_ASKS)):
            form, content_type = _form(self.model, language, prompt, QC_ASKS[i], pcm)
            try:
                raw = self._call("/audio/transcriptions", form, QC_TIMEOUT, content_type)
            except ServiceError as e:
                if e.kind != "refused" or i == len(QC_ASKS) - 1:
                    raise
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                raise ServiceError("notext") from None
            if not isinstance(o, dict) or not isinstance(o.get("text"), str):
                raise ServiceError("notext")
            self.ask = i
            return o["text"], _last_end(o)

    def listing(self, path, field):
        """Nombres de un listado. Vacío si el servicio no lo tiene: no todos lo dan."""
        try:
            o = json.loads(self._call(path, timeout=20))
        except ServiceError as e:
            if e.kind in ("auth", "down"):
                raise
            return []
        except ValueError:
            return []
        items = o.get(field, []) if isinstance(o, dict) else o
        out = []
        for it in items if isinstance(items, list) else []:
            name = it.get("id") or it.get("voice_id") or it.get("name") if isinstance(it, dict) else it
            if isinstance(name, str) and name:
                out.append(name)
        return out


def decode(audio):
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(SR), "pipe:1"],
                           input=audio, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise ServiceError("noaudio", "ffmpeg") from None
    if p.returncode or not p.stdout:
        raise ServiceError("noaudio")
    return p.stdout


def _form(model, language, prompt, ask, pcm):
    """(cuerpo, Content-Type) para /audio/transcriptions: el PCM de say() en un WAV y lo que se pide."""
    fmt, times = ask
    fields = [("model", model), ("response_format", fmt), *(("timestamp_granularities[]", t) for t in times),
              ("language", language), ("prompt", prompt)]
    clip = io.BytesIO()
    with wave.open(clip, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)
    mark = "----storyteller" + secrets.token_hex(16)
    out = bytearray()
    for name, value in fields:
        if value:
            out += f'--{mark}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
    out += (f'--{mark}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n").encode("utf-8")
    out += clip.getvalue() + f"\r\n--{mark}--\r\n".encode("utf-8")
    return bytes(out), "multipart/form-data; boundary=" + mark


def _last_end(o):
    """Cuándo acaba la última palabra, si lo dice: unos las dan sueltas y otros dentro de cada segmento."""
    try:
        words = o.get("words") or [w for s in o.get("segments") or [] for w in s.get("words") or []]
        end = words[-1].get("end") if words else None
    except (AttributeError, TypeError, LookupError):
        return None
    if isinstance(end, bool) or not isinstance(end, (int, float)) or not math.isfinite(end) or end < 0:
        return None
    return end


def probe(url, key="", model="", voice=""):
    """Prueba de verdad, con una frase: {"models", "voices", "seconds"} o ServiceError."""
    c = Client(url, key, model, voice, tries=1)
    models = c.listing("/models", "data")
    voices = c.listing("/audio/voices", "voices")
    c.model = c.model or (models[0] if len(models) == 1 else "")
    c.voice = c.voice or (voices[0] if voices else "")
    return {"models": models, "voices": voices, "seconds": len(c.say("Hola.")) / 2 / SR}


# ---- narrar -----------------------------------------------------------------------------
_print = threading.Lock()


def say(line):
    """Una línea entera: un reintento desde otra petición no debe colarse en medio de otra."""
    with _print:
        print(line, flush=True)


def in_order(work, items, parallel=1):
    """Los resultados de work(item), en el orden de items, con hasta `parallel` peticiones a la vez.
    Ventana deslizante: al acabar una se lanza la siguiente, sin esperar a la frase más lenta, y lo que
    se adelanta aguarda su turno hasta un tope. Un error salta al llegarle el turno, así que todo lo
    anterior se entrega antes."""
    items = iter(items)
    if parallel <= 1:
        for it in items:
            yield work(it)
        return
    pool = concurrent.futures.ThreadPoolExecutor(parallel)
    window = collections.deque()
    try:
        while True:
            for it in islice(items, parallel * AHEAD - len(window)):
                window.append(pool.submit(work, it))
            if not window:
                return
            yield window.popleft().result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _engine():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))    # -I no añade la carpeta del script
    import cc_engine
    return cc_engine


class Reviewer:
    """El QC de los motores locales, con lo que llega del servicio: el mismo juicio y los mismos umbrales
    (cc_engine._qc_eval y qc_audio_fault) y hasta qc_retries reintentos, quedándose con el mejor. Oye con
    un Whisper local (--qc-whisper-dir, que manda, como en cc_engine.qc_hyp) o por URL (--qc-url). Sin
    respuesta del Whisper la frase se queda como llegó y se dice, con las palabras que cuenta la app."""

    def __init__(self, E, a, key=""):
        self.E = E
        self.threshold, self.attempts = a.qc_threshold, max(0, a.qc_retries) + 1
        self.lang = "" if a.lang == "auto" else (a.lang or "")
        self.whisper_dir = a.qc_whisper_dir
        self.ear = None
        if not self.whisper_dir:
            try:
                self.ear = Client(a.qc_url, key, a.qc_model, tries=1)
            except ServiceError:
                pass                             # dirección mala: nadie que oiga, y cada frase lo dirá
        self.pace = threading.Lock()             # cc_engine apunta en una lista la velocidad de los clips buenos
        self.local = threading.Lock()            # y carga su Whisper local una sola vez, sin cerrojo
        try:
            import numpy
            self.np = numpy
        except ImportError:
            self.np = None
            say("QC: sin numpy no se mide la voz de más al final de cada frase")

    def _samples(self, pcm):
        return self.np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0

    def heard(self, pcm):
        """(texto, fin de la última palabra), o (None, None) si no hay respuesta, como cc_engine.qc_hyp."""
        if self.whisper_dir:
            with self.local:
                try:
                    return self.E.qc_transcribe_local(self._samples(pcm), SR, self.whisper_dir, self.lang or None)
                except Exception as e:           # este Python sin numpy o sin soundfile
                    say(f"  QC local falló: {type(e).__name__}")
                    return None, None
        if not self.ear:
            return None, None
        try:
            return self.ear.transcribe(pcm, self.E._whisper_lang(self.lang) or "",
                                       "Lectura de un libro." if self.lang == "es" else ".")
        except ServiceError as e:
            say(f"  QC por URL falló: {e}")
            return None, None

    def best(self, si, spoken, voice):
        """El PCM que se queda para esa frase; voice(spoken) lo pide. Si el servicio falla, el error sube."""
        E = self.E
        best, best_score = None, -1e9
        for att in range(self.attempts):
            pcm = voice(spoken)
            hyp, last_end = self.heard(pcm)
            if hyp is None:
                if best is None:
                    best = pcm
                say(f"  QC frase {si+1}: sin respuesta del Whisper, me quedo con lo generado")
                break
            wav = self._samples(pcm) if self.np else None
            rec, ok, score, motivo = E._qc_eval(spoken, hyp, self.threshold, self._fault(spoken, wav, last_end))
            if score > best_score:
                best_score, best = score, pcm
            if ok:
                if wav is not None:
                    with self.pace:
                        E.qc_note_rate(spoken, wav, SR)
                if att > 0:
                    say(f"  QC frase {si+1}: OK recall={rec:.2f} (intento {att+1})")
                break
            det = f"recall={rec:.2f}" + (f", alucina ({motivo})" if motivo else f" < {self.threshold:.2f}")
            say(f"  QC frase {si+1}: {det}, regenerando ({att+1}/{self.attempts})")
        return best

    def _fault(self, spoken, wav, last_end):
        if wav is None:
            return ""
        if last_end is not None:
            last_end = min(last_end, len(wav) / SR)   # un servicio que diga otra cosa no debe parar la narración
        with self.pace:
            return self.E.qc_audio_fault(spoken, wav, SR, last_end)


def _chunks(E, text):
    """[(trozo, silencio detrás en segundos)]: las mismas pausas que el motor local."""
    if hasattr(E, "split_chunks"):
        return [(c, E.PAUSES[k]) for c, k in E.split_chunks(text)]
    return [(s, 0.28) for s in E.split_sentences(text)]


def _seed(paths, entry, texts):
    """(cues, segundos, pcm) de un capítulo a medias, o None si lo guardado no cuadra."""
    try:
        with open(paths["json"], encoding="utf-8") as f:
            saved = json.load(f)
        cues, frames = saved.get("cues", []), entry.get("frames")
        if not cues or not frames or entry.get("sr") != SR or [c.get("text") for c in cues] != texts[:len(cues)]:
            return None
        with wave.open(paths["wav"], "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SR, 1, 2) or w.getnframes() < frames:
                return None
            return cues, float(saved.get("duration", 0)), w.readframes(frames)
    except (OSError, ValueError, EOFError, wave.Error):
        return None


def narrate_chapter(E, a, client, ci, ch, prev, manifest, save, review=None):
    base = f"cap{ci:02d}"
    paths = {x: os.path.join(a.out, f"{base}.{x}") for x in ("wav", "mp3", "json")}
    chunks = _chunks(E, ch["text"])
    texts = [c for c, _ in chunks]
    entry = {"id": ci, "base": base, "title": ch["title"], "n": 0, "duration": 0.0, "done": False}
    manifest.append(entry)
    cues, t, pending = [], 0.0, bytearray()
    seed = _seed(paths, prev, texts) if prev and not prev.get("done") else None
    if seed:
        cues, t, pcm = seed
        pending += pcm
        say(f"[cap {ci}] '{ch['title'][:40]}' -> reanudo en frase {len(cues)+1}, quedan {len(texts)-len(cues)}")
    else:
        say(f"[cap {ci}] '{ch['title'][:40]}' -> {len(texts)} frases")
    for p in (paths["wav"], paths["mp3"]):       # lo que valía ya está en pending
        try: os.remove(p)
        except OSError: pass

    raw = wav = None
    frames = 0

    def flush():
        """Añade lo pendiente al WAV y al mp3 y deja constancia: tras esto se puede reanudar."""
        nonlocal raw, wav, frames
        if pending:
            if wav is None:
                raw = open(paths["wav"], "wb")
                wav = wave.open(raw, "wb")
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(SR)
            wav.writeframes(bytes(pending))      # también corrige la cabecera, por si matan el proceso
            raw.flush()
            frames += len(pending) // 2
            fd, seg = tempfile.mkstemp(dir=a.out, suffix=".wav.tmp")
            with os.fdopen(fd, "wb") as f, wave.open(f, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
                w.writeframes(bytes(pending))
            E._encode_mp3(seg, paths["mp3"], append=True)
            try: os.remove(seg)
            except OSError: pass
            pending.clear()
        E.write_cues(a.out, base, ch["title"], cues, t)
        entry.update(n=len(cues), duration=round(t, 3), sr=SR, frames=frames)
        save()

    def voice(item):
        si, spoken = item
        if not spoken:
            return b""
        return review.best(si, spoken, client.say) if review else client.say(spoken)

    if seed:
        flush()
    try:
        todo = ((si, E.normalize_text(texts[si]).strip()) for si in range(len(cues), len(texts)))
        answers = in_order(voice, todo, a.parallel)
        for si, pcm in enumerate(answers, len(cues)):
            dur = len(pcm) / 2 / SR
            gap = int(chunks[si][1] * SR)
            cues.append({"i": si, "start": round(t, 3), "end": round(t + dur, 3), "text": texts[si]})
            pending += pcm + b"\0\0" * gap
            t += dur + gap / SR
            say(f"  frase {si+1}/{len(texts)} {dur:.1f}s")
            if (si + 1) % FLUSH_EVERY == 0:
                flush()
                say(f"  ...guardado parcial cap {ci} ({len(cues)} frases)")
        flush()
    except ServiceError:
        if cues:                                 # no volver a pagar lo que ya ha llegado
            flush()
        raise
    finally:
        if wav is not None:
            wav.close(); raw.close()
    if frames:
        E._encode_mp3(paths["wav"], paths["mp3"], append=False)    # un único mp3 limpio del capítulo
    try: os.remove(paths["wav"])
    except OSError: pass
    entry.update(done=True, frames=0)
    save()
    say(f"[cap {ci}] LISTO {t:.1f}s -> {base}.mp3")


def cmd_generate(a):
    E = _engine()
    E.NUM_LANG = a.lang or "es"                  # con «auto» los números se quedan en cifras para el servicio
    if a.pron:
        E.load_pron(a.pron)
    os.makedirs(a.out, exist_ok=True)
    chaps = E.chapters_from_epub(a.epub)
    title = E.epub_title(a.epub)
    if title:
        E.write_atomic(os.path.join(a.out, "title.txt"), title)
    print(f"capitulos: {len(chaps)}", flush=True)
    prev = E.load_prev_manifest(a.out) if a.resume else {}
    E.write_state(a.out, len(chaps), False)
    manifest = []

    def save():
        # Lo que hubiera más adelante (de una tirada en tramos paralelos) sigue en el índice.
        later = [e for i, e in sorted(prev.items()) if i > manifest[-1]["id"]]
        E.write_atomic(os.path.join(a.out, "manifest.json"),
                       json.dumps(manifest + later, ensure_ascii=False, indent=2))

    client = Client(a.url, os.environ.get(KEY_ENV, ""), a.model, a.voice,
                    on_wait=lambda s: say(f"SERVICE-RETRY {s}"), language=a.lang if a.lang != "auto" else "")
    review = Reviewer(E, a, os.environ.get(QC_KEY_ENV, "")) if E.qc_on(a) else None
    try:
        for ci, ch in enumerate(chaps, 1):
            pe = prev.get(ci)
            if pe and pe.get("done"):
                manifest.append(pe); save()
                say(f"[cap {ci}] ya hecho, lo mantengo")
                continue
            narrate_chapter(E, a, client, ci, ch, pe, manifest, save, review)
    except ServiceError as e:
        say("SERVICE-ERROR " + json.dumps({"kind": e.kind, "detail": e.detail}, ensure_ascii=False))
        os._exit(3)                              # lo llegado ya está guardado; no esperar a las que sigan en marcha
    E.write_state(a.out, len(chaps), True)
    print("EPUB-DONE", len(manifest), flush=True)


def cmd_count(a):
    E = _engine()
    chaps = E.chapters_from_epub(a.epub)
    print("CHAPTERS", len(chaps), flush=True)
    print("SENTS", " ".join(str(len(_chunks(E, ch["text"]))) for ch in chaps), flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--epub", required=True); g.add_argument("--out", required=True)
    g.add_argument("--url", required=True); g.add_argument("--model", default="")
    g.add_argument("--voice", default=""); g.add_argument("--lang", default="es")
    g.add_argument("--pron", default=""); g.add_argument("--resume", action="store_true")
    g.add_argument("--parallel", type=lambda v: max(1, min(int(v), MAX_PARALLEL)), default=1)
    g.add_argument("--qc-url", default="")                   # Whisper por URL, con la API estándar
    g.add_argument("--qc-model", default="")                 # su modelo, si hay que decirlo
    g.add_argument("--qc-whisper-dir", default="")           # Whisper LOCAL (sin red); uno u otro activa el QC
    g.add_argument("--qc-threshold", type=float, default=0.85)     # los dos, como los motores locales
    g.add_argument("--qc-retries", type=int, default=2)
    g.set_defaults(func=cmd_generate)
    c = sub.add_parser("count")
    c.add_argument("--epub", required=True)
    c.set_defaults(func=cmd_count)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
