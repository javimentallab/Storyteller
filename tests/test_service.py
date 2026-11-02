# -*- coding: utf-8 -*-
"""Narrar por URL: contra el servidor de verdad (st_server) con un motor simulado, sin GPU.

    python -m unittest tests.test_service -v
"""
import argparse, collections, contextlib, email, http.server, importlib.util, io, json, os, shutil, struct
import subprocess, sys, tempfile, threading, time, unittest, wave, zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
import cc_engine as E
import cc_service as S
import st_server

VENVS = [ROOT / "engine" / v / ("Scripts/python.exe" if os.name == "nt" else "bin/python") for v in (".venv-xtts", ".venv")]
PY = next((p for p in VENVS if p.exists()), None)
FFMPEG = shutil.which("ffmpeg")
CHAPTER = ("<html><body><h1>{title}</h1><p>Primera frase del capítulo, que no es muy larga pero tampoco muy corta. "
           "Segunda frase, con otra idea distinta para que haya más trozos. "
           "Tercera frase del capítulo. Cuarta frase para ir acabando. Quinta frase. "
           "Sexta frase del capítulo. Séptima frase y última del párrafo.</p></body></html>")


def make_epub(path, chapters=2):
    items = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(chapters))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(chapters))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="b.opf"/></rootfiles></container>')
        z.writestr("b.opf", f'<package xmlns:dc="x"><metadata><dc:title>Libro de prueba</dc:title></metadata>'
                            f"<manifest>{items}</manifest><spine>{spine}</spine></package>")
        for i in range(chapters):
            z.writestr(f"c{i}.xhtml", CHAPTER.format(title=f"Capítulo {i + 1}"))


class Engine:
    """Medio segundo de silencio por frase; puede quedarse sin permiso tras N encargos."""

    def __init__(self):
        self.calls = []
        self.then = None
        self.varied = False                      # cada frase dura y tarda según su texto
        self.lock = threading.Lock()
        self.running = self.peak = 0

    def synth(self, text, voice, lang):
        with self.lock:
            self.calls.append(text)
            self.running += 1
            self.peak = max(self.peak, self.running)
            if self.then:
                self.then(len(self.calls))
        if self.varied:
            time.sleep(0.03 + 0.05 * (len(text) % 4))    # unas adelantan a otras
        with self.lock:
            self.running -= 1
        return b"\0\0" * (200 * len(text) if self.varied else 12000), 24000


class BookAndServer(unittest.TestCase):
    """Un libro y el servidor de verdad con un motor simulado. Sin pruebas propias."""
    ENGINE = Engine

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="st-service-test-"))
        v = self.tmp / "engine" / "voices"
        v.mkdir(parents=True)
        (v / "narrator.wav").write_bytes(b"RIFF")
        self.engine = self.ENGINE()
        self.svc = st_server.Service(self.tmp, self.engine, "es")
        self.srv = st_server.make_server(self.svc, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"127.0.0.1:{self.srv.server_address[1]}"
        self.epub = self.tmp / "libro.epub"
        make_epub(self.epub)
        self.out = self.tmp / "out"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_service(self, *extra, key=None, qc_key=None):
        env = dict(os.environ, **{S.KEY_ENV: self.svc.key if key is None else key})
        env.pop(S.QC_KEY_ENV, None)
        if qc_key is not None:
            env[S.QC_KEY_ENV] = qc_key
        cmd = [str(PY), "-I", str(ROOT / "engine" / "cc_service.py"), "generate", "--epub", str(self.epub),
               "--out", str(self.out), "--url", self.url, "--voice", "narrator", *extra]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)

    def manifest(self):
        return json.loads((self.out / "manifest.json").read_text("utf-8"))

    def cues(self, base):
        return json.loads((self.out / f"{base}.json").read_text("utf-8"))

    def state(self):
        return json.loads((self.out / "gen_state.json").read_text("utf-8"))

    def mp3_seconds(self, base):
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(self.out / f"{base}.mp3")], capture_output=True, text=True).stdout)

    def narrated_book(self):
        return [(ch["id"], ch["n"], ch["duration"], self.cues(ch["base"])["cues"]) for ch in self.manifest()]


