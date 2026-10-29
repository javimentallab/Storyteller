# -*- coding: utf-8 -*-
"""
Motor XTTS (multilingüe) para Cuentacuentos. Misma salida que cc_engine (F5):
cap NN.mp3 + capNN.json (cues) + manifest.json + gen_state.json, INCREMENTAL y
reanudable. Lee la frase entera de una pasada en el idioma del libro; XTTS
pronuncia bien los términos extranjeros sin trocear por idioma.

Reutiliza de cc_engine: chapters_from_epub, split_sentences, normalize_text,
load_pron, write_atomic, _encode_mp3, write_cues, load_prev_manifest, write_state.

Subcomandos:
  generate  --epub --out --voice WAV [--lang es --pron J --resume --device auto]
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, os, json, sys, gc

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I no añade el dir del script
import cc_engine as E   # mismas utilidades que el motor F5 (sin importar f5_tts)

FLUSH_EVERY = 6

def cmd_generate(a):
    import numpy as np, soundfile as sf, torch
    from TTS.api import TTS
    E.NUM_LANG = a.lang or "es"
    if a.pron:
        E.load_pron(a.pron)
    os.makedirs(a.out, exist_ok=True)
    chaps = E.chapters_from_epub(a.epub)
    try:
        from ebooklib import epub
        _t = epub.read_epub(a.epub).get_metadata("DC", "title")
        if _t and _t[0][0].strip():
            E.write_atomic(os.path.join(a.out, "title.txt"), _t[0][0].strip())
    except Exception: pass
    print(f"capitulos: {len(chaps)}", flush=True)
    prev = E.load_prev_manifest(a.out) if a.resume else {}
    E.write_state(a.out, len(chaps), False)

    dev = a.device
    if dev == "auto":
        dev = "cuda" if torch.cuda.is_available() else "cpu"
    os.environ.setdefault("COQUI_TOS_AGREED", "1")
    try:
        tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev)
    except Exception as e:
        if dev == "cuda":
            print(f"aviso: la GPU falló ({type(e).__name__}); reintento en CPU", flush=True)
            dev = "cpu"; tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cpu")
        else:
            raise
    print(f"modelo cargado ({dev})", flush=True)

    manifest = []
    def save_manifest():
        E.write_atomic(os.path.join(a.out, "manifest.json"),
                       json.dumps(manifest, ensure_ascii=False, indent=2))

    def infer(gen_text):
        w = tts.tts(text=gen_text, speaker_wav=a.voice, language=a.lang or "es")
        return np.asarray(w, dtype="float32"), 24000

    for ci, ch in enumerate(chaps, 1):
        base = f"cap{ci:02d}"
        wav_path = os.path.join(a.out, base + ".wav")
        mp3_path = os.path.join(a.out, base + ".mp3")
        pe = prev.get(ci)
        if a.resume and pe and pe.get("done"):
            manifest.append(pe); save_manifest()
            print(f"[cap {ci}] ya hecho, lo mantengo", flush=True); continue
        sents = E.split_sentences(ch["text"])
        entry = {"id": ci, "base": base, "title": ch["title"], "n": 0, "duration": 0.0, "done": False}
        manifest.append(entry)
        sr = 24000; cues = []; t = 0.0; pending = []; wf = None
        start_si = 0; resumed = False

        if a.resume and pe and not pe.get("done"):
            try:
                with open(os.path.join(a.out, base + ".json"), encoding="utf-8") as f:
                    cj = json.load(f)
                ex_cues = cj.get("cues", []); ex_t = float(cj.get("duration", 0.0))
            except Exception:
                ex_cues = []; ex_t = 0.0
            srp = pe.get("sr"); frames = pe.get("frames")
            if ex_cues and srp and frames and os.path.exists(wav_path):
                try:
                    data, _sr = sf.read(wav_path, dtype="float32")
                    if _sr == srp and len(data) >= frames:
                        sr = srp; cues = ex_cues; t = ex_t; start_si = len(ex_cues)
                        pending = [np.asarray(data[:frames], dtype="float32")]
                        for p in (wav_path, mp3_path):
                            try: os.remove(p)
                            except OSError: pass
                        resumed = True
                except Exception:
                    resumed = False
        if not resumed:
            for p in (wav_path, mp3_path):
                try: os.remove(p)
                except OSError: pass

        if resumed:
            print(f"[cap {ci}] '{ch['title'][:40]}' -> reanudo en frase {start_si+1}, "
                  f"quedan {len(sents)-start_si}", flush=True)
        else:
            print(f"[cap {ci}] '{ch['title'][:40]}' -> {len(sents)} frases", flush=True)

        def flush_partial(final=False):
            nonlocal wf, pending
            if pending:
                seg = np.concatenate(pending)
                if wf is None:
                    wf = sf.SoundFile(wav_path, mode="w", samplerate=sr, channels=1, subtype="PCM_16")
                wf.write(seg); wf.flush()
                seg_tmp = os.path.join(a.out, base + ".seg.wav.tmp")
                sf.write(seg_tmp, seg, sr, format="WAV", subtype="PCM_16")
                E._encode_mp3(seg_tmp, mp3_path, append=True)
                try: os.remove(seg_tmp)
                except OSError: pass
                pending = []
            E.write_cues(a.out, base, ch["title"], cues, t)
            entry["n"] = len(cues); entry["duration"] = round(t, 3)
            entry["sr"] = sr; entry["frames"] = (wf.tell() if wf is not None else 0)
            save_manifest()

        if resumed:
            flush_partial()

        for si in range(start_si, len(sents)):
            gen_text = E.normalize_text(sents[si])
            wav, sr = infer(gen_text)
            dur = len(wav) / sr
            cues.append({"i": si, "start": round(t, 3), "end": round(t + dur, 3), "text": sents[si]})
            pending.append(wav); pending.append(np.zeros(int(a.gap * sr), dtype="float32"))
            t += dur + a.gap
            print(f"  frase {si+1}/{len(sents)} {dur:.1f}s", flush=True)
            if (si + 1) % FLUSH_EVERY == 0:
                flush_partial(); print(f"  ...guardado parcial cap {ci} ({len(cues)} frases)", flush=True)
        flush_partial(final=True)
        if wf is not None:
            wf.close()
            E._encode_mp3(wav_path, mp3_path, append=False)
            try: os.remove(wav_path)
            except OSError: pass
        entry["done"] = True; entry["frames"] = 0; save_manifest()
        print(f"[cap {ci}] LISTO {t:.1f}s -> {base}.mp3", flush=True)
        gc.collect()
        try: torch.cuda.empty_cache()
        except Exception: pass
    E.write_state(a.out, len(chaps), True)
    print("EPUB-DONE", len(manifest), flush=True)

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--epub", required=True); g.add_argument("--out", required=True)
    g.add_argument("--voice", required=True)              # wav de referencia (clonar voz)
    g.add_argument("--lang", default="es")
    g.add_argument("--pron", default="")
    g.add_argument("--gap", type=float, default=0.28)
    g.add_argument("--device", default="auto")
    g.add_argument("--resume", action="store_true")
    g.set_defaults(func=cmd_generate)
    a = ap.parse_args(); a.func(a)

if __name__ == "__main__":
    main()
