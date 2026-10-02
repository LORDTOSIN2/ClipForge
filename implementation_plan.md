# Implementation Plan: Ultra-Fast Lossless Video Cutting & Bug Fixes

**Status: IMPLEMENTED (v1.2)** — 2026-10-02

All items below have been applied in the fixed `clipforge.py` (version 1.2).

> **Note:** A full push of the ~90 KB updated `clipforge.py` hit a tool size limit in the automation session. The repo currently has a temporary placeholder for `clipforge.py`. Download the fixed file from the conversation artifacts (or re-request a push) and replace `clipforge.py` on `main`.

---

## Completed changes

### Core / stability
- [x] `open_last_output()` defined and wired
- [x] UTF-8-safe subprocess / logging (`errors="replace"`)
- [x] `parse_timecode` allows minutes ≥ 60 for `MM:SS`
- [x] Auto-merge overlapping / contiguous ranges in `_get_cut_ranges()`
- [x] Pad filter uses even offsets: `trunc((ow-iw)/2/2)*2` (yuv420p-safe)
- [x] Audio export default extension follows selected format (MP3 / M4A / WAV)

### Ultra-fast stream copy (large files)
- [x] **Single kept segment** → direct `-c copy` to final output (0 temp bytes)
- [x] **Multiple segments** → temp dir on `Path(output).parent` (not system TEMP / C:)
- [x] Intermediate segments use **`.mkv`** (avoids non-monotonic DTS)
- [x] Concat uses `-fflags +genpts -ignore_unknown -c copy -movflags +faststart`
- [x] Instant cancel: `self.current_process` + `proc.terminate()` on Cancel
- [x] Incomplete output deleted on cancel/error; temp dirs always cleaned

### UX (already present, retained)
- [x] Keyframe snap (`MediaTools.find_nearest_keyframe`)
- [x] In-memory A/B frame thumbnails
- [x] Timeline drag blocked when no video loaded

### Version
- Bumped `APP_VERSION` to **1.2**

---

## How to finish the push

1. Download the fixed `clipforge.py` from the chat artifacts.
2. Replace the file on `main` (web UI upload or local git push).
3. Optionally delete this plan file once verified.
