# -*- coding: utf-8 -*-
"""QC por HTTP: si el Whisper está sin memoria (503) o arrancando, esperar y reintentar.

Antes, un 503 dejaba la frase sin revisar: 253 en un libro el 7-nov, con XTTS y Whisper en la
misma GPU de 6 GB. Prueba con curl de verdad contra un servidor local.

    python -m unittest tests.test_qc_reintento -v
"""
import importlib.util, json, socket, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
HAS_AUDIO = all(importlib.util.find_spec(m) is not None for m in ("numpy", "soundfile"))


def servidor(respuestas):
    """Contesta por orden lo de `respuestas`: (código, cabeceras, cuerpo). Cuenta las peticiones."""
    hechas = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            code, headers, body = respuestas[min(len(hechas), len(respuestas) - 1)]
            hechas.append(code)
            data = json.dumps(body).encode()
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1/audio/transcriptions", hechas


OOM = (503, {"Retry-After": "30"}, {"error": {"message": "CUDA out of memory"}})
BIEN = (200, {}, {"text": "Hola, mundo.", "words": [{"word": "mundo", "end": 0.8}]})


@unittest.skipUnless(HAS_AUDIO, "falta numpy o soundfile")
class ReintentoQC(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import cc_engine
        self.E = cc_engine
        self.wav = np.zeros(1600, dtype="float32")
        self.esperas = []
        p = mock.patch.object(cc_engine.time, "sleep", lambda s: self.esperas.append(s))
        p.start(); self.addCleanup(p.stop)

    def qc(self, url):
        return self.E.qc_transcribe(self.wav, 16000, url, timeout=10)

    def test_sin_memoria_y_luego_bien_revisa_la_frase(self):
        srv, url, hechas = servidor([OOM, OOM, BIEN]); self.addCleanup(srv.shutdown)
        self.assertEqual(self.qc(url), ("Hola, mundo.", 0.8))
        self.assertEqual(hechas, [503, 503, 200])
        self.assertEqual(self.esperas, [30, 30])          # lo que pide Retry-After

    def test_siempre_sin_memoria_se_rinde_como_antes(self):
        srv, url, hechas = servidor([OOM]); self.addCleanup(srv.shutdown)
        self.assertEqual(self.qc(url), (None, None))
        self.assertEqual(len(hechas), len(self.E.QC_BUSY_WAITS) + 1)

    def test_retry_after_enorme_se_recorta(self):
        srv, url, _h = servidor([(503, {"Retry-After": "600"}, {}), BIEN]); self.addCleanup(srv.shutdown)
        self.assertEqual(self.qc(url)[0], "Hola, mundo.")
        self.assertEqual(self.esperas, [60])

    def test_bien_a_la_primera_no_espera(self):
        srv, url, hechas = servidor([BIEN]); self.addCleanup(srv.shutdown)
        self.assertEqual(self.qc(url)[0], "Hola, mundo.")
        self.assertEqual((hechas, self.esperas), ([200], []))

    def test_otro_error_no_reintenta(self):
        srv, url, hechas = servidor([(500, {}, {"detail": "roto"}), BIEN]); self.addCleanup(srv.shutdown)
        self.assertEqual(self.qc(url), (None, None))
        self.assertEqual((hechas, self.esperas), ([500], []))

    def test_servidor_apagado_reintenta_con_la_espera_propia(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.assertEqual(self.qc(f"http://127.0.0.1:{port}/x"), (None, None))
        self.assertEqual(self.esperas, list(self.E.QC_BUSY_WAITS))


if __name__ == "__main__":
    unittest.main()
