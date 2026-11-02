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
import argparse, os, re, json, html, subprocess, tempfile, sys, time

# La consola de Windows va en cp1252: un título con caracteres fuera de cp1252
# (p. ej. ⟪ ⟪) hacía que print() reventara y tumbaba toda la generación.
# Forzamos UTF-8 aquí, ANTES de importar f5_tts/wandb (que capturan stdout).
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

FLUSH_EVERY = 6
NUM_LANG = "es"   # idioma del texto: el que elige el usuario, o el del modelo (lo fija cmd_generate)

# palabra para el separador decimal por idioma (coma/point/…)
_DEC_WORD = {"es": "coma", "en": "point", "fr": "virgule", "de": "Komma",
             "it": "virgola", "pt": "vírgula", "ca": "coma", "gl": "coma",
             "pl": "przecinek", "tr": "virgül", "ru": "запятая", "nl": "komma", "cs": "celá",
             "hu": "egész", "ar": "فاصلة", "zh-cn": "点", "ja": "点", "ko": "점", "hi": "दशमलव"}
_DECIMAL_POINT = {"en", "zh-cn", "ja", "ko", "hi", "ar"}     # se escribe 3.5; en los demás, 3,5

# num2words no tiene hindi, y con cifras XTTS se cae en ese idioma: los números se escriben aquí.
_HI_0_99 = ("शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस "
            "बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस "
            "पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस "
            "सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ "
            "बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर "
            "छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी "
            "नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे").split()

def _hi_number(n):
    """Con lakh y crore, como se cuenta en hindi: 1999 = एक हज़ार नौ सौ निन्यानवे."""
    if n < 100:
        return _HI_0_99[n]
    parts = []
    for size, word in ((10 ** 7, "करोड़"), (10 ** 5, "लाख"), (1000, "हज़ार"), (100, "सौ")):
        if n >= size:
            parts.append(f"{_hi_number(n // size)} {word}")
            n %= size
    if n:
        parts.append(_HI_0_99[n])
    return " ".join(parts)

def _to_words(n, lang):
    from num2words import num2words
    return _hi_number(n) if lang == "hi" else num2words(n, lang=lang)

def _n2w(mobj):
    from num2words import num2words
    s = mobj.group(0)
    lang = NUM_LANG
    sep = "." if lang in _DECIMAL_POINT else ","
    if sep in s:
        ent, dec = s.split(sep, 1)
        try:
            ent_w = _to_words(int(ent) if ent else 0, lang)
            dec_w = " ".join(_to_words(int(d), lang) for d in dec)
        except Exception:
            return s
        return f"{ent_w} {_DEC_WORD.get(lang, 'coma')} {dec_w}"
    try: return _to_words(int(s), lang)
    except Exception: return s

def normalize_numbers(text):
    """Pasa los dígitos a palabras (en el idioma NUM_LANG) para que F5 no alucine.
    El subtítulo conserva el texto original; esto solo se usa para generar el audio."""
    try:
        import num2words  # noqa: F401
    except Exception:
        return text
    es, point = NUM_LANG == "es", NUM_LANG in _DECIMAL_POINT
    # precios: "$74.99" -> "74 dólares con 99"; "$140" -> "140 dólares" (XTTS decía "dólar ciento…")
    if es:
        text = re.sub(r"\$\s?(\d+)\.(\d{2})\b", r"\1 dólares con \2", text)
        text = re.sub(r"\$\s?(\d[\d.,]*\d|\d)", r"\1 dólares", text)
    text = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)  # 3 000 / 3<nbsp>000 -> 3000
    if not point:
        text = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", text)  # 1.000 -> 1000
    # miles al estilo inglés que dejan las traducciones: 3,000 / 21,000 -> 3000
    # (solo grupos exactos de 3 cifras; un decimal "3,5" no casa). En alemán, "1,500" es un decimal.
    if es or point:
        text = re.sub(r"\b\d{1,3}(?:,\d{3})+\b(?![,.]\d)", lambda m: m.group(0).replace(",", ""), text)
    text = re.sub(r"\d+(?:\.\d+)?" if point else r"\d+(?:,\d+)?", _n2w, text)
    if es:                                                # 21000 -> veintiún mil, no "veintiuno mil"
        text = re.sub(r"\bveintiuno (mil|millones)\b", r"veintiún \1", text)
        text = re.sub(r"\buno (mil|millones)\b", r"un \1", text)
    return text


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
# Diccionario del usuario, por idioma: lo de arriba del JSON es del español y otro idioma va en
# "langs": {"en": {"pron": {…}, "say_letters": […], "say_word": […]}}. Lo que se escribe para que
# suene en español («software» -> «sófwer») sonaría mal en cualquier otro.
_PRON_BY_LANG = {}
_NO_PRON = ({}, set(), set())

