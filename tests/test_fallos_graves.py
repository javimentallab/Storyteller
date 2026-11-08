# -*- coding: utf-8 -*-
"""Los fallos graves del análisis del 7-nov-2026 (perder libros, colgarse, traducir de menos) y la
lectura de números largos, cada uno con su caso.

    python -m unittest tests.test_fallos_graves -v
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
import st_core as C
import cc_engine as E

QT = importlib.util.find_spec("PySide6") is not None
N2W = importlib.util.find_spec("num2words") is not None
BS4 = importlib.util.find_spec("bs4") is not None and importlib.util.find_spec("lxml") is not None
DOCX = importlib.util.find_spec("docx") is not None
FFMPEG = shutil.which("ffmpeg") is not None


def tmpdir(test):
    d = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, d, True)
    return d


class Biblioteca(unittest.TestCase):
    def test_un_libro_nuevo_no_pisa_otro_con_el_mismo_nombre(self):      # fallo 1
        books = tmpdir(self)
        self.assertEqual(C.free_book_name(books, "libro"), "libro")
        (books / "libro").mkdir()
        (books / "libro-2").mkdir()
        self.assertEqual(C.free_book_name(books, "libro"), "libro-3")

    def test_regenerar_aparta_el_libro_y_lo_devuelve(self):             # fallo 2
        home = tmpdir(self)
        dest = home / "books" / "libro"
        dest.mkdir(parents=True)
        (dest / "manifest.json").write_text("[1]", "utf-8")
        (dest / "cap01.mp3").write_text("viejo", "utf-8")
        backup = C.set_aside_book(dest)
        self.assertFalse(dest.exists())
        self.assertEqual(backup, home / ".regen-libro")
        dest.mkdir()
        (dest / "cap01.mp3").write_text("a medias", "utf-8")
        C.restore_book(backup, dest)
        self.assertEqual((dest / "cap01.mp3").read_text("utf-8"), "viejo")
        self.assertEqual((dest / "manifest.json").read_text("utf-8"), "[1]")
        self.assertFalse(backup.exists())


class RegeneracionCortada(unittest.TestCase):
    """La app se corta de golpe a mitad de regenerar: se resuelve al arrancar."""

    def libro(self, d, audio, indice=None):
        d.mkdir(parents=True)
        (d / "cap01.mp3").write_text(audio, "utf-8")
        if indice is not None:
            (d / "manifest.json").write_text(indice, "utf-8")

    def test_al_arrancar(self):
        home = tmpdir(self)
        books = home / "books"
        viejo = '[{"id": 1, "n": 9, "done": true}]'
        self.libro(home / ".regen-nada", "viejo", viejo)
        self.libro(books / "nada", "nuevo")
        self.libro(home / ".regen-vacio", "viejo", viejo)
        self.libro(books / "vacio", "nuevo", '[{"id": 1, "n": 0}]')
        self.libro(home / ".regen-medias", "viejo", viejo)
        self.libro(books / "medias", "nuevo", '[{"id": 1, "n": 2, "done": false}]')
        self.libro(home / ".regen-sin-carpeta", "viejo", viejo)
        C.recover_regenerations(books)
        for nombre, audio in (("nada", "viejo"), ("vacio", "viejo"), ("medias", "nuevo"), ("sin-carpeta", "viejo")):
            self.assertEqual((books / nombre / "cap01.mp3").read_text("utf-8"), audio, nombre)
            self.assertFalse((home / f".regen-{nombre}").exists(), nombre)


@unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
class CerrarCapitulo(unittest.TestCase):
    def test_si_ffmpeg_falla_no_se_da_por_hecho(self):                  # fallo 4
        d = tmpdir(self)
        mp3 = d / "cap01.mp3"
        mp3.write_text("lo de antes", "utf-8")
        with self.assertRaises(RuntimeError):
            E._encode_mp3(str(d / "no-existe.wav"), str(mp3))
        self.assertEqual(mp3.read_text("utf-8"), "lo de antes")      # el mp3 de antes, intacto
        self.assertFalse((d / "cap01.mp3.tmp").exists())
        with self.assertRaises(RuntimeError):
            E._encode_mp3(str(d / "no-existe.wav"), str(mp3), append=True)
        self.assertEqual(mp3.read_text("utf-8"), "lo de antes")


@unittest.skipUnless(N2W, "hace falta num2words")
class NumerosLargos(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, E, "NUM_LANG", "es")
        E.NUM_LANG = "es"

    def test_isbn_y_telefonos_cifra_a_cifra(self):
        t = E.normalize_numbers("ISBN 978-84-376-0494-7 y el 912345678.")
        self.assertEqual(t, "ISBN nueve siete ocho, ocho cuatro, tres siete seis, cero cuatro nueve cuatro, siete "
                            "y el nueve uno dos tres cuatro cinco seis siete ocho.")
        self.assertNotIn("billones", E.normalize_numbers("9788437604947"))

    def test_lo_demas_como_siempre(self):
        self.assertEqual(E.normalize_numbers("2026-11-08"), "dos mil veintiséis-once-ocho")
        self.assertEqual(E.normalize_numbers("25000000"), "veinticinco millones")
        self.assertEqual(E.normalize_numbers("1999 y 3,14"), "mil novecientos noventa y nueve y tres coma uno cuatro")
        self.assertEqual(E.normalize_numbers("1990-2000"), "mil novecientos noventa-dos mil")

    def test_una_frase_con_isbn_cabe_en_xtts(self):
        # 170 caracteres escrita, 285 con los números en letra: antes iba en un trozo y XTTS (tope 239
        # en español) se comía el final. Ahora se trocea midiendo lo que se va a decir.
        frase = ("Esta edición, con ISBN 9788437604947 y depósito legal 12345678, se imprimió en "
                 "Madrid en el año 1999 por encargo de la editorial, que la distribuyó en toda España.")
        trozos = E.split_sentences(frase)
        self.assertGreater(len(trozos), 1)
        self.assertEqual(" ".join(trozos), frase)                    # no se pierde ni se repite nada
        for t in trozos:
            self.assertLessEqual(len(E.normalize_text(t)), 239, t)

    def test_un_trozo_sin_numeros_no_cambia(self):
        frase = "Una frase normal, sin cifras, que cabe de sobra en un trozo."
        self.assertEqual(E.split_sentences(frase), [frase])

    def test_sin_num2words_para_el_idioma_quedan_las_cifras(self):
        E.NUM_LANG = "xx"
        self.assertEqual(E.normalize_numbers("978-84-376-0494-7"), "978-84-376-0494-7")


@unittest.skipUnless(BS4, "hace falta beautifulsoup4 y lxml")
class TraducirEpub(unittest.TestCase):                                   # fallo 14
    def doc(self, html):
        import cc_translate as T
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "xml")
        return T, T.EpubDoc.__new__(T.EpubDoc), soup.find("p")

    HTML = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="p1">Usa la orden <code>ls -la</code> y '
            '<a id="ancla"></a>sigue <span id="s2">aquí</span>.</p></body></html>')

    def test_el_codigo_y_las_anclas_no_se_pierden(self):
        T, d, p = self.doc(self.HTML)
        text, held = d._block_parts(p)
        self.assertEqual(text, "Usa la orden ⟦0⟧ y sigue aquí.")
        d._set_block(p, "Use the command ⟦0⟧ and go on here.", held)
        s = str(p)
        self.assertIn("Use the command <code>ls -la</code> and go on here.", s)
        self.assertIn('id="ancla"', s)
        self.assertIn('id="s2"', s)
        self.assertIn('id="p1"', s)

    def test_si_el_traductor_se_come_la_marca_el_codigo_va_al_final(self):
        T, d, p = self.doc(self.HTML)
        text, held = d._block_parts(p)
        d._set_block(p, "Use the command and go on here.", held)
        self.assertTrue(str(p).replace("\n", "").endswith("<code>ls -la</code></p>"))

    def test_el_prompt_solo_cambia_si_hay_marcas(self):
        import cc_translate as T
        self.assertNotIn("⟦", T.build_prompt("Hola.", "es", "en"))
        self.assertIn("Keep every marker", T.build_prompt("Usa ⟦0⟧.", "es", "en"))


@unittest.skipUnless(DOCX, "hace falta python-docx")
class TraducirDocx(unittest.TestCase):                                   # fallo 13
    def test_no_se_salta_ningun_parrafo(self):
        import docx
        import cc_translate as T
        path = tmpdir(self) / "doc.docx"
        # Con párrafos vacíos entre medias: esos no se guardan, Python reutilizaba su id() y el párrafo
        # con texto que caía en el mismo id se daba por «ya visto».
        # Así, con id(), no se traducía NINGUNO de los 300 (comprobado el 8-nov).
        d = docx.Document()
        for i in range(300):
            for _ in range(3):
                d.add_paragraph("")
            d.add_paragraph(f"Este es el párrafo número {i} del documento.")
        d.save(path)
        self.assertEqual(len(T.DocxDoc(str(path), "en").units), 300)


# Se ejecuta aparte, con su propia carpeta de usuario y los procesos simulados.
PROBE = r"""
import json, shutil, sys, types
from pathlib import Path
root = sys.argv[1]
sys.path.insert(0, root)
sys.argv = ["storyteller.py", "--lang", "es"]
import storyteller as S
out = {}

