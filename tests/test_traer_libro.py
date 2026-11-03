# -*- coding: utf-8 -*-
"""Traer del servidor un libro narrado mientras se está escuchando: en Windows el capítulo que suena
no se puede borrar ni reemplazar, y el libro no puede desaparecer de la biblioteca por eso.

    python -m unittest tests.test_traer_libro -v
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_core as C

QT = importlib.util.find_spec("PySide6") is not None
WINDOWS = os.name == "nt"
MANIFEST_VIEJO = [{"id": 8, "base": "cap08", "n": 1, "done": True}]
MANIFEST_NUEVO = MANIFEST_VIEJO + [{"id": 9, "base": "cap09", "n": 2, "done": True}]
MARCADOR = {"cap": 0, "base": "cap08", "pos": 134833}


def llenar(d, ficheros):
    d.mkdir(parents=True, exist_ok=True)
    for nombre, contenido in ficheros.items():
        (d / nombre).write_text(contenido, "utf-8")


def libro_viejo(d):
    llenar(d, {"cap08.mp3": "audio 8", "cap08.json": "{}", "manifest.json": json.dumps(MANIFEST_VIEJO),
               "gen_state.json": json.dumps({"total": 2, "complete": False}),
               "playback.json": json.dumps(MARCADOR)})


def lo_traido(d, cap08="audio 8"):
    llenar(d, {"cap08.mp3": cap08, "cap08.json": "{}", "cap09.mp3": "audio 9", "cap09.json": "{}",
               "manifest.json": json.dumps(MANIFEST_NUEVO), "title.txt": "Un libro",
               "gen_state.json": json.dumps({"total": 2, "complete": False}),
               "playback.json": json.dumps({"cap": 0, "base": "cap08", "pos": 1})})


class Cambiar(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dest = self.tmp / "books" / "libro"
        self.incoming = self.tmp / ".incoming-libro"

    def manifest(self):
        return json.loads((self.dest / "manifest.json").read_text("utf-8"))

    def test_libro_nuevo_sin_carpeta_previa(self):
        lo_traido(self.incoming)
        C.install_incoming(self.incoming, self.dest)
        self.assertEqual(self.manifest(), MANIFEST_NUEVO)
        self.assertFalse(self.incoming.exists())

    def test_quita_lo_que_ya_no_esta_en_el_servidor(self):
        libro_viejo(self.dest)
        llenar(self.dest, {"manifest.p1.json": "[]", "cap08.wav": "x", "cap08.seg.wav.tmp": "x"})
        lo_traido(self.incoming)
        C.install_incoming(self.incoming, self.dest)
        for sobra in ("manifest.p1.json", "cap08.wav", "cap08.seg.wav.tmp"):
            self.assertFalse((self.dest / sobra).exists(), sobra)
        self.assertEqual((self.dest / "cap09.mp3").read_text("utf-8"), "audio 9")

    def test_el_marcador_de_aqui_no_se_pisa(self):
        libro_viejo(self.dest)
        lo_traido(self.incoming)
        C.install_incoming(self.incoming, self.dest)
        self.assertEqual(json.loads((self.dest / "playback.json").read_text("utf-8")), MARCADOR)

    def test_sin_marcador_aqui_vale_el_traido(self):
        lo_traido(self.incoming)
        C.install_incoming(self.incoming, self.dest)
        self.assertEqual(json.loads((self.dest / "playback.json").read_text("utf-8"))["pos"], 1)

    def test_fichero_ocupado_en_cualquier_sistema(self):
        libro_viejo(self.dest)
        lo_traido(self.incoming, cap08="audio 8 nuevo")
        real = os.replace

        def replace(src, dst):
            if Path(dst).name == "cap08.mp3":
                raise PermissionError(13, "ocupado")
            real(src, dst)

        with mock.patch.object(C.os, "replace", replace):
            with self.assertRaises(PermissionError):
                C.install_incoming(self.incoming, self.dest, keep_waiting=lambda: False)
        self.assertEqual(self.manifest(), MANIFEST_VIEJO)
        self.assertEqual((self.incoming / "cap08.mp3").read_text("utf-8"), "audio 8 nuevo")

    @unittest.skipUnless(WINDOWS, "solo Windows impide reemplazar un fichero abierto")
    def test_capitulo_sonando_e_identico_no_molesta(self):
        libro_viejo(self.dest)
        lo_traido(self.incoming)
        with open(self.dest / "cap08.mp3", "rb"):          # lo que hace el reproductor
            C.install_incoming(self.incoming, self.dest)
        self.assertEqual(self.manifest(), MANIFEST_NUEVO)
        self.assertEqual((self.dest / "cap09.mp3").read_text("utf-8"), "audio 9")
        self.assertEqual((self.dest / "cap08.mp3").read_text("utf-8"), "audio 8")
        self.assertFalse(self.incoming.exists())

    @unittest.skipUnless(WINDOWS, "solo Windows impide reemplazar un fichero abierto")
    def test_capitulo_sonando_y_distinto_espera_a_que_lo_suelte(self):
        libro_viejo(self.dest)
        lo_traido(self.incoming, cap08="audio 8 nuevo")
        sonando = open(self.dest / "cap08.mp3", "rb")
        self.addCleanup(sonando.close)
        vistos = []

        def seguir():
            # Mientras espera, el libro sigue en la biblioteca, con su índice de antes.
            vistos.append(self.manifest())
            sonando.close()
            return True

        C.install_incoming(self.incoming, self.dest, keep_waiting=seguir)
        self.assertEqual(vistos, [MANIFEST_VIEJO])
        self.assertEqual((self.dest / "cap08.mp3").read_text("utf-8"), "audio 8 nuevo")
        self.assertEqual(self.manifest(), MANIFEST_NUEVO)
        self.assertFalse(self.incoming.exists())

    @unittest.skipUnless(WINDOWS, "solo Windows impide reemplazar un fichero abierto")
    def test_si_no_se_espera_no_se_pierde_nada(self):
        libro_viejo(self.dest)
        lo_traido(self.incoming, cap08="audio 8 nuevo")
        with open(self.dest / "cap08.mp3", "rb"):
            with self.assertRaises(PermissionError):
                C.install_incoming(self.incoming, self.dest, keep_waiting=lambda: False)
        self.assertEqual(self.manifest(), MANIFEST_VIEJO)   # sigue en la biblioteca
        self.assertEqual((self.dest / "cap08.mp3").read_text("utf-8"), "audio 8")
        self.assertEqual((self.incoming / "cap08.mp3").read_text("utf-8"), "audio 8 nuevo")
        self.assertEqual(json.loads((self.incoming / "manifest.json").read_text("utf-8")), MANIFEST_NUEVO)


# Se ejecuta aparte, con su propia carpeta de usuario: GenWorker.run() de verdad, con ssh y scp simulados.
PROBE = r"""
import json, shutil, sys, types
from pathlib import Path
root, server = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, root)
sys.argv = ["storyteller.py", "--lang", "es"]
import storyteller as S