def _pron_entry(d):
    return ({str(k).lower(): str(v) for k, v in (d.get("pron") or {}).items()},
            {str(s).upper() for s in (d.get("say_word") or [])},
            {str(s).upper() for s in (d.get("say_letters") or [])})

def load_pron(path):
    """Carga el diccionario de pronunciación del usuario (JSON)."""
    global _PRON_BY_LANG, _PRON_RE
    _PRON_RE = {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        _PRON_BY_LANG = {"es": _pron_entry(d)}
        for lang, entry in (d.get("langs") or {}).items():
            _PRON_BY_LANG[str(lang).lower()] = _pron_entry(entry)
    except Exception:
        _PRON_BY_LANG = {}

def _pron_now():
    """(palabra -> pronunciación, siglas como palabra, siglas deletreadas) del idioma en curso."""
    return _PRON_BY_LANG.get(NUM_LANG, _NO_PRON)

def _spell_letters(w):
    if NUM_LANG != "es":                      # el modelo sabe cómo se llaman las letras en su idioma
        return " ".join(w.lower())
    return " ".join(_LETTER_ES.get(c, c) for c in w.lower())

# Palabras cortas que en MAYÚSCULAS son palabras, no siglas (títulos y
# etiquetas de diálogo: "PRUEBA ESTO EN UNA REUNIÓN", "YO:", "TÚ:").
_SHORT_WORDS = {"A", "Y", "O", "U", "E", "DE", "LA", "EL", "EN", "NO", "YO", "TU", "TÚ",
                "ÉL", "MI", "MÍ", "SU", "SE", "SÉ", "LO", "UN", "AL", "ES", "YA", "SI",
                "SÍ", "NI", "VA", "ME", "TE", "LE", "OS", "HA", "HE", "DA", "DE", "VE",
                "OH", "AY", "EH", "AH", "IR", "TÉ", "AÚN", "OÍR"}
# Abreviaturas que se leen enteras (en mayúsculas o no): Sr. -> señor, EE. UU. ...
_ABBR = [(r"\bEE\.?\s?UU\.?(?!\w)", "Estados Unidos"),
         (r"\b[Ss][Rr][Aa]\.(?=\s)", "señora"), (r"\b[Ss][Rr][Tt][Aa]\.(?=\s)", "señorita"),
         (r"\b[Ss][Rr]\.(?=\s)", "señor"), (r"\b[Dd][Rr][Aa]\.(?=\s)", "doctora"),
         (r"\b[Dd][Rr]\.(?=\s)", "doctor"), (r"\b[Jj][Rr]\.?(?!\w)", "júnior")]
# (?!\w) y no (?=\s): "DOROTHY SR.: hola" también es "señor" (antes se deletreaba "ese erre")
_ABBR = [(p.replace(r"\.(?=\s)", r"\.(?!\w)"), r) for p, r in _ABBR]
# tras un nombre propio, Sr./Jr. es "sénior"/"júnior" (Dorothy Sr. = la madre), no "señor"
_ABBR.insert(0, (r"\b(?!(?:El|EL|Al|AL|Del|DEL|Un|UN|Y|Pero|Que|Dijo|Con|Para|Por)\b)"
                 r"([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ']+) [Ss][Rr]\.?(?!\w)", r"\1 sénior"))
_V = "AEIOUÁÉÍÓÚÜ"

def _pronounceable(w):
    """Heurística: ¿parece una palabra leíble (no una sigla deletreada)?"""
    up = w.upper()
    low = w.lower()
    vowels = sum(c in "aeiouáéíóúü" for c in low)
    if vowels == 0:
        return False
    if up in _SHORT_WORDS:
        return True
    if len(up) <= 2:
        return False
    if len(up) == 3:
        # forma de sílaba española: DOC, VOZ, SIN, DEL, MÁS (CVC), IRA, UNA (VCV),
        # CIA (CVV). CPU, USB, ADN, FBI no casan -> se deletrean.
        return bool(re.fullmatch(f"[^{_V}][{_V}]{{1,2}}[^{_V}]?|[{_V}][^{_V}][{_V}]", up))
    # sin vocales seguidas imposibles y con proporción de vocales razonable
    return vowels >= max(1, len(low) // 4)

def _fix_caps(token):
    """Convierte una palabra TODO-MAYÚSCULAS a algo que F5 lea bien."""
    core = re.sub(r"[^\wÁÉÍÓÚÜÑ]", "", token, flags=re.UNICODE)
    if len(core) < 2 or not core.isupper() or not core.isalpha():
        return token
    up = core.upper()
    _pron, say_word, say_letters = _pron_now()
    es = NUM_LANG == "es"
    if up in say_letters:
        rep = _spell_letters(core)
    elif up in say_word or (es and up in _SAY_WORD):
        rep = core.capitalize()
    elif not es:                                # lo de las sílabas solo vale para el español
        return token
    elif _pronounceable(core):
        rep = core.capitalize()                 # p. ej. PREFACIO -> Prefacio
    else:
        rep = _spell_letters(core)              # p. ej. CPU -> ce pe u
    return token.replace(core, rep)

def normalize_punct(text):
    """Puntuación rara -> estándar, para que las pausas (comas) se respeten."""
    text = re.sub(r"\[\[\\?/?[^\]]{0,20}\]\]", "", text)   # marcas de nota/maquetación [[1]]..[[/1]]
    text = text.replace("\xad", "")                       # guion blando invisible
    text = re.sub(r"_{2,}", ", ", text)                    # huecos para rellenar ____
    text = text.replace("—", ", ").replace("–", ", ").replace("―", ", ")
    text = re.sub(r"(?<!\w)-+(?!\w)|(?<=\s)-|-(?=\s)", ", ", text)   # guion suelto (no jiu-jitsu)
    text = re.sub(r"\.{2,}", "…", text).replace("…", ". ")  # "y..." escrito a mano: XTTS alucinaba
    text = re.sub(r"(?<=\w)[’'](?=\w)", "'", text)          # O’Donnell, McDonald’s: apóstrofo dentro de palabra
    text = re.sub(r"[“”«»\"‘’‚„‹›]", "", text)              # comillas fuera
    text = re.sub(r"[()\[\]{}]", ", ", text)                # paréntesis/corchetes: XTTS los lee raro -> pausa
    text = re.sub(r"(?<!\d):|:(?!\d)", ", ", text)          # "Yo: hola" -> pausa (10:30 se queda)
    text = re.sub(r"(?<!\d)\s*/\s*(?!\d)", ", ", text)      # "lujo/caro", versos "a / b" (1/2 se queda)
    if NUM_LANG == "es":
        text = re.sub(r"\s*=\s*", " igual a ", text)
        text = re.sub(r"(?<=[A-Za-z])\+", " más", text)     # A+
    text = text.replace("©", "").replace("®", "").replace("™", "").replace("*", " ")
    text = text.replace("；", ";").replace("，", ",")
    text = re.sub(r"\s*;\s*", ", ", text)        # ; como pausa de coma
    text = re.sub(r"(\d),(\d)", "\\1\x00\\2", text)   # proteger la coma decimal (3,14)
    text = re.sub(r"\s*,\s*", ", ", text)        # espacio tras coma
    text = text.replace("\x00", ",")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)              # ", , " que dejan los cambios de arriba
    text = re.sub(r"\s*,\s*([\.\?\!])", r"\1", text)        # ",." -> "."
    text = re.sub(r"([\.\?\!])\s*,\s*", r"\1 ", text)       # "monitoreo., Sin" -> "monitoreo. Sin"
    text = re.sub(r"([¿¡])\s*,\s*", r"\1", text)
    text = re.sub(r"([.?!])(?:\s*\.)+", r"\1", text)        # 'y...".' -> "y. ." sin comillas -> "y."
    text = re.sub(r"^[\s,\.]+", "", text)
    text = re.sub(r"[\s,]+$", "", text)                      # sin coma colgando al final: XTTS hacía ",." y comía la cola
    return text.strip()

_PRON_RE = {}

def _apply_pron(text):
    """Sustituye términos del diccionario del usuario: palabra completa, de cualquier alfabeto."""
    pron = _pron_now()[0]
    if not pron:
        return text
    rx = _PRON_RE.get(NUM_LANG)
    if rx is None:
        alt = "|".join(re.escape(k) for k in sorted(pron, key=len, reverse=True))
        rx = _PRON_RE[NUM_LANG] = re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE)
    return rx.sub(lambda m: pron.get(m.group(0).lower(), m.group(0)), text)

