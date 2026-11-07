# -*- coding: utf-8 -*-
"""XTTS se crea sin rellenar sus pesos al azar: el checkpoint los pone todos después (carga estricta).

Con PyTorch 2.13 en Linux, ese relleno (normal_) es unas 5 veces más lento que con 2.10 y la carga del
modelo pasaba de ~50 s a más de 4 min en el servidor (7-nov). Comprobado aparte con el modelo real: los
1335 tensores salen idénticos con y sin el relleno.

    python -m unittest tests.test_carga_xtts -v
"""
import importlib.util, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
HAS = all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))


@unittest.skipUnless(HAS, "sin torch o transformers")
class CargaSinRellenar(unittest.TestCase):
    def test_no_rellena_al_crear_y_vuelve_a_la_normalidad(self):
        import torch
        import cc_xtts as X
        visto = {}

        class FakeTTS:
            def __init__(self, name):
                w = torch.zeros(4, 4)
                torch.nn.init.normal_(w, 0.0, 1.0)         # lo que hace transformers al crear el GPT
                visto["name"], visto["tocado"] = name, bool(w.abs().sum())

        X.new_xtts(FakeTTS)
        self.assertEqual(visto, {"name": X.XTTS_NAME, "tocado": False})
        w = torch.zeros(4, 4)
        torch.nn.init.normal_(w, 0.0, 1.0)
        self.assertTrue(w.abs().sum() > 0)                 # fuera, normal_ vuelve a funcionar

    def test_st_server_y_el_motor_crean_xtts_igual(self):
        motor = (ROOT / "engine" / "cc_xtts.py").read_text("utf-8")
        server = (ROOT / "st_server.py").read_text("utf-8")
        self.assertNotIn("TTS(\"tts_models", motor.replace("XTTS_NAME = \"tts_models", ""))
        self.assertNotIn("TTS(name)", server)
        self.assertEqual(server.count("X.new_xtts(TTS)"), 2)


if __name__ == "__main__":
    unittest.main()
