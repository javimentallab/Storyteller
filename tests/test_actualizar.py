# -*- coding: utf-8 -*-
"""«Buscar actualizaciones»: versiones, red simulada, descarga, instalación y el flujo de la ventana.
Sin red de verdad, sin GPU y sin sonido.

    python -m unittest tests.test_actualizar -v
"""
import hashlib, importlib.util, io, json, os, shutil, socket, subprocess, sys, tempfile, unittest, urllib.error, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_update as U

QT = importlib.util.find_spec("PySide6") is not None
REPO = "https://github.com/javimentallab/Storyteller"
API = "https://api.github.com/repos/javimentallab/Storyteller"


class Resp(io.BytesIO):
    def __init__(self, data, length=True):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))} if length else {}


class FakeNet:
    """Hace de GitHub: url → bytes, excepción o función. Apunta lo que se pide y con qué cabeceras."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, dict(req.header_items()), timeout))
        r = self.routes.get(req.full_url)
        if r is None:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        if isinstance(r, BaseException):
            raise r
        return Resp(r) if isinstance(r, bytes) else r()


def make_zip(files, top="javimentallab-Storyteller-abc1234/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(top + name, data)
    return buf.getvalue()


def program(version, extra=None):
    files = {"storyteller.py": f'"""app"""\n__version__ = "{version}"\n',
             "st_core.py": f"# core {version}\n",
             "engine/cc_xtts.py": f"# motor {version}\n",
             "engine/deps/xtts/requirements.txt": "coqui-tts==0.27.2\n",
             "i18n/en.txt": "0|x\n",
             "storyteller.sh": "#!/usr/bin/env bash\n"}
    files.update(extra or {})
    return files


def release_json(tag="v1.3.0", assets=None, **kw):
    d = {"tag_name": tag, "html_url": f"{REPO}/releases/tag/{tag}", "draft": False, "prerelease": False,
         "zipball_url": f"{API}/zipball/{tag}", "assets": assets or []}
    d.update(kw)
    return json.dumps(d).encode()


class Versiones(unittest.TestCase):
    def test_orden_semantico(self):
        self.assertEqual(U.compare_versions("1.10.0", "1.9.9"), 1)          # no es orden alfabético
        self.assertEqual(U.compare_versions("v1.2.1", "1.2.1"), 0)
        self.assertEqual(U.compare_versions("1.3", "1.3.0"), 0)
        self.assertEqual(U.compare_versions("2.0.0", "10.0.0"), -1)
        self.assertEqual(U.compare_versions("1.2.1+build5", "1.2.1"), 0)

    def test_las_previas_van_antes(self):
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                 "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]                     # el ejemplo de semver.org
        for a, b in zip(chain, chain[1:]):
            with self.subTest(a=a, b=b):
                self.assertEqual(U.compare_versions(a, b), -1)
                self.assertEqual(U.compare_versions(b, a), 1)

    def test_lo_que_no_es_version_nunca_es_mas_nueva(self):
        for bad in ("", "latest", "1.2.x", "v", "1..2", "1.2.3-"):
            with self.subTest(bad=bad):
                self.assertIsNone(U.parse_version(bad))
                self.assertFalse(U.is_newer(bad, "1.2.1"))
        self.assertTrue(U.is_newer("v1.2.2", "1.2.1"))
        self.assertFalse(U.is_newer("v1.2.1", "1.2.1"))
        self.assertFalse(U.is_newer("1.2.0", "1.2.1"))


class Comprobar(unittest.TestCase):
    def test_pide_la_ultima_con_un_agente_generico_y_poco_tiempo(self):
        net = FakeNet({U.API_LATEST: release_json()})
        rel = U.fetch_latest("1.2.1", opener=net)
        self.assertEqual((rel.version, rel.tag), ("1.3.0", "v1.3.0"))
        url, headers, timeout = net.calls[0]
        self.assertEqual(url, "https://api.github.com/repos/javimentallab/Storyteller/releases/latest")
        self.assertEqual(headers.get("User-agent"), "Storyteller-updater/1.2.1")
        self.assertEqual(set(headers), {"User-agent", "Accept"})           # ni cookies ni datos del usuario
        self.assertLessEqual(timeout, 10)
        self.assertEqual(rel.download.url, f"{API}/zipball/v1.3.0")
        self.assertFalse(rel.download.verifiable)

    def test_sin_conexion_o_tiempo_agotado(self):
        for exc in (urllib.error.URLError("getaddrinfo failed"), socket.timeout("timed out"),
                    ConnectionResetError()):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(U.UpdateError) as cm:
                    U.fetch_latest("1.2.1", opener=FakeNet({U.API_LATEST: exc}))
                self.assertEqual(cm.exception.kind, "network")

    def test_error_de_github(self):
        with self.assertRaises(U.UpdateError) as cm:
            U.fetch_latest("1.2.1", opener=FakeNet({}))
        self.assertEqual((cm.exception.kind, cm.exception.detail), ("http", "404"))

    def test_respuestas_raras(self):
        cases = {"no es JSON": b"<html>", "sin tag": release_json(tag=""), "borrador": release_json(draft=True),
                 "previa": release_json(prerelease=True), "otra página": release_json(html_url="https://evil.example/x"),
                 "zip de otro repo": release_json(zipball_url="https://api.github.com/repos/otro/repo/zipball/v1.3.0")}
        for name, body in cases.items():
            with self.subTest(name):
                with self.assertRaises(U.UpdateError) as cm:
                    U.fetch_latest("1.2.1", opener=FakeNet({U.API_LATEST: body}))
                self.assertEqual(cm.exception.kind, "bad")

    def test_prefiere_un_zip_adjunto_con_su_huella(self):
        sha = "ab" * 32
        assets = [{"name": "notas.txt", "browser_download_url": f"{REPO}/releases/download/v1.3.0/notas.txt"},
                  {"name": "Storyteller-1.3.0.zip", "digest": "sha256:" + sha, "state": "uploaded",
                   "browser_download_url": f"{REPO}/releases/download/v1.3.0/Storyteller-1.3.0.zip"}]
        rel = U.fetch_latest("1.2.1", opener=FakeNet({U.API_LATEST: release_json(assets=assets)}))
        self.assertEqual((rel.download.name, rel.download.sha256), ("Storyteller-1.3.0.zip", sha))
        self.assertTrue(rel.download.verifiable)

    def test_un_adjunto_de_fuera_del_repo_no_vale(self):
        assets = [{"name": "Storyteller-1.3.0.zip", "browser_download_url": "https://evil.example/Storyteller-1.3.0.zip"}]
        rel = U.fetch_latest("1.2.1", opener=FakeNet({U.API_LATEST: release_json(assets=assets)}))
        self.assertEqual(rel.download.url, f"{API}/zipball/v1.3.0")       # se queda con el código de la versión

    def test_solo_sigue_redirecciones_a_github(self):
        h = U._SafeRedirect()
        req = U.urllib.request.Request(f"{API}/zipball/v1.3.0")
        ok = h.redirect_request(req, None, 302, "Found", {}, "https://codeload.github.com/javimentallab/Storyteller/zip/v1.3.0")
        self.assertIsNotNone(ok)
        for bad in ("https://evil.example/x.zip", "http://codeload.github.com/x.zip"):
            with self.subTest(bad=bad), self.assertRaises(U.UpdateError):
                h.redirect_request(req, None, 302, "Found", {}, bad)


class Bajar(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="st-update-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_comprueba_la_huella(self):
        data = make_zip(program("1.3.0"))
        dl = U.Download(f"{REPO}/releases/download/v1.3.0/S.zip", "S.zip", hashlib.sha256(data).hexdigest())
        dest = self.tmp / "S.zip"
        seen = []
        U.download(dl, dest, "1.2.1", opener=FakeNet({dl.url: data}), progress=lambda g, t: seen.append((g, t)))
        self.assertEqual(dest.read_bytes(), data)
        self.assertEqual(seen[-1], (len(data), len(data)))

        dl.sha256 = "0" * 64
        dest.unlink()
        with self.assertRaises(U.UpdateError) as cm:
            U.download(dl, dest, "1.2.1", opener=FakeNet({dl.url: data}))
        self.assertEqual(cm.exception.kind, "checksum")
        self.assertEqual(list(self.tmp.iterdir()), [])                     # ni el archivo ni el .part

    def test_huella_en_un_sha256sums(self):
        data = make_zip(program("1.3.0"))
        sums = f"{'1' * 64}  otro.zip\n{hashlib.sha256(data).hexdigest()}  S.zip\n".encode()
        dl = U.Download(f"{REPO}/releases/download/v1.3.0/S.zip", "S.zip",
                        sums_url=f"{REPO}/releases/download/v1.3.0/SHA256SUMS")
        U.download(dl, self.tmp / "S.zip", "1.2.1", opener=FakeNet({dl.url: data, dl.sums_url: sums}))
        self.assertTrue((self.tmp / "S.zip").exists())

    def test_cancelar_y_cortes(self):
        data = make_zip(program("1.3.0"))
        dl = U.Download(f"{API}/zipball/v1.3.0", "S.zip")
        with self.assertRaises(U.UpdateError) as cm:
            U.download(dl, self.tmp / "S.zip", "1.2.1", opener=FakeNet({dl.url: data}), stopped=lambda: True)
        self.assertEqual(cm.exception.kind, "canceled")

        class Cut(Resp):
            def read(self, n=-1):
                raise socket.timeout("timed out")
        with self.assertRaises(U.UpdateError) as cm:
            U.download(dl, self.tmp / "S.zip", "1.2.1", opener=FakeNet({dl.url: lambda: Cut(data)}))
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(list(self.tmp.iterdir()), [])


class Instalar(unittest.TestCase):
    """Una instalación de mentira con todo lo del usuario alrededor."""
    USER = {"config.json": '{"model": "xtts-v2"}', "server.json": "{}", "pron.json": "{}", "service.key": "k",
            "books/mi-libro/manifest.json": "[]", "books/mi-libro/gen_state.json": '{"done": 3}',
            "books/mi-libro/playback.json": '{"pos": 12}', "books/mi-libro/cap01.mp3": "audio",
            "models/xtts-v2/model.pth": "pesos", "engine/voices/narrator.wav": "voz",
            "engine/.venv-xtts/Scripts/python.exe": "py", "engine/.venv/pyvenv.cfg": "cfg",
            "translations/t.json": "{}"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="st-update-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "cuentacuentos"
        for rel, text in {**program("1.2.1"), **self.USER}.items():
            p = self.app / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(text.encode())
        self.before = {rel: (self.app / rel).read_bytes() for rel in self.USER}

    def zip(self, files, top="javimentallab-Storyteller-abc1234/"):
        p = self.tmp / "S.zip"; p.write_bytes(make_zip(files, top)); return p

    def assert_user_data_intact(self):
        for rel, data in self.before.items():
            self.assertEqual((self.app / rel).read_bytes(), data, rel)

    def test_cambia_el_programa_y_respeta_lo_del_usuario(self):
        new = program("1.3.0", {"engine/deps/xtts/requirements.txt": "coqui-tts==0.27.3\n", "st_nuevo.py": "# nuevo\n",
                                "engine/deps/nuevo/requirements.txt": "algo==1.0\n",
                                # aunque viniera en el archivo, lo del usuario no se toca
                                "config.json": "{}", "books/mi-libro/gen_state.json": "{}",
                                "models/xtts-v2/model.pth": "otro", "engine/.venv-xtts/Scripts/python.exe": "otro"})
        changed, reqs = U.install(self.zip(new), "1.3.0", self.app, "1.2.1")
        self.assertIn('__version__ = "1.3.0"', (self.app / "storyteller.py").read_text("utf-8"))
        self.assertEqual((self.app / "st_nuevo.py").read_text("utf-8"), "# nuevo\n")
        self.assertEqual(reqs, ["engine/deps/xtts/requirements.txt"])
        self.assertNotIn("i18n/en.txt", changed)                           # lo que no cambia no se reescribe
        self.assert_user_data_intact()
        bak = self.app / ".update" / "backup-1.2.1"
        self.assertIn('__version__ = "1.2.1"', (bak / "storyteller.py").read_text("utf-8"))
        self.assertFalse((self.app / ".update" / "staging").exists())

    def test_zip_sin_carpeta_de_arriba(self):
        U.install(self.zip(program("1.3.0"), top=""), "1.3.0", self.app, "1.2.1")
        self.assertIn('"1.3.0"', (self.app / "storyteller.py").read_text("utf-8"))

    def test_no_es_la_version_anunciada(self):
        with self.assertRaises(U.UpdateError) as cm:
            U.install(self.zip(program("1.2.9")), "1.3.0", self.app, "1.2.1")
        self.assertEqual(cm.exception.kind, "bad")
        self.assertIn('"1.2.1"', (self.app / "storyteller.py").read_text("utf-8"))

    def test_rutas_que_se_salen(self):
        for evil in ("../fuera.py", "/abs.py", "C:/win.py", "a\\..\\b.py"):
            with self.subTest(evil=evil):
                with self.assertRaises(U.UpdateError) as cm:
                    U.install(self.zip({**program("1.3.0"), evil: "x"}, top=""), "1.3.0", self.app, "1.2.1")
                self.assertEqual(cm.exception.kind, "bad")
        self.assertFalse((self.tmp / "fuera.py").exists())
        self.assertIn('"1.2.1"', (self.app / "storyteller.py").read_text("utf-8"))

    def test_si_falla_a_medias_lo_deja_como_estaba(self):
        real = U.os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:
                raise PermissionError(13, "en uso")
            return real(src, dst)
        new = program("1.3.0", {"st_nuevo.py": "# nuevo\n"})
        U.os.replace = flaky
        try:
            with self.assertRaises(U.UpdateError) as cm:
                U.install(self.zip(new), "1.3.0", self.app, "1.2.1")
        finally:
            U.os.replace = real
        self.assertEqual(cm.exception.kind, "install")
        for rel, text in program("1.2.1").items():
            self.assertEqual((self.app / rel).read_bytes(), text.encode(), rel)
        self.assertFalse((self.app / "st_nuevo.py").exists())
        self.assert_user_data_intact()

    def test_cuando_no_es_seguro_cambiarse_sola(self):
        self.assertIsNone(U.install_problem(self.app, self.app))
        self.assertEqual(U.install_problem(self.app, self.tmp / "otra")[0], "elsewhere")
        (self.app / ".git").write_text("gitdir: ../x", "utf-8")             # también un worktree
        self.assertEqual(U.install_problem(self.app, self.app)[0], "git")

    def test_que_es_del_usuario(self):
        for rel in ("config.json", "books/x/manifest.json", "models/a.pth", "engine/voices/n.wav",
                    "engine/.venv-xtts/x", "engine/.venv-translate/y", "engine/__pycache__/a.pyc", "Config.JSON"):
            self.assertTrue(U.is_user_data(rel), rel)
        for rel in ("storyteller.py", "engine/cc_xtts.py", "engine/deps/xtts/requirements.txt", "i18n/en.txt",
                    "docs/player.png", "README.md"):
            self.assertFalse(U.is_user_data(rel), rel)


PROBE = r"""
import json, sys
root, case, appdir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
sys.argv = ["storyteller.py", "--lang", "es"]
import storyteller as S
import st_update as U
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])
win = S.Cuentacuentos()
said, runs = [], []
QMessageBox.information = staticmethod(lambda parent, title, text, *a: said.append(("info", text)))
QMessageBox.question = staticmethod(lambda parent, title, text, *a: (said.append(("ask", text)),
                                    QMessageBox.StandardButton.No)[1])