def _lower_initial(text):
    """Baja a minúscula la 1ª letra del trozo (inicio de frase): F5 a veces lee
    raro la mayúscula inicial y fonéticamente es idéntico en minúscula."""
    for i, ch in enumerate(text):
        if ch.isalpha():
            if NUM_LANG != "es" and text[i+1:i+2].isupper():   # una sigla: en español ya la ha resuelto _fix_caps
                return text
            return text[:i] + ch.lower() + text[i+1:]
        if ch.isdigit():
            break
    return text

def normalize_text(text):
    """Pipeline completo para el audio (no toca el subtítulo)."""
    if NUM_LANG == "es":                      # en otro idioma, sus abreviaturas las sabe el modelo
        for pat, rep in _ABBR:                # antes que la puntuación: el punto de "Sr." no es fin
            text = re.sub(pat, rep, text)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", r"\1 \2", text)   # 10:30 -> "diez treinta"
    text = normalize_punct(text)
    text = " ".join(_fix_caps(t) for t in text.split(" "))
    text = _lower_initial(text)
    text = _apply_pron(text)
    text = normalize_numbers(text)
    return text


# ---- QC de alucinaciones: transcribe la frase generada y mide el recall de
# palabras contra el texto que debería decir. Si baja del umbral, se regenera.
_NO_SPACES = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")

