# -*- coding: utf-8 -*-
"""Página por la que va la escucha (st_pages) y la regla de seguir la línea que suena.

El libro de muestra y lo que se espera de él son los mismos que en las pruebas del móvil
(PagesTest.kt): los dos cuentan igual.

    python -m unittest tests.test_paginas -v
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
import st_pages as P

QT = importlib.util.find_spec("PySide6") is not None
BS4 = importlib.util.find_spec("bs4") is not None and importlib.util.find_spec("lxml") is not None

XHTML = ('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>No se lee</title></head><body>{0}</body></html>')
CAP1 = ('<p>Autor Primero</p><h1>Capítulo uno</h1>'
        '<p><span epub:type="pagebreak" id="p1" title="1"/>Érase una vez, en un lugar muy lejano, una niña que leía.</p>'
        '<p>Leía de día y de noche.<span epub:type="pagebreak" id="p2" title="2"/> Y nunca se cansaba de leer.</p>')
CAP2 = ('<h2>Dos</h2><p>El segundo capítulo empieza aquí, con otra historia.</p>'
        '<p><span role="doc-pagebreak" id="p3">3</span>La historia sigue en la página tres y acaba.</p>')
PAGE_LIST = ('<nav epub:type="page-list"><ol><li><a href="Text/cap1.xhtml#p1">1</a></li>'
             '<li><a href="Text/cap1.xhtml#p2">2</a></li><li><a href="Text/cap2.xhtml#p3">3</a></li></ol></nav>')
NCX_LIST = ('<pageList><pageTarget id="t1" value="1" type="normal"><navLabel><text>1</text></navLabel>'
            '<content src="Text/cap1.xhtml#p1"/></pageTarget><pageTarget id="t2" value="2" type="normal">'
            '<navLabel><text>2</text></navLabel><content src="Text/cap1.xhtml#p2"/></pageTarget>'
            '<pageTarget id="t3" value="3" type="normal"><navLabel><text>3</text></navLabel>'
            '<content src="Text/cap2.xhtml#p3"/></pageTarget></pageList>')

# Las frases tal como podría trocearlas el motor (el cálculo no depende de cómo).
CUES1 = ["Autor Primero.", "Érase una vez, en un lugar muy lejano,", "una niña que leía.",
         "Leía de día y de noche.", "Y nunca se cansaba de leer."]
CUES2 = ["El segundo capítulo empieza aquí,", "con otra historia.", "3 La historia sigue en la página tres y acaba."]
OFFS1 = [0, 12, 41, 55, 72]      # «AutorPrimero» = 12 letras; el título del capítulo no cuenta
OFFS2 = [93, 121, 136]
TOTAL = 172


def sample_epub(path, variant="nav"):
    """variant: nav (page-list de EPUB3), ncx (solo el pageList de EPUB2), inline (solo las
    marcas en el texto), none (sin numeración)."""
    cap1, cap2 = CAP1, CAP2
    if variant == "none":
        cap1 = cap1.replace('<span epub:type="pagebreak" id="p1" title="1"/>', "").replace(
            '<span epub:type="pagebreak" id="p2" title="2"/>', "")
        cap2 = cap2.replace('<span role="doc-pagebreak" id="p3">3</span>', "3")
    nav = '<nav epub:type="toc"><ol><li><a href="Text/cap1.xhtml">Uno</a></li></ol></nav>'
    if variant == "nav":
        nav += PAGE_LIST
    ncx = ('<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           '<navMap><navPoint id="n1"><navLabel><text>Uno</text></navLabel><content src="Text/cap1.xhtml"/></navPoint>'
           '</navMap>' + (NCX_LIST if variant in ("nav", "ncx") else "") + '</ncx>')
    opf = ('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Muestra</dc:title></metadata><manifest>'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           '<item id="c1" href="Text/cap1.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="c2" href="Text/cap2.xhtml" media-type="application/xhtml+xml"/>'
           '</manifest><spine toc="ncx"><itemref idref="nav"/><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", XHTML.format(nav))
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/Text/cap1.xhtml", XHTML.format(cap1))
        z.writestr("OEBPS/Text/cap2.xhtml", XHTML.format(cap2))
    return path


class Paginas(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp, True)

    def book(self, variant, **kw):
        return P.from_epub(sample_epub(self.tmp / f"{variant}.epub", variant), **kw)

    def test_letras_cuentan_igual_en_todas_partes(self):
        self.assertEqual(P.letters("¿Sí? «Ñu», 3,5 €… 名前。"), "SíÑu35名前")
        self.assertEqual(P.letters("x² ½ _ -"), "x")        # ni superíndices ni fracciones: el móvil tampoco

    def test_numeracion_del_libro_impreso(self):
        for variant in ("nav", "ncx", "inline"):
            with self.subTest(variant=variant):
                bp = self.book(variant)
                self.assertTrue(bp.real)
                self.assertEqual(bp.total, 3)
                self.assertEqual(bp.total_letters, TOTAL)
                o1 = bp.chapter_offsets(1, CUES1); o2 = bp.chapter_offsets(2, CUES2)
                self.assertEqual(o1, OFFS1); self.assertEqual(o2, OFFS2)
                self.assertEqual([bp.page(o) for o in o1], ["1", "1", "1", "1", "2"])
                self.assertEqual([bp.page(o) for o in o2], ["2", "2", "3"])
                self.assertEqual(bp.percent(o2[0]), 54)

    def test_sin_numeracion_paginas_aproximadas_y_fijas(self):
        bp = self.book("none", per_page=50)
        self.assertFalse(bp.real)
        self.assertEqual(bp.total_letters, TOTAL)
        self.assertEqual(bp.total, 4)                       # 172 letras / 50
        o2 = bp.chapter_offsets(2, CUES2)
        self.assertEqual(o2, OFFS2)
        self.assertEqual([bp.page(o) for o in o2], ["2", "3", "3"])
        # Otro troceo de las mismas frases cae en las mismas páginas.
        otro = bp.chapter_offsets(2, ["El segundo capítulo empieza aquí, con otra historia.",
                                      "3 La historia sigue", "en la página tres y acaba."])
        self.assertEqual(otro[0], OFFS2[0])
        self.assertEqual(bp.page(otro[1]), "3")
        self.assertEqual(P.from_epub(self.tmp / "none.epub").total, 1)   # 1.500 letras por página

    def test_capitulo_que_no_esta_en_el_epub(self):
        bp = self.book("nav")
        self.assertIsNone(bp.chapter_offsets(1, ["Nada de esto está en el libro."]))
        self.assertIsNone(bp.chapter_offsets(1, []))

    def test_frase_que_no_cuadra_no_descoloca_las_siguientes(self):
        bp = self.book("nav")
        o = bp.chapter_offsets(1, [CUES1[0], "Texto cambiado que no está", *CUES1[1:]])
        self.assertEqual(o[0], 0)
        self.assertEqual(o[2:], OFFS1[1:])

    def test_sin_epub_con_los_subtitulos(self):
        d = self.tmp / "importado"; d.mkdir()
        cues = [{"start": 0, "end": 1, "text": "a" * 30}, {"start": 1, "end": 2, "text": "b" * 40}]
        (d / "cap01.json").write_text(json.dumps({"cues": cues}), "utf-8")
        bp = P.book_pages(d, [{"id": 1, "base": "cap01"}])
        self.assertFalse(bp.real)
        self.assertEqual(bp.total_letters, 70)
        self.assertEqual(bp.chapter_offsets(1, [c["text"] for c in cues]), [0, 30])

    def test_con_epub_lo_usa(self):
        d = self.tmp / "libro"; d.mkdir()
        sample_epub(d / "source.epub", "inline")
        self.assertTrue(P.book_pages(d, []).real)

    def test_epub_roto(self):
        (self.tmp / "roto.epub").write_bytes(b"no es un zip")
        self.assertIsNone(P.from_epub(self.tmp / "roto.epub"))

    @unittest.skipUnless(BS4, "hace falta el venv del motor (bs4 y lxml)")
    def test_con_el_troceo_del_motor(self):
        """Las frases que saca el motor de verdad caen todas seguidas, sin huecos ni saltos."""
        import cc_engine as E
        for variant in ("nav", "none"):
            f = sample_epub(self.tmp / f"m-{variant}.epub", variant)
            bp = P.from_epub(f)
            for i, ch in enumerate(E.chapters_from_epub(f), 1):
                sents = E.split_sentences(ch["text"])
                offs = bp.chapter_offsets(i, sents)
                self.assertIsNotNone(offs)
                for a, b, s in zip(offs, offs[1:], sents):
                    self.assertEqual(b - a, len(P.letters(s)))


class Seguir(unittest.TestCase):
    def test_desplazar_a_mano_para_y_vuelve_solo(self):
        f = P.Follow(pause=5.0)
        self.assertTrue(f.following(100.0))
        f.touched(100.0)
        self.assertFalse(f.following(101.0))
        self.assertAlmostEqual(f.wait(101.0), 4.0)
        f.touched(104.0)                                    # sigue tocando: cuenta desde el último toque
        self.assertFalse(f.following(108.9))
        self.assertTrue(f.following(109.0))
        self.assertEqual(f.wait(109.0), 0.0)

    def test_saltar_vuelve_a_seguir_al_momento(self):
        f = P.Follow(); f.touched(10.0); f.reset()
        self.assertTrue(f.following(10.1))

    def test_pausa_de_cinco_segundos(self):
        self.assertEqual(P.FOLLOW_PAUSE, 5.0)


# Ventana de verdad (sin mostrarse ni sonar): página en la barra y volver solo a lo que suena.
PROBE = r"""
import json, sys, time
from pathlib import Path
root, book = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
sys.argv = ["storyteller.py", "--lang", "es"]
import storyteller as S
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication.instance() or QApplication([])
p = S.PlayerPage(); p.setAttribute(Qt.WA_DontShowOnScreen); p.resize(700, 300); p.show()
p.player.play = lambda: None                                   # que no suene nada
p._follow.pause = 0.3; p._followTimer.setInterval(300)
p.load_book(book)
end = time.monotonic() + 10
while p._pages is None and time.monotonic() < end:
    app.processEvents(); time.sleep(0.02)
