# -*- coding: utf-8 -*-
"""Números romanos en español: «siglo XXI» se deletreaba («equis equis i») porque _fix_caps lo
tomaba por una sigla, antes de que el diccionario del usuario pudiera verlo."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import cc_engine as E

try:
    import num2words  # noqa: F401
    HAS_N2W = True
except Exception:
    HAS_N2W = False


class Romanos(unittest.TestCase):
    def setUp(self):
        self._lang = E.NUM_LANG
        E.NUM_LANG = "es"

    def tearDown(self):
        E.NUM_LANG = self._lang

    def test_valor(self):
        for s, n in [("I", 1), ("IV", 4), ("IX", 9), ("XIV", 14), ("XXI", 21), ("XL", 40),
                     ("MCMXCIX", 1999), ("xxi", 21)]:
            self.assertEqual(E._roman(s), n, s)
        for s in ["IIII", "VV", "IC", "ABC", ""]:
            self.assertIsNone(E._roman(s), s)

    def test_cifras_y_ordinales(self):
        casos = {
            "Es el siglo XXI.": "Es el siglo 21.",
            "En el siglo xxi pasó.": "En el siglo 21 pasó.",
            "Entre los siglos XIX y XX.": "Entre los siglos 19 y 20.",
            "en el siglo I a. C.": "en el siglo primero a. C.",
            "Capítulos III-V": "Capítulos 3 a 5",
            "CAPÍTULO IV": "CAPÍTULO 4",
            "Felipe II y Luis XIV.": "Felipe segundo y Luis 14.",
            "Isabel II reinó.": "Isabel segunda reinó.",
            "Juan Carlos I abdicó.": "Juan Carlos primero abdicó.",
            "Mehmet II tomó la ciudad.": "Mehmet segundo tomó la ciudad.",
            "la II Guerra Mundial": "la segunda Guerra Mundial",
            "el III Reich": "el tercer Reich",
            "Apolo XI": "Apolo 11",
        }
        for src, out in casos.items():
            self.assertEqual(E._roman_numerals(src), out, src)

    def test_lo_que_no_es_un_numero(self):
        for s in ["Anexo C y Sección D.", "Mi CV es corto.", "Then I said hello.", "Talla XL.",
                  "Un CD viejo.", "el libro vi que estaba", "LO VI TODO", "Radio MIX suena.",
                  "La parte mala.", "Vi el capítulo entero."]:
            self.assertEqual(E._roman_numerals(s), s, s)

    def test_otros_idiomas_no_cambian(self):
        E.NUM_LANG = "en"
        self.assertNotIn("21", E.normalize_text("The XXI century"))

    @unittest.skipUnless(HAS_N2W, "sin num2words")
    def test_llega_a_la_voz_en_letra(self):
        self.assertEqual(E.normalize_text("Es el siglo XXI."), "es el siglo veintiuno.")
        self.assertEqual(E.normalize_text("Felipe II y Luis XIV."), "felipe segundo y Luis catorce.")


if __name__ == "__main__":
    unittest.main()
