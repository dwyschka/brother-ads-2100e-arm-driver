# brother-ads – Brother ADS-2100e on the Raspberry Pi

A free userspace driver for the **Brother ADS-2100e** document scanner (USB),
including a daemon for the scanner's "Scan to PC" buttons. Brother only ships
x86 binaries (`brscan4`, `brscan-skey`), so the scanner does not work on ARM
machines such as the Raspberry Pi. This project re-implements the USB protocol
in pure Python (pyusb + Pillow) and therefore runs on any architecture
(armhf, arm64, x86_64). The protocol was reconstructed from the Brother
binaries, see [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

Tested on a Raspberry Pi 4 (Raspberry Pi OS / Debian 13 "trixie", aarch64)
with an ADS-2100e.

## Features

- **Command line scanning** from the automatic document feeder (ADF):
  `brother-ads scan`
- **Duplex** (both sides) and simplex scanning
- Colour (fast JPEG or raw), grayscale, black & white and halftone modes
- 100, 150, 200, 300, 400 and 600 dpi
- Paper sizes A4, A5, A6, B5, Letter, Legal, card, "max" or any custom size
- Brightness and contrast adjustment
- Output as **PDF** (multi-page), JPEG, PNG or TIFF
- **Scan button daemon**: pressing the scanner's button (Image / OCR / E-mail /
  File) starts a scan with a configurable profile and runs an optional hook
- **Post-processing script** (`brother-ads-postscan`): removes blank pages
  (e.g. empty backs in duplex scans) and fixes the page orientation
- Safe concurrent use: CLI scans and the button daemon share the scanner
  through a per-device `flock()` lock
- systemd integration: the daemon starts and stops when the scanner is
  switched on or unplugged (udev)

### Not supported

- The **"Scan to USB"** button. It is handled by the scanner firmware and the
  scanner does not report it to the host. The four "Scan to PC" buttons
  (Image, OCR, E-mail, File) work.
- Other models. The udev rule lists related Brother ADS product IDs
  (ADS-2000e, ADS-2100, ...), but only the ADS-2100e (`04f9:037d`) has been
  tested.

## Installation

Copy the project to the Pi and run the installer as root:

```sh
scp -r brother-ads-pi pi@raspberrypi:
ssh pi@raspberrypi
cd brother-ads-pi && sudo ./install.sh
```

The installer:

- installs `python3-usb`, `python3-pil`, `python3-img2pdf`, `python3-pikepdf`,
  `tesseract-ocr` and `tesseract-ocr-osd` via apt
- creates the system user and group `scanner`
- installs the driver to `/opt/brother-ads` and the commands `brother-ads` and
  `brother-ads-postscan` to `/usr/local/bin`
- installs the udev rule `/etc/udev/rules.d/60-brother-ads.rules`
  (gives the group `scanner` access to the USB device)
- installs `/etc/brother-ads.conf` (an existing file is never overwritten)
- creates `/srv/scans` (owner `scanner`, setgid) for scanned files
- enables `brother-ads-buttons.service`

To let your own user scan from the command line, add it to the group:

```sh
sudo usermod -aG scanner $USER   # log in again afterwards
```

Update: copy the project again and re-run `sudo ./install.sh`.

## Command line

```sh
brother-ads list                         # is the scanner found?
brother-ads -v info                      # protocol version and capabilities
brother-ads scan -o letter.pdf           # duplex, colour (JPEG), 300 dpi, A4
brother-ads scan -o file.pdf -d no       # simplex
brother-ads scan -o doc.pdf -m gray -r 200
brother-ads scan -o page.png -m bw -r 600 -p letter
brother-ads keytest                      # show raw button messages
```

`brother-ads scan` options:

| Option | Values | Default |
|---|---|---|
| `-o, --output` | target file, e.g. `scan.pdf` (required) | – |
| `-f, --format` | `pdf`, `jpg`, `png`, `tiff` (otherwise from the file extension) | `pdf` |
| `-m, --mode` | `color-jpeg`, `color`, `gray`, `bw`, `halftone` | `color-jpeg` |
| `-r, --resolution` | `100`, `150`, `200`, `300`, `400`, `600` | `300` |
| `-d, --duplex` | `yes`, `no`, `device` (use the setting stored on the scanner for the button) | `yes` |
| `-p, --paper` | `a4`, `a5`, `a6`, `b5`, `letter`, `legal`, `card`, `max` or `WIDTHxHEIGHT` in mm | `a4` |
| `--brightness`, `--contrast` | -50 .. 50 | 0 |
| `--back-rotate` | `0`, `90`, `180`, `270`: rotate the back sides of duplex scans | 0 |
| `--jpeg-quality` | 1 .. 100 | 85 |
| `--left-aligned` | align the paper to the left instead of centred | off |
| `--no-compression` | disable run-length compression of the raw data transfer (modes other than `color-jpeg`) | off |
| `--raw-dump FILE` | save the raw USB data stream (for debugging) | – |

