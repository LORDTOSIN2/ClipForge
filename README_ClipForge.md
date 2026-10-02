# ClipForge 1.1

A lightweight CustomTkinter desktop frontend for FFmpeg focused on fast, low-waste video editing.

## Install

Make sure `ffmpeg` and `ffprobe` are available in your PATH.

Then install the only Python dependency:

```bash
python -m pip install customtkinter
```

Run:

```bash
python clipforge.py
```

## Features

### Cut & Remove
- Add multiple sections such as `00:10 -> 00:15`.
- Default: **Fast / Stream Copy (Keyframe)**.
- Uses the requested **split -> discard -> concatenate** workflow with FFmpeg `-c copy`.
- Kept compressed video/audio is copied rather than re-rendered.
- Alternate **Frame-Accurate / Re-encode** mode renders the kept material when exact boundaries matter.
- New draggable **A/B range selector**: drag the A and B dials to select a section, then add it to the removal list.
- Each list row can pull the current A/B selection into its fields.
- Automatic page scrolling keeps controls reachable on smaller screens.
- Range validation catches empty, reversed, out-of-bounds and overlapping ranges.

### Join Videos
- Add multiple clips and reorder them with up/down controls.
- Fast stream-copy concat for compatible streams.
- Re-encode/normalize option for mixed sources.
- Scrollable clip list.

### Extract Audio
- Extract the first audio stream.
- **MP3** with V0/V2/V4 VBR or 320 kbps CBR.
- **M4A (AAC)** at 256 kbps.
- **WAV** as uncompressed PCM.

### General
- `ffprobe` media information.
- Progress bar and live FFmpeg log.
- Copy/clear log controls.
- Open output folder button.
- Overwrite confirmation before replacing an existing output.
- Cancel button for running FFmpeg jobs.
- FFmpeg runs on background threads so the interface stays responsive.
- Modern dark CustomTkinter UI.

## Important fast-cut behavior

Stream-copy cutting is intentionally fast, but compressed video usually starts from a keyframe. Therefore fast mode is not frame-perfect; a requested boundary can move depending on the source video's keyframe/GOP layout.

Use **Frame-Accurate / Re-encode** when the exact frame matters.

Fast joining also expects compatible streams. When inputs differ significantly, use **Re-encode / Normalize**.

## 1.1 changes

- Fixed the cut page layout so the lower controls are no longer hidden behind the progress/log area.
- Added vertical scrolling to the Cut, Join and Audio pages.
- Added draggable A/B range dials.
- Added one-click A/B-to-list range creation.
- Added Clear All ranges.
- Added MP3/M4A/WAV audio extraction.
- Added overwrite confirmation, output-folder opening, and log controls.
