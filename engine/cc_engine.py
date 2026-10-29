# -*- coding: utf-8 -*-
"""
Motor LOCAL de Cuentacuentos (corre en el venv del usuario, con GPU o CPU).
Dos subcomandos:
  download  --repo R --ckpt-name C --vocab-name V --dest D
  generate  --epub E --out O --ckpt C --vocab V [--arch F5TTS_Base]
            [--voice narrator.wav --voice-text narrator.txt]
La generacion escribe INCREMENTAL y ATOMICA (cada 6 frases): mp3+cues+manifest,
para poder detener y conservar lo hecho. No depende de ningun servidor.
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, os, re, json, html, subprocess, tempfile, sys

# La consola de Windows va en cp1252: un título con caracteres fuera de cp1252
# (p. ej. ⟪ ⟪) hacía que print() reventara y tumbaba toda la generación.
# Forzamos UTF-8 aquí, ANTES de importar f5_tts/wandb (que capturan stdout).
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

FLUSH_EVERY = 6
NUM_LANG = "es"   # idioma para expandir números (lo fija cmd_generate según el modelo)

# palabra para el separador decimal por idioma (coma/point/…)
_DEC_WORD = {"es": "coma", "en": "point", "fr": "virgule", "de": "Komma",
             "it": "virgola", "pt": "vírgula", "ca": "coma", "gl": "coma"}

def _n2w(mobj):
    from num2words import num2words
    s = mobj.group(0)
    lang = NUM_LANG
    if "," in s:
        ent, dec = s.split(",", 1)
        try:
            ent_w = num2words(int(ent), lang=lang) if ent else num2words(0, lang=lang)
            dec_w = " ".join(num2words(int(d), lang=lang) for d in dec)
        except Exception:
            return s
        return f"{ent_w} {_DEC_WORD.get(lang, 'coma')} {dec_w}"
    try: return num2words(int(s), lang=lang)
    except Exception: return s

def normalize_numbers(text):
    """Pasa los dígitos a palabras (en el idioma NUM_LANG) para que F5 no alucine.
    El subtítulo conserva el texto original; esto solo se usa para generar el audio."""
    try:
        import num2words  # noqa: F401
    except Exception:
        return text
    text = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)  # 3 000 / 3<nbsp>000 -> 3000
    text = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", text)      # 1.000 -> 1000
    return re.sub(r"\d+(?:,\d+)?", _n2w, text)


# ======================================================================
# Normalización del texto para F5 (solo afecta al audio; el subtítulo se
# queda con el original). Arregla: palabras en MAYÚSCULAS/siglas, puntuación
# rara y pronunciación de términos extranjeros (diccionario del usuario).
# ======================================================================
_LETTER_ES = {"a": "a", "b": "be", "c": "ce", "d": "de", "e": "e", "f": "efe",
              "g": "ge", "h": "hache", "i": "i", "j": "jota", "k": "ka", "l": "ele",
              "m": "eme", "n": "ene", "ñ": "eñe", "o": "o", "p": "pe", "q": "cu",
              "r": "erre", "s": "ese", "t": "te", "u": "u", "v": "uve",
              "w": "uve doble", "x": "equis", "y": "i griega", "z": "zeta"}
# Siglas que se leen como PALABRA (no deletreadas) en español.
_SAY_WORD = {"OTAN", "ONU", "OVNI", "OPEP", "UNESCO", "UNICEF", "FIFA", "OMS",
             "RAE", "UVI", "UCI", "PIB", "IVA", "AVE", "RENFE", "PYME", "MIR",
             "ESO", "LOGSE", "SIDA", "LASER", "RADAR", "OTI", "NASA", "FMI"}
PRON = {}          # diccionario del usuario: palabra(min) -> pronunciación
SAY_WORD = set()   # siglas extra a leer como palabra
SAY_LETTERS = set()  # siglas extra a deletrear

def load_pron(path):
    """Carga el diccionario de pronunciación del usuario (JSON)."""
    global PRON, SAY_WORD, SAY_LETTERS
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        PRON = {str(k).lower(): str(v) for k, v in (d.get("pron") or {}).items()}
        SAY_WORD = {str(s).upper() for s in (d.get("say_word") or [])}
        SAY_LETTERS = {str(s).upper() for s in (d.get("say_letters") or [])}
    except Exception:
        PRON, SAY_WORD, SAY_LETTERS = {}, set(), set()

def _spell_letters(w):
    return " ".join(_LETTER_ES.get(c, c) for c in w.lower())

def _pronounceable(w):
    """Heurística: ¿parece una palabra leíble (no una sigla deletreada)?"""
    low = w.lower()
    vowels = sum(c in "aeiouáéíóúü" for c in low)
    if vowels == 0:
        return False
    # sin vocales seguidas imposibles y con proporción de vocales razonable
    return vowels >= max(1, len(low) // 4) and len(low) >= 4

def _fix_caps(token):
    """Convierte una palabra TODO-MAYÚSCULAS a algo que F5 lea bien."""
    core = re.sub(r"[^\wÁÉÍÓÚÜÑ]", "", token, flags=re.UNICODE)
    if len(core) < 2 or not core.isupper() or not core.isalpha():
        return token
    up = core.upper()
    if up in SAY_LETTERS:
        rep = _spell_letters(core)
    elif up in SAY_WORD or up in _SAY_WORD:
        rep = core.capitalize()
    elif _pronounceable(core):
        rep = core.capitalize()                 # p. ej. PREFACIO -> Prefacio
    else:
        rep = _spell_letters(core)              # p. ej. CPU -> ce pe u
    return token.replace(core, rep)

def normalize_punct(text):
    """Puntuación rara -> estándar, para que las pausas (comas) se respeten."""
    text = text.replace("—", ", ").replace("–", ", ").replace("―", ", ")
    text = text.replace("…", ". ")
    text = re.sub(r"[“”«»\"]", "", text)
    text = text.replace("；", ";").replace("，", ",")
    text = re.sub(r"\s*;\s*", ", ", text)        # ; como pausa de coma
    text = re.sub(r"(\d),(\d)", "\\1\x00\\2", text)   # proteger la coma decimal (3,14)
    text = re.sub(r"\s*,\s*", ", ", text)        # espacio tras coma
    text = text.replace("\x00", ",")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()

def _apply_pron(text):
    """Sustituye términos del diccionario del usuario (palabra completa)."""
    if not PRON:
        return text
    def rep(m):
        return PRON.get(m.group(0).lower(), m.group(0))
    return re.sub(r"[A-Za-zÀ-ÿ]+", rep, text)

def _lower_initial(text):
    """Baja a minúscula la 1ª letra del trozo (inicio de frase): F5 a veces lee
    raro la mayúscula inicial y fonéticamente es idéntico en minúscula."""
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.lower() + text[i+1:]
        if ch.isdigit():
            break
    return text

def normalize_text(text):
    """Pipeline completo para el audio (no toca el subtítulo)."""
    text = normalize_punct(text)
    text = " ".join(_fix_caps(t) for t in text.split(" "))
    text = _lower_initial(text)
    text = _apply_pron(text)
    text = normalize_numbers(text)
    return text


# ---- QC de alucinaciones: transcribe la frase generada y mide el recall de
# palabras contra el texto que debería decir. Si baja del umbral, se regenera.
def _words(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", s)

def _recall(ref_text, hyp_text):
    ref = set(_words(ref_text))
    if not ref: return 1.0
    hyp = set(_words(hyp_text))
    return sum(1 for w in ref if w in hyp) / len(ref)

def _qc_eval(ref_text, hyp_text, threshold=0.85):
    """Evalúa una frase contra su transcripción Whisper. Además del recall (palabras
    del texto presentes), detecta las ALUCINACIONES típicas de XTTS/F5, que el recall
    por conjuntos NO ve: inserciones (transcripción mucho más larga que el texto) y
    bucles de repetición (una palabra dicha muchas más veces de las debidas, p. ej.
    «volumen volumen volumen…»). Devuelve (recall, ok, score, motivo)."""
    from collections import Counter
    rf = _words(ref_text); rh = _words(hyp_text)
    if not rf:
        return 1.0, True, 1.0, ""
    recall = sum(1 for w in set(rf) if w in set(rh)) / len(set(rf))
    cr, ch = Counter(rf), Counter(rh)
    extra_rep = max((ch[w] - cr.get(w, 0) for w in ch), default=0)   # palabra repetida de más
    too_long = len(rh) > len(rf) * 1.5 + 5                           # parrafada / cola alucinada
    halluc = (extra_rep >= 4) or too_long
    ok = (recall >= threshold) and not halluc
    # score para quedarse con el mejor intento cuando ninguno pasa: recall, castigando alucinar
    score = recall - (0.6 if halluc else 0.0) - 0.02 * max(0, extra_rep - 1)
    motivo = (f"repite x{extra_rep}" if extra_rep >= 4
              else (f"texto x{len(rh)/len(rf):.1f}" if too_long else ""))
    return recall, ok, score, motivo

def qc_transcribe(wav, sr, url, timeout=60):
    """Sube un fragmento WAV al voz-server (Whisper) y devuelve el texto, o None."""
    import numpy as np, soundfile as sf
    fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    try:
        sf.write(tmp, np.asarray(wav, dtype="float32"), sr, format="WAV", subtype="PCM_16")
        p = subprocess.run(["curl", "-s", "-m", str(timeout), "-F", f"file=@{tmp}",
                            "-F", "response_format=json", "-F", "language=es", url],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if p.returncode != 0: return None
        return (json.loads(p.stdout.decode("utf-8", "replace")) or {}).get("text", "")
    except Exception:
        return None
    finally:
        try: os.remove(tmp)
        except OSError: pass

_QC_LOCAL_MODEL = None
def qc_load_local(model_dir):
    """Carga (una vez) un faster-whisper LOCAL para el QC. A propósito en CPU int8:
    el QC corre junto a la narración (y hasta 3 tramos XTTS a la vez) en la misma
    GPU; en CPU no pelea VRAM ni tumba la generación."""
    global _QC_LOCAL_MODEL
    if _QC_LOCAL_MODEL is None:
        from faster_whisper import WhisperModel
        _QC_LOCAL_MODEL = WhisperModel(model_dir, device="cpu", compute_type="int8")
    return _QC_LOCAL_MODEL

def qc_transcribe_local(wav, sr, model_dir, lang=None):
    """Transcribe la frase con un Whisper LOCAL (sin red). Devuelve el texto o None."""
    import numpy as np, soundfile as sf
    fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    try:
        sf.write(tmp, np.asarray(wav, dtype="float32"), sr, format="WAV", subtype="PCM_16")
        model = qc_load_local(model_dir)
        segs, _ = model.transcribe(tmp, language=(lang or None), vad_filter=True, beam_size=1)
        return " ".join(s.text for s in segs)
    except Exception as e:
        print(f"  QC local falló: {type(e).__name__}", flush=True)
        return None
    finally:
        try: os.remove(tmp)
        except OSError: pass

def qc_on(a):
    """¿QC activo? Lo activa un Whisper LOCAL (--qc-whisper-dir) o remoto (--qc-url)."""
    return bool(getattr(a, "qc_whisper_dir", "") or getattr(a, "qc_url", ""))

def qc_hyp(wav, sr, a):
    """Transcribe para QC donde toque: LOCAL si hay --qc-whisper-dir, si no por HTTP."""
    wd = getattr(a, "qc_whisper_dir", "")
    if wd:
        return qc_transcribe_local(wav, sr, wd, getattr(a, "lang", None))
    if getattr(a, "qc_url", ""):
        return qc_transcribe(wav, sr, a.qc_url)
    return None

def cmd_download(a):
    from huggingface_hub import hf_hub_download
    os.makedirs(a.dest, exist_ok=True)
    for name in (a.ckpt_name, a.vocab_name):
        print(f"descargando {name}…", flush=True)
        hf_hub_download(a.repo, name, local_dir=a.dest)
    print("DL-DONE", flush=True)

def cmd_whisper_download(a):
    """Descarga un modelo faster-whisper (CTranslate2) de HF a una carpeta local."""
    from huggingface_hub import snapshot_download
    os.makedirs(a.dest, exist_ok=True)
    print(f"descargando {a.repo}…", flush=True)
    snapshot_download(repo_id=a.repo, local_dir=a.dest,
                      allow_patterns=["*.bin", "*.json", "*.txt", "*.model"])
    print("DL-DONE", flush=True)

def cmd_whisper(a):
    """Transcribe un audio con faster-whisper y EMITE cada segmento en streaming
    (`SEG {json}`) para poder guardar incremental y detener/reanudar desde la app.
    --offset desplaza los tiempos (para reanudar sobre un recorte del audio)."""
    from faster_whisper import WhisperModel
    import torch
    dev = a.device
    if dev == "auto":
        dev = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if dev == "cuda" else "int8"
    try:
        model = WhisperModel(a.model_dir, device=dev, compute_type=compute)
    except Exception as e:
        if dev == "cuda":
            print(f"aviso: Whisper en GPU falló ({type(e).__name__}); uso CPU", flush=True)
            dev = "cpu"; model = WhisperModel(a.model_dir, device="cpu", compute_type="int8")
        else:
            raise
    print(f"whisper cargado ({dev})", flush=True)
    lang = None if a.language in ("", "auto") else a.language    # None = autodetectar
    segs, _info = model.transcribe(a.audio, language=lang, vad_filter=True, beam_size=5)
    for s in segs:
        txt = (s.text or "").strip()
        if not txt:
            continue
        print("SEG " + json.dumps({"start": round(a.offset + s.start, 3),
                                   "end": round(a.offset + s.end, 3), "text": txt},
                                  ensure_ascii=False), flush=True)
    print("WH-DONE", flush=True)

def chapters_from_epub(path, min_chars=25):
    from ebooklib import epub, ITEM_DOCUMENT
    from bs4 import BeautifulSoup
    book = epub.read_epub(path)
    id2 = {it.get_id(): it for it in book.get_items()}
    spine = [id2[i] for (i, _lin) in (book.spine or []) if i in id2]
    docs = spine or list(book.get_items_of_type(ITEM_DOCUMENT))
    out = []
    for it in docs:
        try:
            if it.get_type() != ITEM_DOCUMENT: continue
        except Exception:
            continue
        name = (it.get_name() or "").lower()
        if "nav" in name or "toc" in name: continue            # saltar indice
        soup = BeautifulSoup(it.get_content(), "lxml")
        for t in soup(["script", "style"]): t.decompose()
        h = soup.find(["h1", "h2", "h3"])
        title = (h.get_text(" ", strip=True) if h else "").strip()
        if h: h.extract()
        text = html.unescape(soup.get_text("\n", strip=True))
        text = re.sub(r"[ \t]+", " ", text); text = re.sub(r"\n{2,}", "\n", text).strip()
        if len(text) >= min_chars:
            out.append({"title": title or f"Capitulo {len(out)+1}", "text": text})
    return out

def split_sentences(text, target=180, hard=240, soft=170):
    """Una frase por trozo (mejor prosodia/sincronía). Las frases LARGAS se
    parten en las comas/; y los trozos se reagrupan hasta ~target, de modo que
    cada corte cae en una pausa real (lo que ayuda a "respetar las comas")."""
    raw = re.split(r"(?<=[\.\!\?…])\s+", text.replace("\n", " "))
    out = []
    for s in raw:
        s = s.strip()
        if not s: continue
        if len(s) <= soft:
            out.append(s); continue
        parts = re.split(r"(?<=[,;:])\s+", s)        # trocear por comas/;/:
        buf = ""
        for p in parts:
            p = p.strip()
            if not p: continue
            while len(p) > hard:                      # cláusula monstruosa: corte duro
                cut = max(p.rfind(", ", 0, hard), p.rfind("; ", 0, hard))
                if cut < 60: cut = hard
                if buf: out.append(buf.strip()); buf = ""
                out.append(p[:cut+1].strip()); p = p[cut+1:].strip()
            if len(buf) + len(p) + 1 <= target:
                buf = (buf + " " + p).strip()
            else:
                if buf: out.append(buf.strip())
                buf = p
        if buf.strip(): out.append(buf.strip())
    return [c for c in out if re.search(r"[A-Za-zÀ-ɏ0-9]", c)]

def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text)
    os.replace(tmp, path)

def _encode_mp3(src_wav, dst_mp3, append=False):
    """WAV->MP3 con libmp3lame CBR 128k. append=True escribe a stdout y añade
    los bytes al mp3 existente (CBR concatenable); si no, reemplazo atómico."""
    if append:
        p = subprocess.run(["ffmpeg", "-y", "-i", src_wav, "-codec:a", "libmp3lame",
                            "-b:a", "128k", "-f", "mp3", "pipe:1"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with open(dst_mp3, "ab") as o: o.write(p.stdout)
    else:
        tmp = dst_mp3 + ".tmp"
        subprocess.run(["ffmpeg", "-y", "-i", src_wav, "-codec:a", "libmp3lame",
                        "-b:a", "128k", "-f", "mp3", tmp],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(tmp): os.replace(tmp, dst_mp3)

def write_cues(outdir, base, title, cues, dur):
    write_atomic(os.path.join(outdir, base + ".json"),
                 json.dumps({"title": title, "duration": round(dur, 3), "cues": cues},
                            ensure_ascii=False, indent=1))

def load_prev_manifest(outdir, name="manifest.json"):
    """Capitulos ya escritos en una tirada anterior, indexados por id (para reanudar)."""
    prev = {}
    try:
        with open(os.path.join(outdir, name), encoding="utf-8") as f:
            for e in json.load(f):
                if isinstance(e, dict) and "id" in e: prev[e["id"]] = e
    except Exception:
        pass
    return prev

def write_state(outdir, total, complete):
    """Marca si la generacion del libro esta terminada. complete=False => reanudable."""
    write_atomic(os.path.join(outdir, "gen_state.json"),
                 json.dumps({"total": total, "complete": bool(complete)}, ensure_ascii=False))

def cmd_count(a):
    """Imprime el nº de capitulos del EPUB (sin cargar el modelo), para repartir tramos."""
    print("CHAPTERS", len(chapters_from_epub(a.epub)), flush=True)

def cmd_generate(a):
    import numpy as np
    from f5_tts.api import F5TTS
    global NUM_LANG
    NUM_LANG = getattr(a, "lang", None) or "es"      # idioma para leer los números
    if getattr(a, "pron", ""):
        load_pron(a.pron)                            # diccionario de pronunciación del usuario
    os.makedirs(a.out, exist_ok=True)
    chaps = chapters_from_epub(a.epub)
    # Modo paralelo: este proceso narra SOLO los capitulos [chap_start, chap_end]
    # (1-based, 0 = sin limite) y escribe en su propio manifest (--manifest-name)
    # sin tocar gen_state (--no-state); la app fusiona todo al terminar.
    cs = getattr(a, "chap_start", 0) or 0
    ce = getattr(a, "chap_end", 0) or 0
    mani_name = getattr(a, "manifest_name", None) or "manifest.json"
    no_state = getattr(a, "no_state", False)
    if not cs or cs <= 1:                                 # solo el 1er tramo escribe el titulo
        try:
            from ebooklib import epub
            _t = epub.read_epub(a.epub).get_metadata("DC", "title")
            if _t and _t[0][0].strip(): write_atomic(os.path.join(a.out, "title.txt"), _t[0][0].strip())
        except Exception: pass
    print(f"capitulos: {len(chaps)}", flush=True)
    prev = load_prev_manifest(a.out, mani_name) if a.resume else {}
    if not no_state:
        write_state(a.out, len(chaps), False)
    ref_text = open(a.voice_text, encoding="utf-8").read().strip()
    import torch
    dev = a.device
    if dev == "auto":
        dev = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        m = F5TTS(model=a.arch, ckpt_file=a.ckpt, vocab_file=a.vocab, device=dev)
    except Exception as e:
        if dev == "cuda":
            print(f"aviso: la GPU falló ({type(e).__name__}); reintento en CPU", flush=True)
            dev = "cpu"
            m = F5TTS(model=a.arch, ckpt_file=a.ckpt, vocab_file=a.vocab, device=dev)
        else:
            raise
    print(f"modelo cargado ({dev})", flush=True)
    manifest = []
    def save_manifest():
        write_atomic(os.path.join(a.out, mani_name),
                     json.dumps(manifest, ensure_ascii=False, indent=2))
    import gc, soundfile as sf
    for ci, ch in enumerate(chaps, 1):
        if (cs and ci < cs) or (ce and ci > ce):          # fuera de mi tramo
            continue
        base = f"cap{ci:02d}"
        wav_path = os.path.join(a.out, base + ".wav")
        mp3_path = os.path.join(a.out, base + ".mp3")
        pe = prev.get(ci)
        if a.resume and pe and pe.get("done"):               # capítulo ya terminado: se conserva
            manifest.append(pe); save_manifest()
            print(f"[cap {ci}] ya hecho, lo mantengo", flush=True)
            continue
        sents = split_sentences(ch["text"])
        entry = {"id": ci, "base": base, "title": ch["title"], "n": 0, "duration": 0.0, "done": False}
        manifest.append(entry)
        sr = None; cues = []; t = 0.0; pending = []          # pending = audio aún no volcado
        wf = None                                            # handle WAV incremental (append O(1))
        start_si = 0; resumed = False

        # ---- Reanudar un capítulo a medias: recuperar sus frases ya narradas.
        if a.resume and pe and not pe.get("done"):
            try:
                with open(os.path.join(a.out, base + ".json"), encoding="utf-8") as f:
                    cj = json.load(f)
                ex_cues = cj.get("cues", []); ex_t = float(cj.get("duration", 0.0))
            except Exception:
                ex_cues = []; ex_t = 0.0
            srp = pe.get("sr"); frames = pe.get("frames")
            if ex_cues and srp and frames and os.path.exists(wav_path):
                try:
                    data, _sr = sf.read(wav_path, dtype="float32")
                    if _sr == srp and len(data) >= frames:
                        # El WAV guardado es la fuente de verdad: recorto a las muestras
                        # consistentes con los cues y lo uso de semilla. El 1er flush
                        # reescribe wav+mp3 enteros (sin desincronías de un corte a mitad).
                        sr = srp; cues = ex_cues; t = ex_t; start_si = len(ex_cues)
                        pending = [np.asarray(data[:frames], dtype="float32")]
                        for p in (wav_path, mp3_path):
                            try: os.remove(p)
                            except OSError: pass
                        resumed = True
                except Exception:
                    resumed = False
        if not resumed:
            for p in (wav_path, mp3_path):                   # empezar capítulo limpio
                try: os.remove(p)
                except OSError: pass

        if resumed:
            print(f"[cap {ci}] '{ch['title'][:40]}' -> reanudo en frase {start_si+1}, "
                  f"quedan {len(sents)-start_si}", flush=True)
        else:
            print(f"[cap {ci}] '{ch['title'][:40]}' -> {len(sents)} frases", flush=True)

        def flush_partial(final=False):
            """Vuelca lo pendiente: lo añade al WAV, codifica SOLO ese trozo a
            mp3 y lo concatena (O(nuevo), no recodifica el capítulo entero)."""
            nonlocal wf, pending
            if pending:
                seg = np.concatenate(pending)
                if wf is None:
                    wf = sf.SoundFile(wav_path, mode="w", samplerate=sr, channels=1, subtype="PCM_16")
                wf.write(seg); wf.flush()
                seg_tmp = os.path.join(a.out, base + ".seg.wav.tmp")
                sf.write(seg_tmp, seg, sr, format="WAV", subtype="PCM_16")
                _encode_mp3(seg_tmp, mp3_path, append=True)  # mp3 CBR concatenable
                try: os.remove(seg_tmp)
                except OSError: pass
                pending = []
            write_cues(a.out, base, ch["title"], cues, t)
            entry["n"] = len(cues); entry["duration"] = round(t, 3)
            entry["sr"] = sr or 0; entry["frames"] = (wf.tell() if wf is not None else 0)
            save_manifest()

        if resumed:
            flush_partial()                                  # persistir la semilla ya, por si se detiene otra vez

        for si in range(start_si, len(sents)):
            s = sents[si]
            gen_text = normalize_text(s)
            best_wav = None; best_score = -1e9
            attempts = (a.qc_retries + 1) if qc_on(a) else 1
            for att in range(attempts):
                if att > 0:
                    try:
                        import torch, random
                        torch.manual_seed(random.randint(1, 2**31 - 1))
                    except Exception: pass
                nfe = a.nfe + 6 * att                         # variar para que el reintento difiera
                wav, sr, _ = m.infer(ref_file=a.voice, ref_text=ref_text, gen_text=gen_text,
                                     remove_silence=True, nfe_step=nfe)
                wav = np.asarray(wav, dtype="float32")
                if not qc_on(a):
                    best_wav = wav; break
                hyp = qc_hyp(wav, sr, a)
                if hyp is None:
                    if best_wav is None: best_wav = wav
                    print(f"  QC frase {si+1}: sin respuesta del Whisper, me quedo con lo generado", flush=True)
                    break
                rec, ok, score, motivo = _qc_eval(gen_text, hyp, a.qc_threshold)
                if score > best_score: best_score, best_wav = score, wav
                if ok:
                    if att > 0: print(f"  QC frase {si+1}: OK recall={rec:.2f} (intento {att+1})", flush=True)
                    break
                det = f"recall={rec:.2f}" + (f", alucina ({motivo})" if motivo else f" < {a.qc_threshold:.2f}")
                print(f"  QC frase {si+1}: {det}, regenerando ({att+1}/{attempts})", flush=True)
            wav = best_wav; dur = len(wav) / sr
            cues.append({"i": si, "start": round(t, 3), "end": round(t + dur, 3), "text": s})
            pending.append(wav); pending.append(np.zeros(int(a.gap * sr), dtype="float32"))
            t += dur + a.gap
            print(f"  frase {si+1}/{len(sents)} {dur:.1f}s", flush=True)
            if (si + 1) % FLUSH_EVERY == 0:
                flush_partial()
                print(f"  ...guardado parcial cap {ci} ({len(cues)} frases)", flush=True)
        flush_partial(final=True)
        if wf is not None:
            wf.close()
            _encode_mp3(wav_path, mp3_path, append=False)    # un único encode limpio del capítulo
            try: os.remove(wav_path)
            except OSError: pass
        entry["done"] = True; entry["frames"] = 0; save_manifest()
        print(f"[cap {ci}] LISTO {t:.1f}s -> {base}.mp3", flush=True)
        gc.collect()                                         # soltar memoria/VRAM entre capítulos
        try:
            import torch; torch.cuda.empty_cache()
        except Exception: pass
    if not no_state:
        write_state(a.out, len(chaps), True)
    print("EPUB-DONE", len(manifest), flush=True)

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--repo", required=True); d.add_argument("--ckpt-name", required=True)
    d.add_argument("--vocab-name", required=True); d.add_argument("--dest", required=True)
    d.set_defaults(func=cmd_download)
    g = sub.add_parser("generate")
    g.add_argument("--epub", required=True); g.add_argument("--out", required=True)
    g.add_argument("--ckpt", required=True); g.add_argument("--vocab", required=True)
    g.add_argument("--arch", default="F5TTS_Base")
    here = os.path.dirname(os.path.abspath(__file__))
    g.add_argument("--voice", default=os.path.join(here, "voices", "narrator.wav"))
    g.add_argument("--voice-text", default=os.path.join(here, "voices", "narrator.txt"))
    g.add_argument("--gap", type=float, default=0.28); g.add_argument("--nfe", type=int, default=40)
    g.add_argument("--device", default="auto")
    g.add_argument("--qc-url", default="")                   # endpoint Whisper HTTP (voz-server)
    g.add_argument("--qc-whisper-dir", default="")           # modelo Whisper LOCAL (sin red); uno u otro activa el QC
    g.add_argument("--qc-threshold", type=float, default=0.85)
    g.add_argument("--qc-retries", type=int, default=2)
    g.add_argument("--resume", action="store_true")         # continuar un libro a medias
    g.add_argument("--lang", default="es")                   # idioma para leer los números
    g.add_argument("--pron", default="")                     # JSON diccionario de pronunciación
    g.add_argument("--chap-start", type=int, default=0)      # tramo paralelo: 1er cap (1-based, 0=sin tope)
    g.add_argument("--chap-end", type=int, default=0)        # tramo paralelo: ultimo cap (0=sin tope)
    g.add_argument("--manifest-name", default="manifest.json")  # manifest propio del tramo
    g.add_argument("--no-state", action="store_true")        # no escribir gen_state (lo hace la app)
    g.set_defaults(func=cmd_generate)
    c = sub.add_parser("count")                              # nº de capitulos del EPUB (para repartir tramos)
    c.add_argument("--epub", required=True)
    c.set_defaults(func=cmd_count)
    wd = sub.add_parser("whisper-download")
    wd.add_argument("--repo", required=True); wd.add_argument("--dest", required=True)
    wd.set_defaults(func=cmd_whisper_download)
    w = sub.add_parser("whisper")
    w.add_argument("--audio", required=True); w.add_argument("--model-dir", required=True)
    w.add_argument("--device", default="auto"); w.add_argument("--offset", type=float, default=0.0)
    w.add_argument("--language", default="es")               # "" o "auto" = autodetectar
    w.set_defaults(func=cmd_whisper)
    a = ap.parse_args(); a.func(a)

if __name__ == "__main__":
    main()
