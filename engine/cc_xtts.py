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

def _trim_clip(w, sr=24000):
    """XTTS mete al final de cada clip silencio y, a veces, una 'isla' de ruido
    (la alucinación final). Recorta el silencio de los dos extremos (deja 40 ms de
    margen) y, si el último tramo de voz es corto y está separado del cuerpo por un
    silencio largo, lo descarta (= quita la alucinación final sin tocar la voz)."""
    import numpy as np
    if w is None or w.size == 0:
        return w
    fr = max(1, int(0.02 * sr))                     # ventana de 20 ms
    nf = w.size // fr
    if nf < 3:
        return w
    env = np.sqrt(np.mean(w[:nf * fr].reshape(nf, fr) ** 2, axis=1) + 1e-9)
    peak = float(env.max())
    if peak <= 0:
        return w
    thr = peak * (10 ** (-40.0 / 20.0))             # −40 dB rel. pico = hay voz
    idx = np.where(env >= thr)[0]
    if idx.size == 0:
        return w
    first, last = int(idx[0]), int(idx[-1])
    # tramos contiguos de voz
    segs, run = [], [idx[0], idx[0]]
    for k in idx[1:]:
        if k == run[1] + 1:
            run[1] = k
        else:
            segs.append(tuple(run)); run = [k, k]
    segs.append(tuple(run))
    SIL = int(0.22 / 0.02)                           # hueco "largo" >= 220 ms
    SHORT = int(0.35 / 0.02)                          # isla "corta" <= 350 ms
    if len(segs) >= 2:
        (_s0, e0), (s1, e1) = segs[-2], segs[-1]
        if (s1 - e0) >= SIL and (e1 - s1) <= SHORT:   # isla final separada = alucinación
            last = e0
    pad = int(0.04 * sr)
    a0 = max(0, first * fr - pad)
    b0 = min(w.size, (last + 1) * fr + pad)
    return w[a0:b0]

def _clean_clip(w, sr=24000, prop=0.75):
    """Quita el ruido de fondo que XTTS copia de la grabación de referencia.
    Se limpia la SALIDA y no la referencia: limpiar la referencia se come la z/c
    (fricativa débil) y XTTS cae en acento latino. Pasa-altos 70 Hz + reducción
    de ruido estacionario. Sin noisereduce instalado, devuelve el clip tal cual."""
    import numpy as np
    if w is None or w.size < sr // 10:
        return w
    try:
        import noisereduce as nr
        from scipy.signal import butter, sosfiltfilt
    except Exception:
        return w
    w = sosfiltfilt(butter(4, 70, "highpass", fs=sr, output="sos"), w).astype("float32")
    return np.asarray(nr.reduce_noise(y=w, sr=sr, stationary=True, prop_decrease=prop),
                      dtype="float32")

def cmd_generate(a):
    import numpy as np, soundfile as sf, torch
    from TTS.api import TTS
    E.NUM_LANG = a.lang or "es"
    if a.pron:
        E.load_pron(a.pron)
    os.makedirs(a.out, exist_ok=True)
    chaps = E.chapters_from_epub(a.epub)
    # Modo paralelo: este proceso narra SOLO [chap_start, chap_end] (1-based, 0=sin
    # tope), escribe su propio manifest (--manifest-name) y no toca gen_state
    # (--no-state); la app fusiona los tramos al terminar.
    cs = getattr(a, "chap_start", 0) or 0
    ce = getattr(a, "chap_end", 0) or 0
    mani_name = getattr(a, "manifest_name", None) or "manifest.json"
    no_state = getattr(a, "no_state", False)
    if not cs or cs <= 1:                                 # solo el 1er tramo escribe el titulo
        try:
            from ebooklib import epub
            _t = epub.read_epub(a.epub).get_metadata("DC", "title")
            if _t and _t[0][0].strip():
                E.write_atomic(os.path.join(a.out, "title.txt"), _t[0][0].strip())
        except Exception: pass
    print(f"capitulos: {len(chaps)}", flush=True)
    prev = E.load_prev_manifest(a.out, mani_name) if a.resume else {}
    if not no_state:
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
        E.write_atomic(os.path.join(a.out, mani_name),
                       json.dumps(manifest, ensure_ascii=False, indent=2))

    def infer(gen_text):
        # cerrar con punto si no acaba en signo de fin: XTTS necesita el "stop" para
        # no desbarrar al final (principal causa de la alucinación final).
        t = (gen_text or "").strip().rstrip(",;: ")          # ",." al final le hacía comerse la cola
        if t and t[-1] not in ".?!…":
            t += "."
        w = tts.tts(text=t, speaker_wav=a.voice, language=a.lang or "es")
        return _trim_clip(_clean_clip(np.asarray(w, dtype="float32"), 24000), 24000), 24000

    for ci, ch in enumerate(chaps, 1):
        if (cs and ci < cs) or (ce and ci > ce):          # fuera de mi tramo
            continue
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
            # QC de alucinaciones: transcribe la frase con Whisper (voz-server) y, si el
            # recall baja del umbral, regenera (XTTS al re-inferir da otra muestra) y se
            # queda con la mejor. Igual que el motor F5; reutiliza utilidades de cc_engine.
            best_wav = None; best_score = -1e9; sr = 24000
            attempts = (a.qc_retries + 1) if E.qc_on(a) else 1
            for att in range(attempts):
                wav, sr = infer(gen_text)
                if not E.qc_on(a):
                    best_wav = wav; break
                hyp = E.qc_hyp(wav, sr, a)
                if hyp is None:
                    if best_wav is None: best_wav = wav
                    print(f"  QC frase {si+1}: sin respuesta del Whisper, me quedo con lo generado", flush=True)
                    break
                rec, ok, score, motivo = E._qc_eval(gen_text, hyp, a.qc_threshold)
                if score > best_score: best_score, best_wav = score, wav
                if ok:
                    if att > 0: print(f"  QC frase {si+1}: OK recall={rec:.2f} (intento {att+1})", flush=True)
                    break
                det = f"recall={rec:.2f}" + (f", alucina ({motivo})" if motivo else f" < {a.qc_threshold:.2f}")
                print(f"  QC frase {si+1}: {det}, regenerando ({att+1}/{attempts})", flush=True)
            wav = best_wav
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
    if not no_state:
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
    g.add_argument("--gap", type=float, default=0.12)   # hueco corto: la cola ya va recortada
    g.add_argument("--device", default="auto")
    g.add_argument("--resume", action="store_true")
    g.add_argument("--chap-start", type=int, default=0)      # tramo paralelo: 1er cap (1-based, 0=sin tope)
    g.add_argument("--chap-end", type=int, default=0)        # tramo paralelo: ultimo cap (0=sin tope)
    g.add_argument("--manifest-name", default="manifest.json")
    g.add_argument("--no-state", action="store_true")
    g.add_argument("--qc-url", default="")                   # endpoint Whisper HTTP (voz-server)
    g.add_argument("--qc-whisper-dir", default="")           # modelo Whisper LOCAL (sin red); uno u otro activa el QC
    g.add_argument("--qc-threshold", type=float, default=0.85)
    g.add_argument("--qc-retries", type=int, default=2)
    g.set_defaults(func=cmd_generate)
    c = sub.add_parser("count")                              # nº de capitulos (reparto de tramos)
    c.add_argument("--epub", required=True)
    c.set_defaults(func=E.cmd_count)                         # CHAPTERS + SENTS (mismo reparto en frases)
    a = ap.parse_args(); a.func(a)

if __name__ == "__main__":
    main()