@unittest.skipUnless(PY and FFMPEG, "hace falta el venv de un motor y ffmpeg")
class TestNarrate(BookAndServer):
    def test_the_book_comes_out_like_with_a_local_engine(self):
        p = self.run_service()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("EPUB-DONE 2", p.stdout)
        self.assertEqual((self.out / "title.txt").read_text("utf-8"), "Libro de prueba")
        self.assertEqual(self.state(), {"total": 2, "complete": True})
        man = self.manifest()
        self.assertEqual([(c["id"], c["base"], c["done"], c["frames"]) for c in man],
                         [(1, "cap01", True, 0), (2, "cap02", True, 0)])
        for ch in man:
            cj = self.cues(ch["base"])
            self.assertEqual(ch["n"], len(cj["cues"]))
            self.assertGreater(ch["n"], 5)
            for c in cj["cues"]:
                self.assertAlmostEqual(c["end"] - c["start"], 0.5, places=2)
            # El audio y los subtítulos cuentan el mismo tiempo: lo que se oye es lo que se lee.
            self.assertAlmostEqual(self.mp3_seconds(ch["base"]), ch["duration"], delta=0.12)
            self.assertFalse((self.out / f"{ch['base']}.wav").exists())
        self.assertFalse(list(self.out.glob("*.tmp")))

    def test_a_wrong_key_stops_at_once_and_says_why(self):
        p = self.run_service(key="otra")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(json.loads(p.stdout.split("SERVICE-ERROR ")[1])["kind"], "auth")
        self.assertEqual(self.engine.calls, [])
        self.assertFalse((self.out / "manifest.json").exists())

    def test_losing_the_service_keeps_what_arrived_and_resume_does_not_repeat_it(self):
        def lose_the_key(n):
            if n == 9:
                self.svc.key = "ya-no"
        real_key = self.svc.key
        self.engine.then = lose_the_key
        p = self.run_service()
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertFalse(self.state()["complete"])
        man = self.manifest()
        first = man[0]["n"]
        self.assertTrue(man[0]["done"])
        self.assertFalse(man[1]["done"])
        narrated = first + man[1]["n"]
        self.assertEqual(narrated, 9)                # la clave deja de valer para la 10.ª
        calls = list(self.engine.calls)

        self.svc.key, self.engine.then = real_key, None
        p = self.run_service("--resume")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ya hecho", p.stdout)
        self.assertIn("reanudo en frase", p.stdout)
        self.assertTrue(self.state()["complete"])
        again = self.engine.calls[len(calls):]
        whole = [c["text"] for ch in self.manifest() for c in self.cues(ch["base"])["cues"]]
        self.assertEqual(len(whole), narrated + len(again))     # nada repetido, nada perdido
        for ch in self.manifest():
            self.assertAlmostEqual(self.mp3_seconds(ch["base"]), ch["duration"], delta=0.12)

    def test_several_at_a_time_gives_the_same_book(self):
        self.engine.varied = True
        p = self.run_service()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        one_by_one = self.narrated_book()
        self.assertEqual(self.engine.peak, 1)

        self.engine.calls, self.engine.peak = [], 0
        self.out = self.tmp / "otro"
        p = self.run_service("--parallel", "3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreater(self.engine.peak, 1)
        self.assertLessEqual(self.engine.peak, 3)
        self.assertEqual(self.narrated_book(), one_by_one)        # cada texto con su audio, en su sitio
        for ch in self.manifest():
            self.assertAlmostEqual(self.mp3_seconds(ch["base"]), ch["duration"], delta=0.12)

    def test_losing_the_service_with_several_at_a_time_keeps_the_order_and_resumes(self):
        real_key = self.svc.key

        def lose_the_key(n):
            if n == 9:
                self.svc.key = "ya-no"
        self.engine.then, self.engine.varied = lose_the_key, True
        p = self.run_service("--parallel", "3")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout.split("SERVICE-ERROR ")[1])["kind"], "auth")
        narrated = sum(ch["n"] for ch in self.manifest())
        self.assertTrue(0 < narrated <= 9 + 2, narrated)          # + las que ya habían pasado la clave

        self.svc.key, self.engine.then = real_key, None
        before = len(self.engine.calls)
        p = self.run_service("--parallel", "3", "--resume")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        again = self.engine.calls[before:]
        book = [c for ch in self.manifest() for c in self.cues(ch["base"])["cues"]]
        self.assertEqual(len(book), narrated + len(again))        # nada repetido, nada perdido
        for ch in self.manifest():
            cues = self.cues(ch["base"])["cues"]
            for prev, c in zip(cues, cues[1:]):
                self.assertEqual(c["i"], prev["i"] + 1)
                self.assertGreater(c["start"], prev["end"])

    def test_count_like_the_engines(self):
        p = subprocess.run([str(PY), "-I", str(ROOT / "engine" / "cc_service.py"), "count", "--epub", str(self.epub)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertRegex(p.stdout, r"CHAPTERS 2\s+SENTS \d+ \d+")


class TestInOrder(unittest.TestCase):
    """La ventana deslizante, sin red: unas puertas que se abren cuando diga la prueba."""

    def setUp(self):
        self.gates = {}
        self.asked = []
        self.lock = threading.Lock()

    def gate(self, i):
        with self.lock:
            return self.gates.setdefault(i, threading.Event())

    def work(self, i):
        with self.lock:
            self.asked.append(i)
        self.assertTrue(self.gate(i).wait(10))
        return i * 10

    def wait_asked(self, want):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            with self.lock:
                if sorted(self.asked) == sorted(want):
                    return
            time.sleep(0.005)
        self.fail(f"pedidas {sorted(self.asked)}, se esperaban {sorted(want)}")

    def test_one_at_a_time_does_not_get_ahead(self):
        seen = []

        def work(i):
            seen.append(i)
            return i * 10
        it = S.in_order(work, iter(range(3)), 1)
        self.assertEqual(next(it), 0)
        self.assertEqual(seen, [0])
        self.assertEqual(list(it), [10, 20])

    def test_a_slow_sentence_does_not_hold_back_the_others(self):
        it = S.in_order(self.work, range(100), 2)
        got = []
        reader = threading.Thread(target=lambda: got.extend(it), daemon=True)
        reader.start()
        self.wait_asked([0, 1])                  # dos a la vez, ni una más
        self.gate(1).set()                       # acaba la segunda con la primera atascada…
        self.wait_asked([0, 1, 2])               # …y se pide la siguiente
        self.gate(2).set()
        self.wait_asked([0, 1, 2, 3])
        self.gate(3).set()
        self.wait_asked([0, 1, 2, 3, 4])
        self.gate(4).set()
        self.wait_asked([0, 1, 2, 3, 4, 5])
        self.gate(5).set()
        time.sleep(0.1)                          # tope: 2 × AHEAD a la espera de su turno
        with self.lock:
            self.assertEqual(sorted(self.asked), [0, 1, 2, 3, 4, 5])
        self.assertEqual(got, [])                # sin la primera no sale nada
        for i in range(100):
            self.gate(i).set()
        reader.join(10)
        self.assertEqual(got, [i * 10 for i in range(100)])

    def test_an_error_arrives_after_everything_before_it(self):
        def work(i):
            if i == 2:
                raise S.ServiceError("auth")
            time.sleep(0.05 if i == 1 else 0)
            return i
        it = S.in_order(work, range(10), 3)
        self.assertEqual([next(it), next(it)], [0, 1])
        with self.assertRaises(S.ServiceError):
            next(it)

    def test_nothing_to_do(self):
        self.assertEqual(list(S.in_order(self.work, [], 3)), [])


class Answers(http.server.BaseHTTPRequestHandler):
    """Un servicio que contesta lo que le digan, en orden, y apunta lo que le piden."""
    script = []
    seen = []

    def log_message(self, *_a):
        pass

    def _answer(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size) if size else b""
        Answers.seen.append((self.path, dict(self.headers), json.loads(body) if body else None))
        status, head, payload = Answers.script.pop(0) if Answers.script else (200, {}, b"{}")
        self.send_response(status)
        for k, v in head.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer


def silence_wav():
    import io
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 16000)
    return b.getvalue()


class TestClient(unittest.TestCase):
    def setUp(self):
        Answers.script, Answers.seen = [], []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answers)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.waits = []

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def client(self, **k):
        return S.Client(self.url, "clave-secreta", "m", "v", sleep=self.waits.append, **k)

    def failure(self, status, payload=b"", **head):
        Answers.script = [(status, head, payload)]
        with self.assertRaises(S.ServiceError) as c:
            self.client(tries=1)._audio("Hola.")
        return c.exception

    def test_kinds(self):
        self.assertEqual(self.failure(401).kind, "auth")
        self.assertEqual(self.failure(403).kind, "auth")
        self.assertEqual(self.failure(404).kind, "notfound")
        self.assertEqual(self.failure(429).kind, "busy")
        self.assertEqual(self.failure(503).kind, "busy")

    def test_the_message_of_the_service_arrives_without_the_key(self):
        Answers.script = [(422, {}, b'{"error": {"message": "voice no vale con clave-secreta"}}'),
                          (422, {}, b'{"error": {"message": "voice no vale con clave-secreta"}}')]
        with self.assertRaises(S.ServiceError) as c:
            self.client(tries=1)._audio("Hola.")
        self.assertEqual((c.exception.kind, c.exception.detail), ("refused", "voice no vale con ***"))

    def test_a_web_page_is_not_shown_as_a_message(self):
        self.assertEqual(self.failure(502, b"<html>Bad Gateway</html>").detail, "")

    def test_a_redirect_does_not_take_the_key_elsewhere(self):
        self.assertEqual(self.failure(302, Location=self.url + "/otra").kind, "notfound")
        self.assertEqual(len(Answers.seen), 1)

    @unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
    def test_waits_and_tries_again(self):
        ok = (200, {}, silence_wav())
        Answers.script = [(503, {"Retry-After": "30"}, b""), (500, {}, b""), (429, {}, b""), ok]
        told = []
        pcm = self.client(on_wait=told.append).say("Hola.")
        self.assertEqual(self.waits, [30, 4, 8])
        self.assertEqual(told, [30, 4, 8])
        self.assertAlmostEqual(len(pcm) / 2 / S.SR, 1.0, places=1)       # 16 kHz pasado a 24 kHz

    def test_a_wrong_key_is_not_tried_again(self):
        Answers.script = [(401, {}, b"")]
        with self.assertRaises(S.ServiceError):
            self.client().say("Hola.")
        self.assertEqual((len(Answers.seen), self.waits), (1, []))

    def test_gives_up_in_the_end(self):
        Answers.script = [(503, {}, b"")] * 3
        with self.assertRaises(S.ServiceError) as c:
            self.client(tries=3).say("Hola.")
        self.assertEqual((c.exception.kind, len(Answers.seen)), ("busy", 3))

    def test_nobody_home_is_down(self):
        self.srv.shutdown()
        self.srv.server_close()
        with self.assertRaises(S.ServiceError) as c:
            S.Client(f"127.0.0.1:{self.srv.server_address[1]}", tries=1).say("Hola.")
        self.assertEqual(c.exception.kind, "down")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answers)   # para tearDown
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
    def test_a_service_without_wav_gets_asked_for_mp3_from_then_on(self):
        Answers.script = [(400, {}, b'{"error": "wav no"}'), (200, {}, silence_wav()), (200, {}, silence_wav())]
        c = self.client()
        c.say("Una.")
        c.say("Otra.")
        self.assertEqual([s[2]["response_format"] for s in Answers.seen], ["wav", "mp3", "mp3"])

    @unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
    def test_the_language_goes_along_until_the_service_refuses_it(self):
        Answers.script = [(200, {}, silence_wav()), (400, {}, b'{"error": "language no"}'), (400, {}, b'{"error": "language no"}'),
                          (200, {}, silence_wav()), (200, {}, silence_wav())]
        c = self.client(language="ru")
        c.say("Да.")
        self.assertEqual(Answers.seen[0][2]["language"], "ru")
        c.say("Нет.")                            # lo rechaza: con wav y con mp3, y sin idioma ya vale
        self.assertEqual(["language" in s[2] for s in Answers.seen[1:4]], [True, True, False])
        self.assertEqual(Answers.seen[3][2]["response_format"], "wav")
        c.say("Да.")
        self.assertNotIn("language", Answers.seen[4][2])

    @unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
    def test_what_is_sent(self):
        Answers.script = [(200, {}, silence_wav())]
        self.client().say("Hola, ñu.")
        path, head, body = Answers.seen[0]
        self.assertEqual(path, "/v1/audio/speech")
        self.assertEqual(head["Authorization"], "Bearer clave-secreta")
        self.assertNotIn("Python", head["User-Agent"])
        self.assertEqual(body, {"input": "Hola, ñu.", "model": "m", "voice": "v", "response_format": "wav"})

    @unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
    def test_something_that_is_not_audio(self):
        Answers.script = [(200, {}, b"<html>portal cautivo</html>")]
        with self.assertRaises(S.ServiceError) as c:
            self.client().say("Hola.")
        self.assertEqual(c.exception.kind, "noaudio")

    @unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
    def test_probe_fills_in_what_is_missing(self):
        Answers.script = [(200, {}, b'{"data": [{"id": "xtts-v2"}]}'),
                          (200, {}, b'{"voices": ["narrator", {"id": "otra"}]}'),
                          (200, {}, silence_wav())]
        got = S.probe(self.url, "k")
        self.assertEqual((got["models"], got["voices"]), (["xtts-v2"], ["narrator", "otra"]))
        self.assertAlmostEqual(got["seconds"], 1.0, places=1)
        self.assertEqual((Answers.seen[2][2]["model"], Answers.seen[2][2]["voice"]), ("xtts-v2", "narrator"))

    @unittest.skipUnless(FFMPEG, "hace falta ffmpeg")
    def test_probe_of_a_service_without_listings(self):
        Answers.script = [(404, {}, b""), (404, {}, b""), (200, {}, silence_wav())]
        got = S.probe(self.url, "k", "tts", "alloy")
        self.assertEqual((got["models"], got["voices"]), ([], []))
        self.assertEqual((Answers.seen[2][2]["model"], Answers.seen[2][2]["voice"]), ("tts", "alloy"))

    def test_probe_with_a_wrong_key(self):
        Answers.script = [(401, {}, b"")]
        with self.assertRaises(S.ServiceError) as c:
            S.probe(self.url, "k")
        self.assertEqual(c.exception.kind, "auth")


