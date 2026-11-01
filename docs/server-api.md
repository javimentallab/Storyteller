# Storyteller voice server

`st_server.py` narrates with your own XTTS and your own voices, through the same API as the public
text-to-speech services. The desktop app (*Models → A service by URL*), the mobile app or any other
client of that API uses it like any other service: put its URL and key there. Neither needs the other.

    python st_server.py              # shows the URL and the access key
    python st_server.py --lang en    # for texts in another language; the API doesn't carry one

It loads the model on the first request and frees the graphics card after 10 minutes without use.

Several requests at once open more XTTS, each in its own process, so the client decides how many by
how many it sends. There are at most 3 (`--workers N`), each takes about 3 GB, and another one only opens
if 3.5 GB are free: spilling over into system memory is slower than having one less. On a card that
can't report its memory it stays at one.

There is no TLS: it travels in the clear on your home network. To reach it from the internet, put it
behind a proxy with HTTPS.

Every route needs `Authorization: Bearer <key>`. Errors are `{"error": {"message": "…"}}`.

| Route | Notes |
|---|---|
| `GET /v1/models` | `{"data": [{"id": "xtts-v2"}]}` |
| `GET /v1/audio/voices` | `{"voices": [...]}`: the WAV files of `engine/voices` |
| `POST /v1/audio/speech` | `{"input", "voice", "model", "response_format"}` → the audio |

`input` is at most 4096 characters. `response_format` is `mp3` (default), `wav`, `flac`, `opus`, `aac`
or `pcm` (24 kHz, 16 bits). Without ffmpeg on the server it answers WAV, with `Content-Type` saying so.
A 503 with `Retry-After` means the graphics card has no free memory right now.
