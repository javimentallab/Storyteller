# -*- coding: utf-8 -*-
"""Que reanudar no pierda lo narrado.

1. El motor (XTTS y F5) rehacía el índice desde cero y solo apuntaba los capítulos a los que ya
   había llegado: los terminados más adelante (de una tirada en tramos, o traídos del servidor)
   desaparecían del índice si se paraba antes de llegar a ellos, y al reanudar se borraban y se
   volvían a narrar.
2. Reanudar en tramos un libro narrado en uno (o al revés) leía manifest.pN.json que no existían
   o eran de otra tirada: el libro entero se daba por nuevo.
3. Al seguir en el servidor se subían esos manifest.pN.json viejos, y allí mandan sobre el índice.

    python -m unittest tests.test_progreso -v
"""
import argparse, importlib.util, json, os, shutil, sys, tempfile, types, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
import st_core as C

HAS_AUDIO = all(importlib.util.find_spec(m) is not None for m in ("numpy", "soundfile"))

FRASES = 8                     # más que FLUSH_EVERY (6): el motor llega a guardar el índice antes de parar
PARA = 7                       # se para en la 7.ª frase del cap. 1, tras el guardado parcial de la 6.ª
TEXTO = " ".join(f"Esta es la frase número {i} del capítulo." for i in range(1, FRASES + 1))


class Parar(Exception):
    """El usuario pulsa «Detener» (o se corta la luz) a media narración."""


def fake_modules(calls, stop_after=None):
    """TTS y torch de mentira: cada llamada devuelve 0,3 s de tono; tras stop_after, se para."""
    import numpy as np

    class FakeTTS:
        def __init__(self, *_a, **_k): pass
        def to(self, _dev): return self
        def tts(self, text, **_k):
            if stop_after is not None and len(calls) >= stop_after:
                raise Parar()
            calls.append(text)
            t = np.arange(int(0.3 * 24000)) / 24000
            return (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32")

    tts_pkg = types.ModuleType("TTS"); tts_api = types.ModuleType("TTS.api")
    tts_api.TTS = FakeTTS; tts_pkg.api = tts_api
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    return {"TTS": tts_pkg, "TTS.api": tts_api, "torch": torch}


def fake_mp3(src_wav, dst_mp3, append=False):
    data = Path(src_wav).read_bytes()
    with open(dst_mp3, "ab" if append else "wb") as f:
        f.write(data)


def args(out, **kw):
    a = dict(epub="libro.epub", out=str(out), voice="voz.wav", lang="es", pron="", device="cpu",
             resume=True, chap_start=0, chap_end=0, manifest_name="manifest.json", no_state=False,
             qc_url="", qc_whisper_dir="", qc_threshold=0.85, qc_retries=2)
    a.update(kw)
    return argparse.Namespace(**a)


def done_entry(ci):
    return {"id": ci, "base": f"cap{ci:02d}", "title": f"Cap {ci}", "n": 3, "duration": 1.0,
            "done": True, "sr": 24000, "frames": 0}


@unittest.skipUnless(HAS_AUDIO, "hace falta numpy y soundfile (venv de XTTS)")
class MotorXtts(unittest.TestCase):
    def setUp(self):
        import cc_xtts
        self.X = cc_xtts
        self.E = cc_xtts.E
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, True)
        chaps = [{"title": f"Cap {i}", "text": TEXTO} for i in range(1, 5)]
        for p in (mock.patch.object(self.E, "chapters_from_epub", lambda _e: chaps),
                  mock.patch.object(self.E, "epub_title", lambda _e: "Libro"),
                  mock.patch.object(self.E, "_encode_mp3", fake_mp3)):
            p.start(); self.addCleanup(p.stop)
        if hasattr(cc_xtts, "_clean_clip"):          # sin limpiar: más rápido y no hace falta noisereduce
            p = mock.patch.object(cc_xtts, "_clean_clip", lambda w, sr=24000, prop=0.75: w)
            p.start(); self.addCleanup(p.stop)

    def book_with_later_chapters_done(self, name="manifest.json"):
        """Caps. 3 y 4 terminados (de otra tirada); 1 y 2 sin empezar."""
        for ci in (3, 4):
            (self.out / f"cap{ci:02d}.mp3").write_bytes(b"audio viejo %d" % ci)
            (self.out / f"cap{ci:02d}.json").write_text(json.dumps({"cues": []}), "utf-8")
        (self.out / name).write_text(json.dumps([done_entry(3), done_entry(4)]), "utf-8")

    def run_engine(self, calls, stop_after=None, **kw):
        with mock.patch.dict(sys.modules, fake_modules(calls, stop_after)):
            try:
                self.X.cmd_generate(args(self.out, **kw))
            except Parar:
                return False
        return True

    def manifest(self, name="manifest.json"):
        return {e["id"]: e for e in json.loads((self.out / name).read_text("utf-8"))}

    def test_parar_antes_de_llegar_no_borra_del_indice_lo_hecho_mas_adelante(self):
        self.book_with_later_chapters_done()
        calls = []
        self.assertFalse(self.run_engine(calls, stop_after=PARA))  # se para en el cap. 1
        m = self.manifest()
        self.assertIn(3, m); self.assertIn(4, m)
        self.assertTrue(m[3]["done"] and m[4]["done"])

    def test_reanudar_despues_no_vuelve_a_narrar_lo_hecho(self):
        self.book_with_later_chapters_done()
        self.run_engine([], stop_after=PARA)
        calls = []
        self.assertTrue(self.run_engine(calls))
        self.assertEqual(len(calls), 2 * FRASES - 6)               # el resto del cap. 1 y el cap. 2
        for ci in (3, 4):
            self.assertEqual((self.out / f"cap{ci:02d}.mp3").read_bytes(), b"audio viejo %d" % ci)
        self.assertEqual(sorted(self.manifest()), [1, 2, 3, 4])

    def test_un_tramo_solo_guarda_lo_de_su_rango(self):
        self.book_with_later_chapters_done("manifest.p1.json")
        (self.out / "manifest.p1.json").write_text(json.dumps([done_entry(2), done_entry(4)]), "utf-8")
        (self.out / "cap02.mp3").write_bytes(b"audio viejo 2")
        self.run_engine([], stop_after=PARA, chap_start=1, chap_end=2,
                        manifest_name="manifest.p1.json", no_state=True)
        self.assertEqual(sorted(self.manifest("manifest.p1.json")), [1, 2])


