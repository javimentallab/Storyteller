# -*- coding: utf-8 -*-
"""Cuándo cuenta cada motor como instalado, qué dice la app si falta y si el README da los comandos.
Sin PySide6 ni GPU. Solo VozDeReferencia importa torch, y sin él se salta.

    python -m unittest tests.test_motor -v
"""
import argparse, ast, contextlib, io, shutil, sys, tempfile, types, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
import cc_engine as E
import st_core as C
import st_lang as L

# Como los nombra pip de verdad: con guion bajo y con la versión local de PyTorch.
DIST_INFO = {"torch": "torch-2.13.0+cu130", "torchaudio": "torchaudio-2.11.1", "f5-tts": "f5_tts-1.1.22",
             "coqui-tts": "coqui_tts-0.28.0", "faster-whisper": "faster_whisper-1.2.1", "av": "av-18.1.0",
             "num2words": "num2words-0.5.14", "beautifulsoup4": "beautifulsoup4-4.15.0", "lxml": "lxml-6.1.3"}
# Los textos que mandan a instalar algo, y la ruta que tienen que nombrar.
FALTA_MOTOR = {118: "engine/.venv-xtts", 348: "engine/.venv-xtts", 349: ""}


def dist_info(package):
    return DIST_INFO.get(package, package.replace("-", "_") + "-1.0")
COMANDOS = (342, 343, 344, 345, 352, 356)                # los que llevan los comandos en {0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.engine = self.tmp / "engine"
        self.venv = self.engine / ".venv"

    def site(self, windows, venv=None):
        return (venv or self.venv) / ("Lib/site-packages" if windows else "lib/python3.11/site-packages")

    def crear(self, windows, packages=C.F5_PACKAGES, python=True, venv=None):
        venv = venv or self.venv
        site = self.site(windows, venv)
        site.mkdir(parents=True, exist_ok=True)
        if python:
            exe = venv / ("Scripts/python.exe" if windows else "bin/python")
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"")
        for p in packages:
            info = site / (dist_info(p) + ".dist-info")
            info.mkdir(exist_ok=True)
            (info / "RECORD").write_text("", "utf-8")


class MotorInstalado(Base):
    def test_con_todo_cuenta(self):
        for windows in (True, False):
            with self.subTest(windows=windows):
                self.crear(windows)
                self.assertTrue(C.venv_has(self.venv, C.F5_PACKAGES, windows))
                shutil.rmtree(self.venv)

    def test_sin_venv_no_cuenta(self):
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, False))

    def test_el_marcador_antiguo_no_basta(self):
        self.venv.parent.mkdir(parents=True)
        (self.venv.parent / ".ready").write_text("", "utf-8")
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))

    def test_venv_recien_creado_no_cuenta(self):
        self.crear(True, packages=())
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))

    def test_sin_el_python_del_venv_no_cuenta(self):
        self.crear(True, python=False)
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))

    def test_el_python_de_otro_sistema_no_cuenta(self):
        self.crear(False)
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))

    def test_falta_uno_cualquiera(self):
        for missing in C.F5_PACKAGES:
            with self.subTest(falta=missing):
                self.crear(True, [p for p in C.F5_PACKAGES if p != missing])
                self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))
                shutil.rmtree(self.venv)

    def test_instalacion_cortada_no_cuenta(self):
        self.crear(True)
        (self.site(True) / (dist_info("torch") + ".dist-info") / "RECORD").unlink()
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))

    def test_carpeta_suelta_sin_instalar_no_cuenta(self):
        self.crear(True, [p for p in C.F5_PACKAGES if p != "f5-tts"])
        (self.site(True) / "f5_tts").mkdir()
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))

    def test_no_importa_nada_del_venv(self):
        self.crear(True)
        pkg = self.site(True) / "torch"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("raise RuntimeError('no se debe importar')", "utf-8")
        sys.path.insert(0, str(self.site(True)))
        self.addCleanup(sys.path.remove, str(self.site(True)))
        self.assertTrue(C.venv_has(self.venv, C.F5_PACKAGES, True))   # si lo importara, saltaría el error


