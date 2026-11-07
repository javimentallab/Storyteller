"""
Actualizar Storyteller desde sus versiones de GitHub, solo cuando se pulsa «Buscar actualizaciones».

Sin Qt, para poder probarlo con la red simulada. Pide la última versión publicada a la API de GitHub
(HTTPS, User-Agent genérico, sin datos del usuario), la compara con la instalada y, si hay una nueva,
baja su archivo, comprueba su SHA-256 cuando la versión lo publica y cambia SOLO los archivos del
programa: la configuración, los libros (con lo que lleva cada narración), las voces, los modelos y los
entornos de los motores no vienen en el archivo y, por si acaso, nunca se escriben.
"""
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

REPO = "javimentallab/Storyteller"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/"
ASSET_PREFIX = f"https://github.com/{REPO}/releases/download/"
ZIPBALL_PREFIX = f"https://api.github.com/repos/{REPO}/zipball/"
# Adonde puede llevar GitHub una descarga (las redirecciones de los archivos y del código fuente).
HOSTS = {"api.github.com", "github.com", "codeload.github.com",
         "objects.githubusercontent.com", "release-assets.githubusercontent.com"}

CHECK_TIMEOUT = 8              # segundos: la comprobación nunca deja colgada la ventana
DOWNLOAD_TIMEOUT = 20          # por cada lectura de la descarga, no para toda
MAX_DOWNLOAD = 150 * 2 ** 20   # el código fuente ocupa unos pocos MB
MAX_UNPACKED = 300 * 2 ** 20
MAX_FILES = 5000

# Lo que es del usuario y no del programa. No viene en las versiones (está en .gitignore); si llegara,
# se salta igualmente.
USER_DIRS = ("books", "models", "translations", "muestras_voz", "engine/voices", "engine/llama.cpp",
             ".git", ".update", ".venv-tests")
USER_FILES = {"config.json", "server.json", "translate.json", "pron.json", "service.key", "service_qc.key",
              "engine/.ready"}
USER_SUFFIXES = {".wav", ".mp3", ".epub", ".log"}


class UpdateError(Exception):
    """kind: network (sin conexión o GitHub no responde), http (GitHub responde con un error),
    bad (lo que llega no es una versión válida), checksum, install o canceled."""
    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, str(detail)


# ---------------------------------------------------------------- versiones
_VER = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(text):
    """«v1.2.1», «1.3» o «2.0.0-beta.1» → clave para comparar según SemVer; None si no es una versión.
    Lo que falta cuenta como 0 (1.3 = 1.3.0); una versión previa va antes que la final; los metadatos de
    compilación (+…) no cuentan."""
    m = _VER.match((text or "").strip())
    if not m:
        return None
    nums = tuple(int(g or 0) for g in m.group(1, 2, 3))
    if not m.group(4):
        return nums + ((1,),)
    pre = []
    for ident in m.group(4).split("."):
        if not ident:
            return None
        pre.append((0, int(ident), "") if ident.isdigit() else (1, 0, ident))
    return nums + ((0, tuple(pre)),)


def compare_versions(a, b):
    """-1, 0 o 1, como a frente a b. Una versión que no se entiende no es nunca más nueva."""
    ka, kb = parse_version(a), parse_version(b)
    if ka is None or kb is None:
        raise ValueError(f"versión no válida: {a if ka is None else b!r}")
    return (ka > kb) - (ka < kb)


def is_newer(latest, current):
    try:
        return compare_versions(latest, current) > 0
    except ValueError:
        return False


def plain_version(text):
    return (text or "").strip().lstrip("vV")


