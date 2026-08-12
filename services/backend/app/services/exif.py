"""Metadata stripping for the *curated* copy of an image.

The raw original is always retained untouched for authorized audit use; this
produces a separate, sanitised derivative with camera/GPS/comment metadata
removed. Implemented in pure Python so the container needs no image library.

Only container-level metadata segments are removed — pixel data is copied
byte-for-byte, so the derivative is visually identical.
"""

from __future__ import annotations

import struct

JPEG_SOI = b"\xff\xd8"
#: APP1 (Exif/XMP), APP2 (ICC/FlashPix), APP13 (IPTC/Photoshop), COM (comment).
JPEG_STRIP_MARKERS = frozenset({0xE1, 0xE2, 0xE13 & 0xFF, 0xED, 0xFE})
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_STRIP_CHUNKS = frozenset({b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"})


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Remove EXIF/XMP/IPTC/comment segments from a JPEG."""
    if not data.startswith(JPEG_SOI):
        return data
    out = bytearray(JPEG_SOI)
    index = 2
    length = len(data)
    while index + 4 <= length:
        if data[index] != 0xFF:
            # Desynchronised: copy the remainder verbatim rather than corrupt it.
            out.extend(data[index:])
            return bytes(out)
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out.extend(data[index : index + 2])
            index += 2
            continue
        if marker == 0xDA:  # start of scan: everything after this is entropy data
            out.extend(data[index:])
            return bytes(out)
        if index + 4 > length:
            break
        (segment_length,) = struct.unpack(">H", data[index + 2 : index + 4])
        segment_end = index + 2 + segment_length
        if segment_length < 2 or segment_end > length:
            out.extend(data[index:])
            return bytes(out)
        if marker not in JPEG_STRIP_MARKERS:
            out.extend(data[index:segment_end])
        index = segment_end
    return bytes(out)


def strip_png_metadata(data: bytes) -> bytes:
    """Remove textual and EXIF chunks from a PNG."""
    if not data.startswith(PNG_MAGIC):
        return data
    out = bytearray(PNG_MAGIC)
    index = len(PNG_MAGIC)
    length = len(data)
    while index + 8 <= length:
        (chunk_length,) = struct.unpack(">I", data[index : index + 4])
        chunk_type = data[index + 4 : index + 8]
        chunk_end = index + 12 + chunk_length  # length + type + data + crc
        if chunk_end > length:
            break
        if chunk_type not in PNG_STRIP_CHUNKS:
            out.extend(data[index:chunk_end])
        index = chunk_end
        if chunk_type == b"IEND":
            break
    return bytes(out)


def strip_metadata(data: bytes, mime_type: str | None) -> tuple[bytes, bool]:
    """Return ``(bytes, stripped)`` for the curated derivative."""
    if mime_type == "image/jpeg":
        cleaned = strip_jpeg_metadata(data)
        return cleaned, cleaned != data
    if mime_type == "image/png":
        cleaned = strip_png_metadata(data)
        return cleaned, cleaned != data
    # Other formats are copied unchanged; no derivative benefit is claimed.
    return data, False
