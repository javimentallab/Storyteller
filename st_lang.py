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
    'Castellano peninsular, alta calidad.',  # 1
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
    'Narrador — grave (F5 Español)',  # 21
    'calculando tiempo restante…',  # 22
    'menos de 1 min',  # 23
    '~{0} min',  # 24
    '~{0} h {1} min',  # 25
    'mañana {0}',  # 26
    'en {0} días, {1}',  # 27
    'quedan {0} (hacia las {1})',  # 28
    'quedan {0} ({1})',  # 29
    'Sección {0}',  # 30
    'Enviando el EPUB al servidor…',  # 31
    'No pude enviar el EPUB al servidor:\n',  # 32
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
    'Todavía no hay libros.\nPulsa «Abrir EPUB…» y Storyteller lo narrará.',  # 79
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
    'Tras narrar cada frase, la transcribe con Whisper y, si no coincide con el texto, la regenera. Más lento pero caza alucinaciones. Usa el Whisper del sitio donde narras: en LOCAL el modelo descargado en «Modelos»; en el servidor, el del servidor. (En local hace falta un modelo Whisper descargado.)',  # 92
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
    'Falta instalar el motor F5/Whisper en engine/.venv: los comandos están en el README. Mientras tanto puedes generar en un servidor propio.',  # 109
    'Se genera en este equipo con la GPU. Elige modelo y voz; descarga el modelo si hace falta.',  # 110
    'Se genera por SSH en el servidor que indiques. Con XTTS usa el motor y la voz de este equipo, y el servidor necesita tener XTTS instalado. Con F5, el motor y la voz son los del servidor.',  # 111
    '✓ Instalado',  # 112
    'Sin descargar · {0}',  # 113
    '{0}  · Idioma: {1}  ({2})',  # 114
    'Usar',  # 115
    'Borrar',  # 116
    'Descargar  ({0})',  # 117
    'Falta instalar el motor XTTS en engine/.venv-xtts: mira el README',  # 118
    'Falta instalar el motor F5/Whisper en engine/.venv: mira el README',  # 119
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
    'Falta instalar el motor: los comandos están en el README. Mientras tanto puedes cambiar a «Servidor propio» en Modelos.',  # 212
    'Antes descarga el modelo «{0}» en Modelos.',  # 213
    'Reanudando…',  # 214
    'Deteniendo y guardando lo generado…',  # 215
    'Guardando la narración en curso…',  # 216
    'Guardando la transcripción en curso…',  # 217
    'activo',  # 218
    'Pronunciación',  # 219
    '▶ Escuchar',  # 220
    'Escuchar lo que ya está narrado mientras sigue narrando',  # 221
    'Todavía no hay nada narrado. Espera unos segundos a que se guarden las primeras frases.',  # 222
    ' · ● narrándose ahora',  # 223
    '● En directo: lo nuevo se va añadiendo solo',  # 224
    'Esperando a que se narre más…',  # 225
    'Rápido. Cabe en tarjetas gráficas de 6 GB.',  # 226
    'Traduce mejor. Pide una tarjeta gráfica de 10-12 GB.',  # 227
    'Neerlandés',  # 228
    'Polaco',  # 229
    'Coreano',  # 230
    'Japonés',  # 231
    'Ruso',  # 232
    'Ucraniano',  # 233
    'Chino',  # 234
    'Hindi',  # 235
    'Tailandés',  # 236
    'Cancelado.',  # 237
    'Preparando el motor de traducción (una sola vez)…',  # 238
    'Descargando llama.cpp…',  # 239
    'Descargando {0} ({1})…',  # 240
    'Descargando el modelo en el servidor… {0} %',  # 241
    'Traduciendo… {0}/{1}',  # 242
    'quedan {0}',  # 243
    'Leyendo el documento…',  # 244
    'Cargando el modelo (en CPU, irá lento)…',  # 245
    'Guardando el documento traducido…',  # 246
    'Conectando con el servidor…',  # 247
    'Descargando el modelo en el servidor…',  # 248
    'Traducir documentos',  # 249
    'Documento',  # 250
    'Elegir archivo…',  # 251
    'EPUB, PDF, Word, PowerPoint, Excel, subtítulos (SRT/VTT), TXT o Markdown',  # 252
    'Idiomas',  # 253
    'Intercambiar idiomas',  # 254
    'A:',  # 255
    'De:',  # 256
    'Modelo',  # 257
    'Traducir en:',  # 258
    'Se conservan párrafos, títulos, tablas y listas; las cursivas o negritas sueltas dentro de una frase se pierden. Un PDF se convierte en un EPUB traducido, sin su maquetación, listo para narrar.',  # 259
    'Traducir',  # 260
    'Abrir carpeta',  # 261
    'Narrar este libro',  # 262
    'Se usa {0} en el servidor {1} (Ollama). Si no lo tiene, lo descarga allí la primera vez.',  # 263
    'No hay servidor configurado. Ponlo en «Modelos» (usuario@host).',  # 264
    'Instalar motor',  # 265
    'Descargado.',  # 266
    'No descargado ({0}).',  # 267
    'Reanudar traducción',  # 268
    'Documentos',  # 269
    'Elige un documento para traducir',  # 270
    'Borrar modelo',  # 271
    '¿Borrar {0} ({1}) de este equipo?',  # 272
    'El idioma de origen y el de destino son el mismo.',  # 273
    'Guardar la traducción como',  # 274
    'Elige otro nombre: no voy a sobrescribir el original.',  # 275
    'Deteniendo y guardando lo traducido…',  # 276
    'Detenido. Lo traducido está guardado en:\n{0}\nLo que falta sigue en el idioma original. Pulsa «Reanudar traducción» para seguir donde lo dejaste.',  # 277
    'Detenido. Todavía no había nada traducido.',  # 278
    'Traducciones a medias',  # 279
    '{0} de {1} trozos ({2} %)',  # 280
    '{0} trozos',  # 281
    'Descartar lo traducido hasta ahora',  # 282
    'No encuentro el archivo original:\n{0}',  # 283
    'Descartar traducción',  # 284
    '¿Descartar lo traducido de «{0}»? El archivo que ya se guardó no se borra.',  # 285
    'Seguir con:',  # 286
    'Antes descarga {0}: pulsa «Descargar» en esta pantalla.',  # 287
    'Seguir desde donde se detuvo con {0}. Si cambias de modelo en «Modelos», lo ya hecho se conserva.',  # 288
    'Reanudando con {0}…',  # 289
    'Reanudando transcripción con {0}…',  # 290
    'Este libro se empezó en este equipo y el servidor no tiene lo narrado: si siguiera allí, se perdería. Cámbialo a este equipo en «Modelos» para reanudarlo.',  # 291
    'Listo:',  # 292
    'Traducir…',  # 293
    'Traducir un libro o documento a otro idioma (EPUB, PDF, Word, subtítulos…)',  # 294
    'Guardando la traducción en curso…',  # 295
    '{0} de {1} GB ({2} %)',  # 296
    '{0} GB',  # 297
    '{0} MB/s',  # 298
    'Descargando…',  # 299
    'Ya hay una descarga en marcha.',  # 300
    'Descarga cancelada. Si vuelves a descargar, empezará de cero.',  # 301
    'La descarga del modelo de transcripción no se completó.',  # 302
    'Un servicio por URL',  # 303
    'URL:',  # 304
    'Clave de acceso:',  # 305
    'Vacía si el servicio no la pide',  # 306
    'Modelo:',  # 307
    'Voz:',  # 308
    'El que use por defecto',  # 309
    'Probar',  # 310
    'Probando…',  # 311
    'Funciona: la frase de prueba ha durado {0} s.',  # 312
    'Cada frase se pide al servicio de esa URL: el tuyo (st_server.py) o uno público con su clave. Aquí no hace falta GPU ni modelo de voz. La transcripción de audiolibros sigue en este equipo.',  # 313
    'Esa dirección no es válida.',  # 314
    'Esa dirección no va cifrada: la clave y el texto del libro viajarían a la vista. Fuera de tu red de casa, usa https.',  # 315
    'Antes indica la URL del servicio en Modelos.',  # 316
    'El servicio rechazó la clave de acceso.',  # 317
    'El servicio no encuentra esa dirección o ese modelo. Revisa la URL y el modelo.',  # 318
    'El servicio rechazó la petición.',  # 319
    'El servicio sigue ocupado tras varios intentos. Prueba más tarde.',  # 320
    'No hay respuesta del servicio. Comprueba la URL y que esté encendido.',  # 321
    'El servicio no devolvió un audio que se pueda leer.',  # 322
    'El servicio no responde; reintentando en {0} s…',  # 323
    'Conectando con {0}…',  # 324
    'Lo ya narrado está guardado; puedes reanudar el libro desde la biblioteca.',  # 325
    'Un tramo se cortó en el servidor; puede faltar memoria en su GPU. Lo narrado está guardado: baja el número de tramos en «Modelos» y pulsa Reanudar.',  # 326
    'El servidor no pudo arrancar tantos tramos; puede faltar memoria en su GPU. Baja el número de tramos en «Modelos» y vuelve a intentarlo.',  # 327
    '1 tramo',  # 328
    '2 tramos',  # 329
    '3 tramos',  # 330
    'Cuántos tramos narran a la vez en la GPU del servidor. Cada uno ocupa unos 2 GB de su memoria: si no caben, la narración avisa y hay que bajar el número. Más tramos no siempre es más rápido: con F5 en una RTX 2060 se tarda lo mismo con 1 que con 2.',  # 331
    'El servidor no tiene instalado XTTS. Elige otro modelo en «Modelos» o instálalo en el servidor.',  # 332
    'Enviando lo narrado al servidor…',  # 333
    'Se perdió la conexión con el servidor. Lo narrado allí se conserva: pulsa Reanudar para seguir.',  # 334
    'Peticiones a la vez:',  # 335
    'Cuántas frases se piden al servicio a la vez. Más es más rápido si el servicio lo permite: el tuyo (st_server.py) abre hasta 3 XTTS si caben en la tarjeta gráfica, y uno público puede limitar las peticiones por minuto.',  # 336
    'Idioma de la narración:',  # 337
    'El idioma en el que se lee el libro. Elige también una voz que suene bien en él. Los modelos F5 solo leen el suyo.',  # 338
    'Para narrar en ese idioma falta instalar esto en el entorno de XTTS:\n\n{0}',  # 339
    'Para otro idioma, dentro de "langs": {"en": {"pron": {"nginx": "engine x"}}}',  # 340
    'Ya hay un libro en marcha. ¿Lo detengo y paso a «{0}»? Lo hecho queda guardado y se puede reanudar.',  # 341
    'QC activado, pero Whisper no está en el entorno de este motor: esta narración no se revisará. Para instalarlo ahí, ejecuta esto en una terminal:\n\n{0}',  # 342
    'XTTS no está instalado. Para instalarlo, ejecuta esto en una terminal (tarda y ocupa varios GB; sin GPU NVIDIA, cambia cu128 por cpu):\n\n{0}\n\nTambién puedes generar en un servidor propio o con un servicio por URL.',  # 343
    'F5 es opcional y no está instalado, así que no se puede narrar con este modelo. Para instalarlo, ejecuta esto en una terminal (tarda y ocupa varios GB; sin GPU NVIDIA, cambia cu128 por cpu):\n\n{0}\n\nTambién puedes elegir otro modelo en «Modelos» o un servicio por URL.',  # 344
    'Whisper es opcional y no está instalado. Hace falta para transcribir audiolibros y para revisar las frases (QC). Para instalarlo, ejecuta esto en una terminal:\n\n{0}',  # 345
    'Cómo instalarlo',  # 346
    'Whisper es opcional y no está instalado. Hace falta para transcribir audiolibros y para revisar las frases (QC).',  # 347
    'Para narrar en este equipo falta un motor de voz: XTTS (engine/.venv-xtts) o F5, que es opcional. Los comandos están en su ficha y en el README. Mientras tanto puedes generar en un servidor propio o con un servicio por URL. Whisper (transcribir audiolibros y QC) es opcional y se instala aparte.',  # 348
    'El modelo activo necesita un motor que no está instalado. Elige otro modelo o instálalo: los comandos están en su ficha y en el README.',  # 349
    'El QC necesita Whisper en el entorno del motor que narra, y no está.',  # 350
    'El QC necesita un modelo Whisper: descárgalo más abajo.',  # 351
    'Para revisar las frases (QC), Whisper tiene que estar en el entorno del motor que narra, y no está. Para instalarlo ahí, ejecuta esto en una terminal:\n\n{0}',  # 352
    'Sin Whisper instalado, el texto de referencia de una voz lo escribes tú.',  # 353
    'Motor sin instalar',  # 354
    'F5 es opcional y no está instalado: no se puede narrar con este modelo hasta instalarlo.',  # 355
    'Para narrar con un servicio solo hace falta poder leer el EPUB. Para instalarlo, ejecuta esto en una terminal:\n\n{0}',  # 356
    'El QC no pudo revisar {0} frases: se quedan como salieron del modelo. El motivo está en el gen.log del libro.',  # 357
    'sin revisar (QC)',  # 358
    'Sí',  # 359
    'No',  # 360
    'Aceptar',  # 361
    'Copiar',  # 362
    'Whisper para revisar:',  # 363
    'Vacío: el Whisper de este equipo, en CPU',  # 364
    'Clave de Whisper:',  # 365
    'Modelo de Whisper:',  # 366
    'Cada frase que llega se transcribe y, si no coincide con el texto, se pide otra vez, hasta 2 veces más: tarda más y, en un servicio de pago, cuesta más.',  # 367
    'Sin URL de Whisper, el QC necesita Whisper en el entorno de un motor de este equipo, y no está.',  # 368
    'Con este entorno, el QC compara lo que oye Whisper con el texto, pero no mide la voz de más al final de cada frase, porque falta numpy.',  # 369
    'QC activado, pero no hay con qué revisar: ni URL de Whisper ni Whisper en este equipo. Esta narración no se revisará.',  # 370
    'Esa dirección no va cifrada: la clave y el audio viajarían a la vista. Fuera de tu red de casa, usa https.',  # 371
    'Página {0} de {1} · {2} % del libro',  # 372
    'Numeración de páginas del libro impreso, tal como la trae el EPUB.',  # 373
    'Página {0} de {1} (aproximada) · {2} % del libro',  # 374
    'Página aproximada: este libro no trae la numeración de la edición impresa, así que se cuenta una página cada {0} letras.',  # 375
    'No pude hablar con GitHub: no hay conexión o no responde. Inténtalo más tarde; no he cambiado nada.',  # 376
    'GitHub respondió con un error ({0}). Inténtalo más tarde; no he cambiado nada.',  # 377
    'El archivo bajado no coincide con su huella SHA-256: lo he borrado y no he cambiado nada.',  # 378
    'No pude instalar la actualización ({0}). He dejado el programa como estaba.',  # 379
    'Descarga cancelada; no he cambiado nada.',  # 380
    'Lo que llegó de GitHub no es una versión válida de Storyteller; no he cambiado nada.',  # 381
    'Buscar actualizaciones',  # 382
    'Mira en GitHub si hay una versión nueva. Solo se comprueba cuando lo pulsas.',  # 383
    'Buscando…',  # 384
    'Actualizar Storyteller',  # 385
    'Ya tienes la última versión, la {0}.',  # 386
    'Hay una versión nueva: la {0}. Tienes la {1}.',  # 387
    'Hay una narración, una transcripción, una traducción o una descarga en marcha. Termínala o detenla y vuelve a buscar actualizaciones: no se actualiza a medias.',  # 388
    'Esta copia es un clon de git con cambios sin confirmar: no la toco para no pisarlos. Puedes actualizarla tú con git.',  # 389
    'El programa no está en la carpeta de sus datos ({0}): no lo cambio desde aquí para no mezclar dos instalaciones.',  # 390
    'No puedo escribir en la carpeta del programa ({0}).',  # 391
    'Guardaré el archivo en {0} y abriré la página de la versión para que la instales tú. ¿Sigo?',  # 392
    'Antes de instalarla se comprueba su huella SHA-256.',  # 393
    'Esta versión no publica su huella SHA-256: se comprueba que llega cifrada desde GitHub y que es la versión anunciada.',  # 394
    'Se baja de la página de versiones de Storyteller en GitHub y solo se cambian los archivos del programa. La configuración, los libros, lo que llevas de cada narración, las voces, los modelos y los motores instalados se quedan como están y no se vuelven a bajar.',  # 395
    '¿Actualizar ahora?',  # 396
    'Bajando la versión {0}…',  # 397
    'Comprobando e instalando…',  # 398
    'Bajado en {0}. Abro la página de la versión.',  # 399
    'Storyteller {0} ya está instalado. La aplicación se reinicia ahora para usarlo.',  # 400
    'Esta versión cambia los requisitos ({0}). Lo que ya tienes instalado se conserva; si un motor deja de arrancar, repite su paso de instalación del README.',  # 401
    'Esta copia es un clon de git con commits propios que no están en la versión publicada: no la toco, porque no podría avanzar sin mezclar. Puedes actualizarla tú con git.',  # 402
    'Esta copia es un clon de git, pero no encuentro git en este equipo: no puedo actualizarla desde aquí.',  # 403
    '¿Abro la página de la versión?',  # 404
    'Esta copia es un clon de git: se baja la etiqueta {0} del repositorio oficial de Storyteller y la copia avanza hasta ella sin mezclar nada. La configuración, los libros, lo que llevas de cada narración, las voces, los modelos y los motores instalados se quedan como están y no se vuelven a bajar.',  # 405
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
