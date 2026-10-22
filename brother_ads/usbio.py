"""libusb transport (pyusb), mirroring devaccs.c from brscan4."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import time

from .protocol import (
    BROTHER_VID,
    CTRL_REQ_CLOSE,
    CTRL_REQ_KEYSTATUS,
    CTRL_REQ_OPEN,
    KNOWN_MODELS,
    check_ctrl_reply,
)

log = logging.getLogger(__name__)

try:
    import usb.core
    import usb.util
except ImportError:  # pragma: no cover - only hit when pyusb is missing
    usb = None


class TransportError(RuntimeError):
    pass


class DeviceBusy(TransportError):
    """Another process (CLI scan or the button daemon) is using the scanner."""


class DeviceGone(TransportError):
    """The scanner was unplugged or switched off."""


_TIMEOUT_ERRNOS = (errno.ETIMEDOUT, 60)  # 60 = ETIMEDOUT on macOS


def _is_timeout(e) -> bool:
    return isinstance(e, getattr(usb.core, "USBTimeoutError", ())) or getattr(e, "errno", None) in _TIMEOUT_ERRNOS


def _map_error(e, what: str) -> TransportError:
    if getattr(e, "errno", None) in (errno.ENODEV, errno.ENOENT) or "No such device" in str(e):
        return DeviceGone("Scanner getrennt (%s)" % what)
    return TransportError("%s fehlgeschlagen: %s" % (what, e))


def lock_dir() -> str:
    for d in (os.environ.get("BROTHER_ADS_LOCKDIR"), "/run/lock", "/var/lock"):
        if d and os.access(d, os.W_OK | os.X_OK):
            return d
    import tempfile

    return tempfile.gettempdir()


class DeviceLock:
    """Cross-process exclusive access via flock().

    brscan4 and brscan-skey only serialise inside their own process, so a
    button poll can hit the device in the middle of a SANE scan. Every user of
    the scanner here (CLI, daemon) takes this lock first instead. The kernel
    drops it automatically when a process dies, so it can never go stale.
    """

    def __init__(self, path: str):
        self.path = path
        self._fd = None
        self._depth = 0

    def _open(self) -> int:
        # O_RDONLY is enough for flock and works on a lock file another user
        # created; O_CREAT on such a file is refused under fs.protected_regular.
        while True:
            try:
                return os.open(self.path, os.O_RDONLY)
            except FileNotFoundError:
                pass
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o644)
                return fd
            except FileExistsError:
                continue

    def acquire(self, wait: float | None = None) -> bool:
        """wait=None blocks forever, 0 tries once; returns False when busy."""
        if self._depth:
            self._depth += 1
            return True
        fd = self._open()
        deadline = None if wait is None else time.monotonic() + wait
        announced = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                if not announced and wait != 0:
                    log.info("Scanner belegt – warte auf Freigabe ...")
                    announced = True
                time.sleep(0.2)
        self._fd = fd
        self._depth = 1
        return True

    def release(self) -> None:
        if not self._depth:
            return
        self._depth -= 1
        if not self._depth:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._depth > 0


def _require_pyusb():
    if usb is None:
        raise TransportError("pyusb fehlt: sudo apt install python3-usb")


def find_devices(all_brother: bool = False):
    """Return (usb.core.Device, model name) for every attached scanner."""
    _require_pyusb()
    found = []
    try:
        devices = list(usb.core.find(find_all=True, idVendor=BROTHER_VID))
    except usb.core.NoBackendError as e:
        raise TransportError("libusb fehlt: sudo apt install libusb-1.0-0") from e
    for dev in devices:
        name = KNOWN_MODELS.get(dev.idProduct)
        if name is None and not all_brother:
            continue
        found.append((dev, name or "Brother 0x%04x" % dev.idProduct))
    return found


class UsbTransport:
    """Bulk/control access to the vendor-specific scanner interface."""

    READ_CHUNK = 0x4000  # ReadDeviceData clamps USB reads to 16 KiB

    def __init__(self, dev, lock_wait: float | None = 600.0):
        _require_pyusb()
        self.dev = dev
        self.lock = DeviceLock(
            os.path.join(lock_dir(), "brother-ads-%03d-%03d.lock" % (dev.bus or 0, dev.address or 0))
        )
        self.lock_wait = lock_wait
        self.intf = None
        self.ep_in = None
        self.ep_out = None
        self._claimed = False
        self._detached = False

    @classmethod
    def first(cls, product_id: int | None = None) -> "UsbTransport":
        devs = find_devices()
        if product_id is not None:
            devs = [d for d in devs if d[0].idProduct == product_id]
        if not devs:
            raise TransportError("Kein Brother ADS-Scanner per USB gefunden")
        return cls(devs[0][0])

    @property
    def model(self) -> str:
        return KNOWN_MODELS.get(self.dev.idProduct, "0x%04x" % self.dev.idProduct)

    def _select_interface(self):
        cfg = self.dev.get_active_configuration()
        candidates = [
            i
            for i in cfg
            if i.bInterfaceClass == 0xFF
            and i.bInterfaceSubClass == 0xFF
            and i.bInterfaceProtocol == 0xFF
        ]
        if not candidates:
            raise TransportError("Keine Vendor-Scanner-Schnittstelle (ff/ff/ff) gefunden")
        # brscan4 hard-codes interface 1; prefer it when present.
        candidates.sort(key=lambda i: (i.bInterfaceNumber != 1, i.bInterfaceNumber))
        intf = candidates[0]
        ep_in = ep_out = None
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                ep_in = ep_in or ep
            else:
                ep_out = ep_out or ep
        if ep_in is None or ep_out is None:
            raise TransportError("Bulk-Endpunkte nicht gefunden")
        return intf, ep_in, ep_out

    def ensure_configured(self):
        try:
            self.dev.get_active_configuration()
        except usb.core.USBError:
            self.dev.set_configuration(1)

    def claim(self):
        if not self.lock.acquire(self.lock_wait):
            raise DeviceBusy("Scanner wird von einem anderen Prozess benutzt")
        try:
            self.ensure_configured()
            self.intf, self.ep_in, self.ep_out = self._select_interface()
            num = self.intf.bInterfaceNumber
            try:
                if self.dev.is_kernel_driver_active(num):
                    self.dev.detach_kernel_driver(num)
                    self._detached = True
            except (NotImplementedError, usb.core.USBError):
                pass
            usb.util.claim_interface(self.dev, num)
        except usb.core.USBError as e:
            self.lock.release()
            raise _map_error(e, "Interface belegen") from e
        except BaseException:
            self.lock.release()
            raise
        self._claimed = True
        log.debug(
            "interface %d claimed, in=0x%02x out=0x%02x",
            num,
            self.ep_in.bEndpointAddress,
            self.ep_out.bEndpointAddress,
        )

    def release(self):
        if self._claimed:
            try:
                usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
            except usb.core.USBError:
                pass
            self._claimed = False
        if self._detached:
            try:
                self.dev.attach_kernel_driver(self.intf.bInterfaceNumber)
            except (NotImplementedError, usb.core.USBError):
                pass
            self._detached = False
        try:
            usb.util.dispose_resources(self.dev)
        finally:
            self.lock.release()

    # --- control ----------------------------------------------------------
    def control_in(self, request: int, value: int, length: int, timeout_ms: int = 2000) -> bytes:
        return bytes(self.dev.ctrl_transfer(0xC0, request, value, 0, length, timeout=timeout_ms))

    def session_open(self) -> None:
        """OpenDevice(): up to 5 tries of request 1, closing in between."""
        last = b""
        for _ in range(5):
            try:
                last = self.control_in(CTRL_REQ_OPEN, 2, 5)
            except usb.core.USBError as e:
                last = repr(e).encode()
            if check_ctrl_reply(last):
                return
            try:
                self.control_in(CTRL_REQ_CLOSE, 2, 5)
            except usb.core.USBError:
                pass
            time.sleep(0.5)
        raise TransportError("Scanner verweigert Öffnen (Antwort %r)" % last)

    def session_close(self) -> None:
        for _ in range(5):
            try:
                self.control_in(CTRL_REQ_CLOSE, 2, 5)
                return
            except usb.core.USBError:
                time.sleep(0.1)

    def key_status(self, timeout_ms: int = 2000) -> bytes:
        return self.control_in(CTRL_REQ_KEYSTATUS, 0, 0xFF, timeout_ms)

    def poll_key(self, timeout_ms: int = 2000) -> bytes | None:
        """One button poll, only while nobody else uses the scanner.

        Returns None if another process holds the device or the request timed
        out (some firmwares hold the request until a key is pressed).
        """
        if not self.lock.acquire(wait=0):
            return None
        try:
            return self.key_status(timeout_ms)
        except usb.core.USBError as e:
            if _is_timeout(e):
                return None
            raise _map_error(e, "Tastenabfrage") from e
        finally:
            self.lock.release()

    # --- bulk -------------------------------------------------------------
    def write(self, data: bytes, timeout_ms: int = 2000) -> int:
        err = None
        for _ in range(5):
            try:
                return self.ep_out.write(data, timeout=timeout_ms)
            except usb.core.USBError as e:
                err = e
                if not _is_timeout(e):
                    break
        raise _map_error(err, "Schreiben")

    def read(self, size: int = READ_CHUNK, timeout_ms: int = 2000) -> bytes:
        """One bulk read; returns b'' on timeout (like a zero-length read)."""
        try:
            return bytes(self.ep_in.read(min(size, self.READ_CHUNK), timeout=timeout_ms))
        except usb.core.USBError as e:
            if _is_timeout(e):
                return b""
            raise _map_error(e, "Lesen") from e
