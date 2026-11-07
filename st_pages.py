"""
Página por la que va la escucha, y la regla de seguir la línea que suena.

Página = la del libro impreso cuando el EPUB la trae (page-list del nav de EPUB3, pageList
del NCX de EPUB2 o marcas epub:type="pagebreak" / role="doc-pagebreak"). Si no la trae,
páginas fijas de PAGE_LETTERS letras y cifras contadas desde el principio del libro,
marcadas como aproximadas: no cambian aunque cambie el troceo o se narre de otra manera.

Las frases (cues) se sitúan en el EPUB por sus letras y cifras, sin espacios ni signos: así
da igual cómo se trocee el texto o qué puntuación añada el motor. Solo biblioteca estándar:
la ventana no tiene bs4. El móvil hace lo mismo en engine/Pages.kt; las pruebas de los dos
usan el mismo libro de muestra y esperan lo mismo.
"""
import bisect, json, math, posixpath, re, zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

PAGE_LETTERS = 1500      # letras y cifras por página aproximada (~1.800 caracteres con espacios)
FOLLOW_PAUSE = 5.0       # s sin tocar el texto tras desplazarlo a mano: vuelve a lo que suena
_KEY_LETTERS = 40        # letras del principio de un capítulo para encontrarlo en el EPUB
_SLACK = 400             # cuánto puede adelantarse una frase de donde se la esperaba


def letters(text):
    """Solo letras (cualquier alfabeto) y cifras decimales: lo que cuenta igual en todas partes."""
    return "".join(c for c in text or "" if c.isalpha() or c.isdecimal())


class Follow:
    """Seguir la línea que suena. Desplazar a mano deja de seguir; a los `pause` segundos sin
    volver a tocar, sigue solo otra vez. Saltar a una frase, a una posición o a otro
    capítulo vuelve a seguir al momento (reset)."""

    def __init__(self, pause=FOLLOW_PAUSE):
        self.pause = pause
        self._touched = None

    def touched(self, now):
        self._touched = now

    def reset(self):
        self._touched = None

    def following(self, now):
        return self._touched is None or now - self._touched >= self.pause

    def wait(self, now):
        """Segundos que faltan para volver a seguir (0 = ya sigue)."""
        return 0.0 if self.following(now) else self.pause - (now - self._touched)


# ----------------------------------------------------------------- lectura del EPUB
def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


class _Stream(HTMLParser):
    """Letras de un documento, dónde está cada id y las marcas de página que lleve. Como
    chapters_from_epub, sin <head>, scripts ni estilos, y sin el primer h1/h2/h3, que es el
    título del capítulo y no va en su texto (puede ir en medio: «Autor · Título · ePub r1.0»)."""
    SKIP = {"head", "script", "style"}           # <title> fuera de <head> (SVG) sí se narra
    HEADS = {"h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf = []; self.n = 0; self.skip = 0
        self.ids = {}; self.breaks = []          # [[posición, etiqueta, id]]
        self._cap = None                         # [etiqueta html, anidado, textos] de una marca sin título
        self._title = None                       # [etiqueta, anidado] mientras se lee el título; "" = ya pasó

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1; return
        if self.skip:
            return
        if self._title is None and tag in self.HEADS:
            self._title = [tag, 0]
        if self._title and tag == self._title[0]:
            self._title[1] += 1
        a = {k: (v or "") for k, v in attrs}
        if self._cap and tag == self._cap[0]:
            self._cap[1] += 1
        i = a.get("id")
        if i and i not in self.ids:
            self.ids[i] = self.n
        kinds = (a.get("epub:type", "") + " " + a.get("role", "")).split()
        if "pagebreak" in kinds or "doc-pagebreak" in kinds:
            lab = (a.get("title") or a.get("aria-label") or "").strip()
            self.breaks.append([self.n, lab, i])
            if not lab and self._cap is None:
                self._cap = [tag, 1, [], len(self.breaks) - 1]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1); return
        if self._title and tag == self._title[0]:
            self._title[1] -= 1
            if self._title[1] <= 0:
                self._title = ""
        c = self._cap
        if c and tag == c[0]:
            c[1] -= 1
            if c[1] <= 0:
                self.breaks[c[3]][1] = _label("".join(c[2]))
                self._cap = None

    def handle_data(self, data):
        if self.skip:
            return
        if self._cap:
            self._cap[2].append(data)
        if self._title:
            return                               # el título del capítulo no está en su texto
        s = letters(data)
        self.buf.append(s); self.n += len(s)

    def text(self):
        return "".join(self.buf)