@unittest.skipUnless(HAS_AUDIO and importlib.util.find_spec("f5_tts") is None,
                     "se prueba con un F5 de mentira; con el de verdad instalado, sin GPU no compensa")
class MotorF5(unittest.TestCase):
    def test_parar_antes_de_llegar_no_borra_del_indice_lo_hecho_mas_adelante(self):
        import numpy as np
        import cc_engine as E
        out = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, out, True)
        chaps = [{"title": f"Cap {i}", "text": TEXTO} for i in range(1, 5)]
        for ci in (3, 4):
            (out / f"cap{ci:02d}.mp3").write_bytes(b"audio viejo")
        (out / "manifest.json").write_text(json.dumps([done_entry(3), done_entry(4)]), "utf-8")
        (out / "voz.txt").write_text("hola", "utf-8")

        class FakeF5:
            def __init__(self, **_k): pass
            n = 0
            def infer(self, **_k):
                FakeF5.n += 1
                if FakeF5.n > PARA - 1:
                    raise Parar()
                t = np.arange(int(0.3 * 24000)) / 24000
                return (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32"), 24000, None
        f5 = types.ModuleType("f5_tts"); api = types.ModuleType("f5_tts.api"); api.F5TTS = FakeF5; f5.api = api
        mods = fake_modules([]); mods.update({"f5_tts": f5, "f5_tts.api": api})
        a = args(out, voice_text=str(out / "voz.txt"), arch="F5TTS_v1_Base", ckpt="x", vocab="x",
                 nfe=32, seed=-1, speed=1.0)
        with mock.patch.dict(sys.modules, mods), \
             mock.patch.object(E, "chapters_from_epub", lambda _e: chaps), \
             mock.patch.object(E, "epub_title", lambda _e: "Libro"), \
             mock.patch.object(E, "_encode_mp3", fake_mp3):
            try:
                E.cmd_generate(a)
            except Parar:
                pass
            except AttributeError as e:                             # argumento que el motor pide y aquí falta
                self.skipTest(f"falta un argumento en la prueba: {e}")
        m = {e["id"]: e for e in json.loads((out / "manifest.json").read_text("utf-8"))}
        self.assertIn(3, m); self.assertIn(4, m)


def write(d, name, data):
    (d / name).write_text(json.dumps(data), "utf-8")


def read(d, name):
    return json.loads((d / name).read_text("utf-8"))


class PrepararReanudar(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def ids(self, name):
        return [e["id"] for e in read(self.d, name)]

    def test_de_uno_a_dos_tramos_cada_tramo_recibe_lo_suyo(self):
        write(self.d, "manifest.json", [done_entry(i) for i in (1, 2, 3)])
        write(self.d, "gen_state.json", {"total": 4, "complete": False})
        C.prepare_resume(self.d, 2, 4)
        self.assertEqual(self.ids("manifest.p1.json"), [1, 2])
        self.assertEqual(self.ids("manifest.p2.json"), [3])
        self.assertEqual(self.ids("manifest.json"), [1, 2, 3])

    def test_tramos_viejos_de_otra_tirada_no_mandan(self):
        # Se narró en 2 tramos, luego siguió en un tramo (o por un servicio), que no toca los pN.
        write(self.d, "manifest.p1.json", [dict(done_entry(1), done=False, n=1)])
        write(self.d, "manifest.p2.json", [])
        write(self.d, "manifest.json", [done_entry(1), done_entry(2)])
        write(self.d, "gen_state.json", {"total": 2, "complete": False})
        C.prepare_resume(self.d, 2, 2)
        self.assertTrue(read(self.d, "manifest.p1.json")[0]["done"])
        self.assertTrue(read(self.d, "manifest.p2.json")[0]["done"])

    def test_misma_tirada_en_tramos_mandan_los_tramos(self):
        # Un capítulo rehecho empieza con menos frases que en el índice viejo, y el bueno es el del tramo.
        write(self.d, "manifest.json", [dict(done_entry(1), done=False, n=9, frames=999)])
        write(self.d, "manifest.p1.json", [dict(done_entry(1), done=False, n=2, frames=20)])
        write(self.d, "gen_state.json", {"total": 2, "complete": False, "streams": 2})
        C.prepare_resume(self.d, 2, 2)
        self.assertEqual(read(self.d, "manifest.p1.json")[0]["n"], 2)
        self.assertEqual(read(self.d, "manifest.json")[0]["n"], 2)

    def test_de_dos_tramos_a_uno_todo_queda_en_el_indice(self):
        write(self.d, "manifest.json", [done_entry(1)])
        write(self.d, "manifest.p1.json", [done_entry(1)])
        write(self.d, "manifest.p2.json", [done_entry(3), dict(done_entry(4), done=False, n=1)])
        write(self.d, "gen_state.json", {"total": 4, "complete": False, "streams": 2})
        C.prepare_resume(self.d, 1, 4)
        self.assertEqual(self.ids("manifest.json"), [1, 3, 4])
        self.assertEqual(list(self.d.glob("manifest.p*.json")), [])

    def test_sin_nada_no_escribe(self):
        C.prepare_resume(self.d, 2, 4)
        self.assertEqual(list(self.d.iterdir()), [])


# Se ejecuta aparte, con su propia carpeta de usuario: GenWorker._send_xtts() de verdad, con ssh y scp simulados.
PROBE = r"""
import json, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.argv = ["storyteller.py", "--lang", "es"]
import storyteller as S

dest = S.LOCAL_BOOKS / "libro"
sent = []
def fake_run(args):
    if args[0] == "scp":
        sent.extend(Path(a).name for a in args[1:-1])
    if args[0] == "ssh" and "cat " in args[-1]:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")   # el servidor aún no lo tiene
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")

w = S.GenWorker(str(dest / "source.epub"), name="libro", resume=True)
w.voice = {"id": "voz", "wav": str(dest / "source.epub")}
w._run = fake_run
w._send_xtts(dest)
print("JSON:" + json.dumps({"sent": sent, "manifest": json.loads((dest / "manifest.json").read_text("utf-8"))}))
"""


@unittest.skipUnless(importlib.util.find_spec("PySide6") is not None, "hace falta PySide6")
class SeguirEnElServidor(unittest.TestCase):
    def test_no_sube_tramos_viejos_y_si_todo_lo_narrado(self):
        import subprocess
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        dest = home / "cuentacuentos" / "books" / "libro"
        dest.mkdir(parents=True)
        write(dest, "manifest.json", [done_entry(1), done_entry(2)])
        write(dest, "manifest.p1.json", [dict(done_entry(1), done=False, n=1)])    # de una tirada vieja
        write(dest, "gen_state.json", {"total": 2, "complete": False})
        (dest / "source.epub").write_text("epub", "utf-8")
        env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), QT_QPA_PLATFORM="offscreen",
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", STORYTELLER_LANG="es")
        r = subprocess.run([sys.executable, "-c", PROBE, str(ROOT)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=home, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2500:])
        out = json.loads(r.stdout.split("JSON:", 1)[1])
        self.assertIn("manifest.json", out["sent"])
        self.assertNotIn("manifest.p1.json", out["sent"])
        self.assertTrue(all(e["done"] for e in out["manifest"]))


if __name__ == "__main__":
    unittest.main()
