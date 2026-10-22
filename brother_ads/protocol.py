"""Brother scanner wire protocol (as used by brscan4 for the ADS-2x00 series).

Everything in this module was reconstructed from libsane-brother4.so 0.4.11
and brscan-skey-exe 0.3.4; see docs/PROTOCOL.md for the evidence trail.
This module is transport-free: it only builds commands and parses bytes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Union

BROTHER_VID = 0x04F9

# USB product IDs for the ADS family, taken from brscan4 models4/ext_*.ini.
KNOWN_MODELS = {
    0x60A0: "ADS-2000",
    0x60A1: "ADS-2100",
    0x60A4: "ADS-2500W",
    0x60A5: "ADS-2600W",
    0x037C: "ADS-2000e",
    0x037D: "ADS-2100e",
    0x037E: "ADS-2500We",
    0x037F: "ADS-2600We",
}

# --- vendor control requests (bmRequestType 0xC0) -------------------------
CTRL_REQ_OPEN = 0x01      # wValue=2, 5 byte reply: 05 10 01 <u16 status=0x0002>
CTRL_REQ_CLOSE = 0x02     # wValue=2, same reply layout
CTRL_REQ_KEYSTATUS = 0x03  # wValue=0, up to 255 bytes, scan-key event (brscan-skey)

# --- bulk commands ---------------------------------------------------------
CMD_QUERY_DEVICE = b"\x1bQ\n\x80"
CMD_QUERY_SKEY = b"\x1bK\n\x80"
CMD_QUERY_ADF = b"\x1bD\nADF\n\x80"
CMD_CANCEL = b"\x1bR"
CMD_NEXT_PAGE = b"\x1bX\n\x80"  # only for protocol <= 6


class ColorMode(enum.Enum):
    """Scan modes; value = (index used by brscan4, device M= keyword, jpeg)."""

    BW = (0, "TEXT", False)
    HALFTONE = (1, "ERRDIF", False)
    GRAY = (3, "GRAY64", False)
    COLOR = (5, "CGRAY", False)
    COLOR_JPEG = (7, "CGRAY", True)  # brscan4 "24bit Color[Fast]"

    @property
    def keyword(self) -> str:
        return self.value[1]

    @property
    def jpeg(self) -> bool:
        return self.value[2]

    @property
    def one_bit(self) -> bool:
        return self in (ColorMode.BW, ColorMode.HALFTONE)

    @property
    def color(self) -> bool:
        return self in (ColorMode.COLOR, ColorMode.COLOR_JPEG)


MODE_ALIASES = {
    "bw": ColorMode.BW,
    "lineart": ColorMode.BW,
    "halftone": ColorMode.HALFTONE,
    "gray": ColorMode.GRAY,
    "grey": ColorMode.GRAY,
    "color": ColorMode.COLOR,
    "colour": ColorMode.COLOR,
    "color-jpeg": ColorMode.COLOR_JPEG,
    "jpeg": ColorMode.COLOR_JPEG,
}


# --- key (button) events ----------------------------------------------------
class KeyFunction(enum.Enum):
    IMAGE = 3
    OCR = 2
    FILE = 5
    EMAIL = 8


def parse_key_status(buf: bytes) -> KeyFunction | None:
    """Decode the reply to CTRL_REQ_KEYSTATUS (brscan-skey check_skey)."""
    if len(buf) < 5 or buf[1] != 0x10 or buf[2] != 0x03 or buf[3] != 0x20:
        return None
    try:
        return KeyFunction(buf[4])
    except ValueError:
        return None


def check_ctrl_reply(buf: bytes) -> bool:
    """OpenDevice accepts only 05 10 01 02 00 (u16 status 2, bit 15 clear)."""
    return (
        len(buf) >= 5
        and buf[0] == 0x05
        and buf[1] == 0x10
        and buf[2] == 0x01
        and (buf[3] | buf[4] << 8) == 0x0002
    )


# --- Q: device info ---------------------------------------------------------
@dataclass
class DeviceInfo:
    raw: bytes
    protocol: int
    reso_table: int
    max_paper: int
    align: tuple[int, int, int, int]
    flag_e: bool  # adds "E=1" to the scan command (protocol > 8)
    flag_gl: bool  # adds "G=0\nL=0" to the scan command (protocol > 8)

    @property
    def extended_blocks(self) -> bool:
        return self.protocol > 6


def parse_device_info(buf: bytes) -> DeviceInfo:
    b = bytes(buf) + bytes(0x20)
    proto = b[3]
    align = (b[0x10], b[0x11], b[0x12], b[0x13]) if proto > 4 else (0, 0, 0, 0)
    return DeviceInfo(
        raw=bytes(buf),
        protocol=proto,
        reso_table=b[0x0E],
        max_paper=b[0x0F],
        align=align,
        flag_e=proto > 8 and b[0x16] == 1,
        flag_gl=proto > 8 and b[0x1D] == 1,
    )


# --- I: scan parameters -----------------------------------------------------
def build_query_scan_cmd(xres: int, yres: int, mode: ColorMode) -> bytes:
    return b"\x1bI\nR=%d,%d\nM=%s\n\x80" % (xres, yres, mode.keyword.encode())


@dataclass
class ScanInfo:
    xres: int
    yres: int
    planes: int
    width_01mm: int
    max_width_px: int
    height_01mm: int
    max_height_px: int


def parse_scan_info(buf: bytes, protocol: int) -> ScanInfo:
    """Parse "xres,yres,?,width_mm,max_px_x,height_mm,max_px_y" reply."""
    body = bytes(buf)[3 if protocol > 5 else 2 :]
    text = "".join(chr(c) if 0x20 <= c < 0x7F else "\0" for c in body)
    start = next((i for i, c in enumerate(text) if c.isdigit()), 0)
    text = text[start:].split("\0", 1)[0]
    vals = []
    for tok in text.split(","):
        tok = tok.strip()
        vals.append(int(tok) if tok.isdigit() else 0)
    vals += [0] * (7 - len(vals))
    if not vals[0] or not vals[1]:
        raise ValueError(f"unparseable I-reply: {bytes(buf)!r}")
    return ScanInfo(
        xres=vals[0],
        yres=vals[1],
        planes=vals[2],
        width_01mm=vals[3] * 10,
        max_width_px=vals[4],
        height_01mm=vals[5] * 10,
        max_height_px=vals[6],
    )


# --- X: start scan ----------------------------------------------------------
@dataclass
class ScanArea:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def compute_area(
    width_01mm: int,
    height_01mm: int,
    info: ScanInfo,
    centered: bool = True,
) -> ScanArea:
    """Replicates sub_1c001/margin_adjust, GetScanAreaParam and GetDeviceScanArea."""
    # margin_adjust(): brscan4 shrinks every edge by 2.0 mm, clamped at 0.
    left, top = 0, 0
    right = max(0, width_01mm - 20)
    bottom = max(0, height_01mm - 20)
    xr, yr = info.xres, info.yres
    l = left * xr // 254
    r = right * xr // 254
    t = top * yr // 254
    b = bottom * yr // 254
    max_x = info.max_width_px or r
    if r > max_x:
        r = max_x
    if r - l < 64:
        r = l + 64
    max_y = 17 * yr  # brscan4 caps ADF length at 17 in (32 in on some models)
    if b > max_y:
        b = max_y
    if b - t < 64:
        b = t + 64
    if centered:
        # MakeupScanStartCmd: centre the window inside the max feeder width.
        shift = max(0, (max_x - r) // 2)
        l += shift
        r += shift
    l = (l + 8) & 0xFFF0
    r = (r + 8) & 0xFFF0
    if r > max_x:
        r = max_x & 0xFFF0
    if r - l < 16:
        l = r - 16
    return ScanArea(l, t, r, b)


def build_start_cmd(
    info: ScanInfo,
    devinfo: DeviceInfo,
    mode: ColorMode,
    area: ScanArea,
    brightness: int = 0,
    contrast: int = 0,
    duplex: bool | None = None,
    compression: bool = True,
) -> bytes:
    """Same field order as MakeupScanStartCmd."""

    def pct(v: int) -> int:
        return max(0, min(100, v + 50))

    s = "\x1bX\nR=%d,%d\nM=%s\n" % (info.xres, info.yres, mode.keyword)
    if mode.jpeg:
        s += "C=JPEG\nJ=MID\n"
    else:
        s += "C=%s\n" % ("RLENGTH" if compression else "NONE")
    s += "B=%d\nN=%d\n" % (pct(brightness), pct(contrast))
    s += "A=%d,%d,%d,%d\n" % (area.left, area.top, area.right, area.bottom)
    if duplex is not None:
        s += "D=DUP\n" if duplex else "D=SIN\n"
    if devinfo.protocol > 8:
        s += "S=NORMAL_SCAN\nP=0\n"
        if devinfo.flag_e:
            s += "E=1\n"
        if devinfo.flag_gl:
            s += "G=0\nL=0\n"
    return s.encode("ascii") + b"\x80"


# --- image data stream --------------------------------------------------------
STATUS_TEXT = {
    0x80: "page end",
    0x81: "next page",
    0x82: "page end",
    0x83: "cancel acknowledge",
    0x84: "duplex normal",
    0x85: "duplex reverse",
    0x86: "page end",
    0xC2: "no document in feeder",
    0xC3: "document jam",
    0xC4: "cover open",
    0xE3: "cancel acknowledge",
    0xEA: "document jam",
}

ERROR_STATUS = {0xC2, 0xC3, 0xC4, 0xEA}


@dataclass
class DataBlock:
    page: int
    header: int
    payload: bytes


@dataclass
class WhiteLine:
    page: int


@dataclass
class SideMarker:
    page: int
    code: int  # 0x84 duplex normal, 0x85 duplex reverse


@dataclass
class PageEnd:
    page: int
    code: int
    last: bool = False  # simple protocol: 0x80 = no further page


@dataclass
class ScanEnd:
    pass


@dataclass
class DeviceStatus:
    code: int

    @property
    def message(self) -> str:
        return STATUS_TEXT.get(self.code, "service error 0x%02x" % self.code)

    @property
    def is_error(self) -> bool:
        return self.code in ERROR_STATUS or self.code not in STATUS_TEXT


Event = Union[DataBlock, WhiteLine, SideMarker, PageEnd, ScanEnd, DeviceStatus]


class StreamParser:
    """Incremental parser for the bulk-in image stream.

    Protocol <= 6 (ProcessMain):   H [u16 len] data      per line/plane
    Protocol  > 6 (make_cache_block):
        H [u16 L] ext[L] [u16 len] data   with ext[0:2] = page number
    """

    def __init__(self, extended: bool):
        self.extended = extended
        self.page = 1
        self._buf = bytearray()
        self.closed = False

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        out: list[Event] = []
        pos = 0
        buf = self._buf
        while not self.closed:
            res = self._next(buf, pos)
            if res is None:
                break
            ev, pos = res
            out.append(ev)
        del self._buf[:pos]
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)

    def _next(self, buf: bytearray, pos: int):
        n = len(buf) - pos
        if n < 1:
            return None
        h = buf[pos]
        if self.extended:
            return self._next_ext(buf, pos, n, h)
        return self._next_simple(buf, pos, n, h)

    def _next_simple(self, buf, pos, n, h):
        if h == 0x00:
            return WhiteLine(self.page), pos + 1
        if h in (0x84, 0x85):
            if n < 3:
                return None
            return SideMarker(self.page, h), pos + 3
        if h & 0x80:
            if h in (0x80, 0x81, 0x82, 0x86):
                ev = PageEnd(self.page, h, last=(h == 0x80))
                self.page += 1
                if h == 0x80:
                    self.closed = True
                return ev, pos + 1
            self.closed = h in ERROR_STATUS or h in (0x83, 0xE3)
            return DeviceStatus(h), pos + 1
        if n < 3:
            return None
        ln = buf[pos + 1] | buf[pos + 2] << 8
        if n < 3 + ln:
            return None
        return DataBlock(self.page, h, bytes(buf[pos + 3 : pos + 3 + ln])), pos + 3 + ln

    def _next_ext(self, buf, pos, n, h):
        if h == 0x80:
            self.closed = True
            return ScanEnd(), pos + 1
        if h > 0xC1:
            self.closed = True
            return DeviceStatus(h), pos + 1
        if n < 3:
            return None
        hl = buf[pos + 1] | buf[pos + 2] << 8
        if n < 3 + hl:
            return None
        ext = buf[pos + 3 : pos + 3 + hl]
        page = (ext[0] | ext[1] << 8) if hl >= 2 else self.page
        self.page = page
        end = pos + 3 + hl
        if h & 0x80:
            if h in (0x84, 0x85):
                return SideMarker(page, h), end
            if h in (0x81, 0x82, 0x83, 0x86):
                return PageEnd(page, h), end
            self.closed = True
            return DeviceStatus(h), end
        if n < 3 + hl + 2:
            return None
        ln = buf[end] | buf[end + 1] << 8
        if n < 3 + hl + 2 + ln:
            return None
        data_start = end + 2
        if (h & 0xC0) == 0:
            return WhiteLine(page), data_start + ln
        return DataBlock(page, h, bytes(buf[data_start : data_start + ln])), data_start + ln


def block_plane(header: int) -> int:
    """SetupImgLineProc: 1 = gray/mono, 2/3/4 = R/G/B."""
    return ((header & 0x1C) >> 2) + 1


def block_compressed(header: int) -> bool:
    return (header & 0x03) == 0x02


def unpackbits(data: bytes, expected: int | None = None) -> bytes:
    """PackBits as implemented in scandec.c (sub_1e4c6)."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if c < 0x80:
            cnt = c + 1
            out += data[i : i + cnt]
            i += cnt
        elif c > 0x80:
            if i >= n:
                break
            out += bytes((data[i],)) * (257 - c)
            i += 1
        if expected is not None and len(out) >= expected:
            break
    return bytes(out)