# fallo 8: un config.json roto se aparta y se avisa, en vez de pisarlo con lo de fábrica sin decir nada
cfg = S.load_config()
out["config"] = {"engine": cfg["engine"], "apartado": [Path(p).read_text("utf-8") for p in S.CONFIG_UNREADABLE],
                 "sigue": S.CONFIG.exists()}
S.save_config(dict(cfg, voice="otra"))
out["config"]["guardado"] = json.loads(S.CONFIG.read_text("utf-8"))["voice"]

# fallo 12: una sola ventana
l1 = S.single_instance_lock(wait_ms=0)
out["candado"] = [l1 is not None, S.single_instance_lock(wait_ms=0) is None]
l1.unlock()
out["candado"].append(S.single_instance_lock(wait_ms=0) is not None)

def proc(lines=(), rc=0, on_end=None, count_out=""):
    def it():
        for l in lines:
            yield l
        if on_end:
            on_end()
    p = types.SimpleNamespace(stdout=it(), returncode=rc, poll=lambda: rc, terminate=lambda: None, kill=lambda: None)
    p.wait = lambda *a: rc
    p.communicate = lambda *a, **k: (count_out, None)
    return p

def old_book(name="libro"):
    d = S.LOCAL_BOOKS / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "manifest.json").write_text('[{"id": 1, "base": "cap01", "n": 3, "done": true}]', "utf-8")
    (d / "cap01.mp3").write_text("viejo", "utf-8")
    (d / "source.epub").write_text("epub", "utf-8")
    return d

