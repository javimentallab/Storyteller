# -*- coding: utf-8 -*-
"""Cada idioma con sus reglas de texto. Sin GPU ni modelos."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import cc_engine as E


class Idioma(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self.reset)

    def reset(self):
        E.NUM_LANG = "es"
        E._PRON_BY_LANG = {}
        E._PRON_RE = {}

    def pron(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        self.addCleanup(os.remove, path)
        E.load_pron(path)

    def test_lo_de_siempre_en_espanol(self):
        self.assertEqual(E.normalize_text("El Sr. Smith usa una CPU y pagó $74.99 por el 21000."),
                         "el señor Smith usa una ce pe u y pagó setenta y cuatro dólares con noventa y nueve "
                         "por el veintiún mil.")

    def test_las_reglas_del_espanol_no_salen_de_el(self):
        E.NUM_LANG = "en"
        t = E.normalize_text("Mr. Smith has a CPU and A+ = good.")
        self.assertNotIn("señor", t)
        self.assertNotIn("igual", t)
        self.assertNotIn(" más", t)
        self.assertIn("CPU", t)
        self.assertNotIn("ce pe u", t)
        self.assertEqual(E.normalize_text("It cost $5."), "it cost $five.")

    def test_una_sigla_al_principio_no_se_rompe(self):
        E.NUM_LANG = "en"
        self.assertEqual(E.normalize_text("CPU rules."), "CPU rules.")
        self.assertEqual(E.normalize_text("It works."), "it works.")

    def test_decimales_segun_el_idioma(self):
        E.NUM_LANG = "en"
        self.assertEqual(E.normalize_numbers("3.5 and 1,000"), "three point five and one thousand")
        E.NUM_LANG = "de"
        self.assertEqual(E.normalize_numbers("3,5 und 1.000"), "drei Komma fünf und eintausend")
        self.assertEqual(E.normalize_numbers("1,500"), "eins Komma fünf null null")

    def test_hindi_sin_cifras(self):
        E.NUM_LANG = "hi"
        self.assertEqual(E.normalize_numbers("1999 में"), "एक हज़ार नौ सौ निन्यानवे में")
        self.assertEqual(E.normalize_numbers("3.5"), "तीन दशमलव पाँच")
        self.assertEqual(E._hi_number(150000), "एक लाख पचास हज़ार")
        self.assertEqual(E._hi_number(20000000), "दो करोड़")
        self.assertEqual(E._hi_number(101), "एक सौ एक")
        self.assertEqual(len(E._HI_0_99), 100)

    def test_sin_num2words_para_ese_idioma_quedan_las_cifras(self):
        E.NUM_LANG = "zh-cn"
        self.assertEqual(E.normalize_numbers("第1999页"), "第1999页")

    def test_el_diccionario_es_de_cada_idioma(self):
        self.pron({"pron": {"software": "sófwer"}, "say_letters": ["USB"],
                   "langs": {"en": {"pron": {"nginx": "engine x"}, "say_letters": ["SQL"]},
                             "ru": {"pron": {"мир": "мiр"}}}})
        self.assertEqual(E.normalize_text("El software de nginx por USB."), "el sófwer de nginx por u ese be.")
        E.NUM_LANG = "en"
        self.assertEqual(E.normalize_text("The software of nginx, USB and SQL."),
                         "the software of engine x, USB and s q l.")
        E.NUM_LANG = "ru"
        self.assertEqual(E.normalize_text("Весь мир."), "весь мiр.")
        E.NUM_LANG = "fr"
        self.assertEqual(E.normalize_text("Le software."), "le software.")

    def test_el_diccionario_entiende_varias_palabras(self):
        self.pron({"pron": {"New York": "niu york"}})
        self.assertEqual(E.normalize_text("Fui a New York ayer."), "fui a niu york ayer.")

    def test_el_signo_de_fin(self):
        self.assertEqual(E.with_stop("hola,"), "hola.")
        self.assertEqual(E.with_stop("hola?"), "hola?")
        self.assertEqual(E.with_stop("你好，"), "你好.")
        self.assertEqual(E.with_stop("你好。"), "你好。")
        self.assertEqual(E.with_stop("आप कैसे हैं।"), "आप कैसे हैं।")
        self.assertEqual(E.with_stop("  "), "")


class Qc(unittest.TestCase):
    def test_palabras_en_cualquier_alfabeto(self):
        self.assertEqual(E._words("Canción, ñu y 3 ÁRBOLES."), ["cancion", "nu", "y", "3", "arboles"])
        self.assertEqual(E._words("Привет, мир."), ["привет", "мир"])
        self.assertEqual(E._words("你好，世界。"), ["你", "好", "世", "界"])
        self.assertEqual(E._words("彼は行く"), ["彼", "は", "行", "く"])
        self.assertEqual(len(E._words("안녕하세요 세계")), 2)      # NFKD separa el hangul, igual en los dos lados
        self.assertEqual(E._words("안녕하세요 세계"), E._words("안녕하세요, 세계."))

    def test_el_qc_ya_no_da_por_bueno_lo_que_no_lee(self):
        rec, ok, _s, _m = E._qc_eval("Привет, мир, как дела.", "совсем другое")
        self.assertLess(rec, 0.5)
        self.assertFalse(ok)
        rec, ok, _s, _m = E._qc_eval("你好，世界。", "你好世界")
        self.assertEqual(rec, 1.0)
        self.assertTrue(ok)
        rec, ok, _s, _m = E._qc_eval("你好，世界。", "再见朋友")
        self.assertFalse(ok)

    def test_whisper_no_entiende_la_region(self):
        self.assertEqual(E._whisper_lang("zh-cn"), "zh")
        self.assertEqual(E._whisper_lang("es"), "es")
        self.assertIsNone(E._whisper_lang(""))


if __name__ == "__main__":
    unittest.main()
