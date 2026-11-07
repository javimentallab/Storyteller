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
        self.assertEqual(U.update_mode(self.app, self.app), ("zip", None, None))
        self.assertEqual(U.update_mode(self.app, self.tmp / "otra")[:2], ("download", "elsewhere"))
        (self.app / ".git").write_text("gitdir: ../no-existe", "utf-8")   # un .git roto no se toca
        self.assertEqual(U.update_mode(self.app, self.app)[:2], ("page", "no_git"))
        self.assertEqual(U.update_mode(self.app, self.tmp / "otra")[:2], ("page", "elsewhere"))

    def test_que_es_del_usuario(self):
        for rel in ("config.json", "books/x/manifest.json", "models/a.pth", "engine/voices/n.wav",
                    "engine/.venv-xtts/x", "engine/.venv-translate/y", "engine/__pycache__/a.pyc", "Config.JSON"):
            self.assertTrue(U.is_user_data(rel), rel)
        for rel in ("storyteller.py", "engine/cc_xtts.py", "engine/deps/xtts/requirements.txt", "i18n/en.txt",
                    "docs/player.png", "README.md"):
            self.assertFalse(U.is_user_data(rel), rel)


GIT = shutil.which("git")
GIT_TEST = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]


def git(cwd, *args):
    r = subprocess.run(["git", *GIT_TEST, *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@unittest.skipUnless(GIT, "hace falta git")
class ClonDeGit(unittest.TestCase):
    """official hace de javimentallab/Storyteller (v1.2.1 y luego v1.3.0); app es un clon que se quedó en la
    1.2.1, con su origin apuntando a otro sitio y los datos del usuario fuera de git."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="st-update-git-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.official, self.app = self.tmp / "official", self.tmp / "app"
        self.official.mkdir()
        git(self.official, "init", "-q")
        self.put(self.official, {**program("1.2.1"), ".gitignore": "config.json\nbooks/\nmodels/\n",
                                 "requirements.txt": "PySide6-Essentials==6.12.0\n"})
        git(self.official, "add", ".")
        git(self.official, "commit", "-q", "-m", "Versión 1.2.1")
        git(self.official, "tag", "v1.2.1")
        git(self.tmp, "clone", "-q", "-c", "core.autocrlf=false", str(self.official), str(self.app))
        git(self.app, "remote", "set-url", "origin", str(self.tmp / "un-fork-que-no-existe"))
        self.put(self.official, {"storyteller.py": '"""app"""\n__version__ = "1.3.0"\n',
                                 "engine/deps/xtts/requirements.txt": "coqui-tts==0.27.3\n",
                                 "engine/deps/nuevo/requirements.txt": "algo==1.0\n"})
        git(self.official, "add", ".")
        git(self.official, "commit", "-q", "-m", "Versión 1.3.0")
        git(self.official, "tag", "-a", "v1.3.0", "-m", "Storyteller 1.3.0")
        self.put(self.app, {"config.json": '{"model": "xtts-v2"}', "books/mi-libro/gen_state.json": '{"done": 3}',
                            "models/xtts-v2/model.pth": "pesos"})
        self.old = self.head(self.app)

    @staticmethod
    def put(root, files):
        for rel, text in files.items():
            p = Path(root) / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(text.encode())

    @staticmethod
    def head(root):
        return git(root, "rev-parse", "HEAD")

    def install(self, tag="v1.3.0"):
        return U.git_install(tag, self.app, repo_url=str(self.official))

    def assert_user_data_intact(self):
        self.assertEqual((self.app / "config.json").read_text("utf-8"), '{"model": "xtts-v2"}')
        self.assertEqual((self.app / "books/mi-libro/gen_state.json").read_text("utf-8"), '{"done": 3}')
        self.assertEqual((self.app / "models/xtts-v2/model.pth").read_text("utf-8"), "pesos")

    def test_un_clon_limpio_avanza_hasta_la_etiqueta_del_repo_oficial(self):
        self.assertEqual(U.update_mode(self.app, self.app), ("git", None, None))   # los datos no cuentan
        new, reqs = self.install()
        self.assertEqual(new, git(self.official, "rev-parse", "v1.3.0^{commit}"))
        self.assertEqual(self.head(self.app), new)                          # del oficial, aunque el origin falle
        self.assertIn('"1.3.0"', (self.app / "storyteller.py").read_text("utf-8"))
        self.assertEqual(reqs, ["engine/deps/xtts/requirements.txt"])        # el nuevo no se avisa
        self.assertEqual(git(self.app, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(git(self.app, "rev-parse", "--abbrev-ref", "HEAD"), "main")   # la rama avanza
        self.assert_user_data_intact()

    def test_con_cambios_sin_confirmar_no_toca_nada(self):
        (self.app / "st_core.py").write_bytes(b"# mi cambio\n")
        self.assertEqual(U.update_mode(self.app, self.app)[:2], ("page", "local_changes"))
        with self.assertRaises(U.UpdateError) as cm:
            self.install()
        self.assertEqual(cm.exception.kind, "local_changes")
        self.assertEqual(self.head(self.app), self.old)
        self.assertEqual((self.app / "st_core.py").read_bytes(), b"# mi cambio\n")
        self.assert_user_data_intact()

    def test_con_commits_propios_no_toca_nada(self):
        self.put(self.app, {"st_core.py": "# mío\n"})
        git(self.app, "commit", "-q", "-am", "Lo mío")
        mine = self.head(self.app)
        self.assertEqual(U.update_mode(self.app, self.app)[0], "git")      # solo se sabe tras bajar la etiqueta
        with self.assertRaises(U.UpdateError) as cm:
            self.install()
        self.assertEqual(cm.exception.kind, "diverged")
        self.assertEqual(self.head(self.app), mine)
        self.assertEqual((self.app / "st_core.py").read_text("utf-8"), "# mío\n")
        self.assertIn('"1.2.1"', (self.app / "storyteller.py").read_text("utf-8"))

    def test_sin_git_no_toca_nada(self):
        real = U._git_exe
        U._git_exe = lambda: None
        try:
            self.assertEqual(U.update_mode(self.app, self.app)[:2], ("page", "no_git"))
            with self.assertRaises(U.UpdateError) as cm:
                self.install()
        finally:
            U._git_exe = real
        self.assertEqual(cm.exception.kind, "no_git")
        self.assertEqual(self.head(self.app), self.old)

    def test_una_etiqueta_que_no_existe_falla_sin_cambiar_nada(self):
        with self.assertRaises(U.UpdateError) as cm:
            self.install("v9.9.9")
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(self.head(self.app), self.old)

    def test_lo_que_no_es_una_version_ni_se_pide(self):
        for bad in ("main", "v1.3.0;rm -rf", "--upload-pack=x", "v1.3.0 HEAD"):
            with self.subTest(bad=bad), self.assertRaises(U.UpdateError) as cm:
                self.install(bad)
            self.assertEqual(cm.exception.kind, "bad")
        self.assertEqual(self.head(self.app), self.old)

    def test_un_clon_fuera_de_la_carpeta_de_datos_no_se_toca(self):
        self.assertEqual(U.update_mode(self.app, self.tmp / "cuentacuentos")[:2], ("page", "elsewhere"))


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
win._run_update = lambda rel, dest, mode: runs.append((str(dest), mode))
opened = []
S.QDesktopServices.openUrl = lambda url: opened.append(url.toString())
rel = U.Release("9.0.0", "v9.0.0", "https://github.com/javimentallab/Storyteller/releases/tag/v9.0.0",
                U.Download("https://api.github.com/repos/javimentallab/Storyteller/zipball/v9.0.0", "Storyteller-9.0.0.zip"))
if case == "same":
    win._update_found(U.Release(S.__version__, "v" + S.__version__, rel.page, rel.download))
elif case == "busy":
    win._gen_busy = True
    win._update_found(rel)
elif case in ("dirty", "dirty-yes"):
    U.update_mode = lambda app_dir, home: ("page", "local_changes", str(app_dir))
    if case == "dirty-yes":
        QMessageBox.question = staticmethod(lambda parent, title, text, *a: (said.append(("ask", text)),
                                            QMessageBox.StandardButton.Yes)[1])
    win._update_found(rel)
elif case in ("ok", "git"):
    U.update_mode = lambda app_dir, home: ("zip" if case == "ok" else "git", None, None)
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
    win._run_update(rel, S.APP_DIR / ".update" / rel.download.name, "zip")
    win._updDl.wait(5000)
    for _i in range(20): app.processEvents()
    said.append(("got", json.dumps(got)))
    said.append(("zip left", str((S.APP_DIR / ".update" / rel.download.name).exists())))
elif case in ("git-install", "git-diverged"):
    del win._run_update
    got = {}
    def fake_git(tag, root, repo_url=U.REPO_GIT):
        got.update(tag=tag, root=str(root), repo=repo_url)
        if case == "git-diverged":
            raise U.UpdateError("diverged")
        return "a" * 40, ["engine/deps/xtts/requirements.txt"]
    U.git_install = fake_git
    U.download = lambda *a, **k: (_ for _ in ()).throw(AssertionError("en git no se baja el zip"))
    S.APP_DIR = S.Path(appdir)
    win._restart = lambda: runs.append(("restart", True))
    win._run_update(rel, S.APP_DIR / ".update" / rel.download.name, "git")
    win._updDl.wait(5000)
    for _i in range(20): app.processEvents()
    said.append(("got", json.dumps(got)))
elif case == "offline":
    def boom(current, opener=None, timeout=None):
        raise U.UpdateError("network", "sin red")
    U.fetch_latest = boom
    win.check_updates()
    win._updCheck.wait(5000)
    app.processEvents()
print(json.dumps({"said": said, "runs": runs, "opened": opened, "button": win.library.updateBtn.text(),
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

    def test_un_clon_con_cambios_no_se_pisa(self):
        s = self.see("dirty")
        self.assertEqual(s["said"][0][0], "ask")
        self.assertIn("cambios sin confirmar", s["said"][0][1])
        self.assertIn("¿Abro la página de la versión?", s["said"][0][1])
        self.assertEqual((s["runs"], s["opened"]), ([], []))               # dijo que no: nada
        s = self.see("dirty-yes")
        self.assertEqual(s["runs"], [])                                     # ni baja ni instala nada
        self.assertEqual(s["opened"], ["https://github.com/javimentallab/Storyteller/releases/tag/v9.0.0"])

    def test_un_clon_limpio_se_actualiza_con_git(self):
        s = self.see("git")
        kind, text = s["said"][0]
        self.assertEqual(kind, "ask")
        for word in ("clon de git", "v9.0.0", "repositorio oficial", "configuración", "modelos"):
            self.assertIn(word, text)
        self.assertNotIn("SHA-256", text)
        self.assertEqual(s["runs"][0][1], "git")

    def test_git_avanza_avisa_de_requisitos_y_reinicia(self):
        s = self.see("git-install")
        said = dict(s["said"])
        got = json.loads(said["got"])
        self.assertEqual((got["tag"], got["repo"]), ("v9.0.0", "https://github.com/javimentallab/Storyteller.git"))
        self.assertEqual(Path(got["root"]), self.home / "app")
        self.assertIn("engine/deps/xtts/requirements.txt", said["info"])
        self.assertEqual(s["runs"], [["restart", True]])

    def test_git_con_commits_propios_ofrece_la_pagina(self):
        s = self.see("git-diverged")
        asks = [t for k, t in s["said"] if k == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertIn("commits propios", asks[0])
        self.assertNotIn("warn", [k for k, _t in s["said"]])
        self.assertEqual(s["runs"], [])                                     # ni reinicia

    def test_pide_permiso_y_explica_que_se_conserva(self):
        s = self.see("ok")
        kind, text = s["said"][0]
        self.assertEqual(kind, "ask")
        for word in ("configuración", "libros", "modelos", "motores", "SHA-256"):
            self.assertIn(word, text)
        self.assertEqual(len(s["runs"]), 1)
        dest, mode = s["runs"][0]
        self.assertEqual(mode, "zip")
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
