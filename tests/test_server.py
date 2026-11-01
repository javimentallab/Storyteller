# -*- coding: utf-8 -*-
"""Tests del servidor de voz, por HTTP de verdad, con un motor simulado (sin GPU ni modelos).

    python -m unittest tests.test_server -v
"""
import http.client, io, json, os, shutil, sys, tempfile, threading, time, unittest, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_server


class FakeEngine:
    """Medio segundo de silencio por cada llamada, y cuenta las que van a la vez."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def synth(self, text, voice, lang):
        if self.fail:
            raise self.fail
        self.calls.append((text, Path(voice).stem, lang))
        return (b"" if text == "NADA" else b"\0\0" * 12000), 24000


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="st-voice-test-"))
        v = self.home / "engine" / "voices"
        v.mkdir(parents=True)
        for name in ("narrator", "voz-b"):
            (v / f"{name}.wav").write_bytes(b"RIFF")
        (self.home / "config.json").write_text(json.dumps({"voice": "voz-b"}), "utf-8")
        self.engine = FakeEngine()
        self.svc = st_server.Service(self.home, self.engine, "es")
        self.srv = st_server.make_server(self.svc, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.home, ignore_errors=True)

    def req(self, method, path, body=None, key=True, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=20)
        h = {"Authorization": "Bearer " + self.svc.key} if key else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, payload, dict(r.getheaders())

    def speech(self, **q):
        return self.req("POST", "/v1/audio/speech", q)

    def message(self, payload):
        return json.loads(payload)["error"]["message"]


class TestKey(Base):
    def test_everything_needs_the_key(self):
        for m, p in (("GET", "/v1/models"), ("GET", "/v1/audio/voices"), ("POST", "/v1/audio/speech")):
            self.assertEqual(self.req(m, p, {} if m == "POST" else None, key=False)[0], 401, p)
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/v1/models", headers={"Authorization": "Bearer otra"})
        self.assertEqual(c.getresponse().status, 401)
        c.close()
        self.assertEqual(self.engine.calls, [])

    def test_the_key_stays_after_a_restart(self):
        again = st_server.Service(self.home, self.engine)
        self.assertEqual(again.key, self.svc.key)
        self.assertGreater(len(again.key), 30)


class TestListings(Base):
    def test_models_like_the_standard(self):
        st, p, _h = self.req("GET", "/v1/models")
        self.assertEqual((st, json.loads(p)["data"][0]["id"]), (200, "xtts-v2"))

    def test_voices(self):
        self.assertEqual(json.loads(self.req("GET", "/v1/audio/voices")[1])["voices"], ["narrator", "voz-b"])

    def test_unknown_route(self):
        self.assertEqual(self.req("GET", "/v1/otra")[0], 404)
        self.assertEqual(self.req("POST", "/v1/chat/completions", {})[0], 404)


class TestSpeech(Base):
    def test_wav(self):
        st, p, h = self.speech(input="Hola.", voice="narrator", response_format="wav", model="xtts-v2")
        self.assertEqual((st, h["Content-Type"]), (200, "audio/wav"))
        with wave.open(io.BytesIO(p)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 24000, 12000))
        self.assertEqual(self.engine.calls, [("Hola.", "narrator", "es")])

    @unittest.skipUnless(shutil.which("ffmpeg"), "hace falta ffmpeg")
    def test_mp3_is_the_default_like_the_standard(self):
        st, p, h = self.speech(input="Hola.", voice="narrator")
        self.assertEqual((st, h["Content-Type"]), (200, "audio/mpeg"))
        self.assertTrue(p[:3] == b"ID3" or p[0] == 0xFF, p[:4])

    def test_without_ffmpeg_it_gives_wav_and_says_so(self):
        real = shutil.which
        st_server.shutil.which = lambda _n: None
        try:
            _st, p, h = self.speech(input="Hola.", voice="narrator", response_format="mp3")
        finally:
            st_server.shutil.which = real
        self.assertEqual((h["Content-Type"], p[:4]), ("audio/wav", b"RIFF"))

    def test_voice_chosen_on_the_desktop_is_the_default(self):
        self.speech(input="Hola.", response_format="wav")
        self.assertEqual(self.engine.calls[0][1], "voz-b")

    def test_unknown_voice_says_which_there_are(self):
        st, p, _h = self.speech(input="Hola.", voice="alloy")
        self.assertEqual(st, 400)
        self.assertIn("narrator, voz-b", self.message(p))

    def test_voice_cannot_walk_out_of_the_folder(self):
        for v in ("../../config", "..\\config", "/etc/passwd", "narrator/../voz-b"):
            self.assertEqual(self.speech(input="x", voice=v)[0], 400, v)
        self.assertEqual(self.engine.calls, [])

    def test_bad_requests(self):
        self.assertEqual(self.speech(voice="narrator")[0], 400)
        self.assertEqual(self.speech(input="   ")[0], 400)
        self.assertEqual(self.speech(input=5)[0], 400)
        self.assertEqual(self.speech(input="x" * 5000)[0], 400)
        self.assertEqual(self.speech(input="x", response_format="exe")[0], 400)
        self.assertEqual(self.speech(input="x", model="tts-1")[0], 404)
        self.assertEqual(self.req("POST", "/v1/audio/speech", raw=b"no json")[0], 400)
        self.assertEqual(self.req("POST", "/v1/audio/speech", raw=b"[1]")[0], 400)
        self.assertEqual(self.engine.calls, [])

    def test_nothing_readable(self):
        self.assertEqual(self.speech(input="NADA", voice="narrator")[0], 400)

    def test_huge_body_is_refused(self):
        self.assertEqual(self.req("POST", "/v1/audio/speech", raw=b"x" * (st_server.MAX_BODY + 1))[0], 413)

    def test_no_memory_on_the_card_asks_to_come_back(self):
        self.engine.fail = st_server.EngineBusy("CUDA out of memory")
        st, p, h = self.speech(input="Hola.", voice="narrator")
        self.assertEqual((st, h["Retry-After"]), (503, "30"))
        self.assertIn("memoria", self.message(p))

    def test_other_failures_do_not_leak_details(self):
        self.engine.fail = ValueError(r"C:\ruta\secreta falló")
        st, p, _h = self.speech(input="Hola.", voice="narrator")
        self.assertEqual(st, 500)
        self.assertNotIn("ruta", self.message(p))

    def test_no_voices(self):
        shutil.rmtree(self.home / "engine" / "voices")
        st, p, _h = self.speech(input="Hola.")
        self.assertEqual(st, 400)
        self.assertIn("ninguna voz", self.message(p))


class FakeWorker:
    """Un proceso de mentira: contesta lo que le digan."""

    def __init__(self, make_engine):
        self.loading, self.last_use, self.closed = True, time.monotonic(), False
        self.jobs = []
        self.answer = ("ok", (b"voz", 24000))

    def ask(self, job):
        self.jobs.append(job)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def close(self):
        self.closed = True


class TestPool(unittest.TestCase):
    def pool(self, free=99999, **k):
        self.free = free
        return st_server.WorkerPool(None, start=FakeWorker, free_mb=lambda: self.free, reaper=False, **k)

    def opened(self, pool, n):
        """n encargos sin devolver: lo que pasaría con n a la vez, todos ya cargados."""
        out = []
        for _ in range(n):
            w = pool._take()
            w.loading = False
            pool.checked = (0.0, None)
            out.append(w)
        return out

    def test_one_at_a_time_needs_only_one(self):
        p = self.pool()
        for _ in range(5):
            self.assertEqual(p.synth("Hola.", "v.wav", "es"), (b"voz", 24000))
        self.assertEqual(len(p.workers), 1)

    def test_up_to_the_limit_and_no_further(self):
        p = self.pool(size=3)
        self.opened(p, 3)
        self.assertEqual(len(p.workers), 3)
        self.assertFalse(p._can_open())

    def test_the_first_always_opens_even_if_the_card_looks_full(self):
        self.assertTrue(self.pool(free=0)._can_open())

    def test_another_only_if_it_fits(self):
        p = self.pool(free=st_server.WORKER_MB - 1)
        self.opened(p, 1)
        self.assertFalse(p._can_open())
        self.free = st_server.WORKER_MB
        p.checked = (0.0, None)
        self.assertTrue(p._can_open())

    def test_without_knowing_the_memory_it_stays_at_one(self):
        p = self.pool(free=None)
        self.opened(p, 1)
        self.assertFalse(p._can_open())

    def test_no_second_while_the_first_loads(self):
        # Hasta que no ha cargado, la memoria libre todavía no dice lo que ocupa.
        p = self.pool()
        w = p._take()
        self.assertTrue(w.loading)
        self.assertFalse(p._can_open())
        p._give_back(w)
        self.assertFalse(w.loading)

    def test_the_memory_is_looked_at_again_once_loaded(self):
        # Si valiera el dato de antes de cargar, se abriría otro creyendo que aún hay sitio.
        p = self.pool(free=8000)
        w = p._take()
        self.assertFalse(p._can_open())
        self.free = 1000
        p._give_back(w)
        p._take()
        self.assertFalse(p._can_open())

    def test_the_last_used_goes_first_and_the_rest_close(self):
        p = self.pool(size=3, idle=0.2)
        a, b, c = self.opened(p, 3)
        for w in (a, b, c):
            p._give_back(w)
        time.sleep(0.25)
        self.assertIs(p._take(), c)
        p._give_back(c)
        p.release_idle()
        self.assertEqual((a.closed, b.closed, c.closed), (True, True, False))
        self.assertEqual(p.workers, [c])

    def test_nothing_in_use_is_closed(self):
        p = self.pool(idle=0.0)
        w = p._take()
        time.sleep(0.01)
        p.release_idle()
        self.assertFalse(w.closed)
        self.assertEqual(p.workers, [w])

    def test_no_memory_passes_the_job_on_and_lowers_the_ceiling(self):
        p = self.pool(size=3)
        a, b = self.opened(p, 2)
        p._give_back(a)
        b.answer = ("busy", "CUDA out of memory")
        p._give_back(b)                          # el último usado: le toca a él
        self.assertEqual(p.synth("Hola.", "v.wav", "es"), (b"voz", 24000))
        self.assertTrue(b.closed)
        self.assertEqual(len(a.jobs), 1)
        self.assertEqual(p.ceiling, 1)
        self.assertFalse(p._can_open())

    def test_no_memory_with_nobody_else_asks_to_come_back(self):
        p = self.pool()
        w = p._take()
        w.answer = ("busy", "CUDA out of memory")
        p._give_back(w)
        with self.assertRaises(st_server.EngineBusy):
            p.synth("Hola.", "v.wav", "es")
        self.assertEqual((p.workers, p.ceiling), ([], p.size))   # el siguiente vuelve a probar

    def test_a_dead_process_is_replaced(self):
        p = self.pool()
        w = p._take()
        w.answer = EOFError()
        p._give_back(w)
        with self.assertRaises(RuntimeError):
            p.synth("Hola.", "v.wav", "es")
        self.assertEqual(p.synth("Hola.", "v.wav", "es"), (b"voz", 24000))
        self.assertEqual(len(p.workers), 1)
        self.assertIsNot(p.workers[0], w)

    def test_a_failure_does_not_lose_the_process(self):
        p = self.pool()
        w = p._take()
        w.answer = ("error", "ValueError: algo")
        p._give_back(w)
        with self.assertRaises(RuntimeError):
            p.synth("Hola.", "v.wav", "es")
        self.assertEqual(p.waiting, [w])

    def test_close(self):
        p = self.pool()
        a, b = self.opened(p, 2)
        p.close()
        self.assertTrue(a.closed and b.closed)
        self.assertEqual(p.workers, [])


class SlowEngine:
    """Se tiene que importar desde otro proceso: por eso está aquí fuera, sin nada de la prueba."""

    def synth(self, text, voice, lang):
        time.sleep(1.0)
        return str(os.getpid()).encode(), 24000


class TestRealProcesses(unittest.TestCase):
    """Procesos de verdad, sin GPU ni modelo."""

    def setUp(self):
        self.p = st_server.WorkerPool(SlowEngine, size=3, free_mb=lambda: 99999, reaper=False)
        self.addCleanup(self.p.close)

    def together(self, n):
        out = []

        def one():
            out.append(self.p.synth("Hola.", "v.wav", "es")[0])
        threads = [threading.Thread(target=one) for _ in range(n)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        return out, time.monotonic() - t0

    def test_they_work_at_the_same_time_each_in_its_process(self):
        for _ in range(3):                       # se van abriendo de uno en uno
            self.together(3)
        out, seconds = self.together(3)
        self.assertEqual(len(set(out)), 3)
        self.assertLess(seconds, 2.2)            # de uno en uno serían 3 s

    def test_closing_leaves_nothing_running(self):
        self.together(1)
        procs = [w.proc for w in self.p.workers]
        self.p.close()
        self.assertTrue(procs and not any(x.is_alive() for x in procs))

    def test_if_the_server_goes_the_process_goes_too(self):
        # Lo que pasa si se mata el servidor sin más: se cierra su extremo del canal.
        self.together(1)
        w = self.p.workers[0]
        w.conn.close()
        w.proc.join(15)
        self.assertFalse(w.proc.is_alive())
        self.p.workers.clear()
        self.p.waiting.clear()


if __name__ == "__main__":
    unittest.main()