Modes: `color-jpeg` is fast and small, `color` transfers uncompressed colour.
For `jpg`/`pdf` output the JPEG data from the scanner is embedded unchanged
where possible. With `-f jpg`, `png` or `tiff` a multi-page scan creates one
file per page (`name-001.jpg`, ...).

Global options: `-v` (info), `-vv` (debug), `--pid HEX` (USB product ID if
several scanners are connected).

> **Paper height:** The feeder does not stop at the trailing edge of the sheet.
> It scans the full requested height, so use the correct paper size.
> Otherwise every page has a grey strip at the bottom. Use `-p max` only for
> sheets longer than A4/Letter.

## Duplex scanning and blank pages

Duplex is the default for both the CLI and the buttons. A stack of
single-sided sheets then produces an empty back page after every sheet.
`brother-ads-postscan` removes these pages and straightens the rest:

```sh
brother-ads scan -o stack.pdf          # duplex scan
brother-ads-postscan stack.pdf         # drop blank pages, fix orientation
```

With the scan button this happens automatically (see
[Post-processing](#post-processing-brother-ads-postscan)). If the back sides
of duplex scans come out upside down, set `back_rotate = 180`.

## Scan button daemon

`brother-ads buttons` (the systemd service `brother-ads-buttons.service`)
polls the scanner's buttons once per second. When a button is pressed, the
sheets in the feeder are scanned using the profile from
`/etc/brother-ads.conf`, written to `output_dir`, and an optional command is
run.

Which scanner button was pressed selects the profile section
`[profile.<function>]`: `file`, `image`, `ocr` or `email`.

```sh
sudo systemctl status brother-ads-buttons
journalctl -u brother-ads-buttons -f     # live log
```

The service is tied to the udev device `/dev/brother_ads`: it starts when the
scanner is switched on or plugged in (also at boot) and stops when it is
switched off or removed. If it is stopped during a scan it still closes the
USB session cleanly. Messages from the scanner that the driver does not know
are logged once at `INFO` level ("Unbekannte Tastenmeldung"), which helps when
looking for additional buttons.

## Configuration: `/etc/brother-ads.conf`

INI format. Restart the service after editing:
`sudo systemctl restart brother-ads-buttons`.

```ini
[scanner]
output_dir = /srv/scans
# strftime pattern, the extension is added automatically
filename = scan_%Y-%m-%d_%H-%M-%S
# seconds between button polls
poll_interval = 1.0

# Default profile for all buttons
[profile]
mode = color-jpeg        # color-jpeg | color | gray | bw | halftone
resolution = 300         # 100 150 200 300 400 600
paper = a4               # a4 a5 a6 letter legal b5 card max, or e.g. 210x297
duplex = yes             # yes | no | device (use the setting stored on the scanner)
format = pdf             # pdf jpg png tiff
brightness = 0
contrast = 0
back_rotate = 0          # rotate back sides of duplex scans (0/90/180/270)
jpeg_quality = 85
# Optional command run after each scan (see below)
command = /usr/local/bin/brother-ads-postscan {file}

# Per-button overrides: file, image, ocr, email
[profile.ocr]
mode = gray
```

A `[profile.<function>]` section overrides only the keys it contains; all other
values come from `[profile]`. This way every button can have its own mode,
paper size, output format and command.

### After-scan command (hook)

`command` is started in the background (as the user `scanner`) after the file
has been written. It does not block the next scan. Its output goes to the
journal. Placeholders:

| Placeholder | Replaced with |
|---|---|
| `{file}` | the first file that was written |
| `{files}` | all files that were written (as separate arguments) |
| `{function}` | the button function: `file`, `image`, `ocr` or `email` |

Examples:

```ini
[profile]
command = /usr/local/bin/upload.sh {file}

[profile.email]
command = /usr/local/bin/mail-scan.sh {file}
```

### Post-processing: `brother-ads-postscan`

The default configuration runs `brother-ads-postscan {file}` after each scan.
It works on PDFs and does two things:

- **Removes blank pages**, for example the empty back sides of single-sided
  documents scanned in duplex. A page counts as blank when less than 0.03 % of
  its area is darker than the paper. Dust, shadows at the edges and faint
  show-through are ignored. If all pages are blank, the file is left untouched.
- **Fixes the page orientation.** Tesseract's orientation detection (OSD)
  decides per page whether it needs to be turned by 90, 180 or 270 degrees.
  The rotation is stored in the PDF (`/Rotate`), so the image data is not
  changed. Pages with too little text or low confidence stay as they are.

The file is replaced atomically, so a reader (for example over NFS) never sees
a half-written PDF, and the original stays if anything fails. Processing takes
about 2 seconds per page on a Raspberry Pi 4 (it uses all CPU cores).

You can also run it by hand on any PDF:

```sh
brother-ads-postscan scan.pdf               # fix the file in place
brother-ads-postscan --dry-run scan.pdf     # only report what would happen
brother-ads-postscan --no-blank scan.pdf    # orientation only
brother-ads-postscan --no-rotate scan.pdf   # blank page removal only
brother-ads-postscan --blank-ratio 0.001 scan.pdf   # change the blank threshold
```

Remove the `command = ...` line (or leave it empty) to disable the hook.

## Getting the scans from the Pi

Scans are written to `/srv/scans`. One easy way to fetch them is a read-only
NFS export:

```sh
sudo apt install nfs-kernel-server
echo '/srv/scans 192.168.1.0/24(ro,sync,no_subtree_check)' | sudo tee -a /etc/exports
sudo exportfs -ra
```

Adjust the network to yours. NFS has no authentication, so only export to
networks you trust. Mount on a client with
`sudo mount -t nfs raspberrypi:/srv/scans /mnt/scans`. Alternatives are Samba,
`rsync`, or a hook that uploads each file.

## USB access: daemon and CLI at the same time

Brother's `brscan-skey` and `brscan4` only lock the device within their own
process, so the button daemon can disturb a running SANE scan. Here every
device has an `flock()` lock (`/run/lock/brother-ads-<bus>-<address>.lock`):

- `brother-ads scan` and `info` wait until the device is free.
- The daemon skips the button poll while someone else is scanning.
- If a process dies, the kernel releases the lock automatically.
- Button polling uses a control transfer without claiming the interface, so
  it never blocks a scan.

## Troubleshooting

```sh
brother-ads list
brother-ads -vv info
brother-ads -vv scan -o test.pdf --raw-dump /tmp/raw.bin
journalctl -u brother-ads-buttons -f
```

| Problem | Fix |
|---|---|
| `list` finds nothing | Is the scanner switched on? `lsusb` should show `04f9:037d`. Check `/dev/brother_ads` and the udev rule. |
| Permission denied | Add your user to the group `scanner` (see Installation), or run the command as the user `scanner`. |
| `Kein Papier im Einzug` | No sheet in the feeder. |
| `document jam` | Paper jam or badly inserted paper. Clear the feeder and try again. |
| Grey strip at the bottom of each page | The paper size is too tall. Set `paper =` / `-p` to the real size. |
| Back sides are upside down | Use `--back-rotate 180` or `back_rotate = 180`. The post-processing script also fixes this if the text is recognisable. |
| Black & white images are inverted | In `pages.py` replace `"1;I"` with `"1"`. |
| Pages are rotated wrongly | Orientation detection needs readable text. Pages with little text are left alone. Disable with `--no-rotate` if needed. |
| Very large files | Use `-m gray` and/or `-r 200`. |
| Scan starts slowly | A short pause (about 1 s) comes from clearing the USB buffer and the button poll interval (`poll_interval`). |

Known device quirk: the ADS-2100e ignores the first command that follows the
device query (`Q`). The driver detects this and repeats the command after
0.3 s.

## Development

```sh
PYTHONPATH=. python3 -m unittest discover -s tests
```

The tests run against a simulated scanner and need `pyusb` and `Pillow`. The
protocol is documented in [`docs/PROTOCOL.md`](docs/PROTOCOL.md) (German).

Layout:

| Path | Purpose |
|---|---|
| `brother_ads/protocol.py` | commands, reply parsing, scan area |
| `brother_ads/usbio.py` | USB transport, locking, error mapping |
| `brother_ads/scanner.py` | scan session, paper sizes, page assembly |
| `brother_ads/pages.py` | decoding of the scan data stream |
| `brother_ads/output.py` | PDF / JPEG / PNG / TIFF writing |
| `brother_ads/daemon.py` | button daemon, profiles, hook |
| `brother_ads/cli.py` | command line |
| `packaging/` | udev rule, systemd unit, default config, `brother-ads-postscan` |

## Legal notes

This is an independent, unofficial project and is not affiliated with Brother
Industries, Ltd. The protocol was reconstructed by analysing Brother's
publicly available driver binaries for interoperability. The repository does
not contain any Brother software.
