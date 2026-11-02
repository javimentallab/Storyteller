# -*- coding: utf-8 -*-
"""La pantalla de Modelos y la ventana con cada combinación de motores: que no haya botones muertos ni mensajes falsos.
Abre la app de verdad en una carpeta de usuario temporal, sin ventana y sin GPU. Necesita PySide6.

    python -m unittest tests.test_modelos -v
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QT = importlib.util.find_spec("PySide6") is not None
WINDOWS = os.name == "nt"
DIST = {"torch": "torch-2.8.0", "torchaudio": "torchaudio-2.8.0", "f5-tts": "f5_tts-1.1.22", "coqui-tts": "coqui_tts-0.28.0",
        "faster-whisper": "faster_whisper-1.2.1", "av": "av-18.1.0", "num2words": "num2words-0.5.14",
        "beautifulsoup4": "beautifulsoup4-4.15.0", "lxml": "lxml-6.1.3"}
BASE = ["num2words", "beautifulsoup4", "lxml"]
XTTS = BASE + ["torch", "torchaudio", "coqui-tts"]
F5 = BASE + ["torch", "f5-tts"]
WHISPER = ["faster-whisper", "av"]

# Se ejecuta aparte, con su propia carpeta de usuario, y escribe en JSON lo que ve.
PROBE = r"""
import json, sys, types
root, opts = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, root)
sys.argv = ["storyteller.py", "--lang", "es"]
import storyteller as S
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

app = QApplication([])
how, warned = [], []
S.show_how = lambda parent, text: how.append(text)
QMessageBox.warning = staticmethod(lambda parent, title, text: warned.append(text))
QMessageBox.information = staticmethod(lambda parent, title, text: warned.append(text))
if opts.get("xtts_cached"):
    S.xtts_model_cached = lambda: True

def settle():
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()

def card(lay, i):
    w = lay.itemAt(i).widget()
    labels = [l.text() for l in w.findChildren(QLabel)]
    buttons = [(b.text(), b.isEnabled()) for b in w.findChildren(QPushButton)]
    return {"labels": labels, "buttons": buttons}

win = S.Cuentacuentos()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.resize(1000, 800)
win.show()
page = win.models
page.refresh()
settle()
out = {
    "note": page.note.text(),
    "qc": {"visible": page.qcChk.isVisibleTo(page), "enabled": page.qcChk.isEnabled(), "checked": page.qcChk.isChecked(),
           "hint": page.qcHint.text() if page.qcRow.isVisibleTo(page) else "",
           "how": page.qcHow.isVisibleTo(page)},
    "whisper_note": page.whisperNote.isVisibleTo(page),
    "voice_hint": page.vhint.text(),
    "voice": {S.all_models()[i]["id"]: card(page.cards, i) for i in range(page.cards.count())},
    "whisper": {S.WHISPER_MODELS[i]["id"]: card(page.whisperCards, i) for i in range(page.whisperCards.count())},
}
if opts.get("shot"):
    page.setParent(None)
    page.setAttribute(Qt.WA_DontShowOnScreen, True)
    page.resize(1000, 2000)
    page.show()
    settle()
    page.grab().save(opts["shot"])
    win.models = page

# Pulsar los botones de instalar.
for group in ("cards", "whisperCards"):
    lay = getattr(page, group)
    for i in range(lay.count()):
        for b in lay.itemAt(i).widget().findChildren(QPushButton):
            if b.text() == S._("Cómo instalarlo"):
                b.click()
if page.qcHow.isVisibleTo(page):
    page.qcHow.click()
out["how_clicks"] = list(how)
how.clear()

# Narrar: no arranca nada de verdad.
started = []
S.LocalGenWorker.start = lambda self: started.append(self.model.get("id"))
S.GenWorker.start = lambda self: started.append("remoto")
epub = opts["epub"]
win._generate(epub)
win._cleanup_gen_conns()
out["generate"] = {"started": list(started), "how": list(how), "warned": list(warned)}
how.clear(); warned.clear()