class TestAddresses(unittest.TestCase):
    def test_api_base(self):
        for typed, want in (("192.168.1.5:8765", "http://192.168.1.5:8765/v1"),
                            ("http://192.168.1.5:8765/", "http://192.168.1.5:8765/v1"),
                            ("https://api.example.com", "https://api.example.com/v1"),
                            ("https://api.example.com/v1/audio/speech", "https://api.example.com/v1"),
                            ("https://example.com/openai/v1/", "https://example.com/openai/v1"),
                            ("HTTPS://Example.com", "https://example.com/v1"),
                            ("  localhost:8000  ", "http://localhost:8000/v1"),
                            ("[::1]:8000", "http://[::1]:8000/v1")):
            self.assertEqual(S.api_base(typed), want, typed)

    def test_not_addresses(self):
        for typed in ("", "   ", "con espacio.com", "ftp://x.com", "file:///etc/passwd", "http://",
                      "http://a:b@x.com", "http://x.com?a=1", "http://x.com#a", "http://x.com:99999"):
            self.assertIsNone(S.api_base(typed), typed)
        with self.assertRaises(S.ServiceError):
            S.Client("ftp://x.com")

    def test_unsafe(self):
        for u in ("http://example.com", "8.8.8.8:80", "http://192.169.0.1"):
            self.assertTrue(S.is_unsafe(u), u)
        for u in ("https://example.com", "192.168.1.5:8765", "http://10.0.0.1", "localhost:1", "http://172.16.0.1",
                  "http://100.64.0.1", "nas.local", ""):
            self.assertFalse(S.is_unsafe(u), u)

    def test_host(self):
        self.assertEqual(S.host_of("192.168.1.5:8765/v1"), "192.168.1.5")
        self.assertEqual(S.host_of("ftp://x"), "")


