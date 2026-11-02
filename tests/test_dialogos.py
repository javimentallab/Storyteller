# -*- coding: utf-8 -*-
"""Los avisos: botones de Qt en el idioma de la interfaz, comandos sin partir y el desplegable con el nombre entero.
Abre cada idioma aparte, en una carpeta de usuario temporal, sin ventana y sin GPU. Necesita PySide6.

    python -m unittest tests.test_dialogos -v
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QT = importlib.util.find_spec("PySide6") is not None

PROBE = r"""
import json, sys
root, lang = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
sys.argv = ["storyteller.py", "--lang", lang]
import storyteller as S
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QPlainTextEdit, QPushButton

app = QApplication([])
S.install_translations(app)

box = QMessageBox(QMessageBox.Icon.Question, "t", "x",
                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
buttons = {b: box.button(getattr(QMessageBox.StandardButton, b)).text().replace("&", "") for b in ("Yes", "No", "Ok", "Cancel")}

texts = [S.engine_how_text({"backend": "xtts"}), S.engine_how_text({"backend": "f5"}), S.whisper_how_text(),
         S.xtts_needs_text('XTTS-NEEDS {"lang": "ja", "packages": ["cutlet"]}')]
parts = [S.how_parts(t) for t in texts]
dlg = S.how_dialog(None, texts[0])
edit = dlg.findChild(QPlainTextEdit)

combo = QComboBox()
long_name = "Altavoces (Dispositivo de sonido con un nombre larguísimo que no cabe en el cuadro)"
combo.addItems(["Corto", long_name])
combo.setMaximumWidth(120)
S.fit_popup(combo)

print(json.dumps({
    "lang": S.LANG, "buttons": buttons,
    "expected": [S._("Sí"), S._("No"), S._("Aceptar"), S._("Cancelar")],
    "commands": [p[1] for p in parts], "before": [p[0] for p in parts], "after": [p[2] for p in parts],
    "install": [S.install_text("xtts"), S.install_text("f5"), S.whisper_install_text(), S.install_text("xtts-languages")],
    "plain": S.how_parts("Un aviso sin comandos.\n\nCon dos párrafos."),
    "box": edit.toPlainText(), "nowrap": edit.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap, "readonly": edit.isReadOnly(),
    "pressed": [b.text() for b in dlg.findChildren(QPushButton)], "copy": S._("Copiar"), "ok": S._("Aceptar"),
    "popup": combo.view().minimumWidth(), "name": combo.fontMetrics().horizontalAdvance(long_name),
}, ensure_ascii=False))
"""


@unittest.skipUnless(QT, "hace falta PySide6")
class TestDialogos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="st-dialogos-test-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def see(self, lang):
        env = dict(os.environ, USERPROFILE=str(self.home), HOME=str(self.home), QT_QPA_PLATFORM="offscreen",
                   PYTHONIOENCODING="utf-8", STORYTELLER_LANG=lang)
        p = subprocess.run([sys.executable, "-c", PROBE, str(ROOT), lang], capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        return json.loads(p.stdout.strip().splitlines()[-1])

    def languages(self):
        return ["es"] + sorted(f.stem for f in (ROOT / "i18n").glob("*.txt"))

    def test_todos_los_idiomas(self):
        for lang in self.languages():
            with self.subTest(lang=lang):
                s = self.see(lang)
                self.assertEqual(s["lang"], lang)
                # Los botones de Qt, también en tailandés e hindi, que Qt no trae.
                self.assertEqual([s["buttons"][b] for b in ("Yes", "No", "Ok", "Cancel")], s["expected"])
                # Los comandos salen enteros y aparte del texto, en todas las traducciones.
                self.assertEqual(s["commands"], s["install"])
                for before, commands in zip(s["before"], s["commands"]):
                    self.assertTrue(before)
                    self.assertNotIn(commands, before)
                # El cuadro no parte las líneas y no se puede editar sin querer.
                self.assertEqual(s["box"], s["install"][0])
                self.assertTrue(s["nowrap"])
                self.assertTrue(s["readonly"])
                self.assertEqual(sorted(s["pressed"]), sorted([s["copy"], s["ok"]]))

    def test_un_aviso_sin_comandos_sigue_igual(self):
        s = self.see("es")
        self.assertEqual(s["plain"], ["Un aviso sin comandos.\n\nCon dos párrafos.", "", ""])

    def test_el_desplegable_cabe_entero(self):
        s = self.see("es")
        self.assertGreater(s["popup"], s["name"])


if __name__ == "__main__":
    unittest.main()
