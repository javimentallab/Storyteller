# -*- coding: utf-8 -*-
"""Los ficheros de requisitos: fijados, con lo opcional fuera de la base y a la vista de Trivy.
Solo lee texto: no instala nada.

    python -m unittest tests.test_requisitos -v
"""
import json, os, re, subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_core as C

DEPS = ROOT / "engine" / "deps"
# Solo vienen con los motores opcionales: sus licencias tienen que quedar fuera si no se piden.
SOLO_F5 = {"f5-tts", "unidecode", "encodec", "frozendict", "vocos", "x-transformers", "einx"}
SOLO_WHISPER = {"faster-whisper", "av", "ctranslate2"}
# Lo que deja fuera instalar F5 con --no-deps: su interfaz web, y lo que solo le hace falta a ella.
GRADIO = {"gradio", "gradio-client", "groovy", "hf-gradio", "safehttpx", "fastapi", "starlette", "uvicorn", "python-multipart"}
# Lo que importa F5 1.1.22 al recorrer su código desde f5_tts.api, también dentro de funciones: {módulo: paquete}.
# Si sube de versión, hay que repasarlo: F5SinGradio.test_coincide_con_su_codigo lo compara con el F5 instalado.
F5_IMPORTA = {"accelerate": "accelerate", "bitsandbytes": "bitsandbytes", "cached_path": "cached-path", "datasets": "datasets",
              "ema_pytorch": "ema-pytorch", "huggingface_hub": "huggingface-hub", "hydra": "hydra-core", "librosa": "librosa",
              "matplotlib": "matplotlib", "numpy": "numpy", "omegaconf": "omegaconf", "pydub": "pydub", "pypinyin": "pypinyin",
              "rjieba": "rjieba", "safetensors": "safetensors", "soundfile": "soundfile", "torch": "torch",
              "torchaudio": "torchaudio", "torchdiffeq": "torchdiffeq", "tqdm": "tqdm", "transformers": "transformers",
              "vocos": "vocos", "wandb": "wandb", "x_transformers": "x-transformers"}
# Opcionales de verdad: F5 los prueba o solo los usa con otro vocoder.
F5_NO_HACEN_FALTA = {"flash_attn", "third_party"}
F5_PYTHON = os.environ.get("ST_F5_PYTHON", "")          # el Python de un venv con F5 instalado desde este fichero

# Recorre el código de F5 y lo que declara cada paquete, sin importar nada.
F5_SCRIPT = r"""
import ast, importlib.metadata as m, itertools, json, re, sys
from pathlib import Path
from packaging.requirements import Requirement

def norm(n): return re.sub(r"[-_.]+", "-", n).lower()

site = Path(m.distribution("f5-tts").locate_file("f5_tts")).parent
def module_file(mod):
    p = site.joinpath(*mod.split("."))
    return p.with_suffix(".py") if p.with_suffix(".py").exists() else (p / "__init__.py" if (p / "__init__.py").exists() else None)
seen, third, queue = set(), set(), ["f5_tts.api"]
while queue:
    mod = queue.pop()
    f = module_file(mod)
    if mod in seen or not f:
        continue
    seen.add(mod)
    pkg = mod if f.name == "__init__.py" else mod.rpartition(".")[0]
    for node in ast.walk(ast.parse(f.read_text("utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                base = ".".join(parts[:len(parts) - node.level + 1] + ([base] if base else []))
            names = [base] + [base + "." + a.name for a in node.names]
        for n in names:
            top = n.split(".")[0]
            if top == "f5_tts":
                queue.append(n)
            elif top and top not in sys.stdlib_module_names:
                third.add(top)

envs = [dict(sys_platform=p, platform_system=s, platform_machine=a, python_version=v, python_full_version=f,
             implementation_name="cpython", platform_python_implementation="CPython", os_name="nt" if p == "win32" else "posix")
        for (p, s, a), (v, f) in itertools.product(
            [("win32", "Windows", "AMD64"), ("linux", "Linux", "x86_64"), ("darwin", "Darwin", "arm64")],
            [("3.11", "3.11.9"), ("3.12", "3.12.8"), ("3.13", "3.13.1")])]
done, closure, stack = set(), set(), [("f5-tts", "")]
while stack:
    name, extra = stack.pop()
    if (norm(name), extra) in done or norm(name) == "gradio":
        continue
    done.add((norm(name), extra))
    closure.add(norm(name))
    try:
        reqs = m.distribution(name).requires or []
    except m.PackageNotFoundError:
        continue
    for r in reqs:
        q = Requirement(r)
        if q.marker and not any(q.marker.evaluate(dict(e, extra=extra)) for e in envs):
            continue
        stack.append((q.name, ""))
        stack.extend((q.name, x) for x in q.extras)
print(json.dumps({"importa": sorted(third), "cierre": sorted(closure), "version": m.version("f5-tts")}))
"""


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
    MOTORES = ("base", "xtts", "xtts-languages", "whisper", "f5", "translate")

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
        self.assertEqual(sorted(set(full("whisper")) & (SOLO_F5 | {"torch"})), [])

    def test_f5_no_trae_whisper(self):
        self.assertEqual(sorted(set(full("f5")) & SOLO_WHISPER), [])

    def test_whisper_con_av_menor_que_19(self):
        pins = full("whisper")
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

    def test_f5_y_xtts_llevan_las_mismas_versiones(self):
        f5, xtts = full("f5"), full("xtts")
        self.assertEqual(sorted(p for p in set(f5) & set(xtts) if f5[p] != xtts[p]), [])

    def test_pillow_y_filelock_arreglados_en_todos(self):
        for name in ("xtts", "f5", "whisper"):
            with self.subTest(motor=name):
                self.assertEqual(full(name)["filelock"], "4.2.4")
        for name in ("xtts", "f5", "translate"):
            with self.subTest(motor=name):
                self.assertEqual(full(name)["pillow"], "13.0.0")

    def test_el_mismo_pytorch_en_el_readme_que_en_los_ficheros(self):
        version = re.search(r"torch==(\S+)", C.TORCH_INSTALL).group(1)
        for name in ("xtts", "f5"):
            with self.subTest(motor=name):
                self.assertEqual(full(name)["torch"], version)
                # torchaudio ya no sale a la par: la 2.11.1 vale para torch 2.11 en adelante y viene de PyPI.
                self.assertEqual(full(name)["torchaudio"], "2.11.1")
                self.assertNotIn("torchaudio", C.TORCH_INSTALL)

    def test_el_ci_instala_la_base_y_ffmpeg(self):
        flow = (ROOT / ".github/workflows/tests.yml").read_text("utf-8")
        self.assertIn("-r engine/deps/base/requirements.txt", flow)
        self.assertIn("ffmpeg", flow)

    def test_la_traduccion_instala_su_fichero(self):
        source = (ROOT / "st_translate.py").read_text("utf-8")
        self.assertNotIn("REQS =", source)
        self.assertIn('"deps" / "translate" / "requirements.txt"', source)


