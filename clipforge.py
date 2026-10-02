#!/usr/bin/env python3
"""ClipForge v1.2 — temporary bootstrap.

The full fixed clipforge.py could not be pushed in one step due to tool size limits.
Please replace this file with the fixed version from the chat artifacts:

  Download: clipforge.py (v1.2, ~90 KB) from the conversation
  Then: copy it over this file and run again.

Changes in v1.2:
  - Direct stream-copy when only 1 kept segment (0 temp space)
  - Temp dir on output volume (not system TEMP)
  - .mkv intermediates + genpts concat
  - Instant cancel via process.terminate()
  - Pad filter even offsets
  - Audio default extension follows format
"""
import sys
sys.stderr.write(
    "ClipForge: full v1.2 file not yet installed.\n"
    "Download the fixed clipforge.py from the chat artifacts and replace this file.\n"
)
sys.exit(1)