def _words(s):
    """Las palabras de un texto, sin acentos, en cualquier alfabeto. Chino y japonés no separan
    palabras: ahí cuenta cada carácter."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", _NO_SPACES.sub(lambda m: f" {m.group(0)} ", s))

def _recall(ref_text, hyp_text):
    ref = set(_words(ref_text))
    if not ref: return 1.0
    hyp = set(_words(hyp_text))
    return sum(1 for w in ref if w in hyp) / len(ref)

_QC_RATES = []                # letras/s de los clips buenos de este proceso
QC_TAIL_MAX = 0.30            # voz tolerada tras la última palabra reconocida, en s

def _letters(text):
    return len(re.findall(r"[^\W\d_]", text))

def qc_note_rate(text, wav, sr):
    """Apunta la velocidad de un clip bueno: con ella se juzga si otro dura de más."""
    dur = len(wav) / sr
    if dur > 0 and _letters(text) >= 15:
        _QC_RATES.append(_letters(text) / dur)
        del _QC_RATES[:-200]

def qc_audio_fault(text, wav, sr, last_end=None):
    """Lo que el texto de Whisper no ve: XTTS a veces sigue balbuceando sin formar
    palabras y la transcripción sale perfecta. Mide en el audio la voz tras la última
    palabra y si el clip dura demasiado para sus letras. Devuelve el motivo o ""."""
    import numpy as np, statistics
    if wav is None or len(wav) == 0:
        return ""
    if last_end is not None:
        fr = max(1, int(0.01 * sr))
        tail = wav[int(last_end * sr):]
        nf = tail.size // fr
        if nf:
            env = np.sqrt(np.mean(tail[:nf * fr].reshape(nf, fr) ** 2, axis=1) + 1e-12)
            voz = float(np.sum(env > np.sqrt(np.mean(wav ** 2)) * 10 ** (-18 / 20))) * 0.01
            if voz > QC_TAIL_MAX:
                return f"{voz:.1f} s de voz sin palabras al final"
    if len(_QC_RATES) >= 8:
        esperado = _letters(text) / statistics.median(_QC_RATES)
        dur = len(wav) / sr
        if dur > esperado * 1.25 + 0.4:
            return f"dura {dur:.1f} s y deberían ser {esperado:.1f} s"
    return ""

def _qc_eval(ref_text, hyp_text, threshold=0.85, fault=""):
    """Evalúa una frase contra su transcripción Whisper. Además del recall (palabras
    del texto presentes), detecta las ALUCINACIONES típicas de XTTS/F5, que el recall
    por conjuntos NO ve: inserciones (transcripción mucho más larga que el texto) y
    bucles de repetición (una palabra dicha muchas más veces de las debidas, p. ej.
    «volumen volumen volumen…»). `fault` = lo que haya visto qc_audio_fault.
    Devuelve (recall, ok, score, motivo)."""
    from collections import Counter
    rf = _words(ref_text); rh = _words(hyp_text)
    if not rf:
        return 1.0, True, 1.0, ""
    recall = sum(1 for w in set(rf) if w in set(rh)) / len(set(rf))
    cr, ch = Counter(rf), Counter(rh)
    extra_rep = max((ch[w] - cr.get(w, 0) for w in ch), default=0)   # palabra repetida de más
    too_long = len(rh) > len(rf) * 1.5 + 5                           # parrafada / cola alucinada
    cola, sin_final = _qc_tail(rf, rh)
    halluc = (extra_rep >= 4) or too_long or cola >= 3 or bool(fault)
    ok = (recall >= threshold) and not halluc and not sin_final
    # score para quedarse con el mejor intento cuando ninguno pasa: recall, castigando alucinar
    score = (recall - (0.6 if halluc else 0.0) - (0.3 if sin_final else 0.0)
             - 0.02 * max(0, extra_rep - 1) - 0.03 * cola)
    motivo = (fault if fault
              else f"repite x{extra_rep}" if extra_rep >= 4
              else f"texto x{len(rh)/len(rf):.1f}" if too_long
              else f"coletilla de {cola} palabras" if cola >= 3
              else "se come el final" if sin_final else "")
    return recall, ok, score, motivo

def _qc_tail(rf, rh):
    """Mira el FINAL de la frase, que es donde falla XTTS: (palabras inventadas
    después de la última del texto, ¿falta el final?). Medido en «Gracias por
    discutir» cap. 6: 12 de 244 frases acababan con coletilla ("…menor resistencia.
    ¡O no te sigo muerto, ponte!") y el recall/longitud no lo veía."""
    import difflib
    if not rf or not rh:
        return 0, False
    def close(a, b):
        return a == b or (len(a) >= 4 and len(b) >= 4
                          and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75)
    for back, w in enumerate(reversed(rf[-2:])):          # última palabra; si no, penúltima
        for j in range(len(rh) - 1, max(-1, len(rh) - 12), -1):
            if close(rh[j], w):
                # solo aparece la penúltima y no hay nada detrás: la última se la comió
                eaten = (back == 1 and j == len(rh) - 1 and len(rf[-1]) > 3
                         and not any(c.isdigit() for c in rh[-1]))
                return max(0, len(rh) - 1 - j - back), eaten
    # no aparece: Whisper a veces junta/parte ("enojo"/"en ojo") o pone cifras
    tail = "".join(rh[-4:])
    if rf[-1] in tail or len(rf[-1]) <= 3 or any(c.isdigit() for c in tail):
        return 0, False
    return 0, True

def _whisper_lang(lang):
    """El código que entiende Whisper: «zh», no «zh-cn»."""
    return (lang or "").split("-")[0] or None

def qc_transcribe(wav, sr, url, timeout=60, lang="es"):
    """Sube un fragmento WAV a un Whisper por HTTP (voz-server). Devuelve (texto, instante en
    que acaba la última palabra, si el servidor lo da); (None, None) si no hay respuesta."""
    import numpy as np, soundfile as sf
    fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    try:
        sf.write(tmp, np.asarray(wav, dtype="float32"), sr, format="WAV", subtype="PCM_16")
        # El prompt evita el del servidor, que es vocabulario de su dictado, no de un libro.
        prompt = "Lectura de un libro." if (lang or "es") == "es" else "."
        p = subprocess.run(["curl", "-s", "-m", str(timeout), "-F", f"file=@{tmp}",
                            "-F", "response_format=verbose_json", "-F", f"language={_whisper_lang(lang) or 'es'}",
                            "-F", f"prompt={prompt}", url],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if p.returncode != 0: return None, None
        d = json.loads(p.stdout.decode("utf-8", "replace")) or {}
        if "text" not in d: return None, None             # p. ej. 503 sin memoria: no es que suene mal
        words = d.get("words") or []
        return d["text"], (words[-1].get("end") if words else None)
    except Exception:
        return None, None
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
    """Transcribe la frase con un Whisper LOCAL (sin red). Devuelve (texto, instante en
    que acaba la última palabra); (None, None) si falla."""
    import numpy as np, soundfile as sf
    fd, tmp = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    try:
        sf.write(tmp, np.asarray(wav, dtype="float32"), sr, format="WAV", subtype="PCM_16")
        model = qc_load_local(model_dir)
        segs, _ = model.transcribe(tmp, language=_whisper_lang(lang), vad_filter=True, beam_size=1,
                                   word_timestamps=True)
        segs = list(segs)
        words = [w for s in segs for w in (s.words or [])]
        return " ".join(s.text for s in segs), (words[-1].end if words else None)
    except Exception as e:
        print(f"  QC local falló: {type(e).__name__}", flush=True)
        return None, None
    finally:
        try: os.remove(tmp)
        except OSError: pass

def qc_on(a):
    """¿QC activo? Lo activa un Whisper LOCAL (--qc-whisper-dir) o remoto (--qc-url)."""
    return bool(getattr(a, "qc_whisper_dir", "") or getattr(a, "qc_url", ""))

def qc_hyp(wav, sr, a):
    """Transcribe para QC donde toque: LOCAL si hay --qc-whisper-dir, si no por HTTP.
    Devuelve (texto o None, fin de la última palabra o None)."""
    wd = getattr(a, "qc_whisper_dir", "")
    if wd:
        return qc_transcribe_local(wav, sr, wd, getattr(a, "lang", None))
    if getattr(a, "qc_url", ""):
        return qc_transcribe(wav, sr, a.qc_url, lang=getattr(a, "lang", "es"))
    return None, None

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

_BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th",
               "dt", "dd", "pre", "figcaption", "caption", "section", "aside", "header", "footer", "tr"]
_END_PUNCT = ".?!…:;,»\"”’)]。！？：；，、」』）।؟،؛"
_HAS_TEXT = re.compile(r"[^\W_]")                # una letra o cifra, de cualquier alfabeto

def _blocks_text(soup):
    """Texto por BLOQUES: cada párrafo/título/recuadro en su línea y, si no acaba en
    signo, se le pone punto. Antes los títulos de recuadro ("Alerta de Persuasión",
    "INTENTA ESTO EN…") se pegaban a la frase siguiente y XTTS lo leía de un tirón."""
    SEP = " "
    for br in soup.find_all("br"):
        br.replace_with(SEP)
    for b in soup.find_all(_BLOCK_TAGS):
        b.insert_before(SEP); b.append(SEP)
    raw = html.unescape(soup.get_text(" "))
    lines = []
    for ln in raw.split(SEP):
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln:
            continue
        ln = re.sub(r"\s+([,\.;:\!\?\)\]»”])", r"\1", ln)     # "palabra ," que dejan <i>/<span>
        ln = re.sub(r"([\(\[«“¿¡])\s+", r"\1", ln)
        if _HAS_TEXT.search(ln) and ln[-1] not in _END_PUNCT:
            ln += "."
        lines.append(ln)
    return "\n".join(lines)

def _epub_package(path):
    """(zip, carpeta del OPF, OPF ya leído). Un EPUB es un zip: no hace falta otra librería."""
    import zipfile
    from bs4 import BeautifulSoup
    z = zipfile.ZipFile(path)
    opf = BeautifulSoup(z.read("META-INF/container.xml"), "xml").find("rootfile")["full-path"]
    return z, opf.rsplit("/", 1)[0] + "/" if "/" in opf else "", BeautifulSoup(z.read(opf), "xml")


def _epub_docs(path):
    """Documentos del libro en orden de lectura: [(nombre relativo, contenido)]."""
    import posixpath
    from urllib.parse import unquote
    z, base, pkg = _epub_package(path)
    names = set(z.namelist())
    def is_doc(it):
        return it.get("media-type") in ("application/xhtml+xml", "text/html") or \
            (it.get("href") or "").lower().endswith((".xhtml", ".html", ".htm"))
    items = {it.get("id"): it for it in pkg.find_all("item")}
    order = [items[r.get("idref")] for r in pkg.find_all("itemref") if r.get("idref") in items]
    docs = []
    for it in (order or list(items.values())):
        name = unquote(it.get("href") or "")
        full = posixpath.normpath(base + name)
        if is_doc(it) and full in names:
            docs.append((name, z.read(full)))
    return docs


def epub_title(path):
    """Título del libro (dc:title), o "" si no tiene."""
    try:
        t = _epub_package(path)[2].find("title")
        return t.get_text(strip=True) if t else ""
    except Exception:
        return ""


def chapters_from_epub(path, min_chars=25):
    from bs4 import BeautifulSoup
    out = []
    for name, content in _epub_docs(path):
        name = name.lower()
        if "nav" in name or "toc" in name: continue            # saltar indice
        soup = BeautifulSoup(content, "lxml")
        if soup.head: soup.head.decompose()                    # su <title> no se narra
        for t in soup(["script", "style"]): t.decompose()
        h = soup.find(["h1", "h2", "h3"])
        title = (h.get_text(" ", strip=True) if h else "").strip()
        if h: h.extract()
        text = _blocks_text(soup)
        if len(text) >= min_chars:
            out.append({"title": title or f"Capitulo {len(out)+1}", "text": text})
    return out

_ABBR_END = re.compile(r"(?:\b(?:[Ss]r|[Ss]ra|[Ss]rta|[Dd]r|[Dd]ra|[Mm]r|[Mm]rs|[Mm]s|[Ss]t|[Pp]rof|"
                       r"[Ll]ic|[Ii]ng|EE|vs|[Pp]ág|[Nn]úm|aprox)|(?<![\w.])[A-ZÁÉÍÓÚÑ])\.$")

# Fin de frase: signo, quizá una comilla o un paréntesis que cierra, y espacio. Si sigue
# minúscula no es fin ("¿Qué hora es? —dijo él", "etc. y", "Bueno… quizá"). En chino y
# japonés no hay espacio tras el signo: sin cortar ahí, un párrafo entero iría de una vez.
_SENT_SPLIT = re.compile(r"(?:(?<=[.!?…।؟])|(?<=[.!?…।؟][»”\"’)\]]))\s+(?![—–-]?\s*[a-zà-öø-ÿа-яёα-ω])"
                         r"|(?:(?<=[。！？])(?![」』”）])|(?<=[。！？][」』”）]))\s*")
_SENT_END = re.compile(r"[.!?…。！？।؟][»”\"’)\]」』）]*$")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:،؛])\s+|(?<=[，、；：])\s*")
_CLAUSE_END_TIGHT = "，、；："                     # tras estos no se escribe espacio

