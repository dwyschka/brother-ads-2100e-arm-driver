"""brother-ads command line."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from . import protocol as P
from .output import FORMATS, PageWriter
from .scanner import PAPER_SIZES, SUPPORTED_RESOLUTIONS, ScanError, Scanner, ScanSettings
from .usbio import TransportError, UsbTransport, find_devices


def _scanner(args, raw_dump=None) -> Scanner:
    pid = int(args.pid, 16) if args.pid else None
    return Scanner(UsbTransport.first(pid), raw_dump=raw_dump)


def cmd_list(args) -> int:
    devs = find_devices(all_brother=True)
    if not devs:
        print("Keine Brother-Geräte gefunden.")
        return 1
    for dev, name in devs:
        print("%s  %04x:%04x  Bus %03d Gerät %03d"
              % (name, dev.idVendor, dev.idProduct, dev.bus, dev.address))
    return 0


def cmd_info(args) -> int:
    for k, v in _scanner(args).info().items():
        print("%-24s %s" % (k, v))
    return 0


def cmd_scan(args) -> int:
    fmt = args.format or (args.output.rsplit(".", 1)[-1].lower() if "." in args.output else "pdf")
    fmt = {"jpeg": "jpg", "tif": "tiff"}.get(fmt, fmt)
    duplex = {"yes": True, "no": False, "device": None}[args.duplex]
    settings = ScanSettings.paper(
        args.paper,
        mode=P.MODE_ALIASES[args.mode],
        resolution=args.resolution,
        duplex=duplex,
        brightness=args.brightness,
        contrast=args.contrast,
        centered=not args.left_aligned,
        compression=not args.no_compression,
        back_rotate=args.back_rotate,
    )
    dump = open(args.raw_dump, "wb") if args.raw_dump else None
    scanner = _scanner(args, dump)
    writer = PageWriter(args.output, fmt, args.jpeg_quality)
    signal.signal(signal.SIGTERM, lambda *a: scanner.cancel())
    try:
        scanner.scan(settings, on_page=writer)
    except KeyboardInterrupt:
        scanner.cancel()
        writer.abort()
        print("Abgebrochen.", file=sys.stderr)
        return 130
    except BaseException:
        if writer.files:
            for f in writer.finish():
                print("(unvollständig) %s" % f)
        else:
            writer.abort()
        raise
    finally:
        if dump:
            dump.close()
    files = writer.finish()
    if not files:
        print("Keine Seiten gescannt.", file=sys.stderr)
        return 1
    for f in files:
        print(f)
    return 0


def cmd_buttons(args) -> int:
    from .daemon import serve

    serve(args.config)
    return 0


def cmd_keytest(args) -> int:
    t = UsbTransport.first(int(args.pid, 16) if args.pid else None)
    print("Drücke die Scan-Taste am Gerät (Strg+C beendet) ...")
    try:
        while True:
            raw = t.poll_key()
            if raw is None:
                if args.verbose:
                    print(time.strftime("%H:%M:%S"), "(belegt oder Timeout)")
            else:
                func = P.parse_key_status(raw)
                if args.verbose or func:
                    print(time.strftime("%H:%M:%S"), raw[:8].hex(" "), func.name if func else "")
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="brother-ads", description="Treiber für Brother ADS-2100e (USB)")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("--pid", help="USB Product-ID (hex), falls mehrere Scanner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="angeschlossene Brother-Geräte anzeigen").set_defaults(func=cmd_list)
    sub.add_parser("info", help="Geräteinformationen abfragen").set_defaults(func=cmd_info)

    sp = sub.add_parser("scan", help="Dokumente aus dem Einzug scannen")
    sp.add_argument("-o", "--output", required=True, help="Zieldatei, z. B. scan.pdf oder seite.jpg")
    sp.add_argument("-f", "--format", choices=FORMATS + ("jpeg", "tif"))
    sp.add_argument("-m", "--mode", choices=sorted(P.MODE_ALIASES), default="color-jpeg")
    sp.add_argument("-r", "--resolution", type=int, choices=SUPPORTED_RESOLUTIONS, default=300)
    sp.add_argument("-d", "--duplex", choices=("yes", "no", "device"), default="yes",
                    help="device = Einstellung der Scan-Taste am Gerät übernehmen")
    sp.add_argument("-p", "--paper", default="a4",
                    help="%s oder BREITExLÄNGE in mm" % ", ".join(PAPER_SIZES))
    sp.add_argument("--brightness", type=int, default=0, help="-50 .. 50")
    sp.add_argument("--contrast", type=int, default=0, help="-50 .. 50")
    sp.add_argument("--left-aligned", action="store_true", help="Papier links statt mittig anlegen")
    sp.add_argument("--no-compression", action="store_true")
    sp.add_argument("--back-rotate", type=int, default=0, choices=(0, 90, 180, 270),
                    help="Rückseiten im Duplex drehen")
    sp.add_argument("--jpeg-quality", type=int, default=85)
    sp.add_argument("--raw-dump", help="rohen USB-Datenstrom zur Fehlersuche speichern")
    sp.set_defaults(func=cmd_scan)

    bp = sub.add_parser("buttons", help="Daemon: auf Scan-Taste warten")
    bp.add_argument("-c", "--config", default="/etc/brother-ads.conf")
    bp.set_defaults(func=cmd_buttons)

    sub.add_parser("keytest", help="Rohantworten der Tastenabfrage anzeigen").set_defaults(func=cmd_keytest)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose or args.cmd == "buttons" else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (TransportError, ScanError) as e:
        print("Fehler: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
