# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import cc_engine as E

try:
    import numpy as np
except ImportError:
    np = None


class Troceo(unittest.TestCase):
    def test_cada_signo_marca_su_pausa(self):
        self.assertEqual(
            E.split_chunks("La política es una buena prueba, en parte por esto. Otra frase.\nOtro párrafo."),
            [("La política es una buena prueba,", "coma"), ("en parte por esto.", "frase"),
             ("Otra frase.", "parrafo"), ("Otro párrafo.", "parrafo")])

    def test_clausula_corta_va_con_la_siguiente(self):
        self.assertEqual(E.split_sentences("Además, vivimos en Florida."), ["Además, vivimos en Florida."])

    def test_no_corta_decimales_ni_abreviaturas(self):
        self.assertEqual(E.split_sentences("El Sr. Smith pagó 3,5 euros."), ["El Sr. Smith pagó 3,5 euros."])

    def test_dialogo_en_minuscula_no_es_fin_de_frase(self):
        self.assertEqual(E.split_sentences("¿Qué hora es? —dijo él."), ["¿Qué hora es? —dijo él."])

    def test_comilla_de_cierre_es_fin_de_frase(self):
        self.assertEqual(E.split_chunks("Dijo «basta.» Después se fue."),
                         [("Dijo «basta.»", "frase"), ("Después se fue.", "parrafo")])

    def test_clausula_enorme_se_corta_en_un_espacio(self):
        trozos = E.split_sentences("palabra " * 60)
        self.assertGreater(len(trozos), 1)
        self.assertTrue(all(len(t) <= 240 for t in trozos))
        self.assertEqual(" ".join(trozos).split(), ["palabra"] * 60)


class Puntuacion(unittest.TestCase):
    def test_puntos_suspensivos_a_mano(self):
        self.assertEqual(E.normalize_punct('como un "Sí, y...".'), "como un Sí, y.")

    def test_puntos_suspensivos_a_media_frase(self):
        self.assertEqual(E.normalize_punct("Bueno... Otra cosa."), "Bueno. Otra cosa.")


class Reanudar(unittest.TestCase):
    def test_troceo_igual(self):
        self.assertTrue(E.same_cues([{"text": "a"}], ["a", "b"]))

    def test_troceo_distinto(self):
        self.assertFalse(E.same_cues([{"text": "a b"}], ["a", "b"]))


@unittest.skipIf(np is None, "sin numpy")
class Silencios(unittest.TestCase):
    SR = 1000

    def clip(self, *tramos):
        return np.concatenate([np.full(int(s * self.SR), 0.5 if voz else 0.0, dtype="float32") for voz, s in tramos])

    def test_acorta_el_silencio_largo(self):
        w = E.cap_silences(self.clip((1, 1), (0, 0.8), (1, 1)), self.SR)
        self.assertAlmostEqual(w.size / self.SR, 2.32, places=2)

    def test_respeta_el_silencio_normal(self):
        w = self.clip((1, 1), (0, 0.3), (1, 1))
        self.assertEqual(E.cap_silences(w, self.SR).size, w.size)

    def test_no_toca_los_extremos(self):
        w = self.clip((0, 0.8), (1, 1), (0, 0.8))
        self.assertEqual(E.cap_silences(w, self.SR).size, w.size)

    def test_trim_sin_isla_conserva_la_ultima_palabra(self):
        w = self.clip((1, 1), (0, 0.3), (1, 0.2), (0, 0.5))
        self.assertGreater(E.trim_clip(w, self.SR, drop_island=False).size / self.SR, 1.5)
        self.assertLess(E.trim_clip(w, self.SR).size / self.SR, 1.1)


@unittest.skipIf(np is None, "sin numpy")
class ControlDeAudio(unittest.TestCase):
    SR = 1000

    def setUp(self):
        E._QC_RATES.clear()

    def tearDown(self):
        E._QC_RATES.clear()

    def test_voz_sin_palabras_al_final(self):
        w = np.full(3 * self.SR, 0.5, dtype="float32")
        self.assertIn("sin palabras", E.qc_audio_fault("hola", w, self.SR, last_end=1.0))
        self.assertEqual(E.qc_audio_fault("hola", w, self.SR, last_end=2.9), "")

    def test_silencio_tras_la_ultima_palabra_no_cuenta(self):
        w = np.concatenate([np.full(self.SR, 0.5, dtype="float32"), np.zeros(self.SR, dtype="float32")])
        self.assertEqual(E.qc_audio_fault("hola", w, self.SR, last_end=1.0), "")

    def test_dura_demasiado_para_sus_letras(self):
        texto = "a" * 100
        for _ in range(8):
            E.qc_note_rate(texto, np.ones(10 * self.SR, dtype="float32"), self.SR)
        self.assertEqual(E.qc_audio_fault(texto, np.ones(12 * self.SR, dtype="float32"), self.SR), "")
        self.assertIn("dura", E.qc_audio_fault(texto, np.ones(14 * self.SR, dtype="float32"), self.SR))

    def test_sin_velocidad_conocida_no_juzga_la_duracion(self):
        self.assertEqual(E.qc_audio_fault("a" * 100, np.ones(60 * self.SR, dtype="float32"), self.SR), "")

    def test_el_fallo_de_audio_suspende_aunque_el_texto_cuadre(self):
        rec, ok, _score, motivo = E._qc_eval("hola mundo", "hola mundo", 0.85, "2.8 s de voz sin palabras al final")
        self.assertEqual(rec, 1.0)
        self.assertFalse(ok)
        self.assertIn("sin palabras", motivo)


if __name__ == "__main__":
    unittest.main()