class PartesOpcionales(Base):
    """Quién necesita qué: F5 y Whisper son opcionales, y el QC corre en el proceso del motor que narra."""

    def setUp(self):
        super().setUp()
        self.xtts = self.engine / ".venv-xtts"

    def test_nada_opcional_en_lo_que_pide_la_base(self):
        for packages in (C.BASE_PACKAGES, C.XTTS_PACKAGES):
            for opcional in C.WHISPER_PACKAGES + ("f5-tts",):
                self.assertNotIn(opcional, packages)
        self.assertNotIn("torch", C.BASE_PACKAGES)
        self.assertNotIn("coqui-tts", C.F5_PACKAGES)
        self.assertNotIn("av", C.F5_PACKAGES)

    def test_un_venv_de_xtts_a_medias_no_cuenta(self):
        self.crear(True, packages=(), venv=self.xtts)
        self.assertFalse(C.venv_has(self.xtts, C.XTTS_PACKAGES, True))
        self.crear(True, [p for p in C.XTTS_PACKAGES if p != "coqui-tts"], venv=self.xtts)
        self.assertFalse(C.venv_has(self.xtts, C.XTTS_PACKAGES, True))
        self.crear(True, C.XTTS_PACKAGES, venv=self.xtts)
        self.assertTrue(C.venv_has(self.xtts, C.XTTS_PACKAGES, True))

    def test_la_instalacion_de_siempre_lo_tiene_todo(self):
        self.crear(True, C.F5_PACKAGES + C.WHISPER_PACKAGES)
        self.crear(True, C.XTTS_PACKAGES + C.WHISPER_PACKAGES, venv=self.xtts)
        self.assertTrue(C.venv_has(self.venv, C.BASE_PACKAGES, True))
        self.assertTrue(C.venv_has(self.venv, C.F5_PACKAGES, True))
        self.assertTrue(C.venv_has(self.xtts, C.XTTS_PACKAGES, True))
        self.assertEqual(C.whisper_venv(self.engine, True), C.VENV_MAIN)
        self.assertTrue(C.qc_can_run(self.engine, "xtts", True))
        self.assertTrue(C.qc_can_run(self.engine, "f5", True))

    def test_whisper_para_transcribir_vale_en_cualquiera_de_los_dos(self):
        self.assertIsNone(C.whisper_venv(self.engine, True))
        self.crear(True, C.XTTS_PACKAGES + C.WHISPER_PACKAGES, venv=self.xtts)
        self.assertEqual(C.whisper_venv(self.engine, True), C.VENV_XTTS)
        self.crear(True, C.WHISPER_PACKAGES)
        self.assertEqual(C.whisper_venv(self.engine, True), C.VENV_MAIN)

    def test_el_qc_mira_el_venv_del_motor_que_narra(self):
        self.crear(True, C.WHISPER_PACKAGES)                     # solo en engine/.venv
        self.crear(True, C.XTTS_PACKAGES, venv=self.xtts)
        self.assertFalse(C.qc_can_run(self.engine, "xtts", True))
        self.assertTrue(C.qc_can_run(self.engine, "f5", True))
        self.assertEqual(C.whisper_venv(self.engine, True), C.VENV_MAIN)  # transcribir, en cambio, sí puede

    def test_whisper_se_instala_en_el_motor_que_narra(self):
        self.assertEqual(C.whisper_target(self.engine, None, True), "whisper-alone")
        self.crear(True, C.XTTS_PACKAGES, venv=self.xtts)
        self.assertEqual(C.whisper_target(self.engine, None, True), "whisper-xtts")
        self.assertEqual(C.whisper_target(self.engine, "f5", True), "whisper-xtts")  # F5 no está: en XTTS
        self.crear(True, C.F5_PACKAGES)
        self.assertEqual(C.whisper_target(self.engine, "xtts", True), "whisper-xtts")
        self.assertEqual(C.whisper_target(self.engine, "f5", True), "whisper-f5")
        shutil.rmtree(self.xtts)
        self.assertEqual(C.whisper_target(self.engine, "xtts", True), "whisper-f5")

    def test_los_comandos_de_cada_sistema(self):
        self.assertEqual(C.install_commands("whisper-xtts", True),
                         [r"engine\.venv-xtts\Scripts\python -m pip install -r engine\deps\whisper\requirements.txt"])
        self.assertEqual(C.install_commands("whisper-alone", False),
                         ["python3 -m venv engine/.venv",
                          "engine/.venv/bin/python -m pip install -r engine/deps/whisper/requirements.txt"])
        self.assertEqual(C.install_commands("f5", True)[-1],
                         r"engine\.venv\Scripts\python -m pip install --no-deps -r engine\deps\f5\requirements.txt")
        self.assertEqual(C.install_commands("f5", False)[-1],
                         "engine/.venv/bin/python -m pip install --no-deps -r engine/deps/f5/requirements.txt")

    def test_f5_cortado_a_medias_no_cuenta(self):
        # --no-deps no vigila nada: si la instalación se corta, f5-tts puede estar sin lo que importa.
        self.crear(True, ("torch", "f5-tts") + C.BASE_PACKAGES)
        self.assertFalse(C.venv_has(self.venv, C.F5_PACKAGES, True))