def regen(gen):
    d = old_book()
    seen = []
    def popen(cmd, *a, **k):
        if "count" in cmd:
            return proc(count_out="CHAPTERS 1\nSENTS 3\n")
        seen.append(1)
        return gen(d)
    S.subprocess.Popen = popen
    w = S.LocalGenWorker(str(d / "source.epub"), S.model_by_id("xtts-v2"), None, name="libro")
    res = {"failed": [], "done": [], "canceled": 0}
    w.failed.connect(res["failed"].append); w.done.connect(res["done"].append)
    w.canceled.connect(lambda: res.__setitem__("canceled", res["canceled"] + 1))
    return w, d, res, seen

# fallos 2 y 3: el motor se cae sin narrar nada al regenerar -> aviso y vuelve el libro de antes
w, d, res, seen = regen(lambda d: proc(["capitulos: 1\n", "RuntimeError: ffmpeg no pudo\n"], rc=1))
w.run()
out["cae_sin_nada"] = {"failed": res["failed"], "done": res["done"], "mp3": (d / "cap01.mp3").read_text("utf-8"),
                       "backup": (S.LOCAL_BOOKS.parent / ".regen-libro").exists()}

# fallo 3: se cae a mitad -> aviso, lo narrado se queda y su WAV también (para reanudar)
def mitad(d):
    def write():
        (d / "manifest.json").write_text('[{"id": 1, "base": "cap01", "n": 1, "done": false}]', "utf-8")
        (d / "cap01.wav").write_text("a medias", "utf-8")
    return proc(["capitulos: 1\n", "  frase 1/3 1.0s\n", "Killed\n"], rc=137, on_end=write)
w, d, res, seen = regen(mitad)
w.run()
out["cae_a_mitad"] = {"failed": res["failed"], "done": res["done"], "wav": (d / "cap01.wav").exists(),
                      "backup": (S.LOCAL_BOOKS.parent / ".regen-libro").exists()}

# fallo 10: Cancelar mientras cuenta las frases -> no arranca y vuelve el libro de antes
w, d, res, seen = regen(lambda d: proc(["capitulos: 1\n"]))
def popen_count(cmd, *a, **k):
    if "count" in cmd:
        w.mode = "cancel"
        return proc(count_out="")
    seen.append(1)
    return proc()
S.subprocess.Popen = popen_count
w.run()
out["cancelar_contando"] = {"canceled": res["canceled"], "arrancado": len(seen),
                            "mp3": (d / "cap01.mp3").read_text("utf-8")}

# fallo 5: Whisper local se cae a mitad -> no se da por completo
d = S.LOCAL_BOOKS / "audio"; d.mkdir(parents=True, exist_ok=True)
mdir = S.LOCAL_BOOKS.parent / "wm"; mdir.mkdir(exist_ok=True); (mdir / "model.bin").write_text("x", "utf-8")
S.whisper_python = lambda: "py"
S.model_dir = lambda mid: mdir
S.subprocess.Popen = lambda *a, **k: proc(["whisper cargado (cpu)\n", 'SEG {"start": 0, "end": 5, "text": "hola"}\n'], rc=1)
iw = S.ImportWorker(src="x.mp3")
ok, msg, covered = iw._transcribe_local(d, d / "cap01.mp3", "Audio", 100.0, [], 0.0)
out["whisper_cae"] = [ok, msg, covered]