# ---- QC: Whisper y el servicio simulados. Comprueba la lógica, no lo que oye un Whisper de verdad. --------
def read_form(head, body):
    """(campos, audio) de un multipart, con el parser de la biblioteca estándar: si él lo entiende, está bien formado."""
    msg = email.message_from_bytes(b"Content-Type: " + head["Content-Type"].encode() + b"\r\n\r\n" + body)
    fields, audio = {}, None
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        if name == "file":
            audio = part.get_payload(decode=True)
        else:
            fields.setdefault(name, []).append(part.get_payload(decode=True).decode("utf-8"))
    return fields, audio


class Ears(http.server.BaseHTTPRequestHandler):
    """Un Whisper simulado: contesta lo que diga server.answer(cabeceras, campos, audio) y apunta lo que le llega."""

    def log_message(self, *_a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        head = dict(self.headers)
        fields, audio = read_form(head, body)
        self.server.seen.append((self.path, head, fields, audio))
        status, extra, payload = self.server.answer(head, fields, audio)
        self.send_response(status)
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def said(text, **more):
    return 200, {}, json.dumps({"text": text, **more}, ensure_ascii=False).encode("utf-8")


def start_ears(answer):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ears)
    srv.answer, srv.seen = answer, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


PER_CHAR = 480                                   # 20 ms por carácter: da tiempo a medir la voz de más


class MarkedEngine(Engine):
    """Cada encargo lleva su número en las muestras, para que el Whisper simulado sepa qué está oyendo,
    y cada reintento de una frase dura 0,1 s más, para saber con cuál se ha quedado."""

    def __init__(self):
        super().__init__()
        self.asked = collections.Counter()

    def synth(self, text, voice, lang):
        with self.lock:
            self.calls.append(text)
            n = len(self.calls)
            self.asked[text] += 1
            extra = self.asked[text] - 1
            self.running += 1
            self.peak = max(self.peak, self.running)
            if self.then:
                self.then(n)
        time.sleep(0.03)
        with self.lock:
            self.running -= 1
        return struct.pack("<h", 1000 + n) * (PER_CHAR * len(text) + 2400 * extra), 24000


SECOND = "segunda frase, con otra idea distinta para que haya más trozos."     # 11 palabras, tal como se manda
NUMPY = importlib.util.find_spec("numpy") is not None


def py_has_numpy():
    """El venv que narra en estas pruebas: uno solo con engine/deps/base no mide la voz de más."""
    return subprocess.run([str(PY), "-I", "-c", "import numpy"], capture_output=True, timeout=30).returncode == 0

NOT_IN_BOOK = " y esto no lo dice el libro"


