# Storyteller voice server

`st_server.py` narrates with your own XTTS and your own voices, through the same API as the public
text-to-speech services. The desktop app (*Models → A service by URL*), the mobile app or any other
client of that API uses it like any other service: put its URL and key there. Neither needs the other.

    python st_server.py              # shows the URL and the access key
    python st_server.py --lang en    # the language when a request doesn't say which one

It loads the model on the first request and frees the graphics card after 10 minutes without use.

Several requests at once open more XTTS, each in its own process, so the client decides how many by
how many it sends. There are at most 3 (`--workers N`), each takes about 3 GB, and another one only opens
if 3.5 GB are free: spilling over into system memory is slower than having one less. On a card that
can't report its memory it stays at one. In the apps, that number is *Requests at a time*.

There is no TLS: it travels in the clear on your home network. To reach it from the internet, put it
behind a proxy with HTTPS.

Every route needs `Authorization: Bearer <key>`. Errors are `{"error": {"message": "…"}}`.

| Route | Notes |
|---|---|
| `GET /v1/models` | `{"data": [{"id": "xtts-v2"}]}` |
| `GET /v1/audio/voices` | `{"voices": [...]}`: the WAV files of `engine/voices` |
| `POST /v1/audio/speech` | `{"input", "voice", "model", "response_format", "language"}` → the audio |
| `PUT /v1/audio/voices/<name>` | The body is an audio file, in any format ffmpeg reads: it becomes a voice (`201`) |
| `GET /v1/storyteller/books` | `{"books": [{"id", "title", "manifest", "state", "files"}]}`: the finished chapters of each book |
| `GET /v1/storyteller/books/<id>/<file>` | One of those files |

`input` is at most 4096 characters. `response_format` is `mp3` (default), `wav`, `flac`, `opus`, `aac`
or `pcm` (24 kHz, 16 bits). Without ffmpeg on the server it answers WAV, with `Content-Type` saying so.
A 503 with `Retry-After` means the graphics card has no free memory right now.

`language` is an extra of this server, one of `en es fr de it pt pl tr ru nl cs ar zh-cn ja hu ko hi`. The
standard API doesn't have it, so the apps send it and stop sending it if the service refuses it.

An uploaded voice is kept at 24 kHz, mono, peaking at −1 dBFS, and otherwise untouched: at least 3 s, at most
the first 30 s, and at most 32 MB. A name that already exists answers `409`.
