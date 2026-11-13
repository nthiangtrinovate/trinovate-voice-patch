"""Server-side video validation: size + duration.

No ffmpeg/ffprobe dependency is assumed to exist on the deployment host
(the Mac mini worker environment notes in PLAN.md confirm ffmpeg is not
preinstalled there, and we shouldn't assume the portal's own host has it
either). Instead we parse the ISO base media file format (MP4/MOV, i.e.
QuickTime-family containers — what browsers produce for MediaRecorder
output and what phones/cameras export) directly to find the `mvhd`
(movie header) atom and compute duration = duration / timescale.

This covers the expected upload format. Anything we can't parse is
rejected with a clear, actionable error rather than silently accepted or
mis-measured. If the deployment host later gains ffprobe, this can be
swapped for a subprocess call without changing the calling contract
(`probe_duration_seconds` raises `UnsupportedMediaError` either way).
"""
from __future__ import annotations

import struct
from pathlib import Path


class UnsupportedMediaError(Exception):
    """Raised when the file's duration can't be determined safely."""


def _iter_boxes(f, end: int):
    while f.tell() < end:
        start = f.tell()
        header = f.read(8)
        if len(header) < 8:
            return
        size, box_type = struct.unpack(">I4s", header)
        box_type = box_type.decode("latin-1")
        if size == 1:
            # 64-bit extended size
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_len = 16
        elif size == 0:
            # Box extends to end of file (rare, usually only for top-level mdat)
            size = end - start
            header_len = 8
        else:
            header_len = 8
        yield box_type, start, size, header_len
        f.seek(start + size)


def _find_box(f, box_type_wanted: str, start: int, size: int, header_len: int, max_depth: int = 6):
    """Depth-first search for a box type within [start, start+size)."""
    f.seek(start + header_len)
    end = start + size
    for box_type, bstart, bsize, bheader_len in _iter_boxes(f, end):
        if box_type == box_type_wanted:
            return bstart, bsize, bheader_len
        if max_depth > 0 and box_type in ("moov", "trak", "mdia", "minf", "udta"):
            found = _find_box(f, box_type_wanted, bstart, bsize, bheader_len, max_depth - 1)
            if found:
                return found
    return None


def probe_duration_seconds(path: Path) -> float:
    """Parse an MP4/MOV container and return duration in seconds.

    Raises UnsupportedMediaError if the file isn't a parseable ISO-BMFF
    container or has no usable mvhd atom.
    """
    file_size = path.stat().st_size
    if file_size < 16:
        raise UnsupportedMediaError("file too small to be a valid video container")

    try:
        with open(path, "rb") as f:
            # Verify there's at least an ftyp or moov box near the start.
            top_level = list(_iter_boxes(f, file_size))
            if not any(bt in ("ftyp", "moov", "mdat", "wide", "free", "skip") for bt, *_ in top_level):
                raise UnsupportedMediaError(
                    "not a recognized MP4/MOV container (no ftyp/moov box found)"
                )

            f.seek(0)
            moov = None
            for box_type, bstart, bsize, bheader_len in _iter_boxes(f, file_size):
                if box_type == "moov":
                    moov = (bstart, bsize, bheader_len)
                    break
            if moov is None:
                raise UnsupportedMediaError(
                    "no 'moov' atom found; file may be fragmented/streaming MP4 "
                    "or not fully written"
                )

            mvhd = _find_box(f, "mvhd", *moov)
            if mvhd is None:
                raise UnsupportedMediaError("no 'mvhd' atom found inside moov")

            mstart, msize, mheader_len = mvhd
            f.seek(mstart + mheader_len)
            version = f.read(1)[0]
            f.read(3)  # flags
            if version == 1:
                f.read(8 + 8)  # creation/modification time (64-bit)
                timescale = struct.unpack(">I", f.read(4))[0]
                duration = struct.unpack(">Q", f.read(8))[0]
            else:
                f.read(4 + 4)  # creation/modification time (32-bit)
                timescale = struct.unpack(">I", f.read(4))[0]
                duration = struct.unpack(">I", f.read(4))[0]

            if timescale == 0:
                raise UnsupportedMediaError("mvhd timescale is zero")
            return duration / timescale
    except UnsupportedMediaError:
        raise
    except (struct.error, OSError) as exc:
        raise UnsupportedMediaError(f"failed to parse container: {exc}") from exc