S.warn = lambda parent, text, title="": said.append(("warn", text))
win._run_update = lambda rel, dest, install: runs.append((str(dest), install))
rel = U.Release("9.0.0", "v9.0.0", "https://github.com/javimentallab/Storyteller/releases/tag/v9.0.0",
                U.Download("https://api.github.com/repos/javimentallab/Storyteller/zipball/v9.0.0", "Storyteller-9.0.0.zip"))
if case == "same":
    win._update_found(U.Release(S.__version__, "v" + S.__version__, rel.page, rel.download))
elif case == "busy":
    win._gen_busy = True
    win._update_found(rel)
elif case == "git":
    U.install_problem = lambda app_dir, home: ("git", str(app_dir))
    win._update_found(rel)
elif case == "ok":
    U.install_problem = lambda app_dir, home: None
    QMessageBox.question = staticmethod(lambda parent, title, text, *a: (said.append(("ask", text)),
                                        QMessageBox.StandardButton.Yes)[1])
    win._update_found(rel)
elif case == "install":
    del win._run_update                                  # el de verdad, con su hilo
    got = {}
    def fake_download(dl, dest, current, opener=None, progress=None, stopped=None):
        progress(50, 100); dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(b"zip")
    def fake_install(zip_path, version, app_dir, current):
        got.update(zip=str(zip_path), version=version, app=str(app_dir)); return ["x.py"], ["requirements.txt"]
    U.download, U.install = fake_download, fake_install
    S.APP_DIR = S.Path(appdir)
    win._restart = lambda: runs.append(("restart", True))
    win._run_update(rel, S.APP_DIR / ".update" / rel.download.name, True)
    win._updDl.wait(5000)
    for _i in range(20): app.processEvents()
    said.append(("got", json.dumps(got)))
    said.append(("zip left", str((S.APP_DIR / ".update" / rel.download.name).exists())))