@unittest.skipUnless(PY and FFMPEG, "hace falta el venv de un motor y ffmpeg")
class TestNarrateWithQc(BookAndServer):
    ENGINE = MarkedEngine

    def setUp(self):
        super().setUp()
        make_epub(self.epub, chapters=1)         # cada frase, una vez: cuenta cuántas veces se le pide
        self.hear = lambda text, attempt, seconds: said(text)
        self.times_heard = collections.Counter()
        self.ears = start_ears(self.answer)
        self.addCleanup(self.ears.server_close)
        self.addCleanup(self.ears.shutdown)

    def answer(self, _head, _fields, audio):
        with wave.open(io.BytesIO(audio)) as w:
            seconds = w.getnframes() / w.getframerate()
            text = self.engine.calls[struct.unpack("<h", w.readframes(1))[0] - 1000 - 1]
        self.times_heard[text] += 1
        return self.hear(text, self.times_heard[text], seconds)

    def qc(self, *extra):
        return ("--qc-url", f"127.0.0.1:{self.ears.server_address[1]}", *extra)

    def heard_by_ear(self):
        return sorted(self.times_heard.elements())

    def cue_seconds(self, begins):
        cue = next(c for c in self.cues("cap01")["cues"] if c["text"].lower().startswith(begins))
        return round(cue["end"] - cue["start"], 2)

    @staticmethod
    def take_seconds(text, attempt):
        return round((PER_CHAR * len(text) + 2400 * (attempt - 1)) / 24000, 2)

    def assert_book_is_whole(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("EPUB-DONE 1", p.stdout)
        ch = self.manifest()[0]
        self.assertAlmostEqual(self.mp3_seconds("cap01"), ch["duration"], delta=0.12)

    def test_without_asking_for_it_nobody_listens(self):
        p = self.run_service()
        self.assert_book_is_whole(p)
        self.assertEqual(self.ears.seen, [])
        self.assertNotIn("QC", p.stdout)
        self.assertEqual(len(self.engine.calls), self.manifest()[0]["n"])

    def test_a_good_take_is_asked_for_once_and_listened_to_once(self):
        p = self.run_service(*self.qc())
        self.assert_book_is_whole(p)
        self.assertEqual(sorted(self.engine.calls), self.heard_by_ear())
        self.assertEqual(len(self.engine.calls), self.manifest()[0]["n"])
        # Un venv solo con engine/deps/base avisa una vez de lo que no mide; de las frases no dice nada.
        self.assertNotIn("QC frase", p.stdout)

    def test_the_narration_key_does_not_go_to_whoever_listens(self):
        self.assert_book_is_whole(self.run_service(*self.qc()))
        self.assertNotIn("Authorization", self.ears.seen[0][1])

    def test_whoever_listens_gets_their_own_key(self):
        self.assert_book_is_whole(self.run_service(*self.qc(), qc_key="la-del-whisper"))
        self.assertEqual(self.ears.seen[0][1]["Authorization"], "Bearer la-del-whisper")

    def test_an_invented_ending_is_regenerated_and_the_good_take_stays(self):
        self.hear = lambda text, attempt, _s: said(text + NOT_IN_BOOK if text == SECOND and attempt == 1 else text)
        p = self.run_service(*self.qc())
        self.assert_book_is_whole(p)
        self.assertEqual(self.engine.asked[SECOND], 2)
        self.assertEqual(len(self.engine.calls), self.manifest()[0]["n"] + 1)
        self.assertRegex(p.stdout, r"QC frase \d+: recall=1\.00, alucina \(coletilla de 7 palabras\), regenerando \(1/3\)")
        self.assertRegex(p.stdout, r"QC frase \d+: OK recall=1\.00 \(intento 2\)")
        self.assertEqual(self.cue_seconds("segunda"), self.take_seconds(SECOND, 2))

    def test_when_none_is_good_the_best_take_stays(self):
        takes = {1: "segunda frase con otra idea trozos",                       # recall 0,55
                 2: "segunda frase con otra idea distinta para que trozos",     # 0,82: no llega, pero es la mejor
                 3: "segunda frase con otra idea distinta trozos"}              # 0,64
        self.hear = lambda text, attempt, _s: said(takes[attempt] if text == SECOND else text)
        p = self.run_service(*self.qc())
        self.assert_book_is_whole(p)
        self.assertEqual(self.engine.asked[SECOND], 3)
        self.assertRegex(p.stdout, r"QC frase \d+: recall=0\.82 < 0\.85, regenerando \(2/3\)")
        self.assertEqual(self.cue_seconds("segunda"), self.take_seconds(SECOND, 2))

    def test_babbling_that_the_transcription_does_not_see(self):
        if not py_has_numpy():
            self.skipTest("hace falta numpy en el venv")

        def hear(text, attempt, seconds):
            babble = text == SECOND and attempt == 1
            return said(text, words=[{"word": "x", "start": 0, "end": 0.05 if babble else seconds}])
        self.hear = hear
        p = self.run_service(*self.qc())
        self.assert_book_is_whole(p)
        self.assertEqual(self.engine.asked[SECOND], 2)
        self.assertRegex(p.stdout, r"recall=1\.00, alucina \(\d+\.\d s de voz sin palabras al final\), regenerando \(1/3\)")
        self.assertEqual(self.cue_seconds("segunda"), self.take_seconds(SECOND, 2))

    def test_the_end_of_the_last_word_can_come_inside_the_segments(self):
        if not py_has_numpy():
            self.skipTest("hace falta numpy en el venv")

        def hear(text, attempt, seconds):
            babble = text == SECOND and attempt == 1
            return said(text, segments=[{"words": [{"word": "x", "end": 0.05 if babble else seconds}]}])
        self.hear = hear
        self.assert_book_is_whole(self.run_service(*self.qc()))
        self.assertEqual(self.engine.asked[SECOND], 2)

    def test_the_threshold_and_the_retries_are_the_ones_given(self):
        self.hear = lambda text, _a, _s: said("segunda frase con otra idea trozos" if text == SECOND else text)
        p = self.run_service(*self.qc("--qc-threshold", "0.5"))
        self.assert_book_is_whole(p)
        self.assertEqual(self.engine.asked[SECOND], 1)           # recall 0,55: con 0,5 pasa

        self.out, self.engine.asked = self.tmp / "otro", collections.Counter()
        p = self.run_service(*self.qc("--qc-retries", "0"))
        self.assert_book_is_whole(p)
        self.assertEqual(self.engine.asked[SECOND], 1)           # con 0,85 no pasa, pero no hay reintentos
        self.assertRegex(p.stdout, r"recall=0\.55 < 0\.85, regenerando \(1/1\)")

    def test_nobody_answering_keeps_the_narration_going_and_says_so(self):
        self.hear = lambda _t, _a, _s: (401, {}, b'{"error": {"message": "esa clave no vale"}}')
        p = self.run_service(*self.qc())
        self.assert_book_is_whole(p)
        n = self.manifest()[0]["n"]
        self.assertEqual(len(self.engine.calls), n)              # sin nadie que oiga, no se regenera
        self.assertEqual(p.stdout.count("sin respuesta del Whisper"), n)     # lo que cuenta la app: QC_MISSED
        self.assertIn("QC por URL falló: auth: esa clave no vale", p.stdout)

    def test_a_bad_address_for_the_listener_narrates_and_says_so(self):
        p = self.run_service("--qc-url", "ftp://no-vale")
        self.assert_book_is_whole(p)
        self.assertEqual(p.stdout.count("sin respuesta del Whisper"), self.manifest()[0]["n"])

    def test_several_at_a_time_gives_the_same_book_with_qc(self):
        self.hear = lambda text, attempt, _s: said(text + NOT_IN_BOOK if text == SECOND and attempt == 1 else text)
        self.assert_book_is_whole(self.run_service(*self.qc()))
        one_by_one = self.narrated_book()
        self.assertEqual(self.engine.peak, 1)

        self.out = self.tmp / "otro"
        self.engine.calls, self.engine.peak, self.engine.asked = [], 0, collections.Counter()
        self.times_heard = collections.Counter()
        self.assert_book_is_whole(self.run_service(*self.qc("--parallel", "3")))
        self.assertGreater(self.engine.peak, 1)
        self.assertEqual(self.engine.asked[SECOND], 2)
        self.assertEqual(self.narrated_book(), one_by_one)       # cada texto con su audio, en su sitio

    def test_losing_the_service_in_a_retry_keeps_what_arrived_and_resumes(self):
        real_key = self.svc.key

        def lose_the_key(n):
            if self.engine.calls[n - 1] == SECOND:
                self.svc.key = "ya-no"                           # la frase llega; su reintento, ya no
        self.engine.then = lose_the_key
        self.hear = lambda text, attempt, _s: said(text + NOT_IN_BOOK if text == SECOND and attempt == 1 else text)
        p = self.run_service(*self.qc())
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout.split("SERVICE-ERROR ")[1])["kind"], "auth")
        before = self.manifest()[0]["n"]
        self.assertFalse(self.manifest()[0]["done"])
        self.assertGreater(before, 0)

        self.svc.key, self.engine.then = real_key, None
        heard = len(self.ears.seen)
        p = self.run_service(*self.qc("--resume"))
        self.assert_book_is_whole(p)
        self.assertIn("reanudo en frase", p.stdout)
        self.assertEqual(self.engine.asked[SECOND], 2)
        self.assertEqual(len(self.ears.seen) - heard, self.manifest()[0]["n"] - before)     # lo hecho no se vuelve a oír


