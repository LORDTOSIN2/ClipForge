import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image


# ============================================================
# ClipForge
# Fast, lightweight FFmpeg video utility using CustomTkinter.
# Default cutting method: split -> discard -> concatenate
# with stream copy (-c copy), so kept video is NOT re-encoded.
# ============================================================

APP_NAME = "ClipForge"
APP_VERSION = "1.3"

# Windows: hide console window & run FFmpeg at below-normal CPU priority
if sys.platform == "win32":
    _WIN_STARTUPINFO = subprocess.STARTUPINFO()
    _WIN_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _WIN_CREATE_FLAGS = subprocess.CREATE_NO_WINDOW | 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
else:
    _WIN_STARTUPINFO = None
    _WIN_CREATE_FLAGS = 0

# Modern dark palette. CustomTkinter still handles widget states,
# corner radius, scaling and system appearance for us.
BG = "#0b1020"
PANEL = "#111827"
PANEL_2 = "#172033"
BORDER = "#27344a"
TEXT = "#edf2f7"
MUTED = "#94a3b8"
ACCENT = "#6d5dfc"
ACCENT_HOVER = "#5848e8"
SUCCESS = "#22c55e"
WARNING = "#f59e0b"
DANGER = "#ef4444"

TIME_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)")


def format_seconds(value: float) -> str:
    value = max(0.0, float(value))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = value % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"
    return f"{minutes:02d}:{seconds:06.3f}"


def parse_timecode(value: str) -> float:
    """Accept seconds or HH:MM:SS[.mmm] / MM:SS[.mmm].
    For MM:SS, minutes can be >= 60 (e.g. 90:30 = 5430 seconds)."""
    value = value.strip()
    if not value:
        raise ValueError("Time cannot be empty")

    # Plain seconds, e.g. 90.5
    try:
        if ":" not in value:
            seconds = float(value)
            if seconds < 0:
                raise ValueError
            return seconds
    except ValueError:
        pass

    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS")
    try:
        parts_f = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError("Invalid time") from exc

    if len(parts_f) == 2:
        minutes, seconds = parts_f
        # Allow large minutes (e.g. 90:00 for long videos)
        if minutes < 0 or seconds < 0 or seconds >= 60:
            raise ValueError("Invalid MM:SS value (seconds must be 0-59)")
        return minutes * 60 + seconds

    hours, minutes, seconds = parts_f
    if hours < 0 or minutes < 0 or seconds < 0 or minutes >= 60 or seconds >= 60:
        raise ValueError("Invalid HH:MM:SS value")
    return hours * 3600 + minutes * 60 + seconds


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class FFmpegError(RuntimeError):
    pass


class MediaTools:
    """Small wrapper around ffmpeg/ffprobe in PATH."""

    @staticmethod
    def check_tools():
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        return ffmpeg, ffprobe

    @staticmethod
    def probe(path: str) -> dict:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,channels,sample_rate,bit_rate",
            "-of", "json", path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise FFmpegError(result.stderr.strip() or "ffprobe could not read this file.")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise FFmpegError("ffprobe returned invalid JSON.") from exc
        return data

    @staticmethod
    def duration(path: str) -> float:
        data = MediaTools.probe(path)
        dur = safe_float(data.get("format", {}).get("duration"))
        if dur <= 0:
            for s in data.get("streams", []):
                sd = safe_float(s.get("duration"))
                if sd > 0:
                    return sd
        return dur

    @staticmethod
    def has_audio(probe_data: dict) -> bool:
        return any(s.get("codec_type") == "audio" for s in probe_data.get("streams", []))

    @staticmethod
    def find_nearest_keyframe(path: str, target_time: float, search_window: float = 6.0) -> float:
        """Find the nearest keyframe around target_time in a narrow window using ffprobe."""
        start_w = max(0.0, float(target_time) - search_window)
        end_w = float(target_time) + search_window
        cmd = [
            "ffprobe", "-v", "error",
            "-read_intervals", f"{start_w:.3f}%{end_w:.3f}",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=best_effort_timestamp_time,pkt_pts_time",
            "-of", "csv=p=0",
            path,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
            times = []
            for line in res.stdout.strip().splitlines():
                for part in line.split(","):
                    p = part.strip()
                    if p:
                        try:
                            times.append(float(p))
                        except ValueError:
                            pass
            if times:
                return min(times, key=lambda t: abs(t - target_time))
        except Exception:
            pass
        return max(0.0, float(target_time))

    @staticmethod
    def get_thumbnail(path: str, time_seconds: float, size=(120, 68)):
        """Extract a frame thumbnail into memory without writing temporary files to disk."""
        if not path or not os.path.isfile(path):
            return None
        w, h = size
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, float(time_seconds)):.6f}",
            "-i", path,
            "-frames:v", "1",
            "-q:v", "5",
            "-s", f"{w}x{h}",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-",
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
            if res.returncode == 0 and res.stdout:
                return Image.open(io.BytesIO(res.stdout))
        except Exception:
            pass
        return None

    @staticmethod
    def run(command, log_callback=None, progress_callback=None, cancel_event=None, proc_callback=None):
        """
        Run FFmpeg without blocking the GUI.
        Progress comes from FFmpeg's machine-readable -progress output.
        """
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if proc_callback:
            try:
                proc_callback(proc)
            except Exception:
                pass

        current_time = 0.0
        last_lines = []
        stderr_lines = []

        try:
            for raw in proc.stderr:
                line = raw.strip()
                if not line:
                    continue
                stderr_lines.append(line)
                if len(stderr_lines) > 200:
                    stderr_lines.pop(0)

                # -progress pipe:2 emits out_time_us=..., out_time_ms=..., progress=...
                if line.startswith("out_time_us="):
                    current_time = safe_float(line.split("=", 1)[1]) / 1_000_000
                    if progress_callback:
                        progress_callback(current_time)
                elif line.startswith("out_time_ms="):
                    # Depending on FFmpeg version, this field may be microseconds despite its name.
                    current_time = safe_float(line.split("=", 1)[1]) / 1_000_000
                    if progress_callback:
                        progress_callback(current_time)
                elif line.startswith("progress="):
                    if log_callback and line != "progress=continue":
                        log_callback(line)
                else:
                    # Keep useful human-readable diagnostics, but don't flood the UI with frame output.
                    if log_callback and (
                        "Error" in line or "warning" in line.lower() or "Invalid" in line or "Stream" in line
                    ):
                        log_callback(line)
                    last_lines.append(line)
                    if len(last_lines) > 40:
                        last_lines.pop(0)

                if cancel_event and cancel_event.is_set():
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break
        finally:
            return_code = proc.wait()
            if proc_callback:
                try:
                    proc_callback(None)
                except Exception:
                    pass

        if cancel_event and cancel_event.is_set():
            raise FFmpegError("Operation cancelled.")
        if return_code != 0:
            message = "\n".join(stderr_lines[-15:]).strip()
            raise FFmpegError(message or f"FFmpeg exited with code {return_code}.")
        return current_time


class TimelineSelector(ctk.CTkFrame):
    """A lightweight draggable A/B range selector built on a Tk canvas with visual frame previews."""

    def __init__(self, master, on_change=None, **kwargs):
        super().__init__(master, fg_color=PANEL_2, corner_radius=12, **kwargs)
        self.on_change = on_change
        self.duration = 0.0
        self.start = 0.0
        self.end = 10.0
        self.dragging = None
        self._width = 760
        self._pad = 22
        self._video_path = ""
        self._preview_timer = None
        self._img_a_ctk = None
        self._img_b_ctk = None

        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            height=86,
            bg=PANEL_2,
            bd=0,
            highlightthickness=0,
            cursor="arrow",
        )
        self.canvas.grid(row=0, column=0, padx=12, pady=(10, 2), sticky="ew")
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", self._mouse_down)
        self.canvas.bind("<B1-Motion>", self._mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self._mouse_up)

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, padx=12, pady=(1, 4), sticky="ew")
        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(controls, text="START (A)", text_color="#64748b",
                     font=ctk.CTkFont(size=9, weight="bold")).grid(row=0, column=0, sticky="w")
        self.start_label = ctk.CTkLabel(controls, text="00:00.000", text_color=TEXT,
                                        font=ctk.CTkFont(size=12, weight="bold"))
        self.start_label.grid(row=1, column=0, sticky="w", padx=(0, 18))

        ctk.CTkLabel(controls, text="END (B)", text_color="#64748b",
                     font=ctk.CTkFont(size=9, weight="bold")).grid(row=0, column=2, sticky="w")
        self.end_label = ctk.CTkLabel(controls, text="00:10.000", text_color=TEXT,
                                      font=ctk.CTkFont(size=12, weight="bold"))
        self.end_label.grid(row=1, column=2, sticky="w", padx=(0, 18))

        self.duration_label = ctk.CTkLabel(controls, text="No video loaded", text_color=MUTED, anchor="e")
        self.duration_label.grid(row=0, column=3, rowspan=2, sticky="e")

        # Frame preview strip for dials A and B
        preview_bar = ctk.CTkFrame(self, fg_color="transparent")
        preview_bar.grid(row=2, column=0, padx=12, pady=(2, 10), sticky="ew")

        box_a = ctk.CTkFrame(preview_bar, fg_color="#0d1424", corner_radius=8, width=122, height=68)
        box_a.pack(side="left", padx=(0, 10))
        box_a.pack_propagate(False)
        self.preview_a_lbl = ctk.CTkLabel(box_a, text="[ A Frame ]", font=ctk.CTkFont(size=10), text_color="#64748b")
        self.preview_a_lbl.place(relx=0.5, rely=0.5, anchor="center")

        box_b = ctk.CTkFrame(preview_bar, fg_color="#0d1424", corner_radius=8, width=122, height=68)
        box_b.pack(side="left")
        box_b.pack_propagate(False)
        self.preview_b_lbl = ctk.CTkLabel(box_b, text="[ B Frame ]", font=ctk.CTkFont(size=10), text_color="#64748b")
        self.preview_b_lbl.place(relx=0.5, rely=0.5, anchor="center")

        self.preview_hint = ctk.CTkLabel(
            preview_bar,
            text="Frame previews at boundaries A and B (loads automatically).",
            text_color="#64748b",
            font=ctk.CTkFont(size=11),
            anchor="w",
        )
        self.preview_hint.pack(side="left", padx=14)

    def update_video_path(self, path):
        self._video_path = path or ""
        self._queue_preview()

    def set_duration(self, duration, start=None, end=None):
        self.duration = max(0.0, float(duration or 0.0))
        if self.duration <= 0:
            self.start, self.end = 0.0, 10.0
        else:
            default_end = min(10.0, self.duration)
            self.start = min(max(0.0, float(start if start is not None else self.start)), self.duration)
            self.end = min(max(0.0, float(end if end is not None else default_end)), self.duration)
            if self.end <= self.start:
                self.start, self.end = 0.0, default_end
        self._sync_labels()
        self._redraw()
        self._queue_preview()

    def set_range(self, start, end, emit=False):
        if self.duration > 0:
            start = min(max(0.0, float(start)), self.duration)
            end = min(max(0.0, float(end)), self.duration)
            if end < start:
                start, end = end, start
        else:
            start, end = max(0.0, float(start)), max(0.0, float(end))
        self.start, self.end = start, end
        self._sync_labels()
        self._redraw()
        self._queue_preview()
        if emit and self.on_change:
            self.on_change(self.start, self.end)

    def get_range(self):
        return self.start, self.end

    def _sync_labels(self):
        self.start_label.configure(text=format_seconds(self.start))
        self.end_label.configure(text=format_seconds(self.end))
        if self.duration > 0:
            self.duration_label.configure(text=f"Duration  {format_seconds(self.duration)}")
        else:
            self.duration_label.configure(text="No video loaded")

    def _queue_preview(self):
        if not self._video_path or self.duration <= 0:
            return
        if self._preview_timer:
            try:
                self.after_cancel(self._preview_timer)
            except Exception:
                pass
        self._preview_timer = self.after(300, self._fetch_previews)

    def _fetch_previews(self):
        path = self._video_path
        if not path or not os.path.isfile(path) or self.duration <= 0:
            return
        cur_start, cur_end = self.start, self.end

        def worker():
            img_a = MediaTools.get_thumbnail(path, cur_start, size=(120, 68))
            img_b = MediaTools.get_thumbnail(path, cur_end, size=(120, 68))
            try:
                self.after(0, lambda: self._apply_previews(img_a, img_b))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _apply_previews(self, img_a, img_b):
        try:
            if img_a:
                self._img_a_ctk = ctk.CTkImage(light_image=img_a, dark_image=img_a, size=(120, 68))
                self.preview_a_lbl.configure(image=self._img_a_ctk, text="")
            if img_b:
                self._img_b_ctk = ctk.CTkImage(light_image=img_b, dark_image=img_b, size=(120, 68))
                self.preview_b_lbl.configure(image=self._img_b_ctk, text="")
        except Exception:
            pass

    def _track(self):
        width = max(self.canvas.winfo_width(), 300)
        self._width = width
        left = self._pad
        right = width - self._pad
        y = 45
        return left, right, y

    def _x_for_time(self, seconds):
        left, right, _ = self._track()
        if self.duration <= 0:
            return left
        return left + (seconds / self.duration) * (right - left)

    def _time_for_x(self, x):
        left, right, _ = self._track()
        x = min(max(left, x), right)
        if self.duration <= 0 or right <= left:
            return 0.0
        return ((x - left) / (right - left)) * self.duration

    def _nearest_handle(self, x):
        xs = [("start", self._x_for_time(self.start)), ("end", self._x_for_time(self.end))]
        name, hx = min(xs, key=lambda item: abs(item[1] - x))
        return name if abs(hx - x) <= 18 else None

    def _mouse_down(self, event):
        if self.duration <= 0:
            return
        self.dragging = self._nearest_handle(event.x)
        if self.dragging is None:
            self.dragging = "start" if abs(self._time_for_x(event.x) - self.start) <= abs(self._time_for_x(event.x) - self.end) else "end"
            self._set_drag(event.x)

    def _mouse_drag(self, event):
        if self.duration <= 0:
            return
        if self.dragging:
            self._set_drag(event.x)

    def _mouse_up(self, _event):
        self.dragging = None
        self._queue_preview()

    def _set_drag(self, x):
        value = self._time_for_x(x)
        if self.dragging == "start":
            self.start = min(value, max(0.0, self.end - 0.001))
        elif self.dragging == "end":
            self.end = max(value, min(self.duration, self.start + 0.001)) if self.duration else max(value, self.start + 0.001)
        self._sync_labels()
        self._redraw()
        if self.on_change:
            self.on_change(self.start, self.end)

    def _redraw(self):
        self.canvas.delete("all")
        left, right, y = self._track()

        self.canvas.create_line(left, y, right, y, fill=BORDER, width=14, capstyle="round")

        if self.duration > 0:
            sx = self._x_for_time(self.start)
            ex = self._x_for_time(self.end)
            self.canvas.create_line(sx, y, ex, y, fill=ACCENT, width=14, capstyle="round")
            self.canvas.create_line(left, y, sx, y, fill="#202a3b", width=14, capstyle="round")
            self.canvas.create_line(ex, y, right, y, fill="#202a3b", width=14, capstyle="round")

            # Time ticks: enough to orient the user without turning the timeline into an editor.
            tick_count = 5
            for i in range(tick_count + 1):
                ratio = i / tick_count
                x = left + ratio * (right - left)
                t = self.duration * ratio
                self.canvas.create_line(x, y + 13, x, y + 18, fill="#526174", width=1)
                self.canvas.create_text(x, y + 29, text=format_seconds(t).split(".")[0],
                                        fill="#7d8da4", font=("Segoe UI", 8), anchor="n")

            for name, x, label in (("start", sx, "A"), ("end", ex, "B")):
                self.canvas.create_oval(x - 10, y - 10, x + 10, y + 10, fill=ACCENT, outline="#ffffff", width=2)
                self.canvas.create_text(x, y, text=label, fill="#ffffff", font=("Segoe UI", 8, "bold"))

            self.canvas.create_text((sx + ex) / 2, 15, text=f"REMOVE  {format_seconds(max(0.0, self.end - self.start))}",
                                    fill="#c7d2fe", font=("Segoe UI", 9, "bold"))
        else:
            self.canvas.create_text((left + right) / 2, y, text="Load a video to use the draggable range selector",
                                    fill="#64748b", font=("Segoe UI", 10))

