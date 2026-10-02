# Implementation Plan: Ultra-Fast Lossless Video Cutting & Bug Fixes for Large Files

The user wants to test `clipforge.py`, fix all errors, and improve the app so it removes sections from videos as fast as possible without re-rendering the whole video—especially since the video files might be super large.

## Problem Analysis & Diagnosed Bugs

1. **Fatal Startup Crash**:
   - The button `self.open_folder_btn` references `command=self.open_last_output`, but `open_last_output` is **not defined** on `ClipForge`. This causes `AttributeError: '_tkinter.tkapp' object has no attribute 'open_last_output'` immediately upon launching the app.

2. **Windows Charmap / Unicode Crash**:
   - Logging strings containing Unicode characters (`→`, `✦`, `•`) cause `UnicodeEncodeError: 'charmap' codec can't encode character` when running on Windows standard consoles or non-UTF8 environments.

3. **Severe Disk I/O Bottleneck & OS Drive Overflow on Large Videos**:
   - `_cut_worker` creates temporary directories in `tempfile.mkdtemp()`, which defaults to the `C:\` OS drive (`%TEMP%`). For a 50GB–200GB video stored on an external or secondary drive (D:, E:), writing all kept segments to `C:\` can fill up the OS drive and crash the system.
   - Furthermore, it forces double writing: writes 50GB to `C:\`, then reads 50GB from `C:\` and writes 50GB to the target drive.
   - Even when there is only **1 kept segment** (e.g. trimming off the intro or outro), it currently writes to a temp file and copies it, doing redundant work.

4. **Timestamp Corruption & Non-monotonic DTS in Concat**:
   - When stream-copying multiple segments with `-c copy` using `.mp4` intermediate files, FFmpeg emits:
     `Application provided invalid, non monotonically increasing dts to muxer in stream 0: 305 >= 305`
     due to MP4 edit lists and B-frame reordering.
   - Using Matroska (`.mkv`) intermediate segments preserves raw packet timestamps cleanly for all codecs (H.264, HEVC/H.265, AV1, VP9, ProRes, etc.), and adding `-fflags +genpts` and `-movflags +faststart` during final muxing completely eliminates timestamp glitches.

5. **Lack of Instant Process Termination**:
   - `cancel_operation` only sets a threading flag and does not hold a reference to `self.current_process`. If FFmpeg is doing heavy I/O, it cannot be interrupted immediately.

6. **Overlapping Ranges Error**:
   - If a user enters or selects overlapping or contiguous ranges (e.g., 0:10-0:20 and 0:15-0:30), the app currently crashes with an unhelpful validation error instead of automatically merging them.

7. **FFmpeg Odd Dimension Padding Crash in Join Tab**:
   - In normalized join mode, `pad` with `(ow-iw)/2` can yield odd numbers, which causes FFmpeg `yuv420p` pad filter to error out.

8. **MM:SS Timecode Restriction**:
   - `parse_timecode` rejects minutes >= 60 for `MM:SS` (e.g. `90:00`), which is unnatural for long videos.

---

## Proposed Changes

### [clipforge.py](file:///e:/HOME/CODES/2026/ClipForge_bundle/clipforge.py)

#### 1. Core Fixes & Stability
- Define `open_last_output()` to safely open the output file's containing folder or the input file's folder.
- Ensure all subprocess calls and logging handle UTF-8 cleanly with error-tolerant decoding/encoding.
- Allow `minutes >= 60` for `MM:SS` in `parse_timecode`.
- Auto-merge overlapping and contiguous cut ranges in `_get_cut_ranges()`.
- Fix the `pad` filter in normalized join to use `trunc((ow-iw)/2/2)*2` to prevent odd dimension crashes.
- Fix audio export default extension when no output is typed.

#### 2. Ultra-Fast Stream Copy for Super Large Videos
- **Direct Output for Single Segment**: When removing sections leaves a single kept segment (e.g., cutting off an intro or outro), write directly to `output` with stream copy (`-c copy`). This cuts execution time by 50%, eliminates any secondary concat step, and requires **0 bytes** of temporary disk space.
- **Temp Storage on Target Volume**: When multiple segments exist, create the temporary folder directly in `Path(output).parent`. This prevents filling the `C:` drive and maximizes intra-drive NVMe/SSD transfer rates.
- **Lossless Intermediate Container**: Use `.mkv` for intermediate segments, eliminating non-monotonic DTS and audio desync errors across any source video codec.
- **Clean Concat Stream Copy**: In the concat pass, use `-fflags +genpts -map 0 -ignore_unknown -c copy -movflags +faststart` for flawless playback and instant scrubbing.
- **Instant Cancellation & Cleanup**: Store `self.current_process` so clicking "Cancel" terminates FFmpeg immediately. Ensure incomplete/cancelled output files and temp folders are deleted automatically.

#### 3. Enhancements for Precision & Usability
- **Snap to Nearest Keyframe**: Add a helper (`MediaTools.find_nearest_keyframe`) that probes keyframe timestamps in a small local interval (`-read_intervals`) in milliseconds without scanning the entire large file. Provide a "Snap A/B to Keyframe" button for clean, cut-at-keyframe boundaries.
- **Visual Thumbnail Previews**: Add quick in-memory thumbnail previews (using `image2pipe` to RAM, no disk clutter) for Dial A and Dial B so users can visually verify what they are cutting out.
- **Timeline Interaction Safeguards**: Prevent dragging when no video is loaded.

---

## Verification Plan

### Automated Tests
1. Test launching the application to verify startup without `AttributeError`.
2. Test cutting with 1 kept segment (e.g. removing the first 10s of `test_input.mp4`) and verify:
   - File written directly to output with 0 temp files.
   - Decodes with 0 errors via `ffmpeg -v error -i ... -f null -`.
3. Test cutting with multiple kept segments (e.g. removing 5s-10s and 20s-25s) and verify:
   - Intermediate files placed in output folder, cleaned up on completion.
   - Clean timestamps and zero non-monotonic DTS errors.
4. Test overlapping ranges auto-merge (e.g. 5-10s and 8-15s).
5. Test keyframe snapping functionality.
6. Test instant cancellation and partial file cleanup.
7. Test audio extraction and video join flows.

### Manual Verification
- Launch the GUI and verify responsiveness, timeline dragging, thumbnail preview, A/B snap, and log area.