class WhisperSinTorch(unittest.TestCase):
    """Transcribir no puede exigir PyTorch: en engine/.venv puede no estar."""

    def transcribe(self, torch_module):
        seen = {}

        class Model:
            def __init__(self, path, device, compute_type):
                seen.update(device=device, compute=compute_type)

            def transcribe(self, *args, **kwargs):
                return [types.SimpleNamespace(text="Hola", start=0.0, end=1.0)], None

        modules = {"faster_whisper": types.SimpleNamespace(WhisperModel=Model), "torch": torch_module}
        args = argparse.Namespace(device="auto", model_dir="m", language="es", audio="a.wav", offset=0.0)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, modules), contextlib.redirect_stdout(out):
            E.cmd_whisper(args)
        return seen, out.getvalue()

    def test_sin_torch_va_en_cpu(self):
        seen, out = self.transcribe(None)                        # None en sys.modules: ImportError al importarlo
        self.assertEqual(seen, {"device": "cpu", "compute": "int8"})
        self.assertIn("whisper cargado (cpu)", out)
        self.assertIn('SEG {"start": 0.0, "end": 1.0, "text": "Hola"}', out)
        self.assertIn("WH-DONE", out)

    def test_con_torch_y_gpu_usa_la_gpu(self):
        torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
        self.assertEqual(self.transcribe(torch)[0], {"device": "cuda", "compute": "float16"})

    def test_con_torch_sin_gpu_va_en_cpu(self):
        torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
        self.assertEqual(self.transcribe(torch)[0], {"device": "cpu", "compute": "int8"})


class CorteDeCola(unittest.TestCase):
    SR = 24000

    def setUp(self):
        try:
            import numpy
        except ImportError:
            self.skipTest("sin numpy")
        # 1,2 s de voz, 0,4 s de silencio y 1,4 s de cola inventada
        self.wav = numpy.full(self.SR * 3, 0.3, dtype="float32")
        self.wav[int(1.2 * self.SR):int(1.6 * self.SR)] = 0.0
        self.seguida = numpy.full(self.SR * 3, 0.3, dtype="float32")   # voz de corrido hasta el final
        self.addCleanup(E._QC_RATES.clear)
        E._QC_RATES.clear()

    def test_corta_la_cola_en_el_silencio_tras_la_ultima_palabra(self):
        # Whisper marca el fin a 1,0 s, antes de tiempo: se corta en el silencio real (1,2 s)
        out = E.qc_cut_tail("hola amigo", "hola amigo", self.wav, self.SR, 1.0)
        self.assertEqual(len(out), int((1.2 + E.QC_CUT_PAD) * self.SR))
        self.assertEqual(float(out[-1]), 0.0)
        self.assertAlmostEqual(float(out[int(1.15 * self.SR)]), 0.3, places=5)   # la palabra entera

    def test_voz_de_corrido_no_se_corta_aunque_whisper_acabe_antes(self):
        # 7-nov: Whisper acababa la última palabra 0,2-0,5 s antes y se cortaba media palabra
        self.assertIsNone(E.qc_cut_tail("hola amigo", "hola amigo", self.seguida, self.SR, 1.0))
        self.assertIsNone(E.qc_cut_tail("hola amigo", "hola amigo", self.seguida, self.SR, 2.5))

    def test_no_toca_el_original(self):
        E.qc_cut_tail("hola amigo", "hola amigo", self.wav, self.SR, 1.0)
        self.assertEqual(len(self.wav), self.SR * 3)
        self.assertAlmostEqual(float(self.wav[int(1.7 * self.SR)]), 0.3, places=5)

    def test_si_faltan_palabras_decide_el_reintento(self):
        self.assertIsNone(E.qc_cut_tail("hola amigo mio querido", "hola", self.wav, self.SR, 1.0))

    def test_una_cola_corta_se_queda(self):
        self.assertIsNone(E.qc_cut_tail("hola amigo", "hola amigo", self.wav, self.SR, 2.8))

    def test_sin_tiempo_de_whisper_no_corta(self):
        self.assertIsNone(E.qc_cut_tail("hola amigo", "hola amigo", self.wav, self.SR, None))

    def test_un_tiempo_de_whisper_increible_no_corta(self):
        E._QC_RATES.extend([10.0] * 8)            # 15 letras a 10 por segundo: 1,5 s
        self.assertIsNone(E.qc_cut_tail("quince letras justas", "quince letras justas",
                                        self.wav, self.SR, 0.2))


