# -*- coding: utf-8 -*-
"""Los ficheros de requisitos: fijados, con lo opcional fuera de la base y a la vista de Trivy.
Solo lee texto: no instala nada.

    python -m unittest tests.test_requisitos -v
"""
import re, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_core as C

DEPS = ROOT / "engine" / "deps"
# Solo vienen con los motores opcionales: sus licencias tienen que quedar fuera si no se piden.
SOLO_F5 = {"f5-tts", "unidecode", "encodec", "frozendict", "vocos", "x-transformers", "einx"}
SOLO_WHISPER = {"faster-whisper", "av", "ctranslate2"}


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read(path):
    """({paquete: versión}, [ficheros de -r]). Falla si algo no está fijado con ==."""
    pins, includes = {}, []
    for raw in path.read_text("utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            includes.append(line[3:].strip())
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([^\s;]+)(;.*)?", line)
        if not m:
            raise AssertionError(f"{path}: sin fijar: {raw}")
        pins[norm(m.group(1))] = m.group(2)
    return pins, includes


def full(name):
    """Lo que acaba instalando ese fichero, con sus -r."""
    path = DEPS / name / "requirements.txt"
    pins, includes = read(path)
    for inc in includes:
        pins = {**full((path.parent / inc).parent.name), **pins}
    return pins


class Ficheros(unittest.TestCase):
    MOTORES = ("base", "xtts", "xtts-languages", "whisper", "whisper-alone", "f5", "translate")

    def test_estan_todos_y_todo_fijado(self):
        for name in self.MOTORES:
            with self.subTest(motor=name):
                read(DEPS / name / "requirements.txt")
        read(ROOT / "requirements.txt")

    def test_trivy_los_ve_sin_configurar_nada(self):
        # Trivy solo reconoce el nombre exacto requirements.txt, en cualquier carpeta. Un requirements-x.txt
        # quedaría fuera sin avisar (haría falta --file-patterns, que trivy-action no tiene como opción).
        self.assertEqual(sorted(p.name for p in DEPS.iterdir()), sorted(self.MOTORES))
        for folder in DEPS.iterdir():
            self.assertEqual([p.name for p in folder.iterdir()], ["requirements.txt"])
        others = [p for folder in (ROOT, ROOT / "engine") for p in folder.glob("*requirements*.txt")
                  if p.name != "requirements.txt"]
        self.assertEqual(others, [])

    def test_trivy_sigue_mirando_las_dependencias(self):
        flow = (ROOT / ".github/workflows/trivy.yml").read_text("utf-8")
        self.assertIn("scan-type: fs", flow)
        self.assertRegex(flow, r"scanners: vuln")
        self.assertNotIn("skip-dirs", flow)
        self.assertNotIn("trivyignores", flow)

    def test_lo_que_trae_cada_build_de_pytorch_no_va_aqui(self):
        for name in ("xtts", "f5"):
            for package in full(name):
                with self.subTest(motor=name, paquete=package):
                    self.assertNotRegex(package, r"^(nvidia-|triton$)")


class LoOpcional(unittest.TestCase):
    def test_la_base_y_xtts_no_traen_nada_opcional(self):
        for name in ("base", "xtts", "xtts-languages", "translate"):
            with self.subTest(motor=name):
                self.assertEqual(sorted(set(full(name)) & (SOLO_F5 | SOLO_WHISPER)), [])

    def test_leer_el_epub_no_necesita_pytorch(self):
        self.assertNotIn("torch", full("base"))

    def test_whisper_no_trae_f5_ni_pytorch(self):
        for name in ("whisper", "whisper-alone"):
            with self.subTest(motor=name):
                self.assertEqual(sorted(set(full(name)) & (SOLO_F5 | {"torch"})), [])

    def test_f5_no_trae_whisper(self):
        self.assertEqual(sorted(set(full("f5")) & SOLO_WHISPER), [])

    def test_whisper_con_av_menor_que_19(self):
        for name in ("whisper", "whisper-alone"):
            pins = full(name)
            with self.subTest(motor=name):
                self.assertEqual(pins["faster-whisper"], "1.2.1")
                self.assertLess(int(pins["av"].split(".")[0]), 19)  # faster-whisper 1.2.1 se rompe con av 19


class Coherencia(unittest.TestCase):
    def test_whisper_se_puede_anadir_sobre_cualquier_motor(self):
        whisper = full("whisper")
        for name in ("xtts", "f5"):
            pins = full(name)
            for package in set(whisper) & set(pins):
                with self.subTest(motor=name, paquete=package):
                    self.assertEqual(whisper[package], pins[package])

    def test_f5_y_xtts_solo_difieren_en_lo_que_f5_tiene_limitado(self):
        f5, xtts = full("f5"), full("xtts")
        self.assertEqual(sorted(p for p in set(f5) & set(xtts) if f5[p] != xtts[p]), ["filelock", "pillow"])

    def test_whisper_deja_filelock_a_quien_lo_tiene_fijado(self):
        self.assertNotIn("filelock", full("whisper"))
        self.assertEqual(full("whisper-alone")["filelock"], full("xtts")["filelock"])

    def test_el_mismo_pytorch_en_el_readme_que_en_los_ficheros(self):
        version = re.search(r"torch==(\S+)", C.TORCH_INSTALL).group(1)
        for name in ("xtts", "f5"):
            with self.subTest(motor=name):
                self.assertEqual(full(name)["torch"], version)
                self.assertEqual(full(name)["torchaudio"], version)

    def test_el_ci_instala_la_base_y_ffmpeg(self):
        flow = (ROOT / ".github/workflows/tests.yml").read_text("utf-8")
        self.assertIn("-r engine/deps/base/requirements.txt", flow)
        self.assertIn("ffmpeg", flow)

    def test_la_traduccion_instala_su_fichero(self):
        source = (ROOT / "st_translate.py").read_text("utf-8")
        self.assertNotIn("REQS =", source)
        self.assertIn('"deps" / "translate" / "requirements.txt"', source)


class Notice(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "NOTICE").read_text("utf-8")

    def test_todo_lo_que_se_instala_esta_declarado(self):
        names = {norm(n) for n in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]+", self.text)}
        extra = {"beautifulsoup4": "beautiful", "torch": "pytorch", "pdfminer-six": "pdfminer-six"}
        for folder in ["base", "xtts", "xtts-languages", "whisper", "whisper-alone", "f5", "translate"]:
            for package in full(folder):
                with self.subTest(paquete=package):
                    self.assertIn(extra.get(package, package), names)

    def test_av_no_va_entre_las_bsd_sueltas(self):
        block = re.search(r"BSD licences:\n(.*?)\nApache License", self.text.replace("\r\n", "\n"), re.S).group(1)
        self.assertNotRegex(block, r"(^|[\s,])av([\s,(]|$)")

    def test_av_dice_lo_que_lleva_y_de_donde_viene(self):
        for word in ("libx264", "libx265", "GPL", "LGPL", "PyAV", "Whisper"):
            self.assertIn(word, self.text)


if __name__ == "__main__":
    unittest.main()