# Tope de caracteres por trozo: XTTS corta el audio si se pasa (char_limits en coqui; aquí algo
# menos, porque los números se escriben luego en letra). El resto de idiomas admite 240 o más.
_HARD = {"zh-cn": 75, "ja": 65, "ko": 85, "hi": 140, "ar": 155, "ru": 170, "cs": 175, "pt": 190,
         "it": 200, "pl": 210, "hu": 210, "tr": 210}

# Signos con los que un trozo ya acaba: sin uno, XTTS desbarra al final.
STOPS = ".?!…。！？।؟"

def with_stop(text):
    """Lo que se le da a XTTS: sin coma colgando (se comía la cola) y con su signo de fin."""
    t = (text or "").strip().rstrip(",;: ，、；：،؛")
    if t and t[-1] not in STOPS:
        t += "."
    return t

# Silencio que se añade tras cada trozo según cómo acabe; lo marca la puntuación del
# libro, no el modelo. trim_clip deja además 40 ms por lado: se oyen 0,30 / 0,63 / 0,98 s.
PAUSES = {"coma": 0.22, "frase": 0.55, "parrafo": 0.90}

def _split_paragraph(text, hard=240, short=20):
    """Un trozo por cláusula: se corta en cada fin de frase y en cada coma/;/:, para
    que la pausa la ponga PAUSES y no el modelo (XTTS deja en una coma entre 0 y 0,8 s).
    Una cláusula de menos de `short` caracteres va con la siguiente («Además, vivimos…»)."""
    raw = []
    for s in _SENT_SPLIT.split(text):
        # no cortar tras abreviatura o inicial ("el Sr. Smith", "EE. UU.", "John F. Kennedy"):
        # "el Sr." suelto lo leía XTTS como una frase entera y luego el nombre aparte
        if raw and _ABBR_END.search(raw[-1]):
            raw[-1] += " " + s
        else:
            raw.append(s)
    out = []
    for s in raw:
        s = s.strip()
        if not s: continue
        clauses = []
        for p in _CLAUSE_SPLIT.split(s):
            if clauses and len(clauses[-1]) < short:
                clauses[-1] += ("" if clauses[-1][-1] in _CLAUSE_END_TIGHT else " ") + p
            else:
                clauses.append(p)
        for p in clauses:
            while len(p) > hard:                      # cláusula monstruosa: corte en un espacio
                cut = p.rfind(" ", min(60, hard // 2), hard)
                if cut < 0: cut = hard
                out.append(p[:cut].strip()); p = p[cut:].strip()
            out.append(p)
    return [c for c in out if _HAS_TEXT.search(c)]

def split_chunks(text):
    """[(trozo, clave de PAUSES)]. Cada línea del texto es un párrafo (_blocks_text)."""
    out = []
    for para in text.split("\n"):
        parts = _split_paragraph(para, hard=_HARD.get(NUM_LANG, 240))
        for i, c in enumerate(parts):
            kind = "parrafo" if i == len(parts) - 1 else ("frase" if _SENT_END.search(c) else "coma")
            out.append((c, kind))
    return out

def split_sentences(text):
    return [c for c, _ in split_chunks(text)]

def same_cues(cues, sents):
    """¿Lo ya narrado de un capítulo cuadra con su troceo actual? Si no (lo narró otra
    versión), reanudar por posición saltaría o repetiría frases: se rehace el capítulo."""
    return [c.get("text") for c in cues] == sents[:len(cues)]

def trim_clip(w, sr=24000, drop_island=True):
    """Recorta el silencio de los dos extremos del clip (deja 40 ms de margen). Con
    drop_island, si el último tramo de voz es corto y está separado del cuerpo por un
    silencio largo, lo descarta: es la alucinación final de XTTS, no voz."""
    import numpy as np
    if w is None or w.size == 0:
        return w
    fr = max(1, int(0.02 * sr))                     # ventana de 20 ms
    nf = w.size // fr
    if nf < 3:
        return w
    env = np.sqrt(np.mean(w[:nf * fr].reshape(nf, fr) ** 2, axis=1) + 1e-9)
    peak = float(env.max())
    if peak <= 0:
        return w
    thr = peak * (10 ** (-40.0 / 20.0))             # −40 dB rel. pico = hay voz
    idx = np.where(env >= thr)[0]
    if idx.size == 0:
        return w
    first, last = int(idx[0]), int(idx[-1])
    # tramos contiguos de voz
    segs, run = [], [idx[0], idx[0]]
    for k in idx[1:]:
        if k == run[1] + 1:
            run[1] = k
        else:
            segs.append(tuple(run)); run = [k, k]
    segs.append(tuple(run))
    SIL = int(0.22 / 0.02)                           # hueco "largo" >= 220 ms
    SHORT = int(0.35 / 0.02)                          # isla "corta" <= 350 ms
    if drop_island and len(segs) >= 2:
        (_s0, e0), (s1, e1) = segs[-2], segs[-1]
        if (s1 - e0) >= SIL and (e1 - s1) <= SHORT:   # isla final separada = alucinación
            last = e0
    pad = int(0.04 * sr)
    a0 = max(0, first * fr - pad)
    b0 = min(w.size, (last + 1) * fr + pad)
    return w[a0:b0]

def cap_silences(w, sr=24000, over=0.40, keep=0.32, db=-40.0):
    """Acorta a `keep` segundos los silencios internos de más de `over`. XTTS deja en
    una coma entre 0 y 0,8 s: sin tope, una coma puede durar más que un punto."""
    import numpy as np
    if w is None or w.size == 0:
        return w
    fr = max(1, int(0.01 * sr))                     # ventana de 10 ms
    nf = w.size // fr
    if nf < 3:
        return w
    env = np.sqrt(np.mean(w[:nf * fr].reshape(nf, fr) ** 2, axis=1) + 1e-9)
    quiet = env < float(env.max()) * (10 ** (db / 20.0))
    k = int(keep * sr)
    parts, pos, i = [], 0, 0
    while i < nf:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < nf and quiet[j]:
            j += 1
        if i > 0 and j < nf and (j - i) * fr >= int(over * sr):   # solo los internos
            parts.append(w[pos:i * fr + k // 2])
            pos = j * fr - (k - k // 2)
        i = j
    return np.concatenate(parts + [w[pos:]]) if parts else w

def replace_retry(src, dst, tries=60, wait=0.05):
    """os.replace que aguanta lectores: en Windows falla con «Acceso denegado» si
    otro proceso tiene `dst` abierto en ese instante (la app fusionando manifests,
    el reproductor, un antivirus…). Reintenta ~3 s antes de rendirse."""
    for i in range(tries):
        try:
            os.replace(src, dst); return
        except PermissionError:
            if i == tries - 1: raise
            time.sleep(wait)

def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text)
    replace_retry(tmp, path)

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
        if os.path.exists(tmp): replace_retry(tmp, dst_mp3)

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
    """Imprime el nº de capitulos del EPUB (sin cargar el modelo), para repartir tramos,
    y las frases de cada uno (SENTS), para que la app estime el tiempo restante."""
    chaps = chapters_from_epub(a.epub)
    print("CHAPTERS", len(chaps), flush=True)
    print("SENTS", " ".join(str(len(split_sentences(ch["text"]))) for ch in chaps), flush=True)

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
            _t = epub_title(a.epub)
            if _t: write_atomic(os.path.join(a.out, "title.txt"), _t)
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
        chunks = split_chunks(ch["text"])
        sents = [c for c, _ in chunks]
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
            if ex_cues and srp and frames and os.path.exists(wav_path) and same_cues(ex_cues, sents):
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
                wav = cap_silences(trim_clip(np.asarray(wav, dtype="float32"), sr, drop_island=False), sr)
                if not qc_on(a):
                    best_wav = wav; break
                hyp, last_end = qc_hyp(wav, sr, a)
                if hyp is None:
                    if best_wav is None: best_wav = wav
                    print(f"  QC frase {si+1}: sin respuesta del Whisper, me quedo con lo generado", flush=True)
                    break
                rec, ok, score, motivo = _qc_eval(gen_text, hyp, a.qc_threshold,
                                                  qc_audio_fault(gen_text, wav, sr, last_end))
                if score > best_score: best_score, best_wav = score, wav
                if ok:
                    qc_note_rate(gen_text, wav, sr)
                    if att > 0: print(f"  QC frase {si+1}: OK recall={rec:.2f} (intento {att+1})", flush=True)
                    break
                det = f"recall={rec:.2f}" + (f", alucina ({motivo})" if motivo else f" < {a.qc_threshold:.2f}")
                print(f"  QC frase {si+1}: {det}, regenerando ({att+1}/{attempts})", flush=True)
            wav = best_wav; dur = len(wav) / sr
            cues.append({"i": si, "start": round(t, 3), "end": round(t + dur, 3), "text": s})
            gap = int(PAUSES[chunks[si][1]] * sr)
            pending.append(wav); pending.append(np.zeros(gap, dtype="float32"))
            t += dur + gap / sr
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
    g.add_argument("--nfe", type=int, default=40)
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