class CutRangeRow(ctk.CTkFrame):
    def __init__(self, master, index, on_remove, on_change, **kwargs):
        super().__init__(master, fg_color=PANEL_2, corner_radius=10, **kwargs)
        self.index = index
        self.on_remove = on_remove
        self.on_change = on_change

        for col in (2, 4):
            self.grid_columnconfigure(col, weight=1)

        self.num = ctk.CTkLabel(self, text=f"{index:02d}", width=32, text_color=MUTED,
                                font=ctk.CTkFont(size=11, weight="bold"))
        self.num.grid(row=0, column=0, padx=(10, 4), pady=9)

        ctk.CTkLabel(self, text="From", text_color=MUTED).grid(row=0, column=1, padx=(4, 3), sticky="e")
        self.start_var = tk.StringVar(value="00:00")
        self.start = ctk.CTkEntry(self, textvariable=self.start_var, width=112, placeholder_text="00:00")
        self.start.grid(row=0, column=2, padx=4, pady=8, sticky="ew")

        ctk.CTkLabel(self, text="To", text_color=MUTED).grid(row=0, column=3, padx=(8, 3), sticky="e")
        self.end_var = tk.StringVar(value="00:10")
        self.end = ctk.CTkEntry(self, textvariable=self.end_var, width=112, placeholder_text="00:10")
        self.end.grid(row=0, column=4, padx=4, pady=8, sticky="ew")

        self.use_btn = ctk.CTkButton(
            self, text="Use A/B", width=70, height=31,
            fg_color="#263247", hover_color="#35435a", command=self._use_ab,
        )
        self.use_btn.grid(row=0, column=5, padx=(6, 3), pady=8)

        self.remove_btn = ctk.CTkButton(
            self, text="×", width=34, height=32,
            fg_color="#222b3e", hover_color="#3b2532",
            text_color="#ffb4c1", command=self._remove,
        )
        self.remove_btn.grid(row=0, column=6, padx=(3, 10), pady=8)

        self.start.bind("<FocusOut>", lambda _e: self.on_change())
        self.end.bind("<FocusOut>", lambda _e: self.on_change())
        self.start.bind("<Return>", lambda _e: self.on_change())
        self.end.bind("<Return>", lambda _e: self.on_change())

    def _remove(self):
        self.on_remove(self)

    def _use_ab(self):
        owner = self.winfo_toplevel()
        if hasattr(owner, "timeline"):
            start, end = owner.timeline.get_range()
            self.start_var.set(format_seconds(start))
            self.end_var.set(format_seconds(end))
            self.on_change()

    def values(self):
        return self.start_var.get(), self.end_var.get()

    def renumber(self, index):
        self.index = index
        self.num.configure(text=f"{index:02d}")


class JoinFileRow(ctk.CTkFrame):
    def __init__(self, master, index, path, on_up, on_down, on_remove, **kwargs):
        super().__init__(master, fg_color=PANEL_2, corner_radius=10, **kwargs)
        self.path = path
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text=f"{index:02d}", width=34, text_color=MUTED).grid(row=0, column=0, padx=(10, 4), pady=9)
        name = Path(path).name
        ctk.CTkLabel(self, text=name, anchor="w").grid(row=0, column=1, padx=8, sticky="ew")
        ctk.CTkButton(self, text="↑", width=32, command=on_up).grid(row=0, column=2, padx=2)
        ctk.CTkButton(self, text="↓", width=32, command=on_down).grid(row=0, column=3, padx=2)
        ctk.CTkButton(self, text="×", width=32, fg_color="#222b3e", hover_color="#3b2532", command=on_remove).grid(
            row=0, column=4, padx=(2, 10)
        )


