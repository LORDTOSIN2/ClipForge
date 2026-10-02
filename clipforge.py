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
APP_VERSION = "1.2"

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
