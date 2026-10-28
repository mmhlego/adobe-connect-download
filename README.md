# acdl — Adobe Connect recording downloader

Download a **classic Adobe Connect** recording and turn its fragments into one
timeline-accurate, playable MP4 (H.264 + AAC) with a single, loudness-normalized
audio track.

Adobe Connect does not store a recording as one file. It stores several FLV
fragments — screen share, audio, cursor data, chat — and a `mainstream.xml`
timeline that says when each fragment belongs. Most tools download the
fragments separately or concatenate them, which desyncs audio and drops gaps.
`acdl` reads the timeline and rebuilds the recording on one clock.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`

```sh
brew install ffmpeg
```

## Install

```sh
python3 -m pip install -e .
```

Or run it without installing:

```sh
python3 -m acdl --help
```

## Quick start

Open the recording in your browser, copy the full address (it must contain
`session=...`, usually ending in `&proto=true`), then:

```sh
acdl "https://vc.example.ac.ir/pnthnp4e2bzb/?session=breez...&proto=true"
```

Output:

```
downloads/رياضي عمومي 2 (1111100)-31607_0/
  recording.mp4
  metadata.json
```

Process every link in `links.txt`:

```sh
acdl batch links.txt
```

Re-compose an existing ZIP (no network needed):

```sh
acdl compose 17155143.zip
```

## Options

| Option | Description |
|---|---|
| `-o, --output-dir DIR` | Output root (default `downloads`) |
| `--crf N` | x264 quality, lower = better/larger (default `23`) |
| `--fps N` | Output frame rate (default `10`) |
| `--from TIME` | Start offset, e.g. `12:30` |
| `--to TIME` | End offset, e.g. `45:00` |
| `--audio-only` | Write `recording.m4a` instead of video |
| `--lufs N` | Loudness target (default `-16`) |
| `--no-normalize` | Keep the original audio level |
| `--chat` | Also write `chat.txt` from the recording's chat log |
| `--keep-raw` | Keep the downloaded ZIP and extracted FLVs |
| `--name NAME` | Override the output folder name |
| `--force` | Re-download and re-encode even if output exists |
| `--insecure` | Skip TLS verification (automatic fallback still applies) |

## How it works

1. **Resolve** — parse the recording URL, fetch the `?proto=true` launcher page,
   and read the recording id (`sco-id`) and title.
2. **Download** — `GET <room>/output/<sco-id>.zip?download=zip&session=...`.
   The server rebuilds this ZIP on demand, so its bytes can differ between two
   requests; `acdl` re-fetches a 64 KiB overlap, compares it with the partial
   file, and restarts from zero if the archive changed. Finished archives are
   CRC-checked before use.
3. **Timeline** — parse `streamAdded` / `streamRemoved` events from
   `mainstream.xml`. Each event carries an absolute `startTime` in milliseconds.
4. **Compose** — build one ffmpeg filter graph:
   - a full-length canvas,
   - every screen-share fragment overlaid at its offset (gaps stay as background),
   - every audio fragment delayed to its offset and mixed,
   - encode VP6 → H.264 and Nellymoser → AAC.
5. **Normalize audio** — measure the mixed audio with EBU R128 (`loudnorm`),
   then apply the result with a true-peak limiter (see below).
6. **Verify** — probe the result and warn if its duration disagrees with the
   timeline.

Only segments whose companion XML says `video data` / `audio data` are used.
Data-only streams (whiteboard strokes, cursor, chat) are listed in
`metadata.json` and ignored.

## Audio levels

Adobe Connect often records speech around -25 to -30 LUFS — audible, but much
quieter than a normal video. `acdl` runs a **two-pass EBU R128 normalization**:

1. measure the composed audio (`loudnorm` analysis only, no video),
2. apply `loudnorm` with the measured values, targeting **-16 LUFS** with a
   **-1.5 dBTP** ceiling, then resample to 44.1 kHz.

Because the source peaks are often already near full scale, `loudnorm` switches
from a simple gain to **dynamic** mode with true-peak limiting. The output is
roughly 12 dB louder without clipping. On the original course recordings this
moved -27.8 LUFS to about -16.0 LUFS.

The true peak is limited before the final 44.1 kHz resample, so the delivered
file usually measures around -1.1 dBTP even though the filter target is
-1.5 dBTP — comfortably below clipping.

Use `--lufs -18` for a gentler level, or `--no-normalize` to keep the source
untouched. The applied values are recorded in `metadata.json` under `audio`.

## Limitations

- **Classic Flash recordings only.** HTML5 recordings
  (`hasHTMLContent=true`) use a different storage layout and are rejected with
  a clear message.
- **Whiteboard/PDF-only lessons** are stored as vector data, not video. There is
  no way to render them without Flash; use `--audio-only` for those.
- **No speech transcript.** Connect's `transcriptstream.xml` is an event index
  (chats, layout changes), not captions.
- **Webcam** video is not composited. The recordings this tool was built
  against publish audio-only `cameraVoip` streams.
- **Sessions expire.** If the token is stale you get a login page; reopen the
  recording and copy a fresh link.

## Troubleshooting

**"session was rejected"** — the `session=` token expired or the recording now
requires a login. Open it in a private browser window; if it plays, copy the
address from that tab.

**TLS certificate warnings** — many campus servers use an incomplete chain.
`acdl` retries without verification automatically (and prints a warning). Pass
`--insecure` to skip the first attempt.

**"no screen-share video"** — the lesson was whiteboard/PDF-only. Re-run with
`--audio-only`.

## Development

```sh
python3 -m pip install -e .
python3 -m acdl --help
```

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 mmhejazi.
