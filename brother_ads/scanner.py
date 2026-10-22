"""High-level scan session (ScanStart / PageScan / ScanEnd equivalent)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator

from PIL import Image

from . import protocol as P
from .pages import Page, PageBuilder

log = logging.getLogger(__name__)

# Paper widths/heights in 0.1 mm. The feeder does not stop at the trailing
# edge but scans the full requested height, so this must match the paper.
# "max" and "legal" are the longest area the device accepts.
PAPER_SIZES = {
    "a4": (2100, 2970),
    "a5": (1480, 2100),
    "a6": (1050, 1480),
    "letter": (2159, 2794),
    "legal": (2159, 3556),
    "b5": (1820, 2570),
    "card": (900, 1500),
    "max": (2159, 3556),
}

SUPPORTED_RESOLUTIONS = (100, 150, 200, 300, 400, 600)


class ScanError(RuntimeError):
    pass


class NoDocumentError(ScanError):
    pass


@dataclass
class ScanSettings:
    mode: P.ColorMode = P.ColorMode.COLOR_JPEG
    resolution: int = 300
    duplex: bool | None = False  # None = use the device's scan-key setting
    width_01mm: int = 2100
    height_01mm: int = 2970
    centered: bool = True
    brightness: int = 0  # -50 .. +50
    contrast: int = 0  # -50 .. +50
    compression: bool = True
    back_rotate: int = 0  # degrees applied to duplex back sides (even pages)
    idle_timeout: float = 30.0

    @classmethod
    def paper(cls, name: str, **kw) -> "ScanSettings":
        name = name.lower()
        if "x" in name:
            w, h = (float(v) for v in name.split("x", 1))
            size = (int(w * 10), int(h * 10))
        else:
            size = PAPER_SIZES[name]
        return cls(width_01mm=size[0], height_01mm=size[1], **kw)


class Scanner:
    """One scanner; the lock serialises scans and key polling."""

    def __init__(self, transport, raw_dump: BinaryIO | None = None):
        self.t = transport
        self.lock = threading.RLock()
        self.raw_dump = raw_dump
        self.device_info: P.DeviceInfo | None = None
        self._cancel = threading.Event()
        self._after_q = False

    # --- session -----------------------------------------------------------
    def _open(self) -> None:
        self.t.claim()
        try:
            self.t.session_open()
            self._drain(1.0)
        except Exception:
            self.t.release()
            raise

    def _close(self) -> None:
        try:
            self.t.session_close()
        finally:
            self.t.release()

    def _drain(self, quiet_s: float) -> int:
        """OpenDevice recovery / ReadTrash: read until the pipe is quiet."""
        total = 0
        deadline = time.monotonic() + quiet_s
        while time.monotonic() < deadline:
            chunk = self.t.read(timeout_ms=200)
            if chunk:
                total += len(chunk)
                deadline = time.monotonic() + quiet_s
        if total:
            log.debug("discarded %d stale bytes", total)
        return total

    def _command(self, cmd: bytes, timeout: float = 5.0, retry: bool = True) -> bytes:
        """WriteDeviceCommand + ReadNonFixedData (first non-empty reply)."""
        for attempt in range(2 if retry else 1):
            self.t.write(cmd)
            # The ADS-2100e swallows the first command after a Q query, so
            # that one is retried after a short wait instead of the full timeout.
            wait = 0.3 if self._after_q and attempt == 0 and retry else timeout
            self._after_q = False
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                data = self.t.read(timeout_ms=500)
                if data:
                    log.debug("cmd %r -> %r", cmd[:16], data[:64])
                    return data
                time.sleep(0.02)
            log.debug("no reply to %r (attempt %d)", cmd[:16], attempt + 1)
        raise ScanError("Keine Antwort auf Befehl %r" % cmd[:8])

    # --- queries -----------------------------------------------------------
    def _query_device(self) -> P.DeviceInfo:
        info = P.parse_device_info(self._command(P.CMD_QUERY_DEVICE))
        self._after_q = True
        self.device_info = info
        log.info("Gerät: Protokoll %d, Q=%s", info.protocol, info.raw.hex())
        return info

    def _query_skey(self, info: P.DeviceInfo) -> str:
        if info.protocol <= 8:
            return ""
        try:
            raw = self._command(P.CMD_QUERY_SKEY, retry=False)
        except ScanError:
            return ""
        return raw[3:].decode("latin-1", "replace") if len(raw) > 3 else ""

    def _doc_in_adf(self) -> bool:
        raw = self._command(P.CMD_QUERY_ADF)
        return bool(raw) and raw[0] == 0x80

    def info(self) -> dict:
        with self.lock:
            self._open()
            try:
                dev = self._query_device()
                skey = self._query_skey(dev)
                si = P.parse_scan_info(
                    self._command(P.build_query_scan_cmd(300, 300, P.ColorMode.COLOR)),
                    dev.protocol,
                )
                try:
                    adf = self._doc_in_adf()
                except ScanError:
                    adf = None
            finally:
                self._close()
        return {
            "model": getattr(self.t, "model", "?"),
            "protocol": dev.protocol,
            "q_reply": dev.raw.hex(),
            "skey_settings": skey,
            "scan_info_300dpi_color": si,
            "document_in_adf": adf,
        }

    def cancel(self) -> None:
        self._cancel.set()

    # --- scanning ----------------------------------------------------------
    def scan(
        self,
        s: ScanSettings,
        on_page: Callable[[Page], None] | None = None,
    ) -> list[Page]:
        pages = []
        for page in self.iter_scan(s):
            if on_page:
                on_page(page)
            else:
                pages.append(page)
        return pages

    def iter_scan(self, s: ScanSettings) -> Iterator[Page]:
        if s.resolution not in SUPPORTED_RESOLUTIONS:
            raise ScanError("Auflösung %d nicht unterstützt %s" % (s.resolution, SUPPORTED_RESOLUTIONS))
        self._cancel.clear()
        with self.lock:
            self._open()
            try:
                yield from self._scan_session(s)
            finally:
                self._close()

    def _scan_session(self, s: ScanSettings) -> Iterator[Page]:
        dev = self._query_device()
        duplex = s.duplex
        skey = self._query_skey(dev) if duplex is None else ""
        if duplex is None:
            duplex = "D=DUP" in skey
            log.info("Duplex aus Geräte-Tasteneinstellung: %s", duplex)
        si = P.parse_scan_info(
            self._command(P.build_query_scan_cmd(s.resolution, s.resolution, s.mode)),
            dev.protocol,
        )
        log.info("I-Antwort: %s", si)
        area = P.compute_area(s.width_01mm, s.height_01mm, si, s.centered)
        cmd = P.build_start_cmd(
            si, dev, s.mode, area, s.brightness, s.contrast, duplex, s.compression
        )
        log.info("Scanbefehl: %r", cmd)

        parser = P.StreamParser(extended=dev.extended_blocks)
        builders: dict[int, PageBuilder] = {}
        width = area.width

        def builder(n: int) -> PageBuilder:
            if n not in builders:
                builders[n] = PageBuilder(n, s.mode, width, si.xres, si.yres)
            return builders[n]

        def finish(n: int) -> Page | None:
            b = builders.pop(n, None)
            if b is None:
                return None
            page = b.finish()
            if page is None:
                return None
            return self._postprocess(page, s, duplex)

        self.t.write(cmd)
        last_data = time.monotonic()
        done = False
        while not done:
            if self._cancel.is_set():
                self._abort()
                raise ScanError("Scan abgebrochen")
            chunk = self.t.read(timeout_ms=1000)
            if not chunk:
                if time.monotonic() - last_data > s.idle_timeout:
                    self._abort()
                    raise ScanError("Zeitüberschreitung beim Lesen der Bilddaten")
                continue
            last_data = time.monotonic()
            if self.raw_dump:
                self.raw_dump.write(chunk)
            for ev in parser.feed(chunk):
                if isinstance(ev, P.DataBlock):
                    builder(ev.page).add_block(ev.header, ev.payload)
                elif isinstance(ev, P.WhiteLine):
                    builder(ev.page).add_white_line()
                elif isinstance(ev, P.SideMarker):
                    builder(ev.page).side_marker = ev.code
                elif isinstance(ev, P.PageEnd):
                    page = finish(ev.page)
                    if page is not None:
                        yield page
                    if not dev.extended_blocks:
                        if ev.last:
                            done = True
                        else:
                            self.t.write(P.CMD_NEXT_PAGE)
                elif isinstance(ev, P.ScanEnd):
                    done = True
                elif isinstance(ev, P.DeviceStatus):
                    if ev.code == 0xC2 and not any(b.rows or b.jpeg for b in builders.values()):
                        raise NoDocumentError("Kein Papier im Einzug")
                    if ev.is_error:
                        raise ScanError("Scanner meldet: %s" % ev.message)
                    done = True
        for n in sorted(builders):
            page = finish(n)
            if page is not None:
                yield page

    def _abort(self) -> None:
        try:
            self.t.write(P.CMD_CANCEL)
        except Exception:
            pass
        self._drain(1.0)

    @staticmethod
    def _postprocess(page: Page, s: ScanSettings, duplex: bool) -> Page:
        img = page.image
        if page.dpi != (s.resolution, s.resolution):
            w = round(img.width * s.resolution / page.dpi[0])
            h = round(img.height * s.resolution / page.dpi[1])
            img = img.resize((w, h), Image.LANCZOS if img.mode != "1" else Image.NEAREST)
            page.jpeg = None
            page.dpi = (s.resolution, s.resolution)
        if duplex and s.back_rotate and page.number % 2 == 0:
            img = img.rotate(s.back_rotate, expand=True)
            page.jpeg = None
        page.image = img
        return page
