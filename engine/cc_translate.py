# -*- coding: utf-8 -*-
"""
Storyteller — motor de traducción de documentos (TranslateGemma).

Traduce EPUB, DOCX, PPTX, XLSX, SRT/VTT, TXT y MD entre los idiomas de la app, conservando
la estructura del documento (párrafos, títulos, tablas, tiempos de los subtítulos). Un PDF
se convierte en un EPUB con el texto traducido, sin su maquetación. Corre en su propio venv (engine/.venv-translate) y
habla con el modelo por HTTP:

  * local  -> arranca llama-server (llama.cpp) con el GGUF elegido, SOLO mientras traduce,
              y lo cierra al terminar (si este proceso muere, el servidor muere con él).
  * remoto -> Ollama en un servidor propio, por un túnel SSH (Ollama no se expone a la red).

Uso (lo lanza la app; imprime líneas de estado en stdout):
  python -I cc_translate.py translate --in F --out F --src en --tgt es
         (--llama-dir DIR --gguf FILE | --ssh user@host --ollama-model NAME)
         [--cache F] [--stop-file F] [--title T] [--workers N]
  python -I cc_translate.py download --url URL --dest FILE
  python -I cc_translate.py unzip --zip FILE --dest DIR

Protocolo de salida: TOTAL n · SEG i n · PULL pct · DL pct · INFO texto · OUT ruta ·
STOPPED · DONE · ERROR texto
"""
import sys, os, re, io, json, time, copy, html, socket, shutil, hashlib, zipfile, argparse, unicodedata
import subprocess, threading, urllib.request, urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NOWIN = 0x08000000 if os.name == "nt" else 0


def say(*a):
    print(*a, flush=True)


# ----------------------------------------------------------------------------- idiomas
# código de la app -> (nombre en inglés para la instrucción, código para la instrucción)
LANGS = {
    "es": ("Spanish", "es-ES"), "en": ("English", "en"), "de": ("German", "de"),
    "fr": ("French", "fr"), "it": ("Italian", "it"), "pt": ("Portuguese", "pt"),
    "nl": ("Dutch", "nl"), "pl": ("Polish", "pl"), "ru": ("Russian", "ru"),
    "uk": ("Ukrainian", "uk"), "ja": ("Japanese", "ja"), "ko": ("Korean", "ko"),
    "zh": ("Chinese", "zh-Hans"), "hi": ("Hindi", "hi"), "th": ("Thai", "th"),
    "ca": ("Catalan", "ca"),
}

# Instrucción oficial de TranslateGemma (dos líneas en blanco antes del texto).
TPL = ("You are a professional {sl} ({sc}) to {tl} ({tc}) translator. Your goal is to accurately "
       "convey the meaning and nuances of the original {sl} text while adhering to {tl} grammar, "
       "vocabulary, and cultural sensitivities.\n"
       "Produce only the {tl} translation, without any additional explanations or commentary. "
       "Please translate the following {sl} text into {tl}:\n\n\n{text}")


def build_prompt(text, src, tgt):
    sl, sc = LANGS[src]
    tl, tc = LANGS[tgt]
    return TPL.format(sl=sl, sc=sc, tl=tl, tc=tc, text=text)


LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def needs_translation(t):
    return bool(t and LETTER.search(t))


# ----------------------------------------------------------------------------- utilidades
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def replace_retry(tmp, dst, tries=30):
    """os.replace con reintento: en Windows falla si otro proceso lee el fichero en ese instante."""
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.1)


def atomic_write_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    replace_retry(tmp, path)


_JOBS = []