def _label(s):
    """«23», «xii»; «Página 23» → «23». Una marca vacía no cuenta."""
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) > 8:
        m = re.findall(r"\d+", s)
        s = m[-1] if m else s[:8]
    return s


def _stream(data):
    p = _Stream()
    try:
        p.feed(data.decode("utf-8", "replace")); p.close()
    except Exception:
        pass
    return p


class _NavList(HTMLParser):
    """Enlaces del <nav epub:type="page-list"> de EPUB3: [(href, etiqueta)]."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0; self.out = []; self._a = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "nav":
            if self.depth:
                self.depth += 1
            elif "page-list" in a.get("epub:type", "").split() or "doc-pagelist" in a.get("role", "").split():
                self.depth = 1
        elif tag == "a" and self.depth:
            self._a = [a.get("href", ""), []]

    def handle_endtag(self, tag):
        if tag == "nav" and self.depth:
            self.depth -= 1
        elif tag == "a" and self._a is not None:
            self.out.append((self._a[0], _label("".join(self._a[1]))))
            self._a = None

    def handle_data(self, data):
        if self._a is not None:
            self._a[1].append(data)


def _resolve(base_file, href):
    """Ruta dentro del zip y fragmento de un enlace escrito en base_file."""
    href = unquote(href or "")
    path, _, frag = href.partition("#")
    if not path:
        return base_file, frag
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_file), path)), frag


class BookPages:
    """Páginas de un libro. offsets: letras desde el principio del libro."""

    def __init__(self, real, total_letters, markers=(), docs=(), per_page=PAGE_LETTERS):
        self.real = real
        self.per_page = per_page                          # numeración del libro impreso
        self.total_letters = max(0, total_letters)
        self._marks = sorted(markers, key=lambda m: m[0])   # [(posición, etiqueta)]
        self._pos = [m[0] for m in self._marks]
        self.docs = list(docs)                    # [(inicio, letras)] de lo que se narra, en orden
        nums = [int(lab) for _, lab in self._marks if lab.isdigit()]
        if real:
            self.total = max(nums) if nums else len(self._marks)
        else:
            self.total = max(1, math.ceil(self.total_letters / per_page))

    def page(self, offset):
        """Etiqueta de la página en la que cae esa posición."""
        if self.real:
            i = bisect.bisect_right(self._pos, offset) - 1
            return self._marks[max(0, i)][1]
        return str(min(self.total, int(max(0, offset) // self.per_page) + 1))

    def percent(self, offset):
        if not self.total_letters:
            return 0
        return max(0, min(100, int(offset * 100 / self.total_letters)))

    def chapter_offsets(self, chap_id, texts):
        """Posición en el libro del principio de cada frase de un capítulo, o None si no lo
        encuentra. chap_id = nº de sección del manifest (la que lleva el capítulo)."""
        if not self.docs:
            return None
        keys = [letters(t) for t in texts]
        # El capítulo n es el n-ésimo documento con texto; con secciones muy cortas que el motor
        # salta, puede ir algo más adelante: gana el documento que lo contiene más cercano.
        try:
            hint = int(chap_id) - 1
        except (TypeError, ValueError):
            hint = 0

        def locate(key):
            best = None
            for k, (_start, txt) in enumerate(self.docs):
                p = txt.find(key)
                if p >= 0 and (best is None or abs(k - hint) < abs(best[0] - hint)):
                    best = (k, p)
            return best

        key = "".join(keys)[:_KEY_LETTERS]
        best = locate(key) if key else None
        before = 0
        for j, k in enumerate(keys[:8]):          # si el principio no cuadra, por alguna de las primeras frases
            if best is not None:
                break
            if len(k) >= 10:
                found = locate(k[:_KEY_LETTERS])
                if found is not None:
                    best = (found[0], max(0, found[1] - before))
            before += len(k)
        if best is None:
            return None
        start, txt = self.docs[best[0]]
        cur = best[1]; lost = 0; out = []        # lost = letras de frases seguidas que no cuadran
        for k in keys:
            p = txt.find(k, cur, cur + lost + len(k) + _SLACK) if k else -1
            if p < 0:
                # No está tal cual (otro texto, un subtítulo transcrito): se estima detrás de la
                # anterior y se sigue buscando desde la última que cuadró.
                out.append(start + cur + lost); lost += len(k)
            else:
                out.append(start + p); cur = p + len(k); lost = 0
        return out


def from_epub(path, per_page=PAGE_LETTERS):
    """BookPages de un EPUB, o None si no se puede leer."""
    try:
        z = zipfile.ZipFile(path)
        names = set(z.namelist())
        cont = _xml(z.read("META-INF/container.xml"))
        opf = next((e.get("full-path") for e in cont.iter() if _local(e.tag) == "rootfile"), None)
        pkg = _xml(z.read(opf))
    except Exception:
        return None
    if pkg is None:
        return None
    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    items = {}; spine = []; toc_id = None; nav = None; ncx = None
    for e in pkg.iter():
        t = _local(e.tag)
        if t == "item":
            href = unquote(e.get("href") or "")
            items[e.get("id")] = (href, posixpath.normpath(base + href), e.get("media-type") or "", e.get("properties") or "")
        elif t == "spine":
            toc_id = e.get("toc")
        elif t == "itemref":
            spine.append(e.get("idref"))
    for iid, (href, full, mt, props) in items.items():
        if "nav" in props.split():
            nav = full
        if mt == "application/x-dtbncx+xml" or iid == toc_id:
            ncx = ncx or full
    order = [items[i] for i in spine if i in items] or list(items.values())

    def is_doc(mt, href):
        return mt in ("application/xhtml+xml", "text/html") or href.lower().endswith((".xhtml", ".html", ".htm"))

    # Lo que se narra: igual que chapters_from_epub (el índice no se lee).
    streams = {}; docs = []; offset = 0; start_of = {}; inline = []
    for href, full, mt, _p in order:
        if not is_doc(mt, href) or full not in names or full in start_of:
            continue
        start_of[full] = offset
        low = href.lower()
        if "nav" in low or "toc" in low:
            streams[full] = None                  # sus marcas caen donde empieza lo siguiente
            continue
        s = _stream(z.read(full)); streams[full] = s
        txt = s.text()
        if txt:
            docs.append((offset, txt))
        for pos, lab, i in s.breaks:
            inline.append((full, i, offset + pos, lab))
        offset += len(txt)

    def where(full, frag):
        if full not in start_of:
            return None
        s = streams[full]
        if s is None or not frag:
            return start_of[full]
        p = s.ids.get(frag)
        return None if p is None else start_of[full] + p

    targets = []
    if nav and nav in names:
        nl = _NavList()
        try:
            nl.feed(z.read(nav).decode("utf-8", "replace")); nl.close()
        except Exception:
            pass
        targets = [(_resolve(nav, h), lab) for h, lab in nl.out]
    if not targets and ncx and ncx in names:
        root = _xml(z.read(ncx))
        for pt in (root.iter() if root is not None else ()):
            if _local(pt.tag) != "pageTarget":
                continue
            lab = next((x.text or "" for x in pt.iter() if _local(x.tag) == "text"), "") or pt.get("value") or ""
            src = next((x.get("src") for x in pt.iter() if _local(x.tag) == "content"), None)
            if src:
                targets.append((_resolve(ncx, src), _label(lab)))
    marks = []
    if targets:
        own = {(f, i): lab for f, i, _p, lab in inline if i}
        for (full, frag), lab in targets:
            p = where(full, frag)
            lab = lab or own.get((full, frag), "")
            if p is not None and lab:
                marks.append((p, lab))
    else:
        marks = [(p, lab) for _f, _i, p, lab in inline if lab]
    return BookPages(bool(marks), offset, marks, docs, per_page)


def from_cues(chapters, per_page=PAGE_LETTERS):
    """Sin EPUB (audiolibro transcrito): páginas aproximadas con el texto de los subtítulos.
    chapters = [(id, [textos])] en orden."""
    docs = []; offset = 0
    for _cid, texts in chapters:
        txt = "".join(letters(t) for t in texts)
        docs.append((offset, txt)); offset += len(txt)
    return BookPages(False, offset, (), docs, per_page)


def book_pages(book_dir, manifest):
    """Páginas del libro de esa carpeta: con su source.epub si lo tiene, si no con los cues."""
    d = Path(book_dir)
    epub = d / "source.epub"
    if epub.exists():
        bp = from_epub(epub)
        if bp is not None and bp.total_letters:
            return bp
    chaps = []
    for m in manifest or ():
        try:
            data = json.loads((d / f"{m['base']}.json").read_text("utf-8"))
            chaps.append((m.get("id"), [c.get("text", "") for c in data.get("cues", [])]))
        except Exception:
            chaps.append((m.get("id"), []))
    return from_cues(chaps)