class VozFinal(unittest.TestCase):
    def setUp(self):
        try:
            import numpy
        except ImportError:
            self.skipTest("sin numpy")
        self.np = numpy
        import cc_xtts
        self.X = cc_xtts

    def test_deshace_el_tanh_del_generador(self):
        x = self.np.array([0.0, 0.2, -0.5, 1.2, -1.8], dtype="float32")
        out = self.X.finish_voice(self.np.tanh(x))
        self.assertTrue(self.np.allclose(out, x * self.X.OUTPUT_GAIN, atol=1e-3))

    def test_nunca_se_pasa_del_tope(self):
        out = self.X.finish_voice(self.np.array([1.0, -1.0, 0.9999], dtype="float32"))
        self.assertLess(float(self.np.abs(out).max()), 1.0)


class VozDeReferencia(unittest.TestCase):
    """La voz se lee con soundfile, porque desde PyTorch 2.9 torchaudio.load pide torchcodec y FFmpeg."""
    def setUp(self):
        try:
            import numpy, soundfile, torch, torchaudio
        except ImportError:
            self.skipTest("sin torch, torchaudio o soundfile")
        self.np, self.sf, self.torch = numpy, soundfile, torch
        import cc_xtts
        self.X = cc_xtts
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def wav(self, data, sr, subtype="PCM_16"):
        path = self.tmp / "voz.wav"
        self.sf.write(str(path), data, sr, subtype=subtype)
        return path

    def test_como_torchaudio_load(self):
        stereo = self.np.stack([self.np.linspace(-0.5, 0.5, 100), self.np.zeros(100)], axis=1)
        audio, sr = E.read_audio(self.wav(stereo, 22050))
        self.assertEqual((tuple(audio.shape), sr, audio.dtype), ((2, 100), 22050, self.torch.float32))
        self.assertAlmostEqual(float(audio[0, 0]), -0.5, places=3)

    def test_xtts_la_recibe_en_mono_y_a_su_frecuencia(self):
        t = self.np.arange(22050) / 22050
        tone = 0.5 * self.np.sin(2 * self.np.pi * 220 * t)
        audio = self.X.load_voice(self.wav(self.np.stack([tone, tone], axis=1), 22050), 24000)
        self.assertEqual(tuple(audio.shape), (1, 24000))
        self.assertAlmostEqual(float(audio.abs().max()), 0.5, places=2)

    def test_recorta_lo_que_se_sale(self):
        audio = self.X.load_voice(self.wav(self.np.array([1.5, -2.0, 0.25], dtype="float32"), 24000, "FLOAT"), 24000)
        self.assertEqual([round(float(v), 3) for v in audio[0]], [1.0, -1.0, 0.25])

    def test_xtts_usa_la_de_aqui(self):
        xtts = types.ModuleType("TTS.tts.models.xtts")
        xtts.load_audio = None
        names = ("TTS", "TTS.tts", "TTS.tts.models")
        mods = {n: types.ModuleType(n) for n in names}
        mods["TTS"].tts, mods["TTS.tts"].models, mods["TTS.tts.models"].xtts = mods["TTS.tts"], mods["TTS.tts.models"], xtts
        with mock.patch.dict(sys.modules, {**mods, "TTS.tts.models.xtts": xtts}):
            self.X.use_soundfile()
        self.assertIs(xtts.load_audio, self.X.load_voice)

    def test_f5_lee_con_soundfile_y_lo_demas_sigue_igual(self):
        ta = types.SimpleNamespace(load=lambda *a, **k: self.fail("torchaudio.load"), transforms="transforms")
        utils = types.ModuleType("f5_tts.infer.utils_infer")
        utils.torchaudio = ta
        mods = {n: types.ModuleType(n) for n in ("f5_tts", "f5_tts.infer")}
        mods["f5_tts"].infer, mods["f5_tts.infer"].utils_infer = mods["f5_tts.infer"], utils
        with mock.patch.dict(sys.modules, {**mods, "f5_tts.infer.utils_infer": utils}):
            E.f5_reads_with_soundfile()
            E.f5_reads_with_soundfile()                   # dos veces no lo envuelve dos veces
        self.assertIs(utils.torchaudio._ta, ta)
        self.assertEqual(utils.torchaudio.transforms, "transforms")
        audio, sr = utils.torchaudio.load(self.wav(self.np.zeros(50), 16000))
        self.assertEqual((tuple(audio.shape), sr), ((1, 50), 16000))


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
            for n in COMANDOS:
                with self.subTest(lang=lang, n=n):
                    self.assertIn("{0}", table[L.SOURCE[n]])
                    self.assertNotEqual(table[L.SOURCE[n]], L.SOURCE[n])

    def test_todos_explican_el_cambio_de_cu130_a_cpu(self):
        for lang in [l for l in L.available() if l != "es"]:
            table = L.load(lang)
            for n in (343, 344):
                with self.subTest(lang=lang, n=n):
                    self.assertIn("cu130", table[L.SOURCE[n]])
                    self.assertIn("cpu", table[L.SOURCE[n]])

    def test_ningun_texto_nuevo_sin_traducir(self):
        for lang in [l for l in L.available() if l != "es"]:
            table = L.load(lang)
            for n in range(342, len(L.SOURCE)):
                if (lang, L.SOURCE[n]) in (("en", "No"), ("it", "No"), ("pt", "Copiar")):
                    continue                                  # se escriben igual que en español
                with self.subTest(lang=lang, n=n):
                    self.assertNotEqual(table[L.SOURCE[n]], L.SOURCE[n])

    def test_la_app_usa_estos_textos_y_no_el_marcador(self):
        source = (ROOT / "storyteller.py").read_text("utf-8")
        self.assertNotIn('".ready"', source)
        used = {c.args[0].value for c in ast.walk(ast.parse(source))
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_"
                and c.args and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)}
        for n in list(FALTA_MOTOR) + list(range(342, len(L.SOURCE))):
            self.assertIn(L.SOURCE[n], used)

    def test_ya_no_se_dice_que_whisper_y_f5_van_juntos(self):
        source = (ROOT / "storyteller.py").read_text("utf-8")
        self.assertNotIn("F5/Whisper", source)


