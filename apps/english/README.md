# english — vocabulary trainer

Pulls a small batch of words (10 by default) from a Mac companion app over
the LAN. Each word shows headword, IPA, part of speech, English definition,
example sentence, and pinyin gloss. Scrolls vertically when the definition
overflows the screen. SPACE plays a pre-recorded pronunciation (synthesized
on the Mac with `say`).

## Controls

| Key | Action |
| --- | --- |
| `←` `→` | previous / next word |
| `↑` `↓` | scroll the definition body |
| `SPACE` | play pronunciation |
| `r` | force re-sync from Mac (rotate to next batch) |
| `ESC` | exit (uploads session reviews) |

## Device configuration

Per-device config lives at **`/flash/english.json`** (not in source, gitignored).
Create it on the device once the Mac side is running:

```json
{
  "host": "192.168.1.42",
  "port": 38001,
  "token": "<shared secret matching Mac side>",
  "device_id": "cardputer-1"
}
```

If this file is missing the app still runs but shows a tiny mock word list
with `no config` overlay; no audio playback is possible.

Upload from your Mac:

```sh
# write the config file
mpremote connect <port> resume cp english.json :/flash/english.json
# (optional) deploy a fallback ES8311 driver in case the firmware's
# M5.Speaker.playWavFile binding turns out to be missing
mpremote connect <port> resume cp mp-scripts/es8311.py :/flash/libs/es8311.py
```

## Local cache (`/flash/english/`)

The app writes:

- `words.json` — last synced batch (words + metadata)
- `state.json` — `{"prev_batch_id": N}` so the next checkin knows
  what to rotate away from
- `audio/<id>.wav` — pronunciation per word, downloaded from Mac
- `audio/<id>.sha` — sidecar hash so unchanged audio isn't re-downloaded

Stale audio for words not in the current batch is pruned on sync.

## Sync flow

1. App starts → checks WiFi (assumed brought up by launcher).
2. POST `/sync/checkin?t=<token>`, body includes the last batch_id and an
   (initially empty) reviews list.
3. Mac returns the next 10-word batch. App downloads any audio whose
   `audio_sha` doesn't match the local sidecar.
4. User browses. Every word switch logs `duration_ms` and
   `played_audio` to an in-memory review list.
5. On `r` or `ESC`, app POSTs the review list back to the Mac. Mac
   updates its SQLite reviews table for stats.

If Mac is unreachable, the app falls back to the cached `words.json` and
shows `offline` in the meta row. No audio playback for words not yet
cached.

## Why pinyin (no Chinese)

The MicroPython M5GFX binding on this firmware only exposes the bundled
DejaVu Latin fonts. Native Chinese characters cannot be rendered. The Mac
side stores pinyin (with tone marks, in Latin Extended) and that's what
gets shipped to the device.

If your DejaVu binding doesn't render some tone-mark glyphs cleanly, the
Mac side can strip tones at TTS time — see `english-mac/README.md`.