# ---------------------------------------------------------------- red
def allowed_url(url):
    p = urllib.parse.urlsplit(url or "")
    return p.scheme == "https" and (p.hostname or "").lower() in HOSTS


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Solo sigue redirecciones por HTTPS a los servidores de GitHub."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise UpdateError("bad", f"redirección a {urllib.parse.urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener():
    return urllib.request.build_opener(_SafeRedirect())


def user_agent(current):
    return f"Storyteller-updater/{plain_version(current)}"


def _open(url, current, opener, timeout, accept="*/*"):    # la API del zip da 415 con octet-stream
    if not allowed_url(url):
        raise UpdateError("bad", url)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(current), "Accept": accept})
    try:
        return (opener or build_opener()).open(req, timeout=timeout)
    except UpdateError:
        raise
    except urllib.error.HTTPError as e:
        raise UpdateError("http", e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:     # sin red, DNS, TLS, tiempo agotado
        raise UpdateError("network", getattr(e, "reason", e)) from None


class Release:
    def __init__(self, version, tag, page, download):
        self.version, self.tag, self.page, self.download = version, tag, page, download


class Download:
    """De dónde se baja y con qué se comprueba: sha256 (hex) o sums_url (un SHA256SUMS de la versión)."""
    def __init__(self, url, name, sha256=None, sums_url=None):
        self.url, self.name, self.sha256, self.sums_url = url, name, sha256, sums_url

    @property
    def verifiable(self):
        return bool(self.sha256 or self.sums_url)


def _asset_url(a):
    url = a.get("browser_download_url") or ""
    return url if url.startswith(ASSET_PREFIX) and a.get("state", "uploaded") == "uploaded" else None


def pick_download(data, tag):
    """El archivo de la versión: un zip «Storyteller…» adjunto, con su huella; si no lo hay, el código
    fuente de la versión, que GitHub genera y no publica huella."""
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict) and _asset_url(a)]
    zips = [a for a in assets if re.fullmatch(r"storyteller.*\.zip", a.get("name") or "", re.I)]
    if zips:
        a = zips[0]
        digest = (a.get("digest") or "").lower()
        sha = digest[7:] if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) else None
        sums = None
        if not sha:
            names = {(x.get("name") or "").lower(): _asset_url(x) for x in assets}
            sums = next((names[n] for n in (a["name"].lower() + ".sha256", "sha256sums", "sha256sums.txt")
                         if n in names), None)
        return Download(_asset_url(a), a["name"], sha, sums)
    url = data.get("zipball_url") or ""
    if url != ZIPBALL_PREFIX + urllib.parse.quote(tag, safe=""):
        raise UpdateError("bad", "zipball_url")
    return Download(url, f"Storyteller-{plain_version(tag)}.zip")


def fetch_latest(current, opener=None, timeout=CHECK_TIMEOUT):
    """La última versión publicada (ni borradores ni previas: /releases/latest no las da)."""
    with _open(API_LATEST, current, opener, timeout, accept="application/vnd.github+json") as r:
        raw = r.read(2 * 2 ** 20)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("bad", "JSON") from None
    if not isinstance(data, dict):
        raise UpdateError("bad", "JSON")
    tag = data.get("tag_name") or ""
    page = data.get("html_url") or ""
    if data.get("draft") or data.get("prerelease") or parse_version(tag) is None:
        raise UpdateError("bad", tag or "tag_name")
    if not page.startswith(RELEASES_PAGE):
        raise UpdateError("bad", "html_url")
    return Release(plain_version(tag), tag, page, pick_download(data, tag))


def _expected_sha(dl, current, opener):
    if dl.sha256:
        return dl.sha256
    if not dl.sums_url:
        return None
    with _open(dl.sums_url, current, opener, CHECK_TIMEOUT) as r:
        text = r.read(64 * 1024).decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            if len(parts) == 1 or parts[-1].lstrip("*") == dl.name:
                return parts[0].lower()
    raise UpdateError("checksum", "SHA256SUMS")


def download(dl, dest, current, opener=None, progress=None, stopped=None):
    """Baja dl a dest comprobando la huella si la hay. progress(bajado, total o 0); stopped() → cancelar.
    Si algo falla, no deja nada en dest."""
    want = _expected_sha(dl, current, opener)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    try:
        with _open(dl.url, current, opener, DOWNLOAD_TIMEOUT) as r, open(part, "wb") as f:
            try: total = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError, AttributeError): total = 0
            if total > MAX_DOWNLOAD:
                raise UpdateError("bad", "tamaño")
            got = 0
            while True:
                if stopped and stopped():
                    raise UpdateError("canceled")
                try:
                    chunk = r.read(256 * 1024)
                except OSError as e:                      # se corta a mitad o se agota el tiempo
                    raise UpdateError("network", e) from None
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_DOWNLOAD:
                    raise UpdateError("bad", "tamaño")
                h.update(chunk); f.write(chunk)
                if progress: progress(got, total)
        if total and got != total:
            raise UpdateError("network", "incompleta")
        if want and h.hexdigest() != want:
            raise UpdateError("checksum", h.hexdigest())
        os.replace(part, dest)
    except BaseException:
        try: part.unlink()
        except OSError: pass
        raise
    return h.hexdigest()


# ---------------------------------------------------------------- instalar
def is_user_data(rel):
    """¿Es del usuario (configuración, libros, modelos, voces, entornos…)? Entonces no se toca."""
    s = PurePosixPath(rel).as_posix().lower()
    parts = s.split("/")
    if s in USER_FILES or "__pycache__" in parts:
        return True
    if any(s == d or s.startswith(d + "/") for d in USER_DIRS):
        return True
    if len(parts) >= 2 and parts[0] == "engine" and parts[1].startswith(".venv"):
        return True
    return PurePosixPath(s).suffix in USER_SUFFIXES


