# -*- coding: utf-8 -*-
"""Barra de la narración en el servidor.

Antes el total solo sumaba las frases de los capítulos ya empezados, así que la barra marcaba casi
el 100 % todo el rato (cap. 17 de 44 y la barra casi llena). Ahora va sobre el libro entero, con
las frases que cuenta el servidor al empezar.

    python -m unittest tests.test_barra_remoto -v
"""
import sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_core as C


def frase(i, n):
    return f"  frase {i}/{n} 3.2s"


class Recuento(unittest.TestCase):
    def test_lee_capitulos_y_frases(self):
        self.assertEqual(C.parse_count("CHAPTERS 3\nSENTS 10 0 5\n"), (3, [10, 0, 5]))

    def test_sin_frases_o_descuadradas_no_da_estimacion(self):
        self.assertEqual(C.parse_count("CHAPTERS 3\n"), (3, []))
        self.assertEqual(C.parse_count("CHAPTERS 3\nSENTS 10 5\n"), (3, []))
        self.assertEqual(C.parse_count(""), (1, []))


class AvanceRemoto(unittest.TestCase):
    def test_desde_cero_no_marca_el_capitulo_como_el_libro(self):
        p = C.RemoteProgress([10, 30, 60])
        p.line("[cap 1] 'Uno' -> 10 frases")
        for i in range(1, 11):
            self.assertTrue(p.line(frase(i, 10)))
        self.assertEqual(p.pct(), 10)                      # antes: 99 %
        p.line("[cap 2] 'Dos' -> 30 frases")
        self.assertEqual(p.pct(), 10)
        for i in range(1, 16):
            p.line(frase(i, 30))
        self.assertEqual(p.pct(), 25)

    def test_reanudar_cuenta_lo_ya_hecho(self):
        """Como el libro real: 17 capítulos hechos de 44, a mitad del 18."""
        sents = [100] * 44
        p = C.RemoteProgress(sents)
        for cap in range(1, 18):
            p.line(f"[cap {cap}] ya hecho, lo mantengo")
        p.line("[cap 18] 'Dieciocho' -> reanudo en frase 51, quedan 50")
        self.assertEqual(p.pct(), int(1750 / 4400 * 100))
        p.line(frase(51, 100))
        self.assertEqual(p.remaining(), 4400 - 1751)

    def test_tramos_mezclados_y_linea_pegada(self):
        p = C.RemoteProgress([4, 4])
        p.line("[cap 1] 'Uno' -> 4 frases")
        p.line("[cap 2] 'Dos' -> 4 frases")
        p.line(frase(1, 4))
        p.line("99%|#####| " + frase(1, 4).strip())       # la barra de otro tramo, pegada delante
        p.line(frase(2, 4))
        self.assertEqual(p.remaining(), 5)
        self.assertEqual(p.pct(), 37)

    def test_qc_y_otras_lineas_no_cuentan(self):
        p = C.RemoteProgress([5])
        p.line("[cap 1] 'Uno' -> 5 frases")
        self.assertFalse(p.line("  QC frase 1: cola cortada, de 4.0 s a 3.1 s"))
        self.assertFalse(p.line("  QC frase 1: recall=0.40, regenerando (1/3)"))
        self.assertFalse(p.line("[cap 1] LISTO 12.0s -> cap01.mp3"))
        self.assertEqual(p.remaining(), 5)

    def test_nunca_100_ni_negativo(self):
        p = C.RemoteProgress([2])
        p.line("[cap 1] 'Uno' -> 2 frases")
        for _ in range(5):
            p.line(frase(1, 2))
        self.assertEqual(p.remaining(), 0)
        self.assertEqual(p.pct(), 99)

    def test_tiempo_restante_con_el_ritmo(self):
        p = C.RemoteProgress([100])
        p.line("[cap 1] 'Uno' -> 100 frases")
        t = [1000.0]
        with mock.patch.object(C.time, "monotonic", lambda: t[0]):
            self.assertIsNone(p.seconds())
            for i in range(1, 11):
                p.line(frase(i, 100)); t[0] += 10.0       # 10 s por frase
            t[0] -= 10.0
            self.assertAlmostEqual(p.seconds(), 900.0)     # 90 frases × 10 s

    def test_libro_sin_frases_no_da_porcentaje(self):
        self.assertEqual(C.RemoteProgress([]).pct(), -1)


if __name__ == "__main__":
    unittest.main()
