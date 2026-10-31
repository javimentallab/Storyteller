"""
Interface languages for Storyteller.

The program's texts are written in Spanish (SOURCE below, in the order they appear in the
code); _() returns them in the chosen language. Translations live in i18n/<lang>.txt, one
"NN|text" per line, where NN is the text's position in SOURCE; \\n stands for a line break
and the {0}, {1}... placeholders must stay. The language comes from `--lang xx`, the
STORYTELLER_LANG environment variable or the system's language; anything without a file
falls back to English.
"""
import ctypes
import locale
import os
import re
import sys
from pathlib import Path

SOURCE = [
    'F5 Español',  # 0
    'Castellano peninsular, alta calidad. Es la voz A.',  # 1
    'F5 Inglés (oficial)',  # 2
    'Inglés (modelo base oficial F5-TTS). Mejor con una voz de referencia en inglés.',  # 3
    'XTTS v2 (multilingüe)',  # 4
    'Multilingüe: pronuncia bien los términos en inglés dentro del español. Clona tu voz. Usa más VRAM que F5.',  # 5
    'Rápido; calidad suficiente para subtítulos de audiolibros.',  # 6
    'Mejor calidad, algo más lento.',  # 7
    'Máxima calidad; el más pesado y lento.',  # 8
    'Inglés — ejemplo F5',  # 9
    'Español',  # 10
    'Detectar automáticamente',  # 11
    'Inglés',  # 12
    'Francés',  # 13
    'Alemán',  # 14
    'Italiano',  # 15
    'Portugués',  # 16
    'Catalán',  # 17
    'Gallego',  # 18
    'Euskera',  # 19
    'QC activado pero no hay modelo Whisper descargado. Descárgalo en «Modelos» para que revise la calidad. Narro sin QC.',  # 20
    'Voz A — grave (F5 Español)',  # 21
    'calculando tiempo restante…',  # 22
    'menos de 1 min',  # 23
    '~{0} min',  # 24
    '~{0} h {1} min',  # 25
    'mañana {0}',  # 26
    'en {0} días, {1}',  # 27
    'quedan {0} (hacia las {1})',  # 28
    'quedan {0} ({1})',  # 29
    'Sección {0}',  # 30
    'Enviando el EPUB a Mint…',  # 31
    'No pude enviar el EPUB a Mint:\n',  # 32
    'Reanudando el motor de voz…',  # 33
    'Arrancando el motor de voz…',  # 34
    'Narrando capítulo {0}/{1} — frase {2}',  # 35
    'Revisando calidad (QC) y regenerando una frase…',  # 36
    'Capítulo {0} listo',  # 37
    'Liberando la GPU…',  # 38
    'Guardando lo generado…',  # 39
    'Trayendo la grabación…',  # 40
    'Lo detuve, pero aún no había ningún capítulo terminado que guardar.',  # 41
    'La generación terminó pero no pude traerme el audio:\n',  # 42
    'Error inesperado: {0}',  # 43
    'Contando las frases del libro…',  # 44
    'La narración no produjo audio. Puede faltar memoria en la GPU (cierra juegos u apps que la usen) o el modelo estar incompleto.',  # 45
    'Reanudando la narración…',  # 46
    'Cargando el modelo…',  # 47
    'La GPU estaba ocupada; generando en CPU (más lento)…',  # 48
    'Modelo cargado, narrando…',  # 49
    'Narrando en {0} tramos en paralelo — cargando modelos…',  # 50
    'Narrando en {0} tramos — capítulo {1}/{2} — frase {3}',  # 51
    'La GPU estaba ocupada; un tramo va en CPU (más lento)…',  # 52
    'La descarga no dejó el modelo completo.',  # 53
    'La descarga del modelo XTTS no se completó.',  # 54
    'No encuentro el audio de este libro, no puedo reanudar.',  # 55
    'Convirtiendo audio…',  # 56
    'No se pudo convertir el audio (¿formato soportado? ¿ffmpeg instalado?).',  # 57
    'No pude leer la duración del audio.',  # 58
    'Error inesperado al importar: {0}',  # 59
    'Transcribiendo (servidor)… {0}–{1} de {2}  ·  guardado {3}/{4} min',  # 60
    'Se cortó la transcripción (¿servidor de voz caído?). Lo transcrito se ha guardado; puedes Reanudar.',  # 61
    'Antes descarga un modelo de transcripción (Whisper) en «Modelos».',  # 62
    'Cargando el modelo de transcripción…',  # 63
    'Transcribiendo con Whisper… {0} / {1}',  # 64
    'Transcribiendo con Whisper…',  # 65
    'No se pudo transcribir en local (¿modelo Whisper incompleto o falta memoria?).',  # 66
    ' · ⏸ a medias',  # 67
    '{0} capítulos · {1} min{2}',  # 68
    '▸ Reanudar',  # 69
    'Seguir narrando este libro desde donde se detuvo',  # 70
    'Eliminar este libro',  # 71
    'tu biblioteca narrada',  # 72
    '↻ Actualizar',  # 73
    'Volver a leer la carpeta de libros',  # 74
    'Modelos',  # 75
    'Importar audiolibro…',  # 76
    'Añadir un audiolibro/audio existente y transcribirlo (Whisper) para subtítulos',  # 77
    'Abrir EPUB…',  # 78
    'Todavía no hay libros.\nPulsa «Abrir EPUB…» y Storyteller lo narrará con la voz A.',  # 79
    'Detener y guardar',  # 80
    'Cancelar',  # 81
    'Eliminar libro',  # 82
    '¿Eliminar «{0}» y su grabación? No se puede deshacer.',  # 83
    '‹ Biblioteca',  # 84
    'Modelos y voces',  # 85
    'Generar en:',  # 86
    'Este equipo (local)',  # 87
    'Servidor propio',  # 88
    'Servidor:',  # 89
    'usuario@host de tu servidor (SSH)',  # 90
    'Revisar y regenerar frases que suenen mal (QC Whisper)',  # 91
    'Tras narrar cada frase, la transcribe con Whisper y, si no coincide con el texto, la regenera. Más lento pero caza alucinaciones. Usa el Whisper del sitio donde narras: en LOCAL el modelo descargado en «Modelos»; en el Mint, el del Mint. (En local hace falta un modelo Whisper descargado.)',  # 92
    'Velocidad / uso de GPU:',  # 93
    'Normal — 1 tramo (no satura la GPU, más lento)',  # 94
    'Rápido — 2 tramos (≈1,8×)',  # 95
    'Máximo — 3 tramos (≈2,6×, exprime la GPU)',  # 96
    'Cómo narrar en este equipo:\n• Normal: un solo proceso, como siempre. No satura la GPU (puedes seguir usándola), pero es lo más lento.\n• Rápido / Máximo: reparte los capítulos entre 2 o 3 procesos a la vez. Mucho más rápido, pero la GPU queda muy cargada (con 3, al límite: no juegues mientras narra). Si diera error de memoria de la GPU, baja de 3 a 2.',  # 97
    'Modelos de voz (narrar EPUB)',  # 98
    '+ Añadir modelo por URL / repo…',  # 99
    'Transcripción de audiolibros (Whisper)',  # 100
    'Idioma del audio:',  # 101
    'Voces de referencia',  # 102
    '+ Añadir voz (archivo)…',  # 103
    '+ Cargar voz por URL…',  # 104
    'Al añadir una voz, el texto de referencia se transcribe solo (Whisper); puedes corregirlo.',  # 105
    'Voces descargables',  # 106
    'Cómo lee la voz ciertas palabras: términos extranjeros, siglas y palabras concretas que suenen mal. No cambia los subtítulos.',  # 107
    'Editar diccionario de pronunciación…',  # 108
    'El motor local se está instalando (PyTorch + F5). Mientras tanto puedes generar en un servidor propio.',  # 109
    'Se genera en este equipo con la GPU. Elige modelo y voz; descarga el modelo si hace falta.',  # 110
    'Se genera por SSH en el servidor que indiques (debe tener el mismo motor instalado). El modelo y la voz de aquí se usan en el modo local.',  # 111
    '✓ Instalado',  # 112
    'Sin descargar · {0}',  # 113
    '{0}  · Idioma: {1}  ({2})',  # 114
    'Usar',  # 115
    'Borrar',  # 116
    'Descargar  ({0})',  # 117
    'El motor XTTS aún se está instalando',  # 118
    'Espera a que termine de instalarse el motor local',  # 119
    'Quitar',  # 120
    'Diccionario de pronunciación',  # 121
    'pron: palabra → cómo se dice · say_letters: siglas deletreadas · say_word: siglas como palabra',  # 122
    'El JSON no es válido; no se guardó.',  # 123
    'Diccionario de pronunciación guardado.',  # 124
    'Modelo «{0}» borrado (archivos).',  # 125
    'Descargando XTTS v2… (puede tardar, ~1,8 GB)',  # 126
    'XTTS v2 listo.',  # 127
    'Error descargando XTTS: ',  # 128
    'Modelo XTTS borrado (caché).',  # 129
    'No había caché de XTTS.',  # 130
    'Añadir modelo',  # 131
    'Repo o URL de Hugging Face\n(p. ej. usuario/modelo o https://huggingface.co/usuario/modelo):',  # 132
    'Nombre del checkpoint dentro del repo:',  # 133
    'Nombre del vocabulario dentro del repo:',  # 134
    'Arquitectura (F5TTS_Base para la mayoría; F5TTS_v1_Base para el oficial v1):',  # 135
    'Idioma del modelo (código: es, en, fr, de, it, pt…):',  # 136
    'Nombre para mostrar:',  # 137
    'Añadido por ti',  # 138
    'Modelo «{0}» añadido. Pulsa Descargar.',  # 139
    'Descargando «{0}»…',  # 140
    'Descarga completada.',  # 141
    'Error en la descarga: ',  # 142
    'Modelo de transcripción «{0}» borrado.',  # 143
    'Modelo de transcripción listo.',  # 144
    'no se pudo convertir el audio (¿ffmpeg?)',  # 145
    'Transcribiendo la voz con Whisper…',  # 146
    'Texto de la voz',  # 147
    'Texto que dice el audio (autodetectado; corrígelo si hace falta):',  # 148
    'Añadir voz',  # 149
    'Nombre para la voz:',  # 150
    'Mi voz',  # 151
    'Voz «{0}» añadida y activada.',  # 152
    'No pude añadir la voz: ',  # 153
    'Audio de la voz (5-12 s, limpio)',  # 154
    'Audio (*.wav *.mp3 *.flac *.m4a *.ogg *.opus)',  # 155
    'Cargar voz por URL',  # 156
    'Enlace al audio de la voz (http/https, wav/mp3, 5-15 s):',  # 157
    'La URL debe empezar por http:// o https://',  # 158
    'Descargando la voz…',  # 159
    'No se pudo descargar la voz desde esa URL.',  # 160
    'URL de voz no válida.',  # 161
    'No se pudo descargar la voz.',  # 162
    'Voz «{0}» descargada.',  # 163
    'Error al guardar la voz: ',  # 164
    '{0}  · Idioma: {1}',  # 165
    '✓ Descargada',  # 166
    'Descargar',  # 167
    '↻ Regenerar',  # 168
    'Volver a narrar el libro entero desde su EPUB',  # 169
    'Seguir narrando desde donde se detuvo',  # 170
    '⤓ Exportar',  # 171
    'Guardar el audio narrado como MP3',  # 172
    'Salida de audio',  # 173
    'Volumen',  # 174
    'Velocidad',  # 175
    '▲ Página',  # 176
    'Página ▼',  # 177
    'Retroceder una página del texto',  # 178
    'Avanzar una página del texto',  # 179
    '⤓ Ir a posición…',  # 180
    'Saltar a un minuto:segundo del capítulo',  # 181
    '≡ Ir a capítulo…',  # 182
    'Elegir un capítulo de la lista',  # 183
    'Ir a posición',  # 184
    'Minuto:segundo (capítulo de {0}):',  # 185
    'Escribe el tiempo como «minutos:segundos», p. ej. 3:40.',  # 186
    'Ir a capítulo',  # 187
    'Este título tiene un solo capítulo.',  # 188
    'Capítulo:',  # 189
    'Regenerar',  # 190
    '¿Volver a narrar el libro entero desde cero? Se sobrescribe lo actual.',  # 191
    'Exportar',  # 192
    'No hay audio generado para exportar.',  # 193
    'Guardar narración como MP3',  # 194
    'Audio MP3 (*.mp3)',  # 195
    'ffmpeg falló',  # 196
    'No se encontró ffmpeg. Hace falta para unir los capítulos.',  # 197
    'No se pudo exportar:\n{0}',  # 198
    'Guardado:\n{0}\n\n{1} capítulo(s).',  # 199
    'Elige un EPUB',  # 200
    'Libros EPUB (*.epub)',  # 201
    'Elige un audiolibro para importar',  # 202
    'Audio (*.mp3 *.m4a *.aac *.ogg *.opus *.flac *.wav *.wma);;Todos (*.*)',  # 203
    'Reanudando transcripción…',  # 204
    'Preparando…',  # 205
    'Deteniendo y guardando lo transcrito…',  # 206
    'Cancelando…',  # 207
    'Importar audiolibro',  # 208
    'Reanudar',  # 209
    'No encuentro el EPUB original de este libro, no puedo reanudar.',  # 210
    'Ya hay una narración en marcha. Espera a que termine o deténla antes de empezar otra.',  # 211
    'El motor todavía se está instalando. Prueba dentro de un rato, o cambia a «Servidor propio» en Modelos.',  # 212
    'Antes descarga el modelo «{0}» en Modelos.',  # 213
    'Reanudando…',  # 214
    'Deteniendo y guardando lo generado…',  # 215
    'Guardando la narración en curso…',  # 216
    'Guardando la transcripción en curso…',  # 217
    'activo',  # 218
    'Pronunciación',  # 219
]