# Importar: sin Whisper dice cómo instalarlo antes de pedir un fichero.
asked = []
S.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (asked.append(1), ("", ""))[1])
win.import_audio()
out["import"] = {"asked_file": bool(asked), "how": list(how)}
how.clear()

# Una frase que el QC no pudo oír se ve al narrar y al acabar.
w = S.LocalGenWorker(epub, S.model_by_id("xtts-v2"), None, name="t")
lines = iter(["capitulos: 1\n", "[cap 1] -> 2 frases\n", "  frase 1/2 1.0s\n",
              "  QC frase 1: sin respuesta del Whisper, me quedo con lo generado\n", "  frase 2/2 1.0s\n"])
fake = types.SimpleNamespace(stdout=lines, wait=lambda: 0, returncode=0, poll=lambda: 0)
S.subprocess.Popen = lambda *a, **k: fake
emitted = []
w.progress.connect(lambda t, p: emitted.append(t))
dest = S.LOCAL_BOOKS / "t"; dest.mkdir(parents=True, exist_ok=True)
w._run_single(["py"], dest, [2])
app.processEvents()
out["qc_missed"] = {"count": w.qc_missed, "first": [t for t in emitted if "frase 1" in t], "last": [t for t in emitted if "frase 2" in t]}
win.worker = w
win.open_book = lambda folder: None
win._gen_done(str(dest))
out["gen_done"] = list(warned)
print("JSON:" + json.dumps(out, ensure_ascii=False))
win.close()
"""


@unittest.skipUnless(QT, "hace falta PySide6")
class Combinacion(unittest.TestCase):
    def see(self, main=(), xtts=(), config=None, models=(), shot=None, xtts_cached=False):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        app = home / "cuentacuentos"
        (app / "engine").mkdir(parents=True)
        for f in (ROOT / "engine").glob("*.py"):
            shutil.copyfile(f, app / "engine" / f.name)
        for venv, packages in ((".venv", main), (".venv-xtts", xtts)):
            if not packages:
                continue
            d = app / "engine" / venv
            site = d / ("Lib/site-packages" if WINDOWS else "lib/python3.11/site-packages")
            exe = d / ("Scripts/python.exe" if WINDOWS else "bin/python")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            for p in packages:
                info = site / (DIST[p] + ".dist-info")
                info.mkdir(parents=True)
                (info / "RECORD").write_text("", "utf-8")
        if config is not None:
            (app / "config.json").write_text(json.dumps(config), "utf-8")
        for rel in models:
            f = app / "models" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
        epub = home / "libro.epub"
        epub.write_bytes(b"PK")
        env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
                   PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="-1", STORYTELLER_LANG="es")
        opts = {"epub": str(epub), "shot": shot, "xtts_cached": xtts_cached}
        p = subprocess.run([sys.executable, "-c", PROBE, str(ROOT), json.dumps(opts)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=home, timeout=120)
        line = next((l for l in p.stdout.splitlines() if l.startswith("JSON:")), None)
        self.assertIsNotNone(line, p.stdout[-800:] + p.stderr[-2500:])
        return json.loads(line[5:])

    def no_dead_buttons(self, seen):
        for group in ("voice", "whisper"):
            for mid, c in seen[group].items():
                for text, enabled in c["buttons"]:
                    if text != "Usar":                       # «Usar» del activo sigue apagado, como siempre
                        self.assertTrue(enabled, (group, mid, text))

    def installs(self, seen, group, mid):
        return "Cómo instalarlo" in [t for t, _e in seen[group][mid]["buttons"]]

    def test_sin_ningun_motor(self):
        s = self.see()
        self.no_dead_buttons(s)
        self.assertIn("XTTS (engine/.venv-xtts)", s["note"])
        self.assertIn("F5, que es opcional", s["note"])
        self.assertIn("Whisper", s["note"])
        self.assertTrue(self.installs(s, "voice", "xtts-v2"))
        self.assertTrue(self.installs(s, "voice", "f5-spanish"))
        self.assertTrue(self.installs(s, "whisper", "whisper-small"))
        self.assertTrue(any("F5 es opcional" in l for l in s["voice"]["f5-spanish"]["labels"]))
        self.assertFalse(s["qc"]["enabled"])
        self.assertFalse(s["qc"]["checked"])
        self.assertEqual(s["generate"]["started"], [])
        self.assertIn(r"engine\.venv-xtts" if WINDOWS else "engine/.venv-xtts", s["generate"]["how"][0])
        self.assertEqual(s["import"]["asked_file"], False)
        self.assertIn("whisper-alone", s["import"]["how"][0])

    def test_el_modelo_de_partida_es_xtts_y_no_f5(self):
        s = self.see(xtts=XTTS)
        self.assertTrue(any(l == "activo" for l in s["voice"]["xtts-v2"]["labels"]))
        self.assertFalse(any(l == "activo" for l in s["voice"]["f5-spanish"]["labels"]))

    def test_solo_xtts(self):
        s = self.see(xtts=XTTS, xtts_cached=True)
        self.no_dead_buttons(s)
        self.assertIn("Se genera en este equipo", s["note"])         # nada de que falta un motor
        self.assertFalse(self.installs(s, "voice", "xtts-v2"))
        self.assertTrue(self.installs(s, "voice", "f5-spanish"))
        self.assertTrue(any("F5 es opcional" in l for l in s["voice"]["f5-spanish"]["labels"]))
        self.assertTrue(s["whisper_note"])
        self.assertFalse(s["qc"]["enabled"])
        self.assertTrue(s["qc"]["how"])
        self.assertIn("Sin Whisper", s["voice_hint"])
        # F5 no estorba para narrar con XTTS, sin QC y sin avisos.
        self.assertEqual(s["generate"]["started"], ["xtts-v2"])
        self.assertEqual(s["generate"]["warned"], [])
        # Los comandos de Whisper son los de su venv, no los de uno nuevo.
        self.assertTrue(any("engine" in t and ".venv-xtts" in t and "whisper" in t for t in s["how_clicks"]))
        self.assertIn(".venv-xtts", s["import"]["how"][0])

    def test_xtts_con_el_qc_activado_pero_sin_whisper_avisa_y_narra(self):
        s = self.see(xtts=XTTS, xtts_cached=True, config={"model": "xtts-v2", "qc_auto": True})
        self.assertFalse(s["qc"]["checked"])                         # la casilla no engaña
        self.assertEqual(s["generate"]["started"], ["xtts-v2"])
        self.assertEqual(len(s["generate"]["warned"]), 1)
        self.assertIn("no se revisará", s["generate"]["warned"][0])
        self.assertIn(".venv-xtts", s["generate"]["warned"][0])

    def test_xtts_con_whisper_sin_modelo_whisper(self):
        s = self.see(xtts=XTTS + WHISPER)
        self.no_dead_buttons(s)
        self.assertFalse(s["whisper_note"])
        self.assertFalse(self.installs(s, "whisper", "whisper-small"))
        self.assertTrue(any(t.startswith("Descargar") for t, _e in s["whisper"]["whisper-small"]["buttons"]))
        self.assertFalse(s["qc"]["enabled"])                          # falta el modelo, y lo dice
        self.assertFalse(s["qc"]["how"])
        self.assertIn("descárgalo", s["qc"]["hint"])
        self.assertTrue(s["import"]["asked_file"])

    def test_xtts_con_whisper_y_su_modelo_puede_revisar(self):
        s = self.see(xtts=XTTS + WHISPER, models=["whisper-small/model.bin"], xtts_cached=True,
                     config={"model": "xtts-v2", "qc_auto": True})
        self.assertTrue(s["qc"]["enabled"])
        self.assertTrue(s["qc"]["checked"])
        self.assertEqual(s["qc"]["hint"], "")
        self.assertEqual(s["generate"]["warned"], [])

    def test_whisper_solo_en_el_otro_venv_no_engana_al_qc(self):
        s = self.see(main=WHISPER, xtts=XTTS, models=["whisper-small/model.bin"], config={"model": "xtts-v2", "qc_auto": True})
        self.assertFalse(s["whisper_note"])                           # transcribir sí puede
        self.assertTrue(s["import"]["asked_file"])
        self.assertFalse(s["qc"]["enabled"])                          # el QC, en el proceso de XTTS, no
        self.assertFalse(s["qc"]["checked"])
        self.assertTrue(any(".venv-xtts" in t and "whisper" in t for t in s["how_clicks"]))

    def test_solo_whisper(self):
        s = self.see(main=BASE + WHISPER)
        self.no_dead_buttons(s)
        self.assertIn("falta un motor de voz", s["note"])
        self.assertFalse(self.installs(s, "whisper", "whisper-small"))
        self.assertTrue(s["import"]["asked_file"])

    def test_el_activo_es_f5_y_no_esta(self):
        s = self.see(xtts=XTTS, config={"model": "f5-spanish"}, models=["f5-spanish/model_1250000.safetensors", "f5-spanish/vocab.txt"])
        self.no_dead_buttons(s)
        self.assertIn("modelo activo necesita", s["note"])
        buttons = [t for t, _e in s["voice"]["f5-spanish"]["buttons"]]
        self.assertEqual(buttons, ["Cómo instalarlo", "Borrar"])     # los pesos se pueden borrar; usarlos, no
        self.assertEqual(s["generate"]["started"], [])
        self.assertIn("F5 es opcional", s["generate"]["how"][0])
        self.assertIn("deps", s["generate"]["how"][0])

    def test_la_instalacion_de_siempre_sigue_igual(self):
        s = self.see(main=F5 + WHISPER, xtts=XTTS + WHISPER, xtts_cached=True,
                     models=["f5-spanish/model_1250000.safetensors", "f5-spanish/vocab.txt", "whisper-medium/model.bin"],
                     config={"model": "xtts-v2", "qc_auto": True, "whisper_model": "whisper-medium"})
        self.no_dead_buttons(s)
        self.assertIn("Se genera en este equipo", s["note"])
        self.assertIn("✓ Instalado", s["voice"]["f5-spanish"]["labels"])
        self.assertIn("✓ Instalado", s["voice"]["xtts-v2"]["labels"])
        self.assertEqual([t for t, _e in s["voice"]["f5-spanish"]["buttons"]], ["Usar", "Borrar"])
        self.assertFalse(any("opcional" in l for l in s["voice"]["f5-spanish"]["labels"]))
        self.assertFalse(s["whisper_note"])
        self.assertTrue(s["qc"]["enabled"])
        self.assertTrue(s["qc"]["checked"])
        self.assertEqual(s["how_clicks"], [])
        self.assertEqual(s["generate"]["warned"], [])
        self.assertIn("se transcribe solo", s["voice_hint"])

    def test_con_un_servidor_propio_el_qc_no_necesita_whisper_aqui(self):
        s = self.see(config={"engine": "remote", "server": "u@h", "qc_auto": True})
        self.assertTrue(s["qc"]["enabled"])
        self.assertTrue(s["qc"]["checked"])
        self.assertEqual(s["qc"]["hint"], "")
        self.assertEqual(s["generate"]["started"], ["remoto"])
        self.assertTrue(s["import"]["asked_file"])                    # transcribe el servidor

    def test_con_un_servicio_sin_nada_dice_que_instalar(self):
        s = self.see(config={"engine": "service", "service": {"url": "https://x.example/v1"}})
        self.assertFalse(s["qc"]["visible"])
        self.assertEqual(s["generate"]["started"], [])
        self.assertIn(os.path.join("deps", "base") if WINDOWS else "deps/base", s["generate"]["how"][0])

    def test_una_frase_sin_revisar_se_ve(self):
        s = self.see(xtts=XTTS)
        self.assertEqual(s["qc_missed"]["count"], 1)
        self.assertFalse(any("sin revisar" in t for t in s["qc_missed"]["first"]))
        self.assertTrue(all("sin revisar (QC)" in t for t in s["qc_missed"]["last"]) and s["qc_missed"]["last"])
        self.assertEqual(len(s["gen_done"]), 1)
        self.assertIn("no pudo revisar 1 frases", s["gen_done"][0])


if __name__ == "__main__":
    unittest.main()