# fallo 6: Cancelar al reanudar una transcripción no borra el libro
(d / "cap01.mp3").write_text("audio", "utf-8")
(d / "cap01.json").write_text(json.dumps({"title": "Audio", "duration": 100, "cues": [{"i": 0, "start": 0, "end": 5, "text": "hola"}]}), "utf-8")
(d / "gen_state.json").write_text(json.dumps({"total": 1, "complete": False, "kind": "import", "covered": 5, "dur": 100}), "utf-8")
iw = S.ImportWorker(resume_dir=str(d))
def cancel_local(*a):
    iw.mode = "cancel"
    return (True, "", a[5])
iw._transcribe_local = cancel_local
res = {"canceled": 0}
iw.canceled.connect(lambda: res.__setitem__("canceled", 1))
iw.run()
out["cancelar_reanudando"] = {"canceled": res["canceled"], "sigue": d.exists(),
                              "cues": len(json.loads((d / "cap01.json").read_text("utf-8"))["cues"])}

# fallo 9: si ffmpeg no puede convertir la voz, la que ya había con ese nombre sigue entera
S.VOICES_DIR.mkdir(parents=True, exist_ok=True)
(S.VOICES_DIR / "mi-voz.wav").write_text("la de antes", "utf-8")
S.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=1)
try:
    S.ModelsScreen._voice_save(None, "no-existe.mp3", "Mi voz", "texto")
    out["voz"] = "sin error"
except RuntimeError:
    out["voz"] = (S.VOICES_DIR / "mi-voz.wav").read_text("utf-8")

print("JSON:" + json.dumps(out, ensure_ascii=False))
"""


@unittest.skipUnless(QT, "hace falta PySide6")
class App(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = Path(tempfile.mkdtemp())
        cls.home = home
        app = home / "cuentacuentos"
        app.mkdir()
        (app / "config.json").write_text('{"engine": "local", "voi', "utf-8")   # cortado a mitad
        env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), QT_QPA_PLATFORM="offscreen",
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="-1",
                   STORYTELLER_LANG="es")
        r = subprocess.run([sys.executable, "-c", PROBE, str(ROOT)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=home, timeout=180)
        cls.stderr = r.stderr
        cls.out = json.loads(r.stdout.split("JSON:", 1)[1]) if "JSON:" in r.stdout else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, True)

    def setUp(self):
        self.assertIsNotNone(self.out, self.stderr[-3000:])

    def test_config_rota(self):
        c = self.out["config"]
        self.assertEqual(c["engine"], "local")
        self.assertEqual(c["apartado"], ['{"engine": "local", "voi'])
        self.assertEqual(c["guardado"], "otra")

    def test_una_sola_ventana(self):
        self.assertEqual(self.out["candado"], [True, True, True])

    def test_el_motor_se_cae_sin_narrar_nada(self):
        c = self.out["cae_sin_nada"]
        self.assertEqual(c["done"], [])
        self.assertEqual(len(c["failed"]), 1)
        self.assertIn("ffmpeg no pudo", c["failed"][0])
        self.assertEqual(c["mp3"], "viejo")
        self.assertFalse(c["backup"])

    def test_el_motor_se_cae_a_mitad(self):
        c = self.out["cae_a_mitad"]
        self.assertEqual(c["done"], [])
        self.assertIn("Killed", c["failed"][0])
        self.assertIn("puedes reanudar", c["failed"][0])
        self.assertTrue(c["wav"])
        self.assertFalse(c["backup"])

    def test_cancelar_mientras_cuenta(self):
        self.assertEqual(self.out["cancelar_contando"], {"canceled": 1, "arrancado": 0, "mp3": "viejo"})

    def test_whisper_se_cae_a_mitad(self):
        ok, msg, covered = self.out["whisper_cae"]
        self.assertFalse(ok)
        self.assertIn("Reanudar", msg)
        self.assertEqual(covered, 5)

    def test_cancelar_al_reanudar_una_transcripcion(self):
        self.assertEqual(self.out["cancelar_reanudando"], {"canceled": 1, "sigue": True, "cues": 1})

    def test_voz_que_no_se_puede_convertir(self):
        self.assertEqual(self.out["voz"], "la de antes")


if __name__ == "__main__":
    unittest.main()