HERE = Path(__file__).resolve().parent / "i18n"


def _system_lang():
    try:
        if sys.platform == "win32":
            return locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
        return os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    except Exception:
        return ""


def available():
    return ["es"] + sorted(p.stem for p in HERE.glob("*.txt"))


def pick_lang(argv=None):
    argv = sys.argv if argv is None else argv
    wanted = ""
    if "--lang" in argv:
        i = argv.index("--lang")
        if i + 1 < len(argv):
            wanted = argv[i + 1]
    wanted = wanted or os.environ.get("STORYTELLER_LANG", "") or _system_lang()
    base = wanted.lower().replace("-", "_").split("_")[0].split(".")[0]
    return base if base in available() else "en"


def load(lang):
    """{Spanish original: translation} for lang, checked against SOURCE (all texts, same placeholders)."""
    table = {}
    for line in (HERE / f"{lang}.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            n, text = line.split("|", 1)
            src = SOURCE[int(n)]
            # Keep the original's leading/trailing spaces (" · ⏸ a medias", "Error…: " + e):
            # editors and tools tend to trim them from the ends of lines.
            lead = src[:len(src) - len(src.lstrip())]
            trail = src[len(src.rstrip()):]
            table[int(n)] = lead + text.replace("\\n", "\n").strip() + trail
    missing = sorted(set(range(len(SOURCE))) - set(table))
    if missing:
        raise ValueError(f"i18n/{lang}.txt: missing {missing[:10]}")
    for i, src in enumerate(SOURCE):
        if sorted(re.findall(r"\{\d\}", src)) != sorted(re.findall(r"\{\d\}", table[i])):
            raise ValueError(f"i18n/{lang}.txt: placeholders of {i} do not match")
    return {SOURCE[i]: table[i] for i in table}


LANG = pick_lang()
TABLE = {} if LANG == "es" else load(LANG)


def _(text):
    """The Spanish original in the chosen language (itself in Spanish)."""
    return TABLE.get(text, text)
