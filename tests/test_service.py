# -*- coding: utf-8 -*-
"""Narrar por URL: contra el servidor de verdad (st_server) con un motor simulado, sin GPU.

    python -m unittest tests.test_service -v
"""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest, wave, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
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


@unittest.skipUnless(PY and FFMPEG, "hace falta el venv de un motor y ffmpeg")
class TestNarrate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="st-service-test-"))
        v = self.tmp / "engine" / "voices"
        v.mkdir(parents=True)
        (v / "narrator.wav").write_bytes(b"RIFF")
        self.engine = Engine()
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

    def run_service(self, *extra, key=None):
        env = dict(os.environ, **{S.KEY_ENV: self.svc.key if key is None else key})
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

    def narrated_book(self):
        return [(ch["id"], ch["n"], ch["duration"], self.cues(ch["base"])["cues"]) for ch in self.manifest()]

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


if __name__ == "__main__":
    unittest.main()