def bind_to_me(proc):
    """Que el hijo muera si muere este proceso (Windows: Job Object KILL_ON_JOB_CLOSE)."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k32.CreateJobObjectW(None, None)
    info = EXT()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    h = k32.OpenProcess(0x0100 | 0x0001, False, proc.pid)  # SET_QUOTA | TERMINATE
    k32.AssignProcessToJobObject(job, h)
    _JOBS.append(job)  # el handle debe vivir mientras viva este proceso


def _linux_pdeathsig():
    try:
        import ctypes, signal
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)
    except Exception:
        pass


def spawn(cmd, **kw):
    if os.name != "nt":
        kw["preexec_fn"] = _linux_pdeathsig
    p = subprocess.Popen(cmd, creationflags=NOWIN, **kw)
    bind_to_me(p)
    return p


def http_json(url, body=None, timeout=600):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------------------------- backends
class LlamaBackend:
    """llama-server (llama.cpp) local, arrancado solo para esta traducción."""

    def __init__(self, llama_dir, gguf, ctx=4096, workers=2):
        self.ctx, self.workers = ctx, workers
        exe = find_llama_server(llama_dir)
        if not exe:
            raise RuntimeError(f"no encuentro llama-server en {llama_dir}")
        llama_dir = exe.parent
        if not Path(gguf).exists():
            raise RuntimeError(f"no encuentro el modelo {gguf}")
        last = ""
        # llama.cpp mete en la GPU las capas que caben (--fit, activo por defecto). Si no arranca,
        # en CPU. OJO: en Windows CUDA no ve la memoria de otros procesos, así que si coincide
        # con una narración no se reparte solo: el 4B cabe junto a XTTS, el 12B iría lento.
        for mode in ([], ["-ngl", "0"]):
            self.port = free_port()
            self.log = Path(gguf).with_suffix(".server.log")
            lf = open(self.log, "w", encoding="utf-8", errors="replace")
            cmd = [str(exe), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(self.port),
                   "-c", str(ctx * workers), "-np", str(workers), "--no-webui", "-lv", "2"] + mode
            self.proc = spawn(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(llama_dir))
            say("INFO", "arrancando el modelo" + (" (en CPU)" if mode else ""))
            ok = self._wait_ready(300)
            lf.close()
            if ok:
                self.base = f"http://127.0.0.1:{self.port}"
                return
            last = self.log.read_text("utf-8", "replace")[-1500:]
            self.close()
        raise RuntimeError("el servidor del modelo no arrancó:\n" + last)

    def _wait_ready(self, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                return False
            try:
                r = http_json(f"http://127.0.0.1:{self.port}/health", timeout=3)
                if r.get("status") == "ok":
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def generate(self, prompt, max_tokens, temperature=0.0):
        body = {"prompt": "<start_of_turn>user\n" + prompt + "<end_of_turn>\n<start_of_turn>model\n",
                "n_predict": max_tokens, "temperature": temperature, "top_k": 64, "top_p": 0.95,
                "repeat_penalty": 1.0 if temperature == 0 else 1.1,
                "stop": ["<end_of_turn>", "<start_of_turn>"], "cache_prompt": False}
        return http_json(self.base + "/completion", body)["content"]

    def close(self):
        p = getattr(self, "proc", None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except Exception:
                p.kill()


class OllamaBackend:
    """Ollama en un servidor propio, a través de un túnel SSH (o directo con --ollama-url)."""

    def __init__(self, model, ssh=None, url=None, ctx=4096):
        self.model, self.ctx, self.tunnel = model, ctx, None
        if url:
            self.base = url.rstrip("/")
        else:
            port = free_port()
            cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                   "-o", "ServerAliveInterval=30", "-N", "-L", f"{port}:127.0.0.1:11434", ssh]
            self.tunnel = spawn(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.base = f"http://127.0.0.1:{port}"
            say("INFO", f"conectando con {ssh}")
        t0 = time.time()
        while True:
            try:
                http_json(self.base + "/api/version", timeout=3)
                break
            except Exception:
                if self.tunnel and self.tunnel.poll() is not None:
                    err = self.tunnel.stderr.read().decode("utf-8", "replace")
                    raise RuntimeError("no pude abrir el túnel SSH: " + err.strip())
                if time.time() - t0 > 25:
                    raise RuntimeError("Ollama no responde en el servidor")
                time.sleep(0.5)
        self._ensure_model()

    def _ensure_model(self):
        tags = http_json(self.base + "/api/tags", timeout=15).get("models", [])
        names = {m.get("name") for m in tags} | {m.get("model") for m in tags}
        if self.model in names or self.model + ":latest" in names:
            return
        say("INFO", f"descargando {self.model} en el servidor")
        req = urllib.request.Request(self.base + "/api/pull",
                                     json.dumps({"name": self.model, "stream": True}).encode())
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("error"):
                    raise RuntimeError(j["error"])
                if j.get("total"):
                    say("PULL", int(100 * j.get("completed", 0) / j["total"]))

    def generate(self, prompt, max_tokens, temperature=0.0):
        body = {"model": self.model, "stream": False, "keep_alive": "2m",
                "messages": [{"role": "user", "content": prompt}],
                "options": {"num_ctx": self.ctx, "temperature": temperature, "num_predict": max_tokens,
                            "repeat_penalty": 1.0 if temperature == 0 else 1.1}}
        return http_json(self.base + "/api/chat", body)["message"]["content"]

    def close(self):
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()


# ----------------------------------------------------------------------------- traductor
MAXC = 1400  # caracteres por petición; los párrafos más largos se parten por frases
_SENT = re.compile(r"(?<=[.!?;:…。！？])\s+")


def split_long(text):
    if len(text) <= MAXC:
        return [text]
    out, cur = [], ""
    for s in _SENT.split(text):
        while len(s) > MAXC:          # frase gigantesca sin puntos: por comas o a lo bruto
            cut = s.rfind(", ", 0, MAXC)
            cut = cut + 1 if cut > MAXC // 3 else MAXC
            if cur:
                out.append(cur); cur = ""
            out.append(s[:cut].strip()); s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > MAXC:
            out.append(cur); cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    return out


def _looks_broken(src, out):
    if not out.strip():
        return True
    if len(out) > 3 * len(src) + 80:
        return True
    m = re.search(r"(.{6,40}?)\1{4,}", out)   # bucle de repetición
    return bool(m and not re.search(r"(.{6,40}?)\1{4,}", src))


class Translator:
    def __init__(self, backend, src, tgt, model_id, cache_path=None, stop_file=None, workers=1):
        self.b, self.src, self.tgt, self.mid = backend, src, tgt, model_id
        self.cache_path = Path(cache_path) if cache_path else None
        self.stop_file = Path(stop_file) if stop_file else None
        self.workers = max(1, workers)
        self.cache = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text("utf-8"))
            except Exception:
                self.cache = {}
        self._lock = threading.Lock()
        self._last_save = time.time()

    def key(self, text):
        return hashlib.sha1(f"{self.mid}|{self.src}|{self.tgt}|{text}".encode("utf-8")).hexdigest()

    def save(self):
        if self.cache_path:
            with self._lock:
                data = json.dumps(self.cache, ensure_ascii=False)
            atomic_write_text(self.cache_path, data)
            self._last_save = time.time()

    def stopped(self):
        return bool(self.stop_file and self.stop_file.exists())

    def _one(self, text):
        parts = []
        for chunk in split_long(text):
            prompt = build_prompt(chunk, self.src, self.tgt)
            mx = min(4096, int(len(chunk) * 1.5) + 128)
            out = self.b.generate(prompt, mx).strip()
            if _looks_broken(chunk, out):
                out2 = self.b.generate(prompt, mx, temperature=0.4).strip()
                if not _looks_broken(chunk, out2):
                    out = out2
                elif not out.strip():
                    out = chunk
            parts.append(out)
        return " ".join(parts)

    def run(self, texts):
        """texts: lista de textos únicos. Devuelve {texto: traducción}. Respeta la parada."""
        todo = [t for t in texts if needs_translation(t) and self.key(t) not in self.cache]
        total = len([t for t in texts if needs_translation(t)])
        done = total - len(todo)
        say("TOTAL", total)
        say("SEG", done, total)
        stop = False

        def work(t):
            if stop or self.stopped():
                return t, None
            return t, self._one(t)

        with ThreadPoolExecutor(self.workers) as ex:
            for t, tr in ex.map(work, todo):
                if tr is None:
                    stop = True
                    continue
                with self._lock:
                    self.cache[self.key(t)] = tr
                done += 1
                say("SEG", done, total)
                if time.time() - self._last_save > 8:
                    self.save()
                if self.stopped():
                    stop = True
        self.save()
        if stop:
            return None
        return {t: (self.cache.get(self.key(t), t) if needs_translation(t) else t) for t in texts}


# ----------------------------------------------------------------------------- formatos
class Unit:
    """Un trozo de texto traducible y cómo volver a ponerlo en su sitio."""
    __slots__ = ("text", "apply")

    def __init__(self, text, apply):
        self.text, self.apply = text, apply


def _ws(t):
    return re.sub(r"[ \t\r\f\v ]+", " ", re.sub(r"\s*\n\s*", "\n", t)).strip()


# ---- EPUB / XHTML
BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "dt", "dd", "caption",
         "figcaption", "blockquote", "div", "section", "article", "aside", "header", "footer",
         "title", "summary", "label", "legend", "option"}
SKIP = {"script", "style", "code", "pre", "math", "svg", "kbd", "samp", "var"}
KEEP_INLINE = {"img", "image", "svg", "math", "object", "video", "audio", "iframe"}
NOTE_RE = re.compile(r"^\W*(\d{1,3}|[*†‡§]+|[ivxlc]{1,5})\W*$", re.I)


def _lname(tag):
    return (tag.name or "").split(":")[-1].lower()


class EpubDoc:
    def __init__(self, path, tgt):
        from bs4 import BeautifulSoup
        self.BS = BeautifulSoup
        self.tgt = tgt
        self.zin = zipfile.ZipFile(path)
        self.names = self.zin.namelist()
        self.data = {n: self.zin.read(n) for n in self.names}
        cont = BeautifulSoup(self.data["META-INF/container.xml"], "xml")
        self.opf = cont.find("rootfile")["full-path"]
        self.base = self.opf.rsplit("/", 1)[0] + "/" if "/" in self.opf else ""
        self.opf_soup = BeautifulSoup(self.data[self.opf], "xml")
        self.docs = {}
        self.units = []
        for it in self.opf_soup.find_all("item"):
            mt = it.get("media-type", "")
            href = urllib.request.url2pathname(it.get("href", "")).replace("\\", "/")
            name = self._norm(self.base + it.get("href", ""))
            if name not in self.data:
                continue
            if mt in ("application/xhtml+xml", "text/html"):
                self._load_xhtml(name)
            elif mt == "application/x-dtbncx+xml":
                self._load_ncx(name)
        for tag in self.opf_soup.find_all(["title", "description", "subject"]):
            if tag.prefix in ("dc", None) and tag.string and needs_translation(tag.string):
                self.units.append(Unit(_ws(tag.string), self._setter_string(tag)))

    def _norm(self, n):
        n = urllib.request.unquote(n.split("#")[0])
        parts = []
        for p in n.split("/"):
            if p == "..":
                parts and parts.pop()
            elif p and p != ".":
                parts.append(p)
        return "/".join(parts)

    @staticmethod
    def _setter_string(tag):
        def f(t):
            tag.string = t
        return f

    def _load_xhtml(self, name):
        raw = self.data[name]
        try:
            soup = self.BS(raw, "xml")
            if not soup.find(lambda t: _lname(t) == "html"):
                raise ValueError
        except Exception:
            soup = self.BS(raw, "lxml")
        self.docs[name] = soup
        html_tag = soup.find(lambda t: _lname(t) == "html")
        if html_tag is not None:
            for a in ("xml:lang", "lang"):
                if a in html_tag.attrs:
                    html_tag[a] = self.tgt
        root = soup.find(lambda t: _lname(t) == "body") or soup
        head_title = soup.find(lambda t: _lname(t) == "title")
        if head_title is not None and head_title.string and needs_translation(head_title.string):
            self.units.append(Unit(_ws(head_title.string), self._setter_string(head_title)))
        self._walk(root)

    def _is_leaf_block(self, el):
        for d in el.find_all(True):
            if _lname(d) in BLOCK:
                return False
        return True

    def _walk(self, el):
        for child in list(el.children):
            if not getattr(child, "name", None):
                if type(child).__name__ != "NavigableString":
                    continue                      # comentarios, CDATA, doctype…
                # texto suelto directamente dentro de un bloque con más bloques dentro
                s = str(child)
                if needs_translation(s) and el is not None and _lname(el) not in SKIP:
                    self.units.append(Unit(_ws(s), self._setter_navstring(child)))
                continue
            ln = _lname(child)
            if ln in SKIP:
                continue
            if ln in BLOCK and self._is_leaf_block(child):
                self._add_block(child)
            elif ln in BLOCK:
                self._walk(child)
            else:
                # inline fuera de un bloque hoja (p. ej. <span> directo en <body>)
                if self._is_leaf_block(child):
                    self._add_block(child)
                else:
                    self._walk(child)

    @staticmethod
    def _setter_navstring(ns):
        lead = re.match(r"^\s*", str(ns)).group(0)
        trail = re.search(r"\s*$", str(ns)).group(0)

        def f(t):
            ns.replace_with(lead + t + trail)
        return f

    def _block_text(self, el):
        out = []
        for d in el.descendants:
            if getattr(d, "name", None):
                if _lname(d) == "br":
                    out.append("\n")
                continue
            if type(d).__name__ != "NavigableString":
                continue
            par = d.parent
            if par is not None and (_lname(par) in SKIP or self._is_note(par)):
                continue
            out.append(str(d))
        return _ws("".join(out))

    def _is_note(self, el):
        ln = _lname(el)
        if ln not in ("a", "sup", "span", "small"):
            return False
        t = el.get_text()
        if not NOTE_RE.match(t or ""):
            return False
        return ln in ("sup",) or el.find(lambda x: _lname(x) == "sup") is not None or \
            (ln == "a" and ("noteref" in (el.get("epub:type") or "") or "#" in (el.get("href") or "")))

    def _add_block(self, el):
        text = self._block_text(el)
        if not needs_translation(text):
            return
        self.units.append(Unit(text, lambda t, el=el: self._set_block(el, t)))

    def _set_block(self, el, t):
        # notas al pie, imágenes y fórmulas se conservan; el resto del contenido se sustituye
        keep_front, keep_end = [], []
        for d in list(el.find_all(True)):
            ln = _lname(d)
            if ln in KEEP_INLINE and d.find_parent(lambda x: _lname(x) in KEEP_INLINE) is None:
                keep_front.append(d.extract())
            elif self._is_note(d) and d.find_parent(lambda x: self._is_note(x)) is None:
                keep_end.append(d.extract())
        # si todo el bloque iba envuelto en un único inline (<p><i>…</i></p>), se mantiene
        target = el
        while True:
            kids = [k for k in target.children if getattr(k, "name", None) or str(k).strip()]
            if len(kids) == 1 and getattr(kids[0], "name", None) and _lname(kids[0]) not in BLOCK \
                    and _lname(kids[0]) not in ("br",):
                target = kids[0]
            else:
                break
        target.clear()
        for k in keep_front:
            target.append(k)
        lines = t.split("\n")
        for i, line in enumerate(lines):
            if i:
                target.append(self._new_tag(el, "br"))
            target.append(line)
        for k in keep_end:
            target.append(k)

    def _new_tag(self, el, name):
        soup = el
        while soup.parent is not None:
            soup = soup.parent
        return soup.new_tag(name)

    def _load_ncx(self, name):
        soup = self.BS(self.data[name], "xml")
        self.docs[name] = soup
        for t in soup.find_all("text"):
            if t.string and needs_translation(t.string):
                self.units.append(Unit(_ws(t.string), self._setter_string(t)))

    def save(self, out):
        for lang in self.opf_soup.find_all("language"):
            lang.string = self.tgt
        self.data[self.opf] = str(self.opf_soup).encode("utf-8")
        for name, soup in self.docs.items():
            s = str(soup)
            if not s.lstrip().startswith("<?xml") and self.data[name].lstrip().startswith(b"<?xml"):
                s = '<?xml version="1.0" encoding="utf-8"?>\n' + s
            self.data[name] = s.encode("utf-8")
        tmp = Path(str(out) + ".tmp")
        with zipfile.ZipFile(tmp, "w") as z:
            if "mimetype" in self.data:
                z.writestr(zipfile.ZipInfo("mimetype"), self.data["mimetype"], compress_type=zipfile.ZIP_STORED)
            for n in self.names:
                if n != "mimetype":
                    z.writestr(n, self.data[n], compress_type=zipfile.ZIP_DEFLATED)
        replace_retry(tmp, out)


# ---- DOCX
class DocxDoc:
    def __init__(self, path, tgt):
        import docx
        from docx.oxml.ns import qn
        self.qn = qn
        self.doc = docx.Document(path)
        self.units = []
        roots = [self.doc.element.body]
        for s in self.doc.sections:
            for hf in (s.header, s.footer, s.first_page_header, s.first_page_footer,
                       s.even_page_header, s.even_page_footer):
                try:
                    if not hf.is_linked_to_previous:
                        roots.append(hf._element)
                except Exception:
                    pass
        self.xml_parts = []
        from lxml import etree
        self.etree = etree
        for rel in self.doc.part.rels.values():
            if rel.is_external:
                continue
            p = rel.target_part
            if p is not None and str(p.partname) in ("/word/footnotes.xml", "/word/endnotes.xml"):
                el = getattr(p, "_element", None)
                if el is None:
                    el = etree.fromstring(p.blob)
                    self.xml_parts.append((p, el))
                roots.append(el)
        seen = set()
        for root in roots:
            for p in root.iter(qn("w:p")):
                if id(p) in seen:
                    continue
                seen.add(id(p))
                self._add_par(p)
        # idioma del documento
        try:
            for st in (self.doc.styles.element,):
                for lang in st.iter(qn("w:lang")):
                    lang.set(qn("w:val"), LANGS[tgt][1])
        except Exception:
            pass

    def _runs(self, p):
        qn = self.qn
        return [r for r in p.iter(qn("w:r")) if self._owner_par(r) is p]

    def _owner_par(self, el):
        qn = self.qn
        a = el.getparent()
        while a is not None and a.tag != qn("w:p"):
            a = a.getparent()
        return a

    def _run_text(self, r):
        qn = self.qn
        out = []
        for c in r:
            if c.tag == qn("w:t"):
                out.append(c.text or "")
            elif c.tag == qn("w:tab"):
                out.append("\t")
            elif c.tag in (qn("w:br"), qn("w:cr")):
                out.append("\n")
        return "".join(out)

    def _add_par(self, p):
        runs = self._runs(p)
        text = "".join(self._run_text(r) for r in runs)
        text = _ws(text.replace("\t", " \t "))
        if not needs_translation(text):
            return
        self.units.append(Unit(text, lambda t, p=p, runs=runs: self._set_par(runs, t)))

    def _set_par(self, runs, t):
        qn = self.qn
        first = None
        for r in runs:
            for c in list(r):
                if c.tag in (qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")):
                    if first is None and c.tag == qn("w:t"):
                        first = (r, c)
                    else:
                        r.remove(c)
        if first is None:
            return
        r, tnode = first
        lines = t.split("\n")
        tnode.text = lines[0]
        tnode.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        anchor = tnode
        for line in lines[1:]:
            br = self.etree.SubElement(r, qn("w:br"))
            anchor.addnext(br)
            nt = self.etree.SubElement(r, qn("w:t"))
            nt.text = line
            nt.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            br.addnext(nt)
            anchor = nt

    def save(self, out):
        for part, el in self.xml_parts:
            part._blob = self.etree.tostring(el, xml_declaration=True, encoding="UTF-8", standalone=True)
        tmp = Path(str(out) + ".tmp")
        self.doc.save(str(tmp))
        replace_retry(tmp, out)


# ---- PPTX
class PptxDoc:
    def __init__(self, path, tgt):
        from pptx import Presentation
        from pptx.oxml.ns import qn
        self.qn = qn
        self.prs = Presentation(path)
        self.units = []
        for slide in self.prs.slides:
            for el in slide.shapes._spTree.iter(qn("a:p")):
                self._add_par(el)
            if slide.has_notes_slide:
                for el in slide.notes_slide.shapes._spTree.iter(qn("a:p")):
                    self._add_par(el)

    def _add_par(self, p):
        qn = self.qn
        parts = []
        for c in p:
            if c.tag in (qn("a:r"), qn("a:fld")):
                t = c.find(qn("a:t"))
                parts.append((t.text or "") if t is not None else "")
            elif c.tag == qn("a:br"):
                parts.append("\n")
        text = _ws("".join(parts))
        if not needs_translation(text):
            return
        self.units.append(Unit(text, lambda t, p=p: self._set_par(p, t)))

    def _set_par(self, p, t):
        qn = self.qn
        runs = [c for c in p if c.tag == qn("a:r")]
        rpr = None
        if runs and runs[0].find(qn("a:rPr")) is not None:
            rpr = copy.deepcopy(runs[0].find(qn("a:rPr")))
        for c in list(p):
            if c.tag in (qn("a:r"), qn("a:br"), qn("a:fld")):
                p.remove(c)
        end = p.find(qn("a:endParaRPr"))
        from lxml import etree
        new = []
        for i, line in enumerate(t.split("\n")):
            if i:
                br = etree.Element(qn("a:br"))
                if rpr is not None:
                    br.append(copy.deepcopy(rpr))
                new.append(br)
            r = etree.Element(qn("a:r"))
            if rpr is not None:
                r.append(copy.deepcopy(rpr))
            te = etree.SubElement(r, qn("a:t"))
            te.text = line
            new.append(r)
        for el in new:
            if end is not None:
                end.addprevious(el)
            else:
                p.append(el)

    def save(self, out):
        tmp = Path(str(out) + ".tmp")
        self.prs.save(str(tmp))
        replace_retry(tmp, out)


# ---- XLSX
class XlsxDoc:
    def __init__(self, path, tgt):
        import openpyxl
        self.wb = openpyxl.load_workbook(path)
        self.units = []
        for ws in self.wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    v = c.value
                    if isinstance(v, str) and not v.startswith("=") and needs_translation(v):
                        self.units.append(Unit(_ws(v), lambda t, c=c: setattr(c, "value", t)))

    def save(self, out):
        tmp = Path(str(out) + ".tmp")
        self.wb.save(str(tmp))
        replace_retry(tmp, out)


# ---- SRT / VTT
TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
ASS_RE = re.compile(r"^(\{\\[^}]*\})+")


class SubsDoc:
    def __init__(self, path, tgt):
        raw = Path(path).read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                self.text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        self.text = self.text.replace("\r\n", "\n").replace("\r", "\n")
        self.blocks = re.split(r"\n{2,}", self.text.strip("\n"))
        self.units = []
        self.out_blocks = list(self.blocks)
        for i, b in enumerate(self.blocks):
            lines = b.split("\n")
            ti = next((k for k, l in enumerate(lines) if "-->" in l), None)
            if ti is None:
                continue
            body = lines[ti + 1:]
            if not body:
                continue
            joined = "\n".join(body)
            pre = ASS_RE.match(joined)
            prefix = pre.group(0) if pre else ""
            joined = joined[len(prefix):]
            italic = bool(re.fullmatch(r"\s*<i>.*</i>\s*", joined, re.S)) and joined.count("<i>") == 1
            plain = _ws(TAG_RE.sub("", joined))
            if not needs_translation(plain):
                continue
            head = lines[:ti + 1]

            def apply(t, i=i, head=head, prefix=prefix, italic=italic):
                t = t.strip()
                if italic:
                    t = "<i>" + t + "</i>"
                self.out_blocks[i] = "\n".join(head) + "\n" + prefix + t
            self.units.append(Unit(plain, apply))

    def save(self, out):
        atomic_write_text(out, "\n\n".join(self.out_blocks) + "\n")


# ---- TXT / MD
MD_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)+)")


class TextDoc:
    def __init__(self, path, tgt, markdown=False):
        raw = Path(path).read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        text = text.replace("\r\n", "\n")
        self.pieces = []           # lista de str (fijos) o [str] (traducibles, mutables)
        self.units = []
        paras = re.split(r"(\n\s*\n)", text)
        in_code = False
        for chunk in paras:
            if not chunk.strip() or re.fullmatch(r"\n\s*\n", chunk):
                self.pieces.append(chunk)
                continue
            if markdown:
                self._md_block(chunk)
            else:
                self._txt_block(chunk)

    def _slot(self, text, lead="", trail=""):
        slot = [text]
        self.pieces.append(lead)
        self.pieces.append(slot)
        self.pieces.append(trail)
        self.units.append(Unit(text, lambda t, slot=slot: slot.__setitem__(0, t)))

    def _txt_block(self, chunk):
        lines = chunk.split("\n")
        lead = re.match(r"^\s*", chunk).group(0)
        if len(lines) >= 3 and sum(len(l) for l in lines) / len(lines) > 50:
            text = " ".join(l.strip() for l in lines)     # prosa cortada a mano: se une
        else:
            text = "\n".join(l.strip() for l in lines)
        if needs_translation(text):
            self._slot(_ws(text), lead)
        else:
            self.pieces.append(chunk)

    def _md_block(self, chunk):
        lines = chunk.split("\n")
        if getattr(self, "_code", False) or lines[0].lstrip().startswith(("```", "~~~")):
            fences = sum(1 for l in lines if l.lstrip().startswith(("```", "~~~")))
            self.pieces.append(chunk)
            if fences % 2 == 1:
                self._code = not getattr(self, "_code", False)
            return
        if all(l.startswith(("    ", "\t")) for l in lines if l.strip()):
            self.pieces.append(chunk)
            return
        if all(l.strip().startswith("|") for l in lines if l.strip()):
            for k, l in enumerate(lines):
                if k:
                    self.pieces.append("\n")
                if re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", l):
                    self.pieces.append(l)
                    continue
                cells = l.split("|")
                for j, cell in enumerate(cells):
                    if j:
                        self.pieces.append("|")
                    if needs_translation(cell):
                        lead = re.match(r"^\s*", cell).group(0)
                        trail = re.search(r"\s*$", cell).group(0)
                        self._slot(cell.strip(), lead, trail)
                    else:
                        self.pieces.append(cell)
            return
        if any(MD_PREFIX.match(l) for l in lines):
            for k, l in enumerate(lines):
                if k:
                    self.pieces.append("\n")
                m = MD_PREFIX.match(l)
                pre = m.group(0) if m else re.match(r"^\s*", l).group(0)
                body = l[len(pre):]
                if needs_translation(body):
                    self._slot(body.strip(), pre)
                else:
                    self.pieces.append(l)
            return
        self._txt_block(chunk)

    def save(self, out):
        s = "".join(p[0] if isinstance(p, list) else p for p in self.pieces)
        atomic_write_text(out, s)


# ---- PDF -> EPUB con el texto traducido, para leerlo o narrarlo (no conserva la maquetación)
class PdfDoc:
    def __init__(self, path, tgt):
        import logging
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTChar, LTTextContainer, LTTextLine
        logging.getLogger("pdfminer").setLevel(logging.ERROR)       # avisos de fuentes: solo ruido
        self.tgt = tgt
        self.title = ""
        self.units = []
        self.blocks = []        # {page, height, top, bottom, size, slot}
        self.npages = 0
        raw = []
        for pno, page in enumerate(extract_pages(str(path))):
            self.npages += 1
            for el in page:
                if not isinstance(el, LTTextContainer):
                    continue
                lines, sizes, upright = [], [], 0
                for ln in el:
                    if not isinstance(ln, LTTextLine):
                        continue
                    lines.append(ln.get_text())
                    for ch in ln:
                        if isinstance(ch, LTChar) and ch.get_text().strip():
                            sizes.append(ch.size)
                            upright += 1 if ch.upright else 0
                text = self._join(lines)
                if not sizes or upright < len(sizes) / 2 or not needs_translation(text):
                    continue                        # sin texto o girado (sellos, márgenes)
                raw.append({"page": pno, "height": page.height, "top": page.height - el.y1,
                            "bottom": page.height - el.y0, "x0": el.x0, "x1": el.x1,
                            "size": sorted(sizes)[len(sizes) // 2], "slot": [text]})
        # Letra capital: una o dos letras en grande son el comienzo del bloque que sigue a su
        # derecha a la misma altura (pdfminer no las entrega justo antes de su párrafo).
        body = sorted(b["size"] for b in raw)[len(raw) // 2] if raw else 11
        caps = [b for b in raw if len(b["slot"][0]) <= 2 and b["size"] >= body * 1.2]
        merged = set()
        for cap in caps:
            near = [b for b in raw if b["page"] == cap["page"] and b["size"] < cap["size"]
                    and b["top"] < cap["bottom"] and b["bottom"] > cap["top"] and b["x1"] > cap["x1"]]
            if near:
                dest = min(near, key=lambda b: abs(b["top"] - cap["top"]))
                dest["slot"][0] = cap["slot"][0] + dest["slot"][0]
                merged.add(id(cap))
        for b in raw:
            if id(b) not in merged:
                self.blocks.append(b)
                self.units.append(Unit(b["slot"][0], lambda t, slot=b["slot"]: slot.__setitem__(0, t)))

    @staticmethod
    def _clean(t):
        t = re.sub(r"\(cid:\d+\)", "", t)            # glifos sin correspondencia
        return re.sub("[ﬀ-ﬆ]", lambda m: unicodedata.normalize("NFKC", m.group()), t)  # ligaduras

    @classmethod
    def _join(cls, lines):
        out = ""
        for l in lines:
            l = cls._clean(l).strip()
            if not l:
                continue
            if out.endswith("-") and l[:1].islower():
                out = out[:-1] + l
            elif out:
                out += " " + l
            else:
                out = l
        return _ws(out)

    def save(self, out):
        """Capítulos por los títulos (letra mayor); quita cabeceras, pies y números de página."""
        from collections import Counter
        title = self.title or Path(out).stem
        sigs = Counter((round(b["top"] / 8), b["slot"][0][:40]) for b in self.blocks)
        sizes = sorted(b["size"] for b in self.blocks)
        body = sizes[len(sizes) // 2] if sizes else 11
        chapters, cur = [], None
        for b in self.blocks:
            t = b["slot"][0].strip()
            repeated = sigs[(round(b["top"] / 8), t[:40])] >= max(3, self.npages // 4)
            margin = b["bottom"] < b["height"] * 0.08 or b["top"] > b["height"] * 0.92
            if (repeated and margin) or re.fullmatch(r"\W*\d{1,4}\W*", t):
                continue
            heading = b["size"] >= body * 1.2 and 3 <= len(t) < 160
            if heading and (cur is None or any(p[0] == "p" for p in cur["paras"])):
                cur = {"title": t, "paras": []}
                chapters.append(cur)
                continue
            if cur is None:
                cur = {"title": title, "paras": []}
                chapters.append(cur)
            cur["paras"].append(("h" if heading else "p", t, b["page"]))
        if len(chapters) == 1 and self.npages > 15:     # sin títulos: un capítulo cada 10 páginas
            paras, by10 = chapters[0]["paras"], {}
            for k, t, pg in paras:
                by10.setdefault(pg // 10, {"title": f"{title} ({pg // 10 + 1})", "paras": []})["paras"].append((k, t, pg))
            chapters = [by10[n] for n in sorted(by10)]
        if not chapters:
            chapters = [{"title": title, "paras": []}]
        for ch in chapters:
            ch["paras"] = [(k, t) for k, t, _pg in ch["paras"]]
        write_simple_epub(out, title, self.tgt, chapters)


def write_simple_epub(out, title, lang, chapters):
    import uuid
    esc = html.escape
    uid = "urn:uuid:" + str(uuid.uuid4())
    tmp = Path(str(out) + ".tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>', compress_type=zipfile.ZIP_DEFLATED)
        items, spine, nav = [], [], []
        for i, ch in enumerate(chapters, 1):
            fn = f"c{i:03d}.xhtml"
            paras = "\n".join(f"<{'h3' if k == 'h' else 'p'}>{esc(t)}</{'h3' if k == 'h' else 'p'}>"
                              for k, t in ch["paras"])
            z.writestr("OEBPS/" + fn,
                       f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                       f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}"><head>'
                       f'<title>{esc(ch["title"])}</title></head><body><h2>{esc(ch["title"])}</h2>\n{paras}\n</body></html>',
                       compress_type=zipfile.ZIP_DEFLATED)
            items.append(f'<item id="c{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            nav.append(f'<li><a href="{fn}">{esc(ch["title"])}</a></li>')
        z.writestr("OEBPS/nav.xhtml",
                   f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" '
                   f'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}"><head><title>{esc(title)}</title></head>'
                   f'<body><nav epub:type="toc"><ol>{"".join(nav)}</ol></nav></body></html>',
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf",
                   f'<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                   f'unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f'<dc:identifier id="uid">{uid}</dc:identifier><dc:title>{esc(title)}</dc:title>'
                   f'<dc:language>{lang}</dc:language>'
                   f'<meta property="dcterms:modified">{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}</meta>'
                   f'</metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                   f'properties="nav"/>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>',
                   compress_type=zipfile.ZIP_DEFLATED)
    replace_retry(tmp, out)


FORMATS = {".epub": EpubDoc, ".docx": DocxDoc, ".pptx": PptxDoc, ".xlsx": XlsxDoc,
           ".srt": SubsDoc, ".vtt": SubsDoc, ".txt": TextDoc, ".md": "md", ".markdown": "md",
           ".pdf": PdfDoc}


def open_doc(path, tgt):
    ext = Path(path).suffix.lower()
    cls = FORMATS.get(ext)
    if cls is None:
        raise RuntimeError(f"formato no soportado: {ext}")
    if cls == "md":
        return TextDoc(path, tgt, markdown=True)
    return cls(path, tgt)


# ----------------------------------------------------------------------------- comandos
def cmd_translate(a):
    if a.src not in LANGS or a.tgt not in LANGS:
        raise RuntimeError("idioma no soportado")
    say("INFO", "leyendo el documento")
    doc = open_doc(a.inp, a.tgt)
    texts = list(dict.fromkeys(u.text for u in doc.units))
    if not texts:
        raise RuntimeError("no encontré texto que traducir (¿PDF escaneado sin texto?)")
    if a.ssh or a.ollama_url:
        backend = OllamaBackend(a.ollama_model, ssh=a.ssh, url=a.ollama_url)
        mid = "ollama:" + a.ollama_model
        workers = 1
    else:
        backend = LlamaBackend(a.llama_dir, a.gguf, workers=a.workers)
        mid = "gguf:" + Path(a.gguf).name
        workers = a.workers
    try:
        tr = Translator(backend, a.src, a.tgt, mid, a.cache, a.stop_file, workers).run(texts)
    finally:
        backend.close()
    if tr is None:
        say("STOPPED")
        return
    say("INFO", "guardando")
    for u in doc.units:
        u.apply(tr.get(u.text, u.text))
    if isinstance(doc, PdfDoc):
        doc.title = a.title or ""
    doc.save(a.out)
    say("OUT", a.out)
    if a.cache and Path(a.cache).exists():
        Path(a.cache).unlink()
    say("DONE")


def cmd_download(a):
    dest = Path(a.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dest) + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(a.url, headers={"User-Agent": "Storyteller", **({"Range": f"bytes={have}-"} if have else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:
            have = 0
        total = int(r.headers.get("Content-Length") or 0) + have
        mode = "ab" if have else "wb"
        with open(part, mode) as f:
            got, last = have, 0
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                if total and time.time() - last > 0.5:
                    say("DL", int(100 * got / total), f"{got / 1e9:.2f}/{total / 1e9:.2f} GB")
                    last = time.time()
    replace_retry(part, dest)
    say("DL", 100)
    say("DONE")


def find_llama_server(root):
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    root = Path(root)
    if (root / name).exists():
        return root / name
    hits = sorted(root.rglob(name)) if root.exists() else []
    return hits[0] if hits else None


def cmd_unzip(a):
    if a.zip.endswith((".tar.gz", ".tgz")):
        import tarfile
        with tarfile.open(a.zip) as t:
            t.extractall(a.dest, filter="data")
    else:
        with zipfile.ZipFile(a.zip) as z:
            z.extractall(a.dest)
    if os.name != "nt":
        exe = find_llama_server(a.dest)
        if exe:
            exe.chmod(0o755)
    say("DONE")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("translate")
    t.add_argument("--in", dest="inp", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--src", default="en")
    t.add_argument("--tgt", default="es")
    t.add_argument("--llama-dir")
    t.add_argument("--gguf")
    t.add_argument("--ssh")
    t.add_argument("--ollama-url")
    t.add_argument("--ollama-model", default="translategemma:4b")
    t.add_argument("--cache")
    t.add_argument("--stop-file")
    t.add_argument("--title")
    t.add_argument("--workers", type=int, default=2)
    d = sub.add_parser("download")
    d.add_argument("--url", required=True)
    d.add_argument("--dest", required=True)
    u = sub.add_parser("unzip")
    u.add_argument("--zip", required=True)
    u.add_argument("--dest", required=True)
    a = ap.parse_args()
    try:
        {"translate": cmd_translate, "download": cmd_download, "unzip": cmd_unzip}[a.cmd](a)
    except Exception as e:
        say("ERROR", str(e).replace("\n", " | "))
        sys.exit(1)


if __name__ == "__main__":
    main()
