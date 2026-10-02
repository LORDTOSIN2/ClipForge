# ClipForge 1.4

A lightweight CustomTkinter desktop frontend for FFmpeg focused on **fast, low-waste** video editing.

## Install

Make sure `ffmpeg` and `ffprobe` are available in your PATH.

Then install the Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Run:

```bash
python clipforge.py
```

## Features

### Cut & Remove
- Add multiple sections such as `00:10 -> 00:15`.
- Default: **Fast / Stream Copy (Keyframe)**.
- Uses **split → discard → concatenate** with FFmpeg `-c copy` so kept video is **not** re-encoded.
- **Single kept segment** → direct stream-copy to the final file (zero temp files).
- **Multiple segments** → intermediates written as `.mkv` on the **output volume** (avoids filling system TEMP / C:).
- Concat uses `-fflags +genpts -ignore_unknown -movflags +faststart` for reliable timestamps.
- Alternate **Frame-Accurate / Re-encode** mode when exact boundaries matter.
- Draggable **A/B range selector** with live frame previews at the boundaries.
- Auto-merge of overlapping / contiguous ranges.
- Instant cancel via process terminate.

### Join Videos
- Add multiple clips and reorder them with up/down controls.
- Fast stream-copy concat for compatible streams.
- Re-encode / normalize option for mixed sources.

### Extract Audio
- Extract the first audio stream.
- **MP3** (V0 / V2 / V4 VBR or 320 kbps CBR).
- **M4A (AAC)** at 256 kbps.
- **WAV** uncompressed PCM.
- Default output extension follows the selected format.

### Extract Frames *(new in v1.3)*
- Four extraction modes:
  - **Fixed FPS** — one frame every N frames-per-second (e.g. `1` = one per second, `12` = 12/s).
  - **All Frames** — dumps every decoded frame (can produce thousands of files).
  - **Keyframes (I-Frames) Only** — ultra-fast; no P/B decode, just seeks to keyframes.
  - **Scene Cut Detection** — one representative frame per scene change, tunable threshold.
- **Time range** — whole video or custom `From / To` timecode.
- **Output format** — JPEG (with quality slider 2–31) or PNG (lossless).
- **Post-processing** — optional grayscale conversion and width-based resize (height auto-scaled).
- **Naming config** — custom filename prefix, start number, and zero-padding width.
- **Live estimate** — shows approximate frame count before you start.
- FFmpeg runs at **below-normal CPU priority** (Windows) so the system stays responsive.
- Instant cancel. Output folder auto-suggested from input filename.

### 3GPP Converter *(new in v1.4)*
- Convert **images and videos** into files classic feature phones can play.
- Videos are rendered to **3GPP-max `.3gp`**: MPEG-4 Part 2 video + mono AAC audio.
- **Queue multiple files at once** — `Add Files` or `Add Folder` (recursive scan), with per-file status and a progress bar.
- Video settings — maximum width (`176`–`640` px), video bitrate (`64k`–`200k`), audio bitrate (`16k`–`64k`), audio sample rate (`8000`–`44100` Hz).
- Images are re-encoded to **baseline JPEG** with an adjustable quality slider and their own maximum edge.
- **No upscaling** — the width cap only shrinks; a small source keeps its own size.
- Optional **90° rotation** for portrait clips, and optional overwrite (off = numbered duplicates instead).
- Sources with **no audio track** are exported without a silent track.
- Files with no video stream or unsupported types are skipped and reported, not silently dropped.
- Partial `.3gp` files are deleted if a render fails or is cancelled.
- Runs at **below-normal CPU priority** (Windows) and supports instant cancel.

### General
- `ffprobe` media information.
- Progress bar and live FFmpeg log.
- Copy / clear log controls.
- Open output folder button.
- Overwrite confirmation.
- Cancel button for running jobs.
- FFmpeg runs on background threads (UI stays responsive).
- Modern dark CustomTkinter UI.
- Even-dimension padding fix for yuv420p (`trunc((ow-iw)/2/2)*2`).

## Important fast-cut behavior

Stream-copy cutting is intentionally fast, but compressed video usually starts from a keyframe. Therefore fast mode is **not** frame-perfect; a requested boundary can snap to the nearest keyframe.

Use **Frame-Accurate / Re-encode** when the exact frame matters.

Fast joining also expects compatible streams. When inputs differ significantly, use **Re-encode / Normalize**.

## What's new in 1.4

- New **3GPP Converter** tab: batch-convert images and videos to 3GPP-max `.3gp` plus baseline JPEG.
- 3GPP-max render profile (MPEG-4 Part 2 + mono AAC), width cap that never upscales, optional portrait rotation.
- Multi-file queue with per-file status, folder scanning, overall progress bar, and clear-completed.
- Optional overwrite-off mode writes numbered duplicates and never touches existing output.
- Cancelled or failed video renders clean up their partial `.3gp` output.
- `Open Folder` now reveals directory targets directly (used by the multi-file 3GPP output).

## What's new in 1.2

- Ultra-fast single-segment direct `-c copy` path (no temp files).
- Multi-segment temp dir placed next to the output file (not system TEMP).
- `.mkv` intermediate segments + improved concat flags (`+genpts`, `+faststart`).
- Reliable instant cancel (`process.terminate()`).
- Even pad offsets for yuv420p safety.
- Audio export extension follows chosen format.
- Auto-merge overlapping / contiguous cut ranges.
- Keyframe snap + in-memory A/B thumbnails retained.

## Requirements

- Python 3.9+
- FFmpeg + FFprobe in PATH
- `customtkinter` ≥ 5.2
- `pillow` ≥ 9.0