class F5SinGradio(unittest.TestCase):
    """F5 se instala con --no-deps, que no avisa si falta un paquete: el fichero tiene que llevarlo todo."""

    def test_no_lleva_gradio(self):
        self.assertEqual(sorted(set(full("f5")) & GRADIO), [])

    def test_lleva_todo_lo_que_f5_importa_para_generar(self):
        pins = full("f5")
        self.assertEqual(pins["f5-tts"], "1.1.22")       # con otra versión, hay que repasar F5_IMPORTA
        for module, package in F5_IMPORTA.items():
            with self.subTest(modulo=module):
                self.assertIn(package, pins)

    def test_el_motor_cuenta_como_instalado_solo_con_lo_que_lleva_el_fichero(self):
        # Si F5_PACKAGES pidiera algo que el fichero no instala, F5 nunca saldría como instalado.
        pins = full("f5")
        for package in C.F5_PACKAGES:
            with self.subTest(paquete=package):
                self.assertIn(norm(package), pins)

    def test_el_motor_mira_lo_que_f5_importa(self):
        # Sin bitsandbytes, que no existe en todas las plataformas y F5 solo usa para entrenar.
        wanted = set(F5_IMPORTA.values()) - {"bitsandbytes"}
        self.assertEqual(sorted(wanted - {norm(p) for p in C.F5_PACKAGES}), [])

    def test_las_licencias_de_f5_siguen_declaradas_y_solo_ahi(self):
        for package in ("unidecode", "encodec", "frozendict"):
            self.assertIn(package, full("f5"))

    @unittest.skipUnless(F5_PYTHON, "hace falta ST_F5_PYTHON: el Python de un venv con F5")
    def test_coincide_con_su_codigo(self):
        p = subprocess.run([F5_PYTHON, "-c", F5_SCRIPT], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        seen = json.loads(p.stdout)
        self.assertEqual(seen["version"], full("f5")["f5-tts"])
        # Todo lo que importa F5 está en la lista de arriba, y al revés.
        self.assertEqual(sorted(set(seen["importa"]) - F5_NO_HACEN_FALTA), sorted(F5_IMPORTA))
        # Todo lo que piden, también en segundo nivel, sale del fichero (salvo lo que trae cada build de PyTorch).
        missing = [n for n in seen["cierre"] if n not in full("f5") and not re.match(r"nvidia-|triton$", n)]
        self.assertEqual(missing, [])


class Notice(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "NOTICE").read_text("utf-8")

    def test_todo_lo_que_se_instala_esta_declarado(self):
        names = {norm(n) for n in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]+", self.text)}
        extra = {"beautifulsoup4": "beautiful", "torch": "pytorch", "pdfminer-six": "pdfminer-six"}
        for folder in ["base", "xtts", "xtts-languages", "whisper", "f5", "translate"]:
            for package in full(folder):
                with self.subTest(paquete=package):
                    self.assertIn(extra.get(package, package), names)

    def test_no_se_declara_lo_que_ya_no_se_instala(self):
        block = self.text.replace("\r\n", "\n")
        block = block[block.index("Installed along with the above"):block.index("The LGPL and MPL packages")]
        installed = {p for f in ("base", "xtts", "xtts-languages", "whisper", "f5", "translate") for p in full(f)}
        for line in block.splitlines()[4:]:
            if line.startswith("  "):
                for name in map(str.strip, re.split(r"[,;]", re.sub(r"\([^)]*\)+", "", line))):
                    if name:
                        with self.subTest(paquete=name):
                            self.assertIn(norm(name), installed)

    def test_gradio_ya_no_se_declara(self):
        self.assertNotIn("gradio", self.text)

    def test_av_no_va_entre_las_bsd_sueltas(self):
        block = re.search(r"BSD licences:\n(.*?)\nApache License", self.text.replace("\r\n", "\n"), re.S).group(1)
        self.assertNotRegex(block, r"(^|[\s,])av([\s,(]|$)")

    def test_av_dice_lo_que_lleva_y_de_donde_viene(self):
        for word in ("libx264", "libx265", "GPL", "LGPL", "PyAV", "Whisper"):
            self.assertIn(word, self.text)


if __name__ == "__main__":
    unittest.main()
