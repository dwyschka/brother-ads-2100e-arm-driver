"""Scan-key daemon: replaces brscan-skey for USB-attached ADS scanners."""

from __future__ import annotations

import configparser
import logging
import os
import shlex
import signal
import subprocess
import sys
import time

from . import protocol as P
from .output import PageWriter
from .scanner import NoDocumentError, ScanError, Scanner, ScanSettings
from .usbio import DeviceGone, TransportError, UsbTransport

log = logging.getLogger(__name__)

DEFAULTS = {
    "output_dir": "/srv/scans",
    "filename": "scan_%Y-%m-%d_%H-%M-%S",
    "poll_interval": "1.0",
    "mode": "color-jpeg",
    "resolution": "300",
    "paper": "a4",
    "duplex": "device",
    "format": "pdf",
    "brightness": "0",
    "contrast": "0",
    "back_rotate": "0",
    "jpeg_quality": "85",
    "command": "",
}


def load_config(path: str | None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(defaults=DEFAULTS, interpolation=None)
    if path and os.path.exists(path):
        cfg.read(path)
    for sect in ("scanner", "profile"):
        if not cfg.has_section(sect):
            cfg.add_section(sect)
    return cfg


def profile_for(cfg: configparser.ConfigParser, func: P.KeyFunction):
    """[profile.<func>] overrides [profile] which overrides the defaults."""
    sect = "profile." + func.name.lower()
    base = dict(cfg.items("profile"))
    if cfg.has_section(sect):
        base.update(cfg.items(sect))
    return base


def settings_from_profile(prof: dict) -> ScanSettings:
    duplex_opt = prof["duplex"].strip().lower()
    duplex = None if duplex_opt == "device" else duplex_opt in ("1", "yes", "true", "on")
    return ScanSettings.paper(
        prof["paper"],
        mode=P.MODE_ALIASES[prof["mode"].strip().lower()],
        resolution=int(prof["resolution"]),
        duplex=duplex,
        brightness=int(prof["brightness"]),
        contrast=int(prof["contrast"]),
        back_rotate=int(prof["back_rotate"]),
    )


def run_scan(scanner: Scanner, cfg, func: P.KeyFunction) -> list[str]:
    prof = profile_for(cfg, func)
    settings = settings_from_profile(prof)
    out_dir = cfg.get("scanner", "output_dir")
    name = time.strftime(cfg.get("scanner", "filename")) + "." + prof["format"]
    writer = PageWriter(os.path.join(out_dir, name), prof["format"], int(prof["jpeg_quality"]))
    try:
        scanner.scan(settings, on_page=writer)
    except BaseException:
        if writer.files:
            log.warning("Scan unvollständig – %d Seite(n) werden trotzdem gespeichert", len(writer.files))
            files = writer.finish()
            log.info("Gespeichert: %s", ", ".join(files))
        else:
            writer.abort()
        raise
    files = writer.finish()
    log.info("Gespeichert: %s", ", ".join(files))
    cmd = prof.get("command", "").strip()
    if cmd and files:
        argv = [a.replace("{file}", files[0]).replace("{function}", func.name.lower())
                for a in shlex.split(cmd)]
        if "{files}" in argv:
            i = argv.index("{files}")
            argv[i : i + 1] = files
        log.info("Starte Nachbearbeitung: %s", argv)
        subprocess.Popen(argv, start_new_session=True)
    return files


def _stop(signum, frame):
    # SystemExit unwinds through Scanner's finally blocks, so a scan that is
    # interrupted (unplug, systemctl stop) still closes the USB session.
    sys.exit(0)


def serve(config_path: str | None) -> None:
    cfg = load_config(config_path)
    interval = cfg.getfloat("scanner", "poll_interval")
    signal.signal(signal.SIGTERM, _stop)
    scanner = None
    waiting_logged = False
    last_unknown = None
    log.info("Warte auf Scan-Taste (Konfiguration: %s)", config_path or "Standardwerte")
    while True:
        try:
            if scanner is None:
                t = UsbTransport.first()
                scanner = Scanner(t)
                waiting_logged = False
                log.info("Scanner verbunden: %s", t.model)
            with scanner.lock:
                raw = scanner.t.poll_key()  # None while a CLI scan holds the device
            func = P.parse_key_status(raw) if raw else None
            if raw and func is None and raw != last_unknown:
                # z. B. "Scan to USB" o. ä.: unbekannte Tastenmeldung nur bei Änderung loggen
                log.info("Unbekannte Tastenmeldung: %s", raw.hex())
            last_unknown = raw if func is None else None
            if func is not None:
                log.info("Taste gedrückt: %s", func.name)
                try:
                    run_scan(scanner, cfg, func)
                except NoDocumentError:
                    log.warning("Kein Papier im Einzug")
                except DeviceGone:
                    raise
                except (ScanError, TransportError) as e:
                    log.error("Scan fehlgeschlagen: %s", e)
        except (TransportError, OSError, ValueError) as e:
            # Under systemd the unit is bound to the device and stopped on
            # unplug; this loop only matters when run by hand.
            if scanner is not None:
                log.warning("%s – warte auf Scanner", e)
            elif not waiting_logged:
                log.info("%s – warte auf Scanner", e)
            waiting_logged = True
            scanner = None
            time.sleep(5)
            continue
        time.sleep(interval)
