"""Turn decoded stream blocks into page images."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from PIL import Image

from .protocol import ColorMode, block_compressed, block_plane, unpackbits

log = logging.getLogger(__name__)


def _fast_unpackbits(data: bytes, expected: int) -> bytes:
    """Pillow's C PackBits decoder, falling back to the pure Python one."""
    try:
        return Image.frombytes("L", (expected, 1), data, "packbits", "L").tobytes()
    except Exception:
        return unpackbits(data, expected)


@dataclass
class Page:
    number: int
    image: Image.Image
    jpeg: bytes | None = None  # original JPEG stream (no recompression)
    side_marker: int | None = None
    dpi: tuple[int, int] = (300, 300)


@dataclass
class PageBuilder:
    number: int
    mode: ColorMode
    width_px: int
    xres: int
    yres: int
    rows: list = field(default_factory=list)
    jpeg: bytearray = field(default_factory=bytearray)
    side_marker: int | None = None
    _rgb: list = field(default_factory=lambda: [None, None, None])

    @property
    def bytes_per_line(self) -> int:
        if self.mode.one_bit:
            return (self.width_px + 7) // 8
        return self.width_px

    @property
    def white(self) -> int:
        return 0x00 if self.mode.one_bit else 0xFF

    def _fit(self, line: bytes) -> bytes:
        bpl = self.bytes_per_line
        if len(line) == bpl:
            return line
        if len(line) > bpl:
            return line[:bpl]
        return line + bytes((self.white,)) * (bpl - len(line))

    def add_white_line(self) -> None:
        if self.mode.jpeg:
            return
        line = bytes((self.white,)) * self.bytes_per_line
        if self.mode.color:
            self._flush_rgb()
            self.rows.append(line * 3)
        else:
            self.rows.append(line)

    def add_block(self, header: int, payload: bytes) -> None:
        if self.mode.jpeg:
            self.jpeg += payload
            return
        if block_compressed(header):
            line = _fast_unpackbits(payload, self.bytes_per_line)
        else:
            line = payload
        line = self._fit(line)
        plane = block_plane(header)
        if not self.mode.color:
            self.rows.append(line)
            return
        idx = plane - 2
        if not 0 <= idx <= 2:
            # gray block inside a colour scan: replicate into all channels
            self._flush_rgb()
            self.rows.append(self._interleave(line, line, line))
            return
        if self._rgb[idx] is not None:
            self._flush_rgb()
        self._rgb[idx] = line
        if all(p is not None for p in self._rgb):
            self.rows.append(self._interleave(*self._rgb))
            self._rgb = [None, None, None]

    def _flush_rgb(self) -> None:
        if any(p is not None for p in self._rgb):
            white = bytes((0xFF,)) * self.bytes_per_line
            r, g, b = (p if p is not None else white for p in self._rgb)
            self.rows.append(self._interleave(r, g, b))
            self._rgb = [None, None, None]

    @staticmethod
    def _interleave(r: bytes, g: bytes, b: bytes) -> bytes:
        out = bytearray(len(r) * 3)
        out[0::3] = r
        out[1::3] = g
        out[2::3] = b
        return bytes(out)

    def finish(self) -> Page | None:
        if self.mode.jpeg:
            if not self.jpeg:
                return None
            data = bytes(self.jpeg)
            img = Image.open(io.BytesIO(data))
            img.load()
            return Page(self.number, img, jpeg=data, side_marker=self.side_marker,
                        dpi=(self.xres, self.yres))
        self._flush_rgb()
        if not self.rows:
            return None
        h = len(self.rows)
        data = b"".join(self.rows)
        if self.mode.one_bit:
            # Brother sends 1 = black; PIL mode "1" uses 1 = white.
            img = Image.frombytes("1", (self.bytes_per_line * 8, h), data, "raw", "1;I")
            img = img.crop((0, 0, self.width_px, h))
        elif self.mode.color:
            img = Image.frombytes("RGB", (self.width_px, h), data)
        else:
            img = Image.frombytes("L", (self.width_px, h), data)
        self.rows = []
        return Page(self.number, img, side_marker=self.side_marker, dpi=(self.xres, self.yres))
