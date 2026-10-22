"""Offline tests against a simulated ADS scanner (no hardware needed)."""

import io
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image  # noqa: E402

from brother_ads import protocol as P  # noqa: E402
from brother_ads.output import PageWriter  # noqa: E402
from brother_ads.scanner import NoDocumentError, Scanner, ScanSettings  # noqa: E402
from brother_ads.usbio import DeviceLock  # noqa: E402


def packbits(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        run = 1
        while i + run < len(data) and run < 128 and data[i + run] == data[i]:
            run += 1
        if run > 1:
            out += bytes((257 - run, data[i]))
            i += run
        else:
            j = i
            while j < len(data) and j - i < 128 and not (
                j + 1 < len(data) and data[j + 1] == data[j]
            ):
                j += 1
            j = max(j, i + 1)
            out += bytes((j - i - 1,)) + data[i:j]
            i = j
    return bytes(out)


class Stream:
    def __init__(self, extended):
        self.extended = extended
        self.parts = []

    def data(self, page, header, payload):
        if self.extended:
            ext = struct.pack("<HBI", page, 0, 0)[:7].ljust(7, b"\0")
            self.parts.append(bytes((header,)) + struct.pack("<H", 7) + ext
                              + struct.pack("<H", len(payload)) + payload)
        else:
            self.parts.append(bytes((header,)) + struct.pack("<H", len(payload)) + payload)

    def page_end(self, page, code=0x81):
        if self.extended:
            self.parts.append(bytes((code,)) + struct.pack("<H", 7) + struct.pack("<H", page) + bytes(5))
        else:
            self.parts.append(bytes((code,)))

    def end(self):
        self.parts.append(b"\x80")

    def bytes(self):
        return b"".join(self.parts)


class FakeTransport:
    model = "ADS-2100e (fake)"

    def __init__(self, protocol, streams, skey=b"\x00\x00\x00D=SIN\n"):
        self.protocol = protocol
        self.streams = list(streams)  # one per X command
        self.skey = skey
        self.out = bytearray()
        self.written = []
        self.claimed = False

    def claim(self):
        self.claimed = True

    def release(self):
        self.claimed = False

    def session_open(self):
        assert self.claimed

    def session_close(self):
        pass

    def write(self, data, timeout_ms=2000):
        self.written.append(bytes(data))
        if data == P.CMD_QUERY_DEVICE:
            q = bytearray(0x20)
            q[0], q[1], q[3] = 0x20, 0x00, self.protocol
            self.out += q
        elif data == P.CMD_QUERY_SKEY:
            self.out += self.skey
        elif data.startswith(b"\x1bI\n"):
            pre = b"\x00\x1d\x00" + (b"\x00" if self.protocol > 5 else b"")
            self.out += pre + b"300,300,2,209,2480,291,3437,\x00"
        elif data.startswith(b"\x1bX\n"):
            self.out += self.streams.pop(0)
        return len(data)

    def read(self, size=0x4000, timeout_ms=2000):
        chunk = bytes(self.out[:size])
        del self.out[:size]
        return chunk


def area_width():
    si = P.ScanInfo(300, 300, 2, 2090, 2480, 2910, 3437)
    return P.compute_area(2100, 3556, si).width


class ProtocolTests(unittest.TestCase):
    def test_key_status(self):
        self.assertEqual(P.parse_key_status(bytes([0, 0x10, 0x03, 0x20, 0x05])), P.KeyFunction.FILE)
        self.assertEqual(P.parse_key_status(bytes([0, 0x10, 0x03, 0x20, 0x03])), P.KeyFunction.IMAGE)
        self.assertIsNone(P.parse_key_status(bytes([0, 0x10, 0x03, 0x00, 0x05])))

    def test_ctrl_reply(self):
        self.assertTrue(P.check_ctrl_reply(bytes([5, 0x10, 1, 2, 0])))
        self.assertFalse(P.check_ctrl_reply(bytes([5, 0x10, 1, 2, 0x80])))

    def test_start_command_layout(self):
        si = P.ScanInfo(300, 300, 2, 2090, 2480, 2910, 3437)
        dev = P.parse_device_info(bytes([0, 0, 0, 9]) + bytes(0x12) + b"\x01" + bytes(6) + b"\x01")
        area = P.compute_area(2100, 3556, si)
        cmd = P.build_start_cmd(si, dev, P.ColorMode.COLOR_JPEG, area, duplex=True)
        self.assertEqual(
            cmd,
            b"\x1bX\nR=300,300\nM=CGRAY\nC=JPEG\nJ=MID\nB=50\nN=50\nA=16,0,2464,4176\n"
            b"D=DUP\nS=NORMAL_SCAN\nP=0\nE=1\nG=0\nL=0\n\x80",
        )

    def test_unpackbits_roundtrip(self):
        data = bytes(range(50)) + b"\xff" * 300 + b"ab" * 20
        self.assertEqual(P.unpackbits(packbits(data)), data)

    def test_parser_handles_split_chunks(self):
        s = Stream(True)
        s.data(1, 0x40, b"\x10" * 10)
        s.page_end(1)
        s.end()
        raw = s.bytes()
        p = P.StreamParser(True)
        events = []
        for i in range(len(raw)):
            events += p.feed(raw[i : i + 1])
        self.assertEqual([type(e).__name__ for e in events], ["DataBlock", "PageEnd", "ScanEnd"])


class ScanTests(unittest.TestCase):
    def test_gray_duplex_interleaved_packbits(self):
        w = area_width()
        s = Stream(True)
        for y in range(20):  # front (1) and back (2) interleaved, like make_cache_block expects
            s.data(1, 0x42, packbits(bytes([y]) * w))
            s.data(2, 0x40, bytes([200 - y]) * w)
        s.page_end(1)
        s.page_end(2)
        s.end()
        t = FakeTransport(9, [s.bytes()])
        pages = Scanner(t).scan(ScanSettings(mode=P.ColorMode.GRAY, duplex=True))
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].image.size, (w, 20))
        self.assertEqual(pages[0].image.getpixel((5, 7)), 7)
        self.assertEqual(pages[1].image.getpixel((5, 7)), 193)
        x = [c for c in t.written if c.startswith(b"\x1bX")][0]
        self.assertIn(b"M=GRAY64\nC=RLENGTH\n", x)
        self.assertIn(b"D=DUP\n", x)

    def test_color_planes(self):
        w = area_width()
        s = Stream(True)
        for y in range(5):
            s.data(1, 0x44, bytes([10]) * w)
            s.data(1, 0x4A, packbits(bytes([20]) * w))
            s.data(1, 0x4C, bytes([30]) * w)
        s.page_end(1)
        s.end()
        pages = Scanner(FakeTransport(9, [s.bytes()])).scan(ScanSettings(mode=P.ColorMode.COLOR))
        self.assertEqual(pages[0].image.mode, "RGB")
        self.assertEqual(pages[0].image.getpixel((0, 4)), (10, 20, 30))

    def test_jpeg_passthrough(self):
        buf = io.BytesIO()
        Image.new("RGB", (64, 32), (1, 2, 3)).save(buf, "JPEG")
        jpg = buf.getvalue()
        s = Stream(True)
        for i in range(0, len(jpg), 100):
            s.data(1, 0x64, jpg[i : i + 100])
        s.page_end(1)
        s.end()
        pages = Scanner(FakeTransport(9, [s.bytes()])).scan(ScanSettings())
        self.assertEqual(pages[0].jpeg, jpg)
        self.assertEqual(pages[0].image.size, (64, 32))

    def test_simple_protocol_bw_two_pages(self):
        w = area_width()
        bpl = (w + 7) // 8
        streams = []
        for n in range(2):
            s = Stream(False)
            s.data(0, 0x40, b"\xff" * bpl)  # all black
            s.parts.append(b"\x00")  # white line
            s.page_end(0, 0x81 if n == 0 else 0x80)
            streams.append(s.bytes())
        t = FakeTransport(4, streams)
        pages = Scanner(t).scan(ScanSettings(mode=P.ColorMode.BW))
        self.assertEqual(len(pages), 2)
        img = pages[1].image
        self.assertEqual((img.mode, img.size), ("1", (w, 2)))
        self.assertEqual(img.getpixel((3, 0)), 0)
        self.assertEqual(img.getpixel((3, 1)), 255)
        self.assertIn(P.CMD_NEXT_PAGE, t.written)

    def test_no_document(self):
        t = FakeTransport(9, [b"\xc2"])
        with self.assertRaises(NoDocumentError):
            Scanner(t).scan(ScanSettings())

    def test_device_duplex_setting(self):
        s = Stream(True)
        s.end()
        t = FakeTransport(9, [s.bytes()], skey=b"\x00\x00\x00D=DUP\n")
        Scanner(t).scan(ScanSettings(duplex=None))
        self.assertIn(b"D=DUP\n", [c for c in t.written if c.startswith(b"\x1bX")][0])

    def test_pdf_writer(self):
        w = area_width()
        s = Stream(True)
        for y in range(100):
            s.data(1, 0x40, bytes([128]) * w)
        s.page_end(1)
        s.end()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "x.pdf")
            writer = PageWriter(out, "pdf")
            Scanner(FakeTransport(9, [s.bytes()])).scan(ScanSettings(mode=P.ColorMode.GRAY), on_page=writer)
            self.assertEqual(writer.finish(), [out])
            with open(out, "rb") as f:
                self.assertTrue(f.read(5).startswith(b"%PDF"))


class LockTests(unittest.TestCase):
    def test_cross_process_lock(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dev.lock")
            cli, daemon = DeviceLock(path), DeviceLock(path)  # separate fds behave like processes
            self.assertTrue(cli.acquire(0))
            self.assertTrue(cli.acquire(0))  # re-entrant inside one owner
            self.assertFalse(daemon.acquire(0))  # button poll skipped while scanning
            cli.release()
            self.assertFalse(daemon.acquire(0.3))
            cli.release()
            self.assertTrue(daemon.acquire(0))
            daemon.release()
            self.assertFalse(daemon.held)


if __name__ == "__main__":
    unittest.main()
