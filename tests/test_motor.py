# -*- coding: utf-8 -*-
"""Cuándo cuenta el motor como instalado, qué dice la app si falta y si el README da los comandos.
Sin PySide6, sin GPU y sin importar torch.

    python -m unittest tests.test_motor -v
"""
import ast, shutil, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_core as C
import st_lang as L

# Como los nombra pip de verdad: con guion bajo y con la versión local de PyTorch.
DIST_INFO = {"torch": "torch-2.6.0+cu124", "f5-tts": "f5_tts-1.1.22", "faster-whisper": "faster_whisper-1.2.1",
             "num2words": "num2words-0.5.14", "beautifulsoup4": "beautifulsoup4-4.15.0", "lxml": "lxml-6.1.3"}
FALTA_MOTOR = {109: "engine/.venv", 118: "engine/.venv-xtts", 119: "engine/.venv", 212: ""}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.venv = self.tmp / "engine" / ".venv"

    def site(self, windows):
        return self.venv / ("Lib/site-packages" if windows else "lib/python3.11/site-packages")

    def crear(self, windows, packages=C.ENGINE_PACKAGES, python=True):
        site = self.site(windows)
        site.mkdir(parents=True, exist_ok=True)
        if python:
            exe = self.venv / ("Scripts/python.exe" if windows else "bin/python")
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"")
        for p in packages:
            info = site / (DIST_INFO[p] + ".dist-info")
            info.mkdir()
            (info / "RECORD").write_text("", "utf-8")


class MotorInstalado(Base):
    def test_con_todo_cuenta(self):
        for windows in (True, False):
            with self.subTest(windows=windows):
                self.crear(windows)
                self.assertTrue(C.venv_has(self.venv, C.ENGINE_PACKAGES, windows))
                shutil.rmtree(self.venv)

    def test_sin_venv_no_cuenta(self):
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, False))

    def test_el_marcador_antiguo_no_basta(self):
        self.venv.parent.mkdir(parents=True)
        (self.venv.parent / ".ready").write_text("", "utf-8")
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))

    def test_venv_recien_creado_no_cuenta(self):
        self.crear(True, packages=())
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))

    def test_sin_el_python_del_venv_no_cuenta(self):
        self.crear(True, python=False)
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))

    def test_el_python_de_otro_sistema_no_cuenta(self):
        self.crear(False)
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))

    def test_falta_uno_cualquiera(self):
        for missing in C.ENGINE_PACKAGES:
            with self.subTest(falta=missing):
                self.crear(True, [p for p in C.ENGINE_PACKAGES if p != missing])
                self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))
                shutil.rmtree(self.venv)

    def test_instalacion_cortada_no_cuenta(self):
        self.crear(True)
        (self.site(True) / (DIST_INFO["torch"] + ".dist-info") / "RECORD").unlink()
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))

    def test_carpeta_suelta_sin_instalar_no_cuenta(self):
        self.crear(True, [p for p in C.ENGINE_PACKAGES if p != "f5-tts"])
        (self.site(True) / "f5_tts").mkdir()
        self.assertFalse(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))

    def test_no_importa_nada_del_venv(self):
        self.crear(True)
        pkg = self.site(True) / "torch"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("raise RuntimeError('no se debe importar')", "utf-8")
        sys.path.insert(0, str(self.site(True)))
        self.addCleanup(sys.path.remove, str(self.site(True)))
        self.assertTrue(C.venv_has(self.venv, C.ENGINE_PACKAGES, True))   # si lo importara, saltaría el error


class Textos(unittest.TestCase):
    def test_nadie_dice_que_se_esta_instalando(self):
        for text in L.SOURCE:
            self.assertNotRegex(text, r"se está instalando|de instalarse")

    def test_todos_los_idiomas_dicen_donde_mirar(self):
        langs = [l for l in L.available() if l != "es"]
        self.assertEqual(len(langs), 14)
        for lang in langs:
            table = L.load(lang)                          # comprueba además que estén todos y sus marcadores
            for n, path in FALTA_MOTOR.items():
                with self.subTest(lang=lang, n=n):
                    self.assertIn("README", table[L.SOURCE[n]])
                    self.assertIn(path, table[L.SOURCE[n]])
                    self.assertNotEqual(table[L.SOURCE[n]], L.SOURCE[n])

    def test_la_app_usa_estos_textos_y_no_el_marcador(self):
        source = (ROOT / "storyteller.py").read_text("utf-8")
        self.assertNotIn('".ready"', source)
        used = {c.args[0].value for c in ast.walk(ast.parse(source))
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_"
                and c.args and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)}
        for n in FALTA_MOTOR:
            self.assertIn(L.SOURCE[n], used)


class Readme(unittest.TestCase):
    def test_da_los_comandos_en_los_dos_idiomas(self):
        for name in ("README.md", "README.es.md"):
            text = (ROOT / name).read_text("utf-8")
            with self.subTest(readme=name):
                self.assertIn("git clone https://github.com/javimentallab/Storyteller.git cuentacuentos", text)
                self.assertIn("python -m pip install PySide6-Essentials PySide6-Addons", text)
                for py in (r"engine\.venv\Scripts\python", "engine/.venv/bin/python"):
                    line = next(l for l in text.splitlines() if l.startswith(py) and "f5-tts" in l)
                    for package in C.ENGINE_PACKAGES[1:]:
                        self.assertIn(package, line)
                    self.assertTrue(any(l.startswith(f"{py} -m pip install torch==") for l in text.splitlines()))


if __name__ == "__main__":
    unittest.main()