app.processEvents()
out = {"real": p._pages.real if p._pages else None}
p._last_pos = 0; p.highlight(0.5); out["pag1"] = p.pageLbl.text(); out["tip1"] = p.pageLbl.toolTip()
p.highlight(4.5); out["pag2"] = p.pageLbl.text()
app.processEvents()                                            # acaba el desplazamiento automático
p.on_user_scroll(0)                                            # desplazó a mano
out["sigue_tras_tocar"] = p._follow.following(time.monotonic())
end = time.monotonic() + 3
while not p._follow.following(time.monotonic()) and time.monotonic() < end:
    app.processEvents(); time.sleep(0.02)
out["vuelve_solo"] = p._follow.following(time.monotonic())
p.on_user_scroll(0); p.seek_to(0.0)
out["saltar_sigue"] = p._follow.following(time.monotonic())
p.stop()
print("JSON:" + json.dumps(out, ensure_ascii=False))
"""


@unittest.skipUnless(QT, "hace falta PySide6")
class Ventana(unittest.TestCase):
    def test_pagina_y_volver_a_lo_que_suena(self):
        home = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, home, True)
        book = home / "cuentacuentos" / "books" / "muestra"; book.mkdir(parents=True)
        sample_epub(book / "source.epub", "nav")
        cues = [{"i": i, "start": float(i), "end": i + 1.0, "text": t} for i, t in enumerate(CUES1)]
        (book / "cap01.json").write_text(json.dumps({"title": "Uno", "duration": 5.0, "cues": cues}), "utf-8")
        (book / "manifest.json").write_text(json.dumps([{"id": 1, "base": "cap01", "title": "Uno", "n": 5,
                                                         "duration": 5.0, "done": True}]), "utf-8")
        (book / "gen_state.json").write_text(json.dumps({"total": 2, "complete": True}), "utf-8")
        env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
                   PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="-1", STORYTELLER_LANG="es")
        r = subprocess.run([sys.executable, "-c", PROBE, str(ROOT), str(book)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=home, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2500:])
        out = json.loads(r.stdout.split("JSON:", 1)[1])
        self.assertTrue(out["real"])
        self.assertEqual(out["pag1"], "Página 1 de 3 · 0 % del libro")
        self.assertIn("libro impreso", out["tip1"])
        self.assertEqual(out["pag2"], "Página 2 de 3 · 41 % del libro")
        self.assertFalse(out["sigue_tras_tocar"])
        self.assertTrue(out["vuelve_solo"])
        self.assertTrue(out["saltar_sigue"])


if __name__ == "__main__":
    unittest.main()