dest = S.LOCAL_BOOKS / "libro"
epub = dest / "source.epub"

def fake_run(args):
    if args[0] == "scp" and "-r" in args:                  # traer la carpeta narrada
        target = Path(args[-1])
        for f in server.iterdir():
            shutil.copyfile(f, target / f.name)
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")

w = S.GenWorker(str(epub), name="libro", resume=True)
w.xtts = False
w.mode = "stopsave"
w._run = fake_run
S.subprocess.Popen = lambda *a, **k: types.SimpleNamespace(stdout=iter(["RUN-RC=0\n"]), wait=lambda: 0)
out = {"done": [], "failed": []}
w.done.connect(out["done"].append)
w.failed.connect(out["failed"].append)
sonando = open(dest / "cap08.mp3", "rb")                   # el reproductor, con ese capítulo abierto
w.run()
books = S.list_books()
out["biblioteca"] = [b["n"] for b in books]
out["ficheros"] = sorted(p.name for p in dest.iterdir())
marcador = dest / "playback.json"
out["marcador"] = json.loads(marcador.read_text("utf-8")) if marcador.exists() else None
sonando.close()
print("JSON:" + json.dumps(out, ensure_ascii=False))
"""


@unittest.skipUnless(QT and WINDOWS, "hace falta PySide6, y solo Windows impide borrar un fichero abierto")
class TraerEscuchando(unittest.TestCase):
    def test_el_libro_sigue_en_la_biblioteca_y_con_todo(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        dest = home / "cuentacuentos" / "books" / "libro"
        libro_viejo(dest)
        llenar(dest, {"source.epub": "epub"})
        server = home / "servidor"
        lo_traido(server)
        env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
                   PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="-1", STORYTELLER_LANG="es")
        r = subprocess.run([sys.executable, "-c", PROBE, str(ROOT), str(server)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=home, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2500:])
        out = json.loads(r.stdout.split("JSON:", 1)[1])
        self.assertEqual(out["failed"], [])
        self.assertEqual(len(out["done"]), 1)
        self.assertEqual(out["biblioteca"], [2])            # sale, y con el capítulo nuevo
        for nombre in ("cap08.mp3", "cap09.mp3", "manifest.json", "gen_state.json", "title.txt", "source.epub"):
            self.assertIn(nombre, out["ficheros"])
        self.assertEqual(out["marcador"], MARCADOR)
        self.assertFalse((home / "cuentacuentos" / ".incoming-libro").exists())


if __name__ == "__main__":
    unittest.main()
