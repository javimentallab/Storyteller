# -*- coding: utf-8 -*-
"""Que ningún tramo se quede sin trabajo al reanudar.

Los capítulos se repartían por número (1-22 y 23-44), estuvieran hechos o no: en un libro
avanzado, un tramo acababa al momento y el otro narraba solo, con el selector en 2.

    python -m unittest tests.test_reparto -v
"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import st_core as C


def done(c):
    return {"id": c, "done": True, "n": 5}


def half(c, n):
    return {"id": c, "done": False, "n": n}


class Reparto(unittest.TestCase):
    def test_libro_avanzado_los_dos_tramos_tienen_trabajo(self):
        # El caso real: 44 capítulos, hasta el 32 hechos, y el 35 con más de la mitad de lo que falta.
        sents = [100] * 32 + [352, 321, 1390, 225, 134, 61, 44, 2, 30, 2, 7, 4]
        plan = C.plan_streams(44, 2, sents, [done(c) for c in range(1, 33)] + [half(33, 150)])
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0], list(range(1, 33)) + [35])
        self.assertEqual(plan[1], [33, 34, 36, 37, 38, 39, 40, 41, 42, 43, 44])

    def test_cada_capitulo_entero_en_un_solo_tramo(self):
        sents = [7, 90, 3, 45, 45, 2, 60, 11]
        entries = [done(2), half(4, 10)]
        for streams in (1, 2, 3):
            plan = C.plan_streams(8, streams, sents, entries)
            self.assertEqual(sorted(c for p in plan for c in p), list(range(1, 9)))

    def test_lo_narrado_de_un_capitulo_a_medias_no_cuenta(self):
        # El 1 tiene 100 frases, pero solo le quedan 10: pesa menos que el 2.
        self.assertEqual(C.plan_streams(3, 2, [100, 50, 45], [half(1, 90)]), [[2], [1, 3]])

    def test_con_menos_pendientes_que_tramos_salen_menos_tramos(self):
        plan = C.plan_streams(4, 3, [5, 5, 5, 5], [done(1), done(2), done(3)])
        self.assertEqual(plan, [[1, 2, 3, 4]])

    def test_todo_hecho_es_un_tramo_que_lo_conserva(self):
        self.assertEqual(C.plan_streams(3, 2, [5, 5, 5], [done(1), done(2), done(3)]), [[1, 2, 3]])

    def test_libro_nuevo_equilibra_por_frases(self):
        self.assertEqual(C.plan_streams(4, 2, [100, 10, 10, 10]), [[1], [2, 3, 4]])

    def test_sin_recuento_de_frases_reparte_por_capitulos(self):
        plan = C.plan_streams(4, 2, [])
        self.assertEqual(sorted(len(p) for p in plan), [2, 2])

    def test_un_solo_tramo_es_el_libro_entero(self):
        self.assertEqual(C.plan_streams(3, 1, [5, 5, 5]), [[1, 2, 3]])


class TiempoRestante(unittest.TestCase):
    def test_con_capitulos_sueltos_cuenta_los_de_cada_tramo(self):
        eta = C.EtaTracker([10, 20, 30], {1: [2], 2: [1, 3]})
        self.assertEqual(eta._rem(1), 20)
        self.assertEqual(eta._rem(2), 40)


if __name__ == "__main__":
    unittest.main()