class TestTranscribe(unittest.TestCase):
    def setUp(self):
        self.script = []
        self.srv = start_ears(lambda *_a: self.script.pop(0))
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.waits = []

    def client(self, key="clave-secreta", model=""):
        return S.Client(self.url, key, model, tries=1, sleep=self.waits.append)

    def test_what_is_sent(self):
        self.script = [said("hola")]
        pcm = struct.pack("<h", 1000) * 2400
        self.assertEqual(self.client(model="whisper-1").transcribe(pcm, "es", "Lectura de un libro."), ("hola", None))
        path, head, fields, audio = self.srv.seen[0]
        self.assertEqual(path, "/v1/audio/transcriptions")
        self.assertEqual(head["Authorization"], "Bearer clave-secreta")
        self.assertNotIn("Python", head["User-Agent"])
        self.assertEqual(fields, {"model": ["whisper-1"], "response_format": ["verbose_json"],
                                  "timestamp_granularities[]": ["word", "segment"], "language": ["es"],
                                  "prompt": ["Lectura de un libro."]})
        with wave.open(io.BytesIO(audio)) as w:          # lo que se oiría, tal cual
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (S.SR, 1, 2))
            self.assertEqual(w.readframes(2400), pcm)

    def test_what_is_empty_is_not_sent(self):
        self.script = [said("hola")]
        S.Client(self.url).transcribe(b"\0\0" * 100)
        _path, head, fields, _audio = self.srv.seen[0]
        self.assertNotIn("Authorization", head)
        self.assertEqual(sorted(fields), ["response_format", "timestamp_granularities[]"])

    def test_the_whole_path_of_the_endpoint_also_works(self):
        self.script = [said("hola")]
        S.Client(self.url + "/v1/audio/transcriptions").transcribe(b"\0\0" * 100)
        self.assertEqual(self.srv.seen[0][0], "/v1/audio/transcriptions")

    def test_asks_for_less_when_refused_and_remembers_it(self):
        self.script = [(400, {}, b'{"error": "word no"}'), (422, {}, b'{"error": "verbose no"}'), said("una"), said("otra")]
        c = self.client()
        self.assertEqual(c.transcribe(b"\0\0" * 100)[0], "una")
        self.assertEqual(c.transcribe(b"\0\0" * 100)[0], "otra")
        asked = [(s[2]["response_format"][0], "timestamp_granularities[]" in s[2]) for s in self.srv.seen]
        self.assertEqual(asked, [("verbose_json", True), ("verbose_json", False), ("json", False), ("json", False)])

    def test_a_failure_says_nothing_of_what_the_service_can_give(self):
        self.script = [(503, {}, b""), said("hola")]
        c = self.client()
        with self.assertRaises(S.ServiceError) as e:
            c.transcribe(b"\0\0" * 100)
        self.assertEqual(e.exception.kind, "busy")
        c.transcribe(b"\0\0" * 100)
        self.assertEqual([s[2]["response_format"][0] for s in self.srv.seen], ["verbose_json", "verbose_json"])
        self.assertIn("timestamp_granularities[]", self.srv.seen[1][2])

    def test_it_does_not_wait_or_try_again(self):
        # Con reintentos por defecto: el QC no debe parar la narración a esperar a un Whisper ocupado.
        self.script = [(503, {"Retry-After": "30"}, b"")]
        c = S.Client(self.url, "k", sleep=self.waits.append)
        with self.assertRaises(S.ServiceError):
            c.transcribe(b"\0\0" * 100)
        self.assertEqual((len(self.srv.seen), self.waits), (1, []))

    def test_a_redirect_does_not_take_the_key_elsewhere(self):
        self.script = [(302, {"Location": self.url + "/otra"}, b"")]
        with self.assertRaises(S.ServiceError) as e:
            self.client().transcribe(b"\0\0" * 100)
        self.assertEqual((e.exception.kind, len(self.srv.seen)), ("notfound", 1))

    def test_the_key_does_not_appear_in_what_is_reported(self):
        self.script = [(401, {}, b'{"error": {"message": "clave-secreta no vale"}}')]
        with self.assertRaises(S.ServiceError) as e:
            self.client().transcribe(b"\0\0" * 100)
        self.assertNotIn("clave-secreta", str(e.exception))

    def test_what_is_not_a_transcription(self):
        for answer in ((200, {}, b"<html>portal cautivo</html>"), (200, {}, b"[1, 2]"), (200, {}, b'{"segments": []}'),
                       (200, {}, b'{"text": 7}'), (200, {}, b"\xff\xfe")):
            self.script = [answer]
            with self.assertRaises(S.ServiceError) as e:
                self.client().transcribe(b"\0\0" * 100)
            self.assertEqual(e.exception.kind, "notext", answer)

    def test_the_end_of_the_last_word(self):
        for o, want in (({"words": [{"end": 1.0}, {"end": 2.5}]}, 2.5),
                        ({"segments": [{"words": [{"end": 1.0}]}, {"words": [{"end": 3}]}]}, 3),
                        ({"words": [{"end": 2.0}], "segments": [{"words": [{"end": 9.0}]}]}, 2.0),
                        ({}, None), ({"words": []}, None), ({"segments": [{"text": "x"}]}, None),
                        ({"words": [{"end": "x"}]}, None), ({"words": [{"end": float("nan")}]}, None),
                        ({"words": [{"end": float("inf")}]}, None), ({"words": [{"end": -1}]}, None),
                        ({"words": [{"end": True}]}, None), ({"words": ["x"]}, None), ({"words": 7}, None),
                        ({"words": {"a": 1}}, None), ({"segments": 7}, None), ({"segments": ["x"]}, None)):
            self.assertEqual(S._last_end(o), want, o)