def _safe_rel(name):
    """Ruta relativa y limpia dentro del zip; None si intenta salirse de la carpeta."""
    if "\\" in name or ":" in name or name.startswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return PurePosixPath(*parts)


def _version_in(source):
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.M)
    return m.group(1) if m else None


def unpack(zip_path, staging, version):
    """Saca el programa del zip a staging (sin la carpeta de arriba que pone GitHub) y comprueba que sea
    la versión anunciada. Devuelve las rutas relativas que trae, sin lo que es del usuario."""
    staging = Path(staging)
    if staging.exists():
        shutil.rmtree(staging)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdateError("bad", e) from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos or len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise UpdateError("bad", "zip")
        rels = []
        for i in infos:
            rel = _safe_rel(i.filename)
            if rel is None:
                raise UpdateError("bad", i.filename)
            rels.append((i, rel))
        tops = {rel.parts[0] for _i, rel in rels}
        strip = len(tops) == 1 and all(len(rel.parts) > 1 for _i, rel in rels)
        files = []
        for i, rel in rels:
            if strip:
                rel = PurePosixPath(*rel.parts[1:])
            if is_user_data(rel):
                continue
            out = staging.joinpath(*rel.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(i) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = (i.external_attr >> 16) & 0o777
            if mode and os.name != "nt":
                os.chmod(out, mode | 0o200)               # storyteller.sh sigue siendo ejecutable
            files.append(rel.as_posix())
    main = staging / "storyteller.py"
    found = _version_in(main.read_text("utf-8", "replace")) if main.exists() else None
    if not found or compare_versions(found, version) != 0:
        raise UpdateError("bad", f"storyteller.py {found}")
    return sorted(files)


def is_requirements(rel):
    s = PurePosixPath(rel).as_posix()
    return s == "requirements.txt" or bool(re.fullmatch(r"engine/deps/[^/]+/requirements\.txt", s))


def _same(a, b):
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def apply(staging, files, app_dir, backup):
    """Pone en app_dir los archivos de staging que cambian, guardando antes una copia de los viejos en
    backup. Si algo falla, lo deja todo como estaba. Devuelve (cambiados, requisitos que cambian)."""
    staging, app_dir, backup = Path(staging), Path(app_dir), Path(backup)
    if backup.exists():
        shutil.rmtree(backup)
    todo = [r for r in files if not is_user_data(r)
            and not _same(staging / r, app_dir / r)]
    # Solo los que ya había: uno nuevo es un motor opcional nuevo, no algo que reinstalar.
    reqs = [r for r in todo if is_requirements(r) and (app_dir / r).exists()]
    done = []
    try:
        for rel in todo:
            src, dst, bak = staging / rel, app_dir / rel, backup / rel
            if dst.is_dir():
                raise UpdateError("install", rel)
            existed = dst.exists()
            if existed:
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bak)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            done.append((rel, existed))
    except Exception as e:
        for rel, existed in reversed(done):
            try:
                if existed: shutil.copy2(backup / rel, app_dir / rel)
                else: (app_dir / rel).unlink()
            except OSError:
                pass
        raise UpdateError("install", getattr(e, "detail", "") or e) from None
    return todo, reqs


def install_problem(app_dir, home_dir):
    """Por qué no es seguro que el programa se cambie solo: None si lo es; si no, (motivo, ruta).
    git: es un clon y se pisaría lo que haya en él; elsewhere: el programa no está en la carpeta de sus
    datos, que es de donde tira de los motores; readonly: no se puede escribir."""
    app_dir, home_dir = Path(app_dir), Path(home_dir)
    if (app_dir / ".git").exists():
        return "git", str(app_dir)
    try:
        same = os.path.samefile(app_dir, home_dir)
    except OSError:
        same = False
    if not same:
        return "elsewhere", str(home_dir)
    if not os.access(app_dir, os.W_OK) or not os.access(app_dir / "storyteller.py", os.W_OK):
        return "readonly", str(app_dir)
    return None


def install(zip_path, version, app_dir, current):
    """Instala el zip ya bajado y comprobado. La copia de lo anterior queda en .update/backup-<versión>."""
    app_dir = Path(app_dir)
    work = app_dir / ".update"
    staging = work / "staging"
    try:
        files = unpack(zip_path, staging, version)
        changed, reqs = apply(staging, files, app_dir, work / f"backup-{plain_version(current)}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for old in work.glob("backup-*"):                     # solo se guarda la copia de la última
        if old.name != f"backup-{plain_version(current)}":
            shutil.rmtree(old, ignore_errors=True)
    return changed, reqs