class Readme(unittest.TestCase):
    PARTES = ("xtts", "xtts-languages", "f5", "base", "whisper-xtts", "whisper-f5", "whisper-alone")

    def test_da_los_comandos_en_los_dos_idiomas(self):
        for name in ("README.md", "README.es.md"):
            lines = [l.strip() for l in (ROOT / name).read_text("utf-8").splitlines()]
            with self.subTest(readme=name):
                self.assertIn("git clone https://github.com/javimentallab/Storyteller.git cuentacuentos", lines)
                self.assertIn("python -m pip install -r requirements.txt", lines)
                for what in self.PARTES:
                    for windows in (True, False):
                        for command in C.install_commands(what, windows):
                            self.assertIn(command, lines)

    def test_f5_y_whisper_salen_como_opcionales(self):
        for name, word in (("README.md", "optional"), ("README.es.md", "opcional")):
            text = (ROOT / name).read_text("utf-8")
            with self.subTest(readme=name):
                self.assertIn(f"F5 ({word})", text)
                self.assertIn(f"Whisper ({word})", text)

    def test_nadie_instala_sin_version(self):
        for name in ("README.md", "README.es.md"):
            for line in (ROOT / name).read_text("utf-8").splitlines():
                if " -m pip install " in line:
                    with self.subTest(readme=name, line=line):
                        self.assertRegex(line, r"pip install (--no-deps -r |-r |torch==)")

    def test_explica_por_que_f5_lleva_no_deps(self):
        for name, word in (("README.md", "expected"), ("README.es.md", "esperado")):
            text = (ROOT / name).read_text("utf-8")
            section = text[text.index("### F5 ("):]
            section = section[:section.index("\n## ")]
            with self.subTest(readme=name):
                for needed in ("--no-deps", "pip check", "gradio", "cached-path", "pillow", "filelock", word):
                    self.assertIn(needed, section)


if __name__ == "__main__":
    unittest.main()