class TestReviewer(unittest.TestCase):
    """El juicio, sin red: un servicio y un Whisper que contestan lo que les digan."""
    PCM = struct.pack("<h", 3000) * 24000        # 1 s, todo «voz»
    TEXT = "tres palabras nada más."

    def setUp(self):
        rates = list(E._QC_RATES)
        E._QC_RATES.clear()
        self.addCleanup(lambda: (E._QC_RATES.clear(), E._QC_RATES.extend(rates)))
        self.lines = io.StringIO()

    def args(self, **k):
        return argparse.Namespace(**{"lang": "es", "qc_url": "127.0.0.1:1", "qc_model": "", "qc_whisper_dir": "",
                                     "qc_threshold": 0.85, "qc_retries": 2, **k})

    def review(self, heard, takes=None, **k):
        """(lo que se queda, cuántas veces se pidió, la salida): heard es lo que oye en cada intento."""
        heard, asked = iter(heard), []
        takes = takes or [self.PCM] * 3
        with contextlib.redirect_stdout(self.lines):
            r = S.Reviewer(E, self.args(**k))
            r.heard = lambda _pcm: next(heard)

            def voice(_spoken):
                asked.append(1)
                return takes[len(asked) - 1]
            kept = r.best(4, self.TEXT, voice)
        return kept, len(asked), self.lines.getvalue()

    def test_the_verdict_is_the_one_of_the_local_engines(self):
        for hyp in (self.TEXT, "tres palabras", "tres palabras nada más y esto no lo dice el libro",
                    "volumen volumen volumen volumen " + self.TEXT, "", "otra cosa"):
            want = E._qc_eval(self.TEXT, hyp, 0.85, "")
            _kept, asked, _out = self.review([(hyp, None)] * 3)
            self.assertEqual(asked, 1 if want[1] else 3, hyp)

    def test_it_keeps_the_best_score_like_the_local_engines_even_if_a_later_take_passes(self):
        words = [a + b for a in "bcdfg" for b in "aeiou"]        # 25 palabras distintas
        text = " ".join(words)
        fails = " ".join(words[:10] + words[14:])                # recall 0,84: no pasa, sin más castigo
        passes = " ".join(words[:2] * 2 + words[:10] + words[13:] + ["xx", "yy"])  # 0,88: pasa, pero repite y añade
        a, b = E._qc_eval(text, fails, 0.85, ""), E._qc_eval(text, passes, 0.85, "")
        self.assertFalse(a[1])
        self.assertTrue(b[1])
        self.assertGreater(a[2], b[2])
        self.TEXT = text
        kept, asked, _out = self.review([(fails, None), (passes, None)], takes=[b"\1\0" * 10, b"\2\0" * 10])
        self.assertEqual((kept, asked), (b"\1\0" * 10, 2))

    def test_nobody_answering_on_a_retry_keeps_the_best_so_far(self):
        kept, asked, out = self.review([("otra cosa", None), (None, None)], takes=[b"\1\0" * 10, b"\2\0" * 10])
        self.assertEqual((kept, asked), (b"\1\0" * 10, 2))
        self.assertIn("QC frase 5: sin respuesta del Whisper, me quedo con lo generado", out)

    def test_the_failure_of_the_service_goes_up(self):
        with contextlib.redirect_stdout(self.lines):
            r = S.Reviewer(E, self.args())

        def voice(_spoken):
            raise S.ServiceError("auth")
        with self.assertRaises(S.ServiceError):
            r.best(0, self.TEXT, voice)

    def local(self, lang):
        with contextlib.redirect_stdout(self.lines), mock.patch.object(E, "qc_transcribe_local", return_value=("x", None)) as t:
            r = S.Reviewer(E, self.args(lang=lang, qc_whisper_dir="/modelo"))
            r.ear = mock.Mock(side_effect=AssertionError("con Whisper local no se usa la red"))
            self.assertEqual(r.heard(self.PCM), ("x", None))
        return t.call_args.args

    @unittest.skipUnless(NUMPY, "hace falta numpy")
    def test_a_local_whisper_goes_first_and_gets_samples_and_the_language(self):
        wav, sr, model_dir, lang = self.local("es")
        self.assertEqual((sr, model_dir, lang, str(wav.dtype), len(wav)), (S.SR, "/modelo", "es", "float32", 24000))
        self.assertAlmostEqual(float(wav[0]), 3000 / 32768, places=5)
        self.assertEqual(self.local("zh-cn")[3], "zh-cn")        # cc_engine ya lo pasa a lo que entiende Whisper
        self.assertIsNone(self.local("auto")[3])                 # que lo detecte: «auto» no es un idioma para él

    @unittest.skipUnless(NUMPY, "hace falta numpy")
    def test_a_local_whisper_that_cannot_run_is_nobody_answering(self):
        with contextlib.redirect_stdout(self.lines), mock.patch.object(E, "qc_transcribe_local", side_effect=ImportError):
            kept, asked, out = self.review_real_ear(qc_whisper_dir="/modelo")
        self.assertEqual((kept, asked), (self.PCM, 1))
        self.assertIn("QC local falló: ImportError", out)
        self.assertIn("sin respuesta del Whisper", out)

    def review_real_ear(self, **k):
        asked = []
        r = S.Reviewer(E, self.args(**k))

        def voice(_spoken):
            asked.append(1)
            return self.PCM
        return r.best(0, self.TEXT, voice), len(asked), self.lines.getvalue()

    def form_language(self, lang):
        got = []
        with contextlib.redirect_stdout(self.lines):
            r = S.Reviewer(E, self.args(lang=lang))
            r.ear.transcribe = lambda _pcm, language, prompt: got.append((language, prompt)) or ("x", None)
            r.heard(self.PCM)
        return got[0]

    def test_the_language_and_the_hint_sent_by_url(self):
        self.assertEqual(self.form_language("es"), ("es", "Lectura de un libro."))
        self.assertEqual(self.form_language("en"), ("en", "."))
        self.assertEqual(self.form_language("zh-cn"), ("zh", "."))
        self.assertEqual(self.form_language("auto"), ("", "."))

    @unittest.skipUnless(NUMPY, "hace falta numpy")
    def test_the_audio_is_judged_with_the_same_function_as_the_local_engines(self):
        with contextlib.redirect_stdout(self.lines):
            r = S.Reviewer(E, self.args())
        wav = r._samples(self.PCM)
        self.assertEqual(r._fault(self.TEXT, wav, 0.2), E.qc_audio_fault(self.TEXT, wav, S.SR, 0.2))
        self.assertIn("voz sin palabras", r._fault(self.TEXT, wav, 0.2))
        self.assertEqual(r._fault(self.TEXT, wav, None), "")
        self.assertEqual(r._fault(self.TEXT, wav, 1e308), "")    # un final disparatado no debe tumbar nada

    @unittest.skipUnless(NUMPY, "hace falta numpy")
    def test_good_takes_teach_it_how_long_a_sentence_should_last(self):
        text = "una frase con bastantes letras para contar"
        with contextlib.redirect_stdout(self.lines):
            r = S.Reviewer(E, self.args())
            r.heard = lambda _pcm: (text, None)
            good = struct.pack("<h", 3000) * 24000
            for _ in range(8):
                r.best(0, text, lambda _s: good)
            self.assertEqual(len(E._QC_RATES), 8)
            takes = iter([good * 4, good])       # 4 s donde debería durar 1: otra vez
            self.assertEqual(r.best(1, text, lambda _s: next(takes)), good)
        self.assertRegex(self.lines.getvalue(), r"alucina \(dura 4\.0 s y deberían ser 1\.0 s\), regenerando \(1/3\)")

    def test_without_numpy_it_still_judges_the_text_and_says_what_it_does_not_measure(self):
        with mock.patch.dict(sys.modules, {"numpy": None}):      # como un entorno solo con engine/deps/base
            kept, asked, out = self.review([("otra cosa", None), (self.TEXT, None)], takes=[b"\1\0" * 10, b"\2\0" * 10])
        self.assertEqual((kept, asked), (b"\2\0" * 10, 2))
        self.assertIn("sin numpy no se mide la voz de más", out)
        self.assertEqual(E._QC_RATES, [])

    def test_a_negative_number_of_retries_is_no_retries(self):
        _kept, asked, _out = self.review([("otra cosa", None)], qc_retries=-3)
        self.assertEqual(asked, 1)

    def test_it_only_turns_on_when_asked(self):
        self.assertFalse(E.qc_on(self.args(qc_url="", qc_whisper_dir="")))
        self.assertTrue(E.qc_on(self.args(qc_url="x", qc_whisper_dir="")))
        self.assertTrue(E.qc_on(self.args(qc_url="", qc_whisper_dir="x")))


if __name__ == "__main__":
    unittest.main()