elif case == "offline":
    def boom(current, opener=None, timeout=None):
        raise U.UpdateError("network", "sin red")
    U.fetch_latest = boom
    win.check_updates()
    win._updCheck.wait(5000)
    app.processEvents()
print(json.dumps({"said": said, "runs": runs, "button": win.library.updateBtn.text(),
                  "enabled": win.library.updateBtn.isEnabled()}, ensure_ascii=False))
"""


@unittest.skipUnless(QT, "hace falta PySide6")
class Ventana(unittest.TestCase):
    """El flujo de la ventana con GitHub simulado: nunca baja nada sin preguntar ni con trabajo en marcha."""
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="st-update-win-"))
        shutil.copytree(ROOT / "engine", cls.home / "cuentacuentos" / "engine",
                        ignore=shutil.ignore_patterns(".venv*", "__pycache__", "voices", "llama.cpp"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def see(self, case):
        env = dict(os.environ, USERPROFILE=str(self.home), HOME=str(self.home), QT_QPA_PLATFORM="offscreen",
                   PYTHONIOENCODING="utf-8")
        app = self.home / "app"                                          # nunca la carpeta del repo
        p = subprocess.run([sys.executable, "-c", PROBE, str(ROOT), case, str(app)], capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_ya_tiene_la_ultima(self):
        s = self.see("same")
        self.assertEqual(len(s["said"]), 1)
        self.assertIn("Ya tienes la última versión", s["said"][0][1])
        self.assertEqual(s["runs"], [])

    def test_con_una_narracion_en_marcha_solo_avisa(self):
        s = self.see("busy")
        self.assertEqual([k for k, _t in s["said"]], ["info"])
        self.assertIn("9.0.0", s["said"][0][1])
        self.assertIn("no se actualiza a medias", s["said"][0][1])
        self.assertEqual(s["runs"], [])

    def test_un_clon_de_git_no_se_pisa(self):
        s = self.see("git")
        self.assertEqual(s["said"][0][0], "ask")
        self.assertIn("git pull", s["said"][0][1])
        self.assertEqual(s["runs"], [])                                     # dijo que no: nada

    def test_pide_permiso_y_explica_que_se_conserva(self):
        s = self.see("ok")
        kind, text = s["said"][0]
        self.assertEqual(kind, "ask")
        for word in ("configuración", "libros", "modelos", "motores", "SHA-256"):
            self.assertIn(word, text)
        self.assertEqual(len(s["runs"]), 1)
        dest, install = s["runs"][0]
        self.assertTrue(install)
        self.assertTrue(dest.endswith(os.path.join(".update", "Storyteller-9.0.0.zip")))

    def test_baja_instala_y_reinicia(self):
        s = self.see("install")
        said = dict(s["said"])
        self.assertIn("requirements.txt", said["info"])                   # avisa de los requisitos que cambian
        self.assertIn("9.0.0", said["info"])
        got = json.loads(said["got"])
        self.assertEqual((got["version"], Path(got["app"])), ("9.0.0", self.home / "app"))
        self.assertEqual(said["zip left"], "False")                       # no deja el zip tirado
        self.assertEqual(s["runs"], [["restart", True]])

    def test_sin_conexion(self):
        s = self.see("offline")
        self.assertEqual(s["said"][0][0], "warn")
        self.assertIn("No pude hablar con GitHub", s["said"][0][1])
        self.assertEqual((s["button"], s["enabled"]), ("Buscar actualizaciones", True))


if __name__ == "__main__":
    unittest.main()