class ClipForge(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__()
        self.title(f"{APP_NAME} — FFmpeg Video Toolkit")
        self.geometry("1240x860")
        self.minsize(1000, 700)
        self.configure(fg_color=BG)

        # State
        self.input_path = ""
        self.input_probe = None
        self.input_duration = 0.0
        self.output_path = ""
        self.last_output = ""
        self.cut_rows = []
        self.join_files = []
        self.current_process = None
        self.cancel_event = threading.Event()
        self.busy = False
        self.ui_queue = queue.Queue()
        self.log_lines = []

        self._build_ui()
        self._check_ffmpeg()
        self.after(100, self._drain_ui_queue)

    # ---------------------------- UI ----------------------------

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color="#0d1424")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(8, weight=1)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, padx=20, pady=(24, 20), sticky="ew")
        ctk.CTkLabel(brand, text="✦", text_color=ACCENT, font=ctk.CTkFont(size=28, weight="bold")).pack(side="left")
        ctk.CTkLabel(brand, text=APP_NAME, font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=9)

        self.subtitle = ctk.CTkLabel(
            self.sidebar, text="Fast cuts. No pointless rendering.", text_color=MUTED,
            font=ctk.CTkFont(size=11), wraplength=175, justify="left",
        )
        self.subtitle.grid(row=1, column=0, padx=20, sticky="w")

        self.nav_buttons = {}
        for row, (name, label) in enumerate([
            ("cut", "✂  Cut & Remove"),
            ("join", "⇄  Join Videos"),
            ("audio", "♫  Extract Audio"),
            ("frames", "🎞  Extract Frames"),
        ], start=3):
            btn = ctk.CTkButton(
                self.sidebar, text=label, height=42, anchor="w", corner_radius=9,
                fg_color=ACCENT if name == "cut" else "transparent",
                hover_color=ACCENT_HOVER if name == "cut" else PANEL_2,
                command=lambda n=name: self._show_tab(n),
            )
            btn.grid(row=row, column=0, padx=14, pady=4, sticky="ew")
            self.nav_buttons[name] = btn

        ctk.CTkLabel(self.sidebar, text="", text_color=MUTED).grid(row=8, column=0)

        bottom = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        bottom.grid(row=9, column=0, padx=18, pady=16, sticky="ew")
        self.tool_status = ctk.CTkLabel(bottom, text="●  Checking FFmpeg...", text_color=MUTED, anchor="w")
        self.tool_status.pack(fill="x")
        ctk.CTkLabel(bottom, text=f"v{APP_VERSION}  •  FFmpeg frontend", text_color="#64748b", font=ctk.CTkFont(size=10)).pack(anchor="w", pady=(4, 0))

        # Main area
        self.main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self.main, fg_color="transparent")
        top.grid(row=0, column=0, padx=28, pady=(22, 12), sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self.page_title = ctk.CTkLabel(top, text="Cut & Remove", font=ctk.CTkFont(size=28, weight="bold"))
        self.page_title.grid(row=0, column=0, sticky="w")
        self.page_desc = ctk.CTkLabel(top, text="Remove one or more sections using fast stream copying.", text_color=MUTED)
        self.page_desc.grid(row=1, column=0, sticky="w", pady=(3, 0))

        self.content = ctk.CTkFrame(self.main, fg_color="transparent")
        self.content.grid(row=1, column=0, padx=28, pady=8, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self._build_cut_tab()
        self._build_join_tab()
        self._build_audio_tab()
        self._build_frames_tab()
        self._show_tab("cut")

        # Bottom task bar shared by tools
        self.bottom_bar = ctk.CTkFrame(self.main, fg_color=PANEL, corner_radius=14, height=75)
        self.bottom_bar.grid(row=2, column=0, padx=28, pady=(8, 20), sticky="ew")
        self.bottom_bar.grid_columnconfigure(0, weight=1)
        self.progress_label = ctk.CTkLabel(self.bottom_bar, text="Ready", text_color=MUTED, anchor="w")
        self.progress_label.grid(row=0, column=0, padx=16, pady=(10, 0), sticky="ew")
        self.progress = ctk.CTkProgressBar(self.bottom_bar, height=10, corner_radius=5, progress_color=ACCENT)
        self.progress.grid(row=1, column=0, padx=16, pady=(6, 13), sticky="ew")
        self.progress.set(0)
        self.open_folder_btn = ctk.CTkButton(
            self.bottom_bar, text="Open Folder", width=104, height=36,
            fg_color="#263247", hover_color="#35435a", command=self.open_last_output,
        )
        self.open_folder_btn.grid(row=0, column=1, rowspan=2, padx=(4, 8))
        self.cancel_btn = ctk.CTkButton(
            self.bottom_bar, text="Cancel", width=92, height=36,
            fg_color="#263247", hover_color="#35435a", command=self.cancel_operation, state="disabled",
        )
        self.cancel_btn.grid(row=0, column=2, rowspan=2, padx=(8, 16))

        self.log_frame = ctk.CTkFrame(self.main, fg_color=PANEL, corner_radius=14)
        self.log_frame.grid(row=3, column=0, padx=28, pady=(0, 20), sticky="ew")
        self.log_frame.grid_columnconfigure(0, weight=1)
        log_head = ctk.CTkFrame(self.log_frame, fg_color="transparent")
        log_head.grid(row=0, column=0, padx=10, pady=(8, 0), sticky="ew")
        log_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_head, text="FFMPEG LOG", text_color="#64748b", font=ctk.CTkFont(size=9, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(log_head, text="Copy", width=58, height=26, fg_color="#263247", hover_color="#35435a", command=self.copy_log).grid(row=0, column=1, padx=3)
        ctk.CTkButton(log_head, text="Clear", width=58, height=26, fg_color="#263247", hover_color="#35435a", command=self.clear_log).grid(row=0, column=2)
        self.log_text = ctk.CTkTextbox(self.log_frame, height=82, fg_color="#0d1424", border_width=0, text_color="#a8b5c7")
        self.log_text.grid(row=1, column=0, sticky="ew", padx=10, pady=(6, 10))
        self.log_text.insert("end", "ClipForge ready.\n")
        self.log_text.configure(state="disabled")

    def _build_section_header(self, parent, title, subtitle):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(frame, text=subtitle, text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", pady=(3, 0))
        return frame

    def _build_cut_tab(self):
        # The whole page is scrollable so the action controls never disappear
        # underneath the fixed progress/log area on smaller screens.
        self.cut_tab = ctk.CTkScrollableFrame(
            self.content,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT,
            scrollbar_fg_color="transparent",
        )
        self.cut_tab.grid(row=0, column=0, sticky="nsew")
        self.cut_tab.grid_columnconfigure(0, weight=1)

        input_card = ctk.CTkFrame(self.cut_tab, fg_color=PANEL, corner_radius=14)
        input_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        input_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(input_card, text="SOURCE VIDEO", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(14, 4), sticky="w")
        input_line = ctk.CTkFrame(input_card, fg_color="transparent")
        input_line.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")
        input_line.grid_columnconfigure(0, weight=1)
        self.cut_input_entry = ctk.CTkEntry(input_line, placeholder_text="Select a video file…", height=38)
        self.cut_input_entry.grid(row=0, column=0, sticky="ew")
        self.cut_input_entry.bind("<Return>", lambda _e: self.load_cut_input())
        ctk.CTkButton(input_line, text="Browse", width=105, height=38, command=self.browse_cut_input).grid(row=0, column=1, padx=(8, 0))

        self.media_info_label = ctk.CTkLabel(input_card, text="No media loaded", text_color=MUTED, anchor="w")
        self.media_info_label.grid(row=2, column=0, padx=16, pady=(0, 13), sticky="ew")

        settings_card = ctk.CTkFrame(self.cut_tab, fg_color=PANEL, corner_radius=14)
        settings_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        settings_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(settings_card, text="CUT MODE", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(row=0, column=0, padx=16, pady=(14, 3), sticky="w")
        self.cut_mode = ctk.StringVar(value="Fast / Stream Copy (Keyframe)")
        self.cut_mode_menu = ctk.CTkOptionMenu(
            settings_card, variable=self.cut_mode,
            values=["Fast / Stream Copy (Keyframe)", "Frame-Accurate / Re-encode"],
            width=250, command=self._update_cut_mode_description,
        )
        self.cut_mode_menu.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="w")
        self.cut_mode_help = ctk.CTkLabel(
            settings_card,
            text="Fast mode copies compressed streams. Cut boundaries can land on nearby keyframes.",
            text_color=MUTED, anchor="w", wraplength=650,
        )
        self.cut_mode_help.grid(row=1, column=1, padx=10, sticky="w")

        ctk.CTkLabel(settings_card, text="OUTPUT", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(row=2, column=0, padx=16, pady=(8, 3), sticky="w")
        output_line = ctk.CTkFrame(settings_card, fg_color="transparent")
        output_line.grid(row=3, column=0, columnspan=2, padx=16, pady=(0, 14), sticky="ew")
        output_line.grid_columnconfigure(0, weight=1)
        self.cut_output_entry = ctk.CTkEntry(output_line, placeholder_text="Output path…", height=36)
        self.cut_output_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(output_line, text="Choose", width=90, height=36, command=self.choose_cut_output).grid(row=0, column=1, padx=(8, 0))

        timeline_card = ctk.CTkFrame(self.cut_tab, fg_color=PANEL, corner_radius=14)
        timeline_card.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        header = ctk.CTkFrame(timeline_card, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(13, 4), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="RANGE SELECTOR", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="Drag A and B to choose the section to remove", text_color=MUTED).grid(row=0, column=1, sticky="e")

        self.timeline = TimelineSelector(timeline_card, on_change=self._timeline_changed)
        self.timeline.grid(row=1, column=0, padx=12, pady=(2, 8), sticky="ew")
        timeline_card.grid_columnconfigure(0, weight=1)

        tl_actions = ctk.CTkFrame(timeline_card, fg_color="transparent")
        tl_actions.grid(row=2, column=0, padx=16, pady=(0, 14), sticky="ew")
        ctk.CTkButton(tl_actions, text="Reset to first 10s", width=135, height=34,
                      fg_color="#263247", hover_color="#35435a", command=self.reset_timeline).pack(side="left")
        ctk.CTkButton(tl_actions, text="Snap A/B to Keyframe", width=170, height=34,
                      fg_color="#1e2f45", hover_color="#2a4060", command=self.snap_ab_to_keyframe).pack(side="left", padx=8)
        ctk.CTkButton(tl_actions, text="+ Add A/B to list", width=145, height=36,
                      command=self.add_timeline_range).pack(side="right")

        ranges_card = ctk.CTkFrame(self.cut_tab, fg_color=PANEL, corner_radius=14)
        ranges_card.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ranges_card.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(ranges_card, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(13, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="SECTIONS TO REMOVE", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(header, text="＋ Add Range", width=110, height=32, command=self.add_cut_range).grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(
            ranges_card,
            text="Everything outside these ranges is kept. You can type times or use the A/B selector above.",
            text_color=MUTED,
        ).grid(row=1, column=0, padx=16, pady=(0, 8), sticky="w")

        self.cut_scroll = ctk.CTkScrollableFrame(
            ranges_card,
            fg_color="#0d1424",
            corner_radius=10,
            height=205,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT,
        )
        self.cut_scroll.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="ew")
        self.cut_scroll.grid_columnconfigure(0, weight=1)

        self.cut_empty = ctk.CTkLabel(self.cut_scroll, text="No cut ranges yet. Add one above or use the A/B selector.", text_color="#64748b")
        self.cut_empty.grid(row=0, column=0, pady=35)

        action_row = ctk.CTkFrame(ranges_card, fg_color="transparent")
        action_row.grid(row=3, column=0, padx=16, pady=(5, 15), sticky="ew")
        ctk.CTkButton(action_row, text="Clear All", width=92, height=36,
                      fg_color="#263247", hover_color="#35435a", command=self.clear_cut_ranges).pack(side="left")
        ctk.CTkButton(action_row, text="＋ Add Manual Range", width=145, height=36,
                      fg_color="#263247", hover_color="#35435a", command=self.add_cut_range).pack(side="left", padx=8)
        ctk.CTkButton(action_row, text="✦  Remove Sections", width=180, height=40, command=self.start_cut).pack(side="right")

        safety = ctk.CTkFrame(self.cut_tab, fg_color="#101a2c", corner_radius=12)
        safety.grid(row=4, column=0, sticky="ew", pady=(0, 16))
        ctk.CTkLabel(safety, text="⚡ Fast mode", text_color="#c7d2fe", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            safety,
            text="No video rendering is performed in Fast / Stream Copy mode. The app creates the kept pieces, discards the unwanted ranges, then concatenates the pieces with -c copy.",
            text_color=MUTED, wraplength=840, justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 12))

    def _build_join_tab(self):
        self.join_tab = ctk.CTkScrollableFrame(
            self.content, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT,
            scrollbar_fg_color="transparent",
        )
        self.join_tab.grid(row=0, column=0, sticky="nsew")
        self.join_tab.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(self.join_tab, fg_color=PANEL, corner_radius=14)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(card, text="JOIN MODE", text_color="#64748b", font=ctk.CTkFont(size=10, weight="bold")).grid(row=0, column=0, padx=16, pady=(14, 4), sticky="w")
        self.join_mode = ctk.StringVar(value="Fast / Stream Copy")
        ctk.CTkOptionMenu(card, variable=self.join_mode, values=["Fast / Stream Copy", "Re-encode / Normalize"], width=250).grid(row=1, column=0, padx=16, pady=(0, 7), sticky="w")
        ctk.CTkLabel(card, text="Fast joining requires compatible stream parameters. Re-encode is more tolerant of mixed sources.", text_color=MUTED, anchor="w", wraplength=700).grid(row=1, column=1, padx=8, sticky="w")
        ctk.CTkLabel(card, text="OUTPUT", text_color="#64748b", font=ctk.CTkFont(size=10, weight="bold")).grid(row=2, column=0, padx=16, pady=(7, 3), sticky="w")
        output = ctk.CTkFrame(card, fg_color="transparent")
        output.grid(row=3, column=0, columnspan=2, padx=16, pady=(0, 14), sticky="ew")
        output.grid_columnconfigure(0, weight=1)
        self.join_output_entry = ctk.CTkEntry(output, placeholder_text="Output path…", height=36)
        self.join_output_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(output, text="Choose", width=90, height=36, command=self.choose_join_output).grid(row=0, column=1, padx=(8, 0))

        list_card = ctk.CTkFrame(self.join_tab, fg_color=PANEL, corner_radius=14)
        list_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        list_card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(list_card, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(13, 7), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="VIDEO ORDER", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(header, text="＋ Add Videos", width=110, height=32, command=self.add_join_files).grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(list_card, text="Use ↑ / ↓ to reorder clips. Remove files with ×.", text_color=MUTED).grid(row=1, column=0, padx=16, pady=(0, 6), sticky="w")

        self.join_scroll = ctk.CTkScrollableFrame(list_card, fg_color="#0d1424", corner_radius=10, height=330,
                                                   scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT)
        self.join_scroll.grid(row=2, column=0, padx=12, pady=2, sticky="ew")
        self.join_scroll.grid_columnconfigure(0, weight=1)
        self.join_empty = ctk.CTkLabel(self.join_scroll, text="Add two or more videos to get started.", text_color="#64748b")
        self.join_empty.grid(row=0, column=0, pady=55)

        action = ctk.CTkFrame(list_card, fg_color="transparent")
        action.grid(row=3, column=0, padx=16, pady=(8, 15), sticky="ew")
        ctk.CTkButton(action, text="Clear All", width=92, height=36, fg_color="#263247", hover_color="#35435a", command=self.clear_join_files).pack(side="left")
        ctk.CTkButton(action, text="⇄  Join Videos", width=165, height=40, command=self.start_join).pack(side="right")

        tip = ctk.CTkFrame(self.join_tab, fg_color="#101a2c", corner_radius=12)
        tip.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        ctk.CTkLabel(tip, text="Tip", text_color="#c7d2fe", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(tip, text="Fast join is basically a container operation: FFmpeg copies the compressed streams instead of rendering every frame.", text_color=MUTED, wraplength=850, justify="left").pack(anchor="w", padx=14, pady=(0, 12))

    def _build_audio_tab(self):
        self.audio_tab = ctk.CTkScrollableFrame(
            self.content, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT,
            scrollbar_fg_color="transparent",
        )
        self.audio_tab.grid(row=0, column=0, sticky="nsew")
        self.audio_tab.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(self.audio_tab, fg_color=PANEL, corner_radius=14)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="SOURCE MEDIA", text_color="#64748b", font=ctk.CTkFont(size=10, weight="bold")).grid(row=0, column=0, padx=16, pady=(14, 4), sticky="w")
        source = ctk.CTkFrame(card, fg_color="transparent")
        source.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")
        source.grid_columnconfigure(0, weight=1)
        self.audio_input_entry = ctk.CTkEntry(source, placeholder_text="Select a video or audio file…", height=38)
        self.audio_input_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(source, text="Browse", width=105, height=38, command=self.browse_audio_input).grid(row=0, column=1, padx=(8, 0))
        self.audio_info_label = ctk.CTkLabel(card, text="MP3 uses the first audio stream.", text_color=MUTED, anchor="w")
        self.audio_info_label.grid(row=2, column=0, padx=16, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(card, text="AUDIO FORMAT", text_color="#64748b", font=ctk.CTkFont(size=10, weight="bold")).grid(row=3, column=0, padx=16, pady=(5, 3), sticky="w")
        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.grid(row=4, column=0, padx=16, pady=(0, 9), sticky="ew")
        self.audio_format = ctk.StringVar(value="MP3")
        self.audio_format_menu = ctk.CTkOptionMenu(row1, variable=self.audio_format, values=["MP3", "M4A (AAC)", "WAV"], width=180, command=self._update_audio_format)
        self.audio_format_menu.pack(side="left")
        self.audio_format_help = ctk.CTkLabel(row1, text="MP3 is broadly compatible; M4A is often smaller at similar quality; WAV is uncompressed.", text_color=MUTED, anchor="w")
        self.audio_format_help.pack(side="left", padx=14)

        ctk.CTkLabel(card, text="QUALITY", text_color="#64748b", font=ctk.CTkFont(size=10, weight="bold")).grid(row=5, column=0, padx=16, pady=(5, 3), sticky="w")
        qrow = ctk.CTkFrame(card, fg_color="transparent")
        qrow.grid(row=6, column=0, padx=16, pady=(0, 9), sticky="ew")
        self.mp3_quality = ctk.StringVar(value="V2 • ~190 kbps")
        self.mp3_quality_menu = ctk.CTkOptionMenu(qrow, variable=self.mp3_quality, values=["V0 • ~245 kbps", "V2 • ~190 kbps", "V4 • ~165 kbps", "320 kbps CBR"], width=210)
        self.mp3_quality_menu.pack(side="left")
        self.audio_quality_help = ctk.CTkLabel(qrow, text="V2 is a good everyday MP3 balance.", text_color=MUTED)
        self.audio_quality_help.pack(side="left", padx=14)

        ctk.CTkLabel(card, text="OUTPUT", text_color="#64748b", font=ctk.CTkFont(size=10, weight="bold")).grid(row=7, column=0, padx=16, pady=(5, 3), sticky="w")
        outrow = ctk.CTkFrame(card, fg_color="transparent")
        outrow.grid(row=8, column=0, padx=16, pady=(0, 15), sticky="ew")
        outrow.grid_columnconfigure(0, weight=1)
        self.audio_output_entry = ctk.CTkEntry(outrow, placeholder_text="Output path…", height=36)
        self.audio_output_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(outrow, text="Choose", width=90, height=36, command=self.choose_audio_output).grid(row=0, column=1, padx=(8, 0))

        tip = ctk.CTkFrame(self.audio_tab, fg_color=PANEL, corner_radius=14)
        tip.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(tip, text="♫  Audio conversion", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=16, pady=(14, 2))
        ctk.CTkLabel(tip, text="Audio extraction does encode or remux the selected audio because the output format may differ from the source. For MP3, the first audio stream is encoded with LAME.", text_color=MUTED, wraplength=850, justify="left").pack(anchor="w", padx=16, pady=(0, 14))
        ctk.CTkButton(self.audio_tab, text="♫  Extract Audio", width=175, height=42, command=self.start_audio).grid(row=2, column=0, pady=(2, 16), sticky="e")
        self._update_audio_format("MP3")

    def _show_tab(self, name):
        for frame in (self.cut_tab, self.join_tab, self.audio_tab, self.frames_tab):
            frame.grid_remove()
        tab = {
            "cut": self.cut_tab,
            "join": self.join_tab,
            "audio": self.audio_tab,
            "frames": self.frames_tab,
        }[name]
        tab.grid(row=0, column=0, sticky="nsew")
        for key, btn in self.nav_buttons.items():
            active = key == name
            btn.configure(
                fg_color=ACCENT if active else "transparent",
                hover_color=ACCENT_HOVER if active else PANEL_2,
            )
        titles = {
            "cut": ("Cut & Remove", "Remove one or more sections using fast stream copying."),
            "join": ("Join Videos", "Stitch multiple clips together without touching the pixels when possible."),
            "audio": ("Extract Audio", "Save the first audio stream as MP3, M4A, or WAV."),
            "frames": ("Extract Frames", "Dump individual frames from any video as JPEG or PNG images."),
        }
        self.page_title.configure(text=titles[name][0])
        self.page_desc.configure(text=titles[name][1])

    # ----------------------- FRAMES TAB -------------------------

    def _build_frames_tab(self):
        self.frames_tab = ctk.CTkScrollableFrame(
            self.content, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT,
            scrollbar_fg_color="transparent",
        )
        self.frames_tab.grid(row=0, column=0, sticky="nsew")
        self.frames_tab.grid_columnconfigure(0, weight=1)

        # Frame state
        self.frames_input_path = ""
        self.frames_input_duration = 0.0
        self.frames_input_fps = 0.0

        # ── Source Video Card ──────────────────────────────────────────────
        src_card = ctk.CTkFrame(self.frames_tab, fg_color=PANEL, corner_radius=14)
        src_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        src_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(src_card, text="SOURCE VIDEO", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(14, 4), sticky="w")
        src_line = ctk.CTkFrame(src_card, fg_color="transparent")
        src_line.grid(row=1, column=0, padx=16, pady=(0, 6), sticky="ew")
        src_line.grid_columnconfigure(0, weight=1)
        self.frames_input_entry = ctk.CTkEntry(src_line, placeholder_text="Select a video file…", height=38)
        self.frames_input_entry.grid(row=0, column=0, sticky="ew")
        self.frames_input_entry.bind("<Return>", lambda _e: self._load_frames_input())
        ctk.CTkButton(src_line, text="Browse", width=105, height=38,
                      command=self._browse_frames_input).grid(row=0, column=1, padx=(8, 0))
        self.frames_info_label = ctk.CTkLabel(src_card, text="No video loaded",
                                              text_color=MUTED, anchor="w")
        self.frames_info_label.grid(row=2, column=0, padx=16, pady=(0, 13), sticky="ew")

        # ── Extraction Mode Card ───────────────────────────────────────────
        mode_card = ctk.CTkFrame(self.frames_tab, fg_color=PANEL, corner_radius=14)
        mode_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        mode_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(mode_card, text="EXTRACTION MODE", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=16, pady=(14, 6), sticky="w")

        self.frames_mode = tk.StringVar(value="fps")
        modes = [
            ("fps",       "Fixed FPS",               "Extract at a specific frames-per-second rate"),
            ("all",       "All Frames",               "Extract every single frame (can be thousands)"),
            ("keyframes", "Keyframes (I-Frames) Only", "Ultra-fast — only keyframes, no re-decode"),
            ("scene",     "Scene Cut Detection",      "One frame per detected scene change"),
        ]
        for i, (val, label, hint) in enumerate(modes):
            rb = ctk.CTkRadioButton(
                mode_card, text=label, variable=self.frames_mode, value=val,
                font=ctk.CTkFont(size=13),
                command=self._frames_mode_changed,
            )
            rb.grid(row=i + 1, column=0, padx=16, pady=4, sticky="w")
            ctk.CTkLabel(mode_card, text=hint, text_color=MUTED,
                         font=ctk.CTkFont(size=11)).grid(
                row=i + 1, column=1, padx=8, pady=4, sticky="w")

        # FPS sub-input
        fps_row = ctk.CTkFrame(mode_card, fg_color="transparent")
        fps_row.grid(row=5, column=0, columnspan=2, padx=16, pady=(4, 2), sticky="w")
        ctk.CTkLabel(fps_row, text="Rate:", text_color=MUTED).pack(side="left")
        self.frames_fps_entry = ctk.CTkEntry(fps_row, width=72, height=30,
                                              placeholder_text="12")
        self.frames_fps_entry.pack(side="left", padx=(6, 4))
        self.frames_fps_entry.insert(0, "12")
        self.frames_fps_entry.bind("<KeyRelease>", lambda _e: self._frames_update_estimate())
        ctk.CTkLabel(fps_row, text="frames / second", text_color=MUTED).pack(side="left")
        self.frames_fps_row = fps_row

        # Scene threshold sub-input
        scene_row = ctk.CTkFrame(mode_card, fg_color="transparent")
        scene_row.grid(row=6, column=0, columnspan=2, padx=16, pady=(2, 6), sticky="w")
        ctk.CTkLabel(scene_row, text="Threshold (0.0–1.0):", text_color=MUTED).pack(side="left")
        self.frames_scene_entry = ctk.CTkEntry(scene_row, width=68, height=30,
                                                placeholder_text="0.3")
        self.frames_scene_entry.pack(side="left", padx=(6, 4))
        self.frames_scene_entry.insert(0, "0.3")
        ctk.CTkLabel(scene_row, text="(lower = more cuts)", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.frames_scene_row = scene_row
        self._frames_mode_changed()

        # ── Time Range Card ────────────────────────────────────────────────
        range_card = ctk.CTkFrame(self.frames_tab, fg_color=PANEL, corner_radius=14)
        range_card.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        range_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(range_card, text="TIME RANGE", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(14, 6), sticky="w")

        self.frames_range_mode = tk.StringVar(value="full")
        ctk.CTkRadioButton(
            range_card, text="Whole video", variable=self.frames_range_mode, value="full",
            command=self._frames_update_estimate,
        ).grid(row=1, column=0, padx=16, pady=3, sticky="w")
        ctk.CTkRadioButton(
            range_card, text="Custom range", variable=self.frames_range_mode, value="range",
            command=self._frames_update_estimate,
        ).grid(row=2, column=0, padx=16, pady=3, sticky="w")

        range_inputs = ctk.CTkFrame(range_card, fg_color="transparent")
        range_inputs.grid(row=3, column=0, padx=16, pady=(2, 12), sticky="w")
        ctk.CTkLabel(range_inputs, text="From:", text_color=MUTED).pack(side="left")
        self.frames_start_entry = ctk.CTkEntry(range_inputs, width=100, height=30,
                                               placeholder_text="00:00")
        self.frames_start_entry.pack(side="left", padx=(6, 4))
        self.frames_start_entry.insert(0, "00:00")
        ctk.CTkLabel(range_inputs, text="To:", text_color=MUTED).pack(side="left", padx=(10, 0))
        self.frames_end_entry = ctk.CTkEntry(range_inputs, width=100, height=30,
                                             placeholder_text="00:30")
        self.frames_end_entry.pack(side="left", padx=(6, 0))
        self.frames_end_entry.insert(0, "00:30")
        for w in (self.frames_start_entry, self.frames_end_entry):
            w.bind("<KeyRelease>", lambda _e: self._frames_update_estimate())

        # ── Output Format Card ─────────────────────────────────────────────
        fmt_card = ctk.CTkFrame(self.frames_tab, fg_color=PANEL, corner_radius=14)
        fmt_card.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        fmt_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(fmt_card, text="OUTPUT FORMAT", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(14, 6), sticky="w")

        fmt_row = ctk.CTkFrame(fmt_card, fg_color="transparent")
        fmt_row.grid(row=1, column=0, padx=16, pady=(0, 6), sticky="ew")
        self.frames_img_format = tk.StringVar(value="jpg")
        ctk.CTkRadioButton(
            fmt_row, text="JPEG (smaller files)", variable=self.frames_img_format, value="jpg",
            command=self._frames_format_changed,
        ).pack(side="left", padx=(0, 20))
        ctk.CTkRadioButton(
            fmt_row, text="PNG (lossless, larger)", variable=self.frames_img_format, value="png",
            command=self._frames_format_changed,
        ).pack(side="left")

        # JPEG quality slider
        self.frames_quality_frame = ctk.CTkFrame(fmt_card, fg_color="transparent")
        self.frames_quality_frame.grid(row=2, column=0, padx=16, pady=(0, 14), sticky="ew")
        ctk.CTkLabel(self.frames_quality_frame, text="JPEG Quality:",
                     text_color=MUTED).pack(side="left")
        self.frames_quality_var = tk.IntVar(value=3)
        self.frames_quality_slider = ctk.CTkSlider(
            self.frames_quality_frame, from_=2, to=31, number_of_steps=29,
            variable=self.frames_quality_var, width=200,
        )
        self.frames_quality_slider.pack(side="left", padx=(10, 6))
        self.frames_quality_label_val = ctk.CTkLabel(
            self.frames_quality_frame, text="3", text_color=TEXT, width=24)
        self.frames_quality_label_val.pack(side="left")
        ctk.CTkLabel(self.frames_quality_frame,
                     text="(2=best quality  ·  31=smallest file)",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left", padx=8)
        self.frames_quality_var.trace_add("write", lambda *_: self.frames_quality_label_val.configure(
            text=str(self.frames_quality_var.get())))

        # ── Post-processing Card ───────────────────────────────────────────
        post_card = ctk.CTkFrame(self.frames_tab, fg_color=PANEL, corner_radius=14)
        post_card.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        post_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(post_card, text="POST-PROCESSING", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(14, 6), sticky="w")

        self.frames_grayscale = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(post_card, text="Convert to Grayscale",
                        variable=self.frames_grayscale).grid(
            row=1, column=0, padx=16, pady=4, sticky="w")

        resize_row = ctk.CTkFrame(post_card, fg_color="transparent")
        resize_row.grid(row=2, column=0, padx=16, pady=(4, 14), sticky="w")
        self.frames_resize_enabled = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(resize_row, text="Resize width to:",
                        variable=self.frames_resize_enabled).pack(side="left")
        self.frames_resize_entry = ctk.CTkEntry(resize_row, width=72, height=30,
                                                placeholder_text="1280")
        self.frames_resize_entry.pack(side="left", padx=(8, 4))
        self.frames_resize_entry.insert(0, "1280")
        ctk.CTkLabel(resize_row, text="px  (height auto-scaled)",
                     text_color=MUTED).pack(side="left")

        # ── Naming & Output Card ───────────────────────────────────────────
        naming_card = ctk.CTkFrame(self.frames_tab, fg_color=PANEL, corner_radius=14)
        naming_card.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        naming_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(naming_card, text="OUTPUT FILES", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(14, 6), sticky="w")

        naming_row = ctk.CTkFrame(naming_card, fg_color="transparent")
        naming_row.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(naming_row, text="Prefix:", text_color=MUTED).pack(side="left")
        self.frames_prefix_entry = ctk.CTkEntry(naming_row, width=110, height=30,
                                                placeholder_text="frame")
        self.frames_prefix_entry.pack(side="left", padx=(6, 16))
        self.frames_prefix_entry.insert(0, "frame")
        ctk.CTkLabel(naming_row, text="Start #:", text_color=MUTED).pack(side="left")
        self.frames_startn_entry = ctk.CTkEntry(naming_row, width=64, height=30,
                                                placeholder_text="1")
        self.frames_startn_entry.pack(side="left", padx=(6, 16))
        self.frames_startn_entry.insert(0, "1")
        ctk.CTkLabel(naming_row, text="Digits:", text_color=MUTED).pack(side="left")
        self.frames_digits_entry = ctk.CTkEntry(naming_row, width=52, height=30,
                                                placeholder_text="5")
        self.frames_digits_entry.pack(side="left", padx=(6, 0))
        self.frames_digits_entry.insert(0, "5")

        out_line = ctk.CTkFrame(naming_card, fg_color="transparent")
        out_line.grid(row=2, column=0, padx=16, pady=(0, 14), sticky="ew")
        out_line.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(naming_card, text="OUTPUT FOLDER", text_color="#64748b",
                     font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=2, column=0, padx=16, pady=(6, 4), sticky="w")
        out_folder_line = ctk.CTkFrame(naming_card, fg_color="transparent")
        out_folder_line.grid(row=3, column=0, padx=16, pady=(0, 14), sticky="ew")
        out_folder_line.grid_columnconfigure(0, weight=1)
        self.frames_output_entry = ctk.CTkEntry(out_folder_line,
                                                placeholder_text="Output folder…", height=36)
        self.frames_output_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(out_folder_line, text="Browse", width=90, height=36,
                      command=self._choose_frames_output).grid(row=0, column=1, padx=(8, 0))

        # ── Estimate & Actions ─────────────────────────────────────────────
        est_card = ctk.CTkFrame(self.frames_tab, fg_color=PANEL, corner_radius=14)
        est_card.grid(row=6, column=0, sticky="ew", pady=(0, 10))
        est_card.grid_columnconfigure(0, weight=1)

        self.frames_estimate_label = ctk.CTkLabel(
            est_card,
            text="Load a video to see an estimate.",
            text_color=MUTED, anchor="w",
        )
        self.frames_estimate_label.grid(row=0, column=0, padx=16, pady=(12, 0), sticky="ew")

        action_row = ctk.CTkFrame(est_card, fg_color="transparent")
        action_row.grid(row=1, column=0, padx=16, pady=(8, 14), sticky="ew")
        ctk.CTkButton(
            action_row, text="Open Output Folder", width=145, height=36,
            fg_color="#263247", hover_color="#35435a",
            command=self._open_frames_output_folder,
        ).pack(side="left")
        ctk.CTkButton(
            action_row, text="🎞  Extract Frames", width=165, height=40,
            command=self.start_frames,
        ).pack(side="right")

        # ── Info tip ──────────────────────────────────────────────────────
        tip = ctk.CTkFrame(self.frames_tab, fg_color="#101a2c", corner_radius=12)
        tip.grid(row=7, column=0, sticky="ew", pady=(0, 16))
        ctk.CTkLabel(tip, text="Tip", text_color="#c7d2fe",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            tip,
            text=("Keyframe-only mode is very fast because FFmpeg does not decode P/B frames. "
                  "Fixed-FPS mode re-decodes every frame to hit the exact rate. "
                  "Scene-cut mode scores each frame's visual delta — lower threshold = more frames."),
            text_color=MUTED, wraplength=840, justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 12))

    def _frames_mode_changed(self):
        mode = self.frames_mode.get()
        if mode == "fps":
            self.frames_fps_row.grid()
            self.frames_scene_row.grid_remove()
        elif mode == "scene":
            self.frames_fps_row.grid_remove()
            self.frames_scene_row.grid()
        else:
            self.frames_fps_row.grid_remove()
            self.frames_scene_row.grid_remove()
        self._frames_update_estimate()

    def _frames_format_changed(self):
        if self.frames_img_format.get() == "jpg":
            self.frames_quality_frame.grid()
        else:
            self.frames_quality_frame.grid_remove()

    def _frames_update_estimate(self):
        if self.frames_input_duration <= 0:
            return
        try:
            if self.frames_range_mode.get() == "range":
                start = parse_timecode(self.frames_start_entry.get())
                end = parse_timecode(self.frames_end_entry.get())
            else:
                start, end = 0.0, self.frames_input_duration
            span = max(0.0, end - start)
            mode = self.frames_mode.get()
            if mode in ("keyframes", "scene"):
                text = f"Estimated output: variable (filtered) over {span:.1f}s"
            elif mode == "all":
                rate = self.frames_input_fps or 0
                est = int(span * rate)
                text = f"Estimated output: ~{est:,} frames over {span:.1f}s at {rate:.2f} fps"
            else:  # fps
                try:
                    rate = float(self.frames_fps_entry.get())
                except ValueError:
                    rate = 0
                est = int(span * rate)
                text = f"Estimated output: ~{est:,} frames over {span:.1f}s at {rate:.2f} fps"
            self.frames_estimate_label.configure(text=text)
        except Exception:
            pass

    def _browse_frames_input(self):
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv *.m4v *.ts *.mts"),
                       ("All files", "*.*")],
        )
        if path:
            self._set_entry(self.frames_input_entry, path)
            # Auto-fill output folder
            if not self.frames_output_entry.get().strip():
                out = str(Path(path).with_suffix("")) + "_frames"
                self._set_entry(self.frames_output_entry, out)
            self._load_frames_input()

    def _load_frames_input(self):
        if not self._ensure_tools():
            return
        path = self.frames_input_entry.get().strip().strip('"')
        if not path or not os.path.isfile(path):
            messagebox.showerror("Input video", "Please choose an existing video file.")
            return

        def worker():
            try:
                probe = MediaTools.probe(path)
                duration = safe_float(probe.get("format", {}).get("duration"))
                streams = probe.get("streams", [])
                video = next((s for s in streams if s.get("codec_type") == "video"), {})
                fps_raw = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
                num, _, den = fps_raw.partition("/")
                fps = float(num) / float(den) if den and float(den) != 0 else float(num or 0)
                info = (
                    f"{format_seconds(duration)}  •  "
                    f"{video.get('width', '?')}x{video.get('height', '?')}  •  "
                    f"{video.get('codec_name', '?')}  •  {fps:.3f} fps"
                )
                self.ui_queue.put(("frames_load", path, duration, fps, info))
            except Exception as exc:
                self.ui_queue.put(("error", "Could not read video", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _choose_frames_output(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self._set_entry(self.frames_output_entry, folder)

    def _open_frames_output_folder(self):
        folder = self.frames_output_entry.get().strip()
        if folder and os.path.isdir(folder):
            self._open_folder(folder)
        else:
            messagebox.showwarning("Folder not found",
                                   "Output folder does not exist yet.\nRun an extraction first.")

    def start_frames(self):
        if self.busy:
            return
        if not self._ensure_tools():
            return
        path = self.frames_input_entry.get().strip().strip('"')
        if not path or not os.path.isfile(path):
            messagebox.showerror("Extract Frames", "Load a video first.")
            return

        out_folder = self.frames_output_entry.get().strip()
        if not out_folder:
            messagebox.showerror("Extract Frames", "Choose an output folder.")
            return

        mode = self.frames_mode.get()
        if mode == "fps":
            try:
                fps_val = float(self.frames_fps_entry.get())
                if fps_val <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Extract Frames", "FPS must be a positive number.")
                return

        if self.frames_range_mode.get() == "range":
            try:
                s = parse_timecode(self.frames_start_entry.get())
                e = parse_timecode(self.frames_end_entry.get())
                if e <= s:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Extract Frames",
                                     "Invalid time range. End must be after Start.")
                return

        try:
            int(self.frames_startn_entry.get())
            int(self.frames_digits_entry.get())
        except ValueError:
            messagebox.showerror("Extract Frames",
                                 "Start number and Digits must be integers.")
            return

        self.cancel_event = threading.Event()
        threading.Thread(
            target=self._frames_worker,
            args=(path, out_folder),
            daemon=True,
        ).start()

    def _frames_worker(self, source, out_folder):
        self.ui_queue.put(("busy", True))
        self.ui_queue.put(("progress", 0.0, "Preparing extraction…"))
        try:
            out_dir = Path(out_folder)
            out_dir.mkdir(parents=True, exist_ok=True)

            mode = self.frames_mode.get()
            img_format = self.frames_img_format.get()
            digits = int(self.frames_digits_entry.get())
            prefix = self.frames_prefix_entry.get() or "frame"
            start_num = int(self.frames_startn_entry.get())
            pattern = str(out_dir / f"{prefix}_%0{digits}d.{img_format}")

            # ---- Build FFmpeg command ----
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]

            # Time range
            if self.frames_range_mode.get() == "range":
                ss = parse_timecode(self.frames_start_entry.get())
                ee = parse_timecode(self.frames_end_entry.get())
                cmd += ["-ss", f"{ss:.6f}"]
                cmd += ["-i", source]
                cmd += ["-t", f"{max(0.0, ee - ss):.6f}"]
                total_seconds = max(0.001, ee - ss)
            else:
                cmd += ["-i", source]
                total_seconds = max(0.001, self.frames_input_duration or 0.001)

            # Video filters
            vf_parts = []
            if mode == "keyframes":
                vf_parts.append("select=eq(pict_type\\,I)")
            elif mode == "scene":
                thresh = self.frames_scene_entry.get() or "0.3"
                vf_parts.append(f"select=gt(scene\\,{thresh})")
            elif mode == "fps":
                fps_val = self.frames_fps_entry.get() or "12"
                vf_parts.append(f"fps={fps_val}")
            # else "all" — no fps filter

            if self.frames_resize_enabled.get():
                w = self.frames_resize_entry.get() or "1280"
                vf_parts.append(f"scale={w}:-2")

            if self.frames_grayscale.get():
                vf_parts.append("hue=s=0")

            if vf_parts:
                cmd += ["-vf", ",".join(vf_parts)]

            if mode in ("keyframes", "scene"):
                cmd += ["-vsync", "vfr"]

            if img_format == "jpg":
                cmd += ["-qscale:v", str(self.frames_quality_var.get())]

            cmd += ["-start_number", str(start_num)]
            cmd += ["-progress", "pipe:2", "-nostats"]
            cmd += [pattern]

            self.log("Extract Frames command: " + " ".join(
                f'"{c}"' if " " in c else c for c in cmd))

            total_us = total_seconds * 1_000_000
            frame_count = 0
            current_us = 0

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                startupinfo=_WIN_STARTUPINFO,
                creationflags=_WIN_CREATE_FLAGS,
            )
            self._set_current_process(proc)

            stderr_lines = []
            for raw in proc.stderr:
                if self.cancel_event.is_set():
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break
                line = raw.strip()
                if not line:
                    continue
                stderr_lines.append(line)
                if len(stderr_lines) > 200:
                    stderr_lines.pop(0)

                if line.startswith("out_time_us="):
                    current_us = safe_float(line.split("=", 1)[1])
                    pct = min(current_us / total_us, 1.0)
                    self.ui_queue.put(("frames_update", pct, frame_count))
                elif line.startswith("frame="):
                    try:
                        frame_count = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                elif line.startswith("progress="):
                    if line.endswith("end"):
                        self.ui_queue.put(("frames_update", 1.0, frame_count))
                elif "error" in line.lower() or "Error" in line:
                    self.log(line)

            return_code = proc.wait()
            self._set_current_process(None)

            if self.cancel_event.is_set():
                self.ui_queue.put(("progress", 0.0, "Cancelled."))
                return  # cancelled cleanly — no error dialog
            if return_code != 0:
                msg = "\n".join(stderr_lines[-15:]).strip()
                raise RuntimeError(msg or f"FFmpeg exited with code {return_code}.")

            # Count output files
            try:
                n_files = len(list(out_dir.glob(f"{prefix}_*.{img_format}")))
            except Exception:
                n_files = frame_count

            self.log(f"Frame extraction complete: {n_files} frames saved to {out_folder}")
            self.ui_queue.put(("finished", "Extraction complete",
                               f"{n_files} frame(s) saved to:\n{out_folder}"))
        except Exception as exc:
            self.log(f"Extraction error: {exc}")
            self.ui_queue.put(("error", "Extraction failed", str(exc)))
        finally:
            self._set_current_process(None)
            self.ui_queue.put(("busy", False))

    # ------------------------ Common helpers ------------------------

    def _check_ffmpeg(self):
        ffmpeg, ffprobe = MediaTools.check_tools()
        if ffmpeg and ffprobe:
            self.tool_status.configure(text="●  FFmpeg ready", text_color=SUCCESS)
            self.log("FFmpeg + FFprobe found in PATH.")
        elif ffmpeg:
            self.tool_status.configure(text="●  FFmpeg only", text_color=WARNING)
            self.log("FFmpeg found, but ffprobe is missing. Media info/cutting depends on ffprobe.")
        else:
            self.tool_status.configure(text="●  FFmpeg not found", text_color=DANGER)
            self.log("FFmpeg is not in PATH. Install FFmpeg or add it to PATH, then restart ClipForge.")

    def log(self, message):
        self.ui_queue.put(("log", message))

    def set_progress(self, fraction, label=None):
        self.ui_queue.put(("progress", max(0.0, min(1.0, fraction)), label))

    def _drain_ui_queue(self):
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log_lines.append(str(event[1]))
                    self.log_lines = self.log_lines[-60:]
                    self.log_text.configure(state="normal")
                    self.log_text.delete("1.0", "end")
                    self.log_text.insert("end", "\n".join(self.log_lines))
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "progress":
                    frac = event[1]
                    label = event[2] or f"Working… {frac * 100:.1f}%"
                    self.progress.set(frac)
                    self.progress_label.configure(text=label)
                elif kind == "busy":
                    self._set_busy(event[1])
                elif kind == "load_cut":
                    info, out = event[1], event[2]
                    duration = event[3] if len(event) > 3 else self.input_duration
                    vid_path = event[4] if len(event) > 4 else self.input_path
                    self.media_info_label.configure(text=info, text_color=MUTED)
                    self._set_entry(self.cut_output_entry, out)
                    self.log(f"Loaded: {Path(self.input_path).name}")
                    self.progress.set(0)
                    self.progress_label.configure(text="Ready")
                    if hasattr(self, "timeline"):
                        self.timeline.set_duration(duration)
                        self.timeline.update_video_path(vid_path)
                elif kind == "info":
                    title, msg = event[1], event[2]
                    messagebox.showinfo(title, msg)
                elif kind == "error":
                    title, msg = event[1], event[2]
                    messagebox.showerror(title, msg)
                elif kind == "finished":
                    title, msg = event[1], event[2]
                    self._set_busy(False)
                    self.progress.set(1)
                    self.progress_label.configure(text="Done")
                    lines = str(msg).splitlines()
                    if lines:
                        candidate = lines[-1].strip()
                        if candidate and os.path.exists(candidate):
                            self.last_output = candidate
                    messagebox.showinfo(title, msg)
                elif kind == "snap_ab":
                    new_start, new_end = event[1], event[2]
                    if hasattr(self, "timeline"):
                        self.timeline.set_range(new_start, new_end, emit=True)
                    self.progress_label.configure(
                        text=f"Snapped: A={format_seconds(new_start)}  B={format_seconds(new_end)}"
                    )
                elif kind == "frames_load":
                    path, duration, fps, info = event[1], event[2], event[3], event[4]
                    self.frames_input_path = path
                    self.frames_input_duration = duration
                    self.frames_input_fps = fps
                    self.frames_info_label.configure(text=info, text_color=MUTED)
                    self.frames_end_entry.delete(0, "end")
                    self.frames_end_entry.insert(0, format_seconds(duration))
                    self.log(f"Loaded: {Path(path).name}")
                    self.progress.set(0)
                    self.progress_label.configure(text="Ready")
                    self._frames_update_estimate()
                elif kind == "frames_update":
                    pct, frame_count = event[1], event[2]
                    self.progress.set(pct)
                    self.progress_label.configure(
                        text=f"Extracting frames… {pct * 100:.1f}%  ({frame_count} frames)"
                    )
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    def _set_busy(self, busy):
        self.busy = busy
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if not busy:
            self.cancel_event.clear()

    def _ensure_tools(self):
        ffmpeg, ffprobe = MediaTools.check_tools()
        if not ffmpeg or not ffprobe:
            messagebox.showerror("FFmpeg missing", "ClipForge needs both ffmpeg and ffprobe available in PATH.")
            return False
        return True

    def _confirm_overwrite(self, output):
        if os.path.exists(output):
            return messagebox.askyesno("Overwrite output?", f"This file already exists:\n\n{output}\n\nReplace it?")
        return True

    def open_last_output(self):
        """Open the folder containing the last output file, or the last loaded input."""
        target = self.last_output or self.input_path or ""
        if target and os.path.exists(target):
            self._open_folder(target)
        else:
            self.progress_label.configure(text="No output file yet.")

    def _open_folder(self, path):
        try:
            folder = str(Path(path).resolve().parent)
            if os.name == "nt":
                os.startfile(folder)
            elif shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("open"):
                subprocess.Popen(["open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.log(f"Could not open folder: {exc}")

    def clear_log(self):
        self.log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "Log cleared.\n")
        self.log_text.configure(state="disabled")

    def copy_log(self):
        text = self.log_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.progress_label.configure(text="Log copied to clipboard")

    def _default_output(self, source, suffix, ext=None):
        p = Path(source)
        extension = ext or p.suffix or ".mp4"
        return str(p.with_name(f"{p.stem}{suffix}{extension}"))

    def _set_entry(self, entry, value):
        entry.delete(0, "end")
        entry.insert(0, value)

    def _ask_output(self, initial, title="Choose output file", default_ext=".mp4"):
        path = filedialog.asksaveasfilename(
            title=title,
            initialfile=Path(initial).name if initial else "output" + default_ext,
            defaultextension=default_ext,
            filetypes=[
                ("MP4 video", "*.mp4"),
                ("MKV video", "*.mkv"),
                ("MOV video", "*.mov"),
                ("All files", "*.*"),
            ],
        )
        return path

    # -------------------------- CUT TAB --------------------------

    def browse_cut_input(self):
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mts *.m2ts"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._set_entry(self.cut_input_entry, path)
            self.load_cut_input()

    def load_cut_input(self):
        if not self._ensure_tools():
            return
        path = self.cut_input_entry.get().strip().strip('"')
        if not path or not os.path.isfile(path):
            messagebox.showerror("Input video", "Please choose an existing video file.")
            return

        def worker():
            try:
                probe = MediaTools.probe(path)
                duration = safe_float(probe.get("format", {}).get("duration"))
                streams = probe.get("streams", [])
                video = next((s for s in streams if s.get("codec_type") == "video"), {})
                audio_count = sum(s.get("codec_type") == "audio" for s in streams)
                size = safe_float(probe.get("format", {}).get("size"))
                size_gb = size / (1024 ** 3) if size else 0
                info = (
                    f"{format_seconds(duration)}  •  "
                    f"{video.get('width', '?')}×{video.get('height', '?')}  •  "
                    f"{video.get('codec_name', '?')}  •  "
                    f"{audio_count} audio stream(s)  •  {size_gb:.2f} GB"
                )
                self.input_path = path
                self.input_probe = probe
                self.input_duration = duration
                out = self._default_output(path, "_cut")
                self.ui_queue.put(("load_cut", info, out, duration, path))
            except Exception as exc:
                self.ui_queue.put(("error", "Could not read video", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def choose_cut_output(self):
        current = self.cut_output_entry.get().strip() or self._default_output(self.cut_input_entry.get(), "_cut")
        path = self._ask_output(current)
        if path:
            self._set_entry(self.cut_output_entry, path)

    def _update_cut_mode_description(self, mode):
        if mode.startswith("Fast"):
            self.cut_mode_help.configure(text="Fast mode copies compressed streams. Cut boundaries can land on nearby keyframes.")
        else:
            self.cut_mode_help.configure(text="Frame-accurate mode re-encodes the kept video/audio. Slower, but precise.")

    def _timeline_changed(self, start, end):
        # Keep this deliberately lightweight: dragging should never trigger expensive work.
        dur = max(0.0, end - start)
        self.progress_label.configure(
            text=f"A={format_seconds(start)}  B={format_seconds(end)}  (removing {format_seconds(dur)})"
        )

    def reset_timeline(self):
        if self.input_duration <= 0:
            return
        end = min(10.0, self.input_duration)
        self.timeline.set_range(0.0, end)
        self.progress_label.configure(text="A/B selector reset")

    def snap_ab_to_keyframe(self):
        """Snap both A and B handles to the nearest keyframe in the source video."""
        if not self.input_path or self.input_duration <= 0:
            messagebox.showwarning("Snap to Keyframe", "Load a video first.")
            return
        start, end = self.timeline.get_range()
        self.progress_label.configure(text="Snapping to nearest keyframes...")

        def worker():
            try:
                new_start = MediaTools.find_nearest_keyframe(self.input_path, start)
                new_end = MediaTools.find_nearest_keyframe(self.input_path, end)
                if new_start >= new_end:
                    new_end = min(new_start + 1.0, self.input_duration)
                self.ui_queue.put(("snap_ab", new_start, new_end))
            except Exception as exc:
                self.ui_queue.put(("progress", 0.0, f"Snap failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def add_timeline_range(self):
        start, end = self.timeline.get_range()
        if end <= start:
            messagebox.showerror("Range selector", "End must be after start.")
            return
        self.add_cut_range(format_seconds(start), format_seconds(end))
        self.progress_label.configure(text=f"Added range  {format_seconds(start)} to {format_seconds(end)}")

    def clear_cut_ranges(self):
        for row in list(self.cut_rows):
            row.destroy()
        self.cut_rows.clear()
        self.cut_empty.grid()
        self.progress_label.configure(text="Cut ranges cleared")

    def add_cut_range(self, start=None, end=None):
        self.cut_empty.grid_remove()
        if start is None or end is None:
            if self.input_duration > 0:
                start = format_seconds(min(self.input_duration * 0.15, max(0.0, self.input_duration - 1.0)))
                end = format_seconds(min(parse_timecode(start) + 10.0, self.input_duration))
            else:
                start, end = "00:00", "00:10"
        row = CutRangeRow(self.cut_scroll, len(self.cut_rows) + 1, self.remove_cut_row, self.cut_ranges_changed)
        row.start_var.set(start)
        row.end_var.set(end)
        row.grid(row=len(self.cut_rows), column=0, padx=4, pady=4, sticky="ew")
        self.cut_rows.append(row)
        self.cut_scroll.update_idletasks()
        try:
            self.cut_scroll._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def sample_cut_range(self):
        if self.input_duration <= 10:
            messagebox.showinfo("Sample range", "Load a longer video first; this helper needs more than 10 seconds.")
            return
        start = min(60.0, self.input_duration / 3)
        end = min(start + 10.0, self.input_duration - 1)
        self.add_cut_range(format_seconds(start), format_seconds(end))

    def remove_cut_row(self, row):
        if row in self.cut_rows:
            self.cut_rows.remove(row)
            row.destroy()
            for idx, item in enumerate(self.cut_rows, 1):
                item.grid_configure(row=idx - 1)
                item.renumber(idx)
            if not self.cut_rows:
                self.cut_empty.grid()
            self.progress_label.configure(text=f"{len(self.cut_rows)} cut range(s)")

    def cut_ranges_changed(self):
        pass

    def _get_cut_ranges(self):
        if not self.cut_rows:
            raise ValueError("Add at least one section to remove.")
        if self.input_duration <= 0:
            raise ValueError("Load a video first.")

        raw = []
        for row in self.cut_rows:
            start_s, end_s = row.values()
            start = parse_timecode(start_s)
            end = parse_timecode(end_s)
            if start >= end:
                raise ValueError(f"Range {row.index}: start must be earlier than end.")
            if start < 0 or end > self.input_duration + 0.001:
                raise ValueError(
                    f"Range {row.index} is outside the video duration ({format_seconds(self.input_duration)})."
                )
            raw.append((start, min(end, self.input_duration)))

        raw.sort()
        # Auto-merge overlapping or adjacent ranges
        merged = [raw[0]]
        for start, end in raw[1:]:
            if start <= merged[-1][1] + 1e-6:  # overlapping or touching
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def _kept_ranges(self, cut_ranges):
        kept = []
        cursor = 0.0
        for start, end in cut_ranges:
            if start > cursor + 0.001:
                kept.append((cursor, start))
            cursor = end
        if cursor < self.input_duration - 0.001:
            kept.append((cursor, self.input_duration))
        return kept

    def start_cut(self):
        if self.busy:
            return
        if not self._ensure_tools():
            return
        path = self.input_path or self.cut_input_entry.get().strip().strip('"')
        if not path or not os.path.isfile(path):
            messagebox.showerror("Cut & Remove", "Load a video first.")
            return
        self.input_path = path

        try:
            cut_ranges = self._get_cut_ranges()
            kept = self._kept_ranges(cut_ranges)
            if not kept:
                raise ValueError("Your cut ranges remove the entire video. Keep at least some content.")
        except ValueError as exc:
            messagebox.showerror("Invalid cut ranges", str(exc))
            return

        output = self.cut_output_entry.get().strip()
        if not output:
            output = self._default_output(path, "_cut")
            self._set_entry(self.cut_output_entry, output)

        if os.path.abspath(output) == os.path.abspath(path):
            messagebox.showerror("Output file", "Output must be different from the input file.")
            return

        if not self._confirm_overwrite(output):
            return
        self.last_output = output
        mode = self.cut_mode.get()
        self.cancel_event = threading.Event()
        threading.Thread(
            target=self._cut_worker,
            args=(path, output, kept, mode),
            daemon=True,
        ).start()

    def _cut_worker(self, source, output, kept, mode):
        self.ui_queue.put(("busy", True))
        self.ui_queue.put(("progress", 0.0, "Preparing cut…"))
        temp_dir = None
        output_written = False
        try:
            total_work = sum(end - start for start, end in kept)
            out_ext = Path(output).suffix.lower()
            if not out_ext:
                out_ext = ".mp4"
                output += out_ext

            os.makedirs(str(Path(output).parent), exist_ok=True)

            if mode.startswith("Fast"):
                # ----------------------------------------------------
                # Fast stream-copy path.
                # - 1 kept segment  → write directly to output (0 temp space)
                # - N kept segments → .mkv intermediates on the *output*
                #   volume, then lossless concat
                # ----------------------------------------------------
                if len(kept) == 1:
                    start, end = kept[0]
                    duration = end - start
                    cmd = [
                        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                        "-ss", f"{start:.6f}",
                        "-i", source,
                        "-t", f"{duration:.6f}",
                        "-map", "0",
                        "-c", "copy",
                        "-avoid_negative_ts", "make_zero",
                        "-movflags", "+faststart",
                        "-progress", "pipe:2", "-nostats",
                        output,
                    ]
                    self.log(
                        f"Fast cut (single segment, direct write): "
                        f"{format_seconds(start)} → {format_seconds(end)}"
                    )
                    MediaTools.run(
                        cmd,
                        log_callback=self.log,
                        progress_callback=lambda current: self.ui_queue.put(
                            ("progress", min(current / max(duration, 1), 1.0), "Fast cut • direct")
                        ),
                        cancel_event=self.cancel_event,
                        proc_callback=self._set_current_process,
                    )
                    output_written = True
                else:
                    # Temp folder lives next to the final output so large
                    # segments never fill the OS drive.
                    temp_dir = tempfile.mkdtemp(
                        prefix="clipforge_",
                        dir=str(Path(output).parent),
                    )
                    segment_files = []
                    work_done = 0.0

                    self.log(
                        f"Fast cut: keeping {len(kept)} segment(s) "
                        f"(temp on output volume), discarding everything else."
                    )
                    for i, (start, end) in enumerate(kept, 1):
                        if self.cancel_event.is_set():
                            raise FFmpegError("Operation cancelled.")
                        # Matroska intermediates avoid non-monotonic DTS
                        # issues that .mp4 intermediates often produce.
                        seg = os.path.join(temp_dir, f"segment_{i:04d}.mkv")
                        duration = end - start
                        cmd = [
                            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                            "-ss", f"{start:.6f}",
                            "-i", source,
                            "-t", f"{duration:.6f}",
                            "-map", "0",
                            "-c", "copy",
                            "-avoid_negative_ts", "make_zero",
                            "-progress", "pipe:2", "-nostats",
                            seg,
                        ]
                        self.log(
                            f"Copying segment {i}/{len(kept)}: "
                            f"{format_seconds(start)} → {format_seconds(end)}"
                        )

                        def segment_progress(current, base=work_done, dur=duration, idx=i):
                            self.ui_queue.put((
                                "progress",
                                (base + min(current, dur)) / total_work,
                                f"Fast cut • segment {idx}/{len(kept)}",
                            ))

                        MediaTools.run(
                            cmd,
                            log_callback=self.log,
                            progress_callback=segment_progress,
                            cancel_event=self.cancel_event,
                            proc_callback=self._set_current_process,
                        )
                        segment_files.append(seg)
                        work_done += duration

                    concat_file = os.path.join(temp_dir, "concat.txt")
                    with open(concat_file, "w", encoding="utf-8", newline="\n") as f:
                        for seg in segment_files:
                            escaped = seg.replace("'", "'\\''")
                            f.write(f"file '{escaped}'\n")

                    cmd = [
                        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                        "-fflags", "+genpts",
                        "-f", "concat", "-safe", "0", "-i", concat_file,
                        "-map", "0",
                        "-ignore_unknown",
                        "-c", "copy",
                        "-movflags", "+faststart",
                        "-progress", "pipe:2", "-nostats",
                        output,
                    ]
                    self.log("Concatenating kept segments with stream copy…")
                    MediaTools.run(
                        cmd,
                        log_callback=self.log,
                        progress_callback=lambda current: self.ui_queue.put((
                            "progress",
                            0.95 + min(current / max(total_work, 1), 1) * 0.05,
                            "Joining segments…",
                        )),
                        cancel_event=self.cancel_event,
                        proc_callback=self._set_current_process,
                    )
                    output_written = True

            else:
                # ----------------------------------------------------
                # Frame-accurate re-encode of kept content only.
                # ----------------------------------------------------
                probe = self.input_probe or MediaTools.probe(source)
                has_audio = MediaTools.has_audio(probe)
                filters = []
                video_refs = []
                audio_refs = []

                for i, (start, end) in enumerate(kept):
                    filters.append(
                        f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]"
                    )
                    video_refs.append(f"[v{i}]")
                    if has_audio:
                        filters.append(
                            f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]"
                        )
                        audio_refs.append(f"[a{i}]")

                if has_audio:
                    filters.append(
                        "".join(video_refs + audio_refs)
                        + f"concat=n={len(kept)}:v=1:a=1[outv][outa]"
                    )
                else:
                    filters.append(
                        "".join(video_refs) + f"concat=n={len(kept)}:v=1:a=0[outv]"
                    )

                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                    "-i", source,
                    "-filter_complex", ";".join(filters),
                    "-map", "[outv]",
                ]
                if has_audio:
                    cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
                cmd += [
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-progress", "pipe:2", "-nostats",
                    output,
                ]

                self.log("Frame-accurate mode: re-encoding kept content with H.264.")
                MediaTools.run(
                    cmd,
                    log_callback=self.log,
                    progress_callback=lambda current: self.ui_queue.put((
                        "progress",
                        min(current / max(total_work, 1), 1.0),
                        "Re-encoding kept content…",
                    )),
                    cancel_event=self.cancel_event,
                    proc_callback=self._set_current_process,
                )
                output_written = True

            self.log(f"Finished: {output}")
            self.ui_queue.put(("progress", 1.0, "Done"))
            self.ui_queue.put(("finished", "Cut complete", f"Saved:\n{output}"))
        except Exception as exc:
            self.log(f"Cut failed: {exc}")
            # Remove incomplete output on cancel/error so the user is not
            # left with a half-written file.
            if not output_written and output and os.path.isfile(output):
                try:
                    os.remove(output)
                    self.log("Removed incomplete output file.")
                except OSError:
                    pass
            self.ui_queue.put(("error", "Cut failed", str(exc)))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self.current_process = None
            self.ui_queue.put(("busy", False))

    # -------------------------- JOIN TAB --------------------------

    def add_join_files(self):
        paths = filedialog.askopenfilenames(
            title="Select videos to join",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mts *.m2ts"), ("All files", "*.*")],
        )
        if not paths:
            return
        for p in paths:
            if p not in self.join_files:
                self.join_files.append(p)
        self._render_join_files()
        if self.join_files and not self.join_output_entry.get().strip():
            self._set_entry(self.join_output_entry, self._default_output(self.join_files[0], "_joined"))

    def _render_join_files(self):
        for child in self.join_scroll.winfo_children():
            child.destroy()
        if not self.join_files:
            self.join_empty = ctk.CTkLabel(self.join_scroll, text="Add two or more videos to get started.", text_color="#64748b")
            self.join_empty.grid(row=0, column=0, pady=55)
            return
        for idx, path in enumerate(self.join_files, 1):
            row = JoinFileRow(
                self.join_scroll, idx, path,
                on_up=lambda i=idx - 1: self.move_join_file(i, -1),
                on_down=lambda i=idx - 1: self.move_join_file(i, 1),
                on_remove=lambda i=idx - 1: self.remove_join_file(i),
            )
            row.grid(row=idx - 1, column=0, padx=4, pady=4, sticky="ew")

    def move_join_file(self, index, delta):
        new_index = index + delta
        if 0 <= new_index < len(self.join_files):
            self.join_files[index], self.join_files[new_index] = self.join_files[new_index], self.join_files[index]
            self._render_join_files()

    def remove_join_file(self, index):
        if 0 <= index < len(self.join_files):
            self.join_files.pop(index)
            self._render_join_files()

    def clear_join_files(self):
        self.join_files.clear()
        self._render_join_files()

    def choose_join_output(self):
        source = self.join_files[0] if self.join_files else "joined.mp4"
        initial = self.join_output_entry.get().strip() or self._default_output(source, "_joined", ".mp4")
        path = self._ask_output(initial, "Choose joined output")
        if path:
            self._set_entry(self.join_output_entry, path)

    def start_join(self):
        if self.busy:
            return
        if not self._ensure_tools():
            return
        if len(self.join_files) < 2:
            messagebox.showerror("Join Videos", "Add at least two videos.")
            return
        output = self.join_output_entry.get().strip()
        if not output:
            output = self._default_output(self.join_files[0], "_joined")
            self._set_entry(self.join_output_entry, output)
        if any(os.path.abspath(output) == os.path.abspath(p) for p in self.join_files):
            messagebox.showerror("Output file", "Output must be different from every input file.")
            return

        if not self._confirm_overwrite(output):
            return
        self.last_output = output

        total_duration = 0.0
        try:
            for p in self.join_files:
                total_duration += MediaTools.duration(p)
        except Exception as exc:
            messagebox.showerror("Join Videos", f"Could not inspect the input videos.\n\n{exc}")
            return

        self.cancel_event = threading.Event()
        threading.Thread(target=self._join_worker, args=(list(self.join_files), output, self.join_mode.get(), total_duration), daemon=True).start()

    def _join_worker(self, files, output, mode, total_duration):
        self.ui_queue.put(("busy", True))
        try:
            os.makedirs(str(Path(output).parent), exist_ok=True)
            if mode.startswith("Fast"):
                temp_dir = tempfile.mkdtemp(prefix="clipforge_join_")
                try:
                    concat_txt = os.path.join(temp_dir, "concat.txt")
                    with open(concat_txt, "w", encoding="utf-8", newline="\n") as f:
                        for path in files:
                            escaped = path.replace("'", "'\\''")
                            f.write(f"file '{escaped}'\n")
                    self.log("Fast join: using FFmpeg concat demuxer + stream copy.")
                    cmd = [
                        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                        "-fflags", "+genpts",
                        "-f", "concat", "-safe", "0", "-i", concat_txt,
                        "-map", "0",
                        "-ignore_unknown",
                        "-c", "copy",
                        "-movflags", "+faststart",
                        "-progress", "pipe:2", "-nostats",
                        output,
                    ]
                    MediaTools.run(
                        cmd, log_callback=self.log,
                        progress_callback=lambda current: self.ui_queue.put(("progress", min(current / max(total_duration, 1), 1), "Joining…")),
                        cancel_event=self.cancel_event,
                        proc_callback=self._set_current_process,
                    )
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
            else:
                # More tolerant join: use the concat filter and normalize
                # video dimensions to the first video's dimensions.
                probes = [MediaTools.probe(p) for p in files]
                first_video = next((s for s in probes[0].get("streams", []) if s.get("codec_type") == "video"), {})
                width = int(first_video.get("width") or 1280)
                height = int(first_video.get("height") or 720)
                width += width % 2
                height += height % 2
                fps = 30

                # For simplicity and reliability, normalize every input to a video stream and
                # synthesize silence for files that lack audio. This handles many mixed inputs.
                filter_parts = []
                vrefs = []
                arefs = []
                durations = []
                for i, probe in enumerate(probes):
                    dur = safe_float(probe.get("format", {}).get("duration"))
                    durations.append(dur)
                    # Use even pad offsets (trunc(.../2)*2) so yuv420p never
                    # receives an odd dimension/offset and crashes.
                    filter_parts.append(
                        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:trunc((ow-iw)/2/2)*2:trunc((oh-ih)/2/2)*2:color=black,"
                        f"fps={fps},format=yuv420p,setsar=1[v{i}]"
                    )
                    vrefs.append(f"[v{i}]")
                    if MediaTools.has_audio(probe):
                        filter_parts.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,aresample=async=1:first_pts=0[a{i}]")
                    else:
                        filter_parts.append(f"anullsrc=r=48000:cl=stereo:d={dur:.6f}[a{i}]")
                    arefs.append(f"[a{i}]")

                filter_parts.append("".join(vrefs + arefs) + f"concat=n={len(files)}:v=1:a=1[outv][outa]")
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
                for path in files:
                    cmd += ["-i", path]
                cmd += [
                    "-filter_complex", ";".join(filter_parts),
                    "-map", "[outv]", "-map", "[outa]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    "-progress", "pipe:2", "-nostats",
                    output,
                ]
                self.log("Re-encode join: normalizing video/audio streams.")
                MediaTools.run(
                    cmd, log_callback=self.log,
                    progress_callback=lambda current: self.ui_queue.put(("progress", min(current / max(total_duration, 1), 1), "Normalizing + joining…")),
                    cancel_event=self.cancel_event,
                    proc_callback=self._set_current_process,
                )

            self.log(f"Joined output: {output}")
            self.ui_queue.put(("finished", "Join complete", f"Saved:\n{output}"))
        except Exception as exc:
            self.log(f"Join failed: {exc}")
            self.ui_queue.put(("error", "Join failed", str(exc)))
        finally:
            self.current_process = None
            self.ui_queue.put(("busy", False))

    # ------------------------- AUDIO TAB -------------------------

    def _update_audio_format(self, fmt):
        is_mp3 = fmt == "MP3"
        self.mp3_quality_menu.configure(state="normal" if is_mp3 else "disabled")
        if fmt == "MP3":
            self.audio_quality_help.configure(text="V2 is a good everyday MP3 balance.")
        elif fmt == "M4A (AAC)":
            self.audio_quality_help.configure(text="AAC 256 kbps is used for a compact, high-quality M4A.")
        else:
            self.audio_quality_help.configure(text="WAV is uncompressed PCM and can be much larger.")
        source = self.audio_input_entry.get().strip().strip('"') if hasattr(self, "audio_input_entry") else ""
        if source and os.path.isfile(source):
            ext = {"MP3": ".mp3", "M4A (AAC)": ".m4a", "WAV": ".wav"}[fmt]
            self._set_entry(self.audio_output_entry, str(Path(source).with_suffix(ext)))

    def browse_audio_input(self):
        path = filedialog.askopenfilename(
            title="Select media",
            filetypes=[("Media files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4a *.wav *.flac *.aac *.mp3"), ("All files", "*.*")],
        )
        if path:
            self._set_entry(self.audio_input_entry, path)
            self.load_audio_input(path)

    def load_audio_input(self, path=None):
        path = path or self.audio_input_entry.get().strip().strip('"')
        if not path or not os.path.isfile(path):
            return
        try:
            probe = MediaTools.probe(path)
            audio = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None)
            if not audio:
                self.audio_info_label.configure(text="No audio stream found.", text_color=DANGER)
                return
            duration = safe_float(probe.get("format", {}).get("duration"))
            channels = audio.get("channels", "?")
            codec = audio.get("codec_name", "?")
            rate = audio.get("sample_rate", "?")
            self.audio_info_label.configure(text=f"{codec}  •  {channels} channel(s)  •  {rate} Hz  •  {format_seconds(duration)}", text_color=MUTED)
            fmt = self.audio_format.get() if hasattr(self, "audio_format") else "MP3"
            ext = {"MP3": ".mp3", "M4A (AAC)": ".m4a", "WAV": ".wav"}[fmt]
            self._set_entry(self.audio_output_entry, str(Path(path).with_suffix(ext)))
        except Exception as exc:
            self.audio_info_label.configure(text="Could not inspect media.", text_color=DANGER)
            self.log(f"Audio probe failed: {exc}")

    def choose_audio_output(self):
        fmt = self.audio_format.get()
        ext = {"MP3": ".mp3", "M4A (AAC)": ".m4a", "WAV": ".wav"}[fmt]
        current = self.audio_output_entry.get().strip() or f"audio{ext}"
        path = filedialog.asksaveasfilename(
            title=f"Choose {fmt} output",
            initialfile=Path(current).name,
            defaultextension=ext,
            filetypes=[
                ("MP3 audio", "*.mp3"),
                ("M4A audio", "*.m4a"),
                ("WAV audio", "*.wav"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._set_entry(self.audio_output_entry, path)

    def start_audio(self):
        if self.busy:
            return
        if not self._ensure_tools():
            return
        source = self.audio_input_entry.get().strip().strip('"')
        output = self.audio_output_entry.get().strip()
        if not source or not os.path.isfile(source):
            messagebox.showerror("Extract Audio", "Choose a media file first.")
            return
        fmt = self.audio_format.get()
        if not output:
            ext = {"MP3": ".mp3", "M4A (AAC)": ".m4a", "WAV": ".wav"}.get(fmt, ".mp3")
            output = str(Path(source).with_suffix(ext))
            self._set_entry(self.audio_output_entry, output)
        if os.path.abspath(source) == os.path.abspath(output):
            messagebox.showerror("Output file", "Output must be different from the input file.")
            return

        if not self._confirm_overwrite(output):
            return
        self.last_output = output
        quality = self.mp3_quality.get()
        self.cancel_event = threading.Event()
        threading.Thread(target=self._audio_worker, args=(source, output, fmt, quality), daemon=True).start()

    def _audio_worker(self, source, output, fmt, quality):
        self.ui_queue.put(("busy", True))
        try:
            duration = MediaTools.duration(source)
            os.makedirs(str(Path(output).parent), exist_ok=True)
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-i", source,
                "-vn", "-map", "0:a:0",
            ]

            if fmt == "MP3":
                cmd += ["-codec:a", "libmp3lame"]
                if quality.startswith("V0"):
                    cmd += ["-q:a", "0"]
                elif quality.startswith("V2"):
                    cmd += ["-q:a", "2"]
                elif quality.startswith("V4"):
                    cmd += ["-q:a", "4"]
                else:
                    cmd += ["-b:a", "320k"]
            elif fmt == "M4A (AAC)":
                cmd += ["-codec:a", "aac", "-b:a", "256k"]
            else:
                cmd += ["-codec:a", "pcm_s16le"]

            cmd += ["-progress", "pipe:2", "-nostats", output]

            self.log(f"Extracting {fmt}…")
            MediaTools.run(
                cmd, log_callback=self.log,
                progress_callback=lambda current: self.ui_queue.put(("progress", min(current / max(duration, 1), 1), f"Encoding {fmt}…")),
                cancel_event=self.cancel_event,
                proc_callback=self._set_current_process,
            )
            self.log(f"Audio saved: {output}")
            self.ui_queue.put(("finished", "Audio extraction complete", f"Saved:\n{output}"))
        except Exception as exc:
            self.log(f"Audio extraction failed: {exc}")
            self.ui_queue.put(("error", "Audio extraction failed", str(exc)))
        finally:
            self.current_process = None
            self.ui_queue.put(("busy", False))

    # ------------------------- CANCEL -------------------------

    def _set_current_process(self, proc):
        """Track the live FFmpeg process so Cancel can terminate it immediately."""
        self.current_process = proc

    def cancel_operation(self):
        if not self.busy:
            return
        self.cancel_event.set()
        self.progress_label.configure(text="Cancelling…")
        self.log("Cancellation requested…")
        proc = self.current_process
        if proc is not None:
            try:
                proc.terminate()
                self.log("FFmpeg process terminated.")
            except OSError:
                pass


def main():
    app = ClipForge()
    app.mainloop()


if __name__ == "__main__":
    main()
