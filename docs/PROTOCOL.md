# Brother ADS-2100e USB-Protokoll (rekonstruiert)

Quellen: `libsane-brother4.so.1.0.7` aus `brscan4-0.4.11-1.amd64.deb`
(sha256 `7a658823…cfda`) und `brscan-skey-exe` aus `brscan-skey-0.3.4-0.amd64.deb`
(sha256 `7558173c…2ca3`). Beide Binaries haben Symbole; Funktionsnamen unten
sind die Originalnamen.

## Geräteerkennung

| Modell | VID:PID | brscan4-Typ |
|---|---|---|
| ADS-2100e | 04f9:037d | 315 |
| ADS-2000e | 04f9:037c | 315 |
| ADS-2100 | 04f9:60a1 | 115 |

`check_available_scanner`: VID 0x04f9, mehr als 1 Interface, Interface[1]
mit Klasse/Subklasse/Protokoll `ff/ff/ff`. Endpunkte aus diesem Interface
(`get_endpoint`), Fallback Bulk-OUT 0x03 / Bulk-IN 0x84.

## Sitzung (devaccs.c)

1. `set_configuration(1)` bzw. Toggle-Reset, `claim_interface(1)`
2. `OpenDevice`: Control-IN `bmRequestType=0xC0, bRequest=1, wValue=2, wIndex=0, len=5`.
   Erwartet `05 10 01 02 00` (u16 LE Status = 0x0002). Sonst `bRequest=2`, 0,5 s warten, bis zu 5×.
3. Recovery: ca. 1 s Bulk-IN leerlesen; kam etwas, `ESC Q` senden.
4. `CloseDevice`: Control-IN `bRequest=2, wValue=2, len=5`, dann `release_interface(1)`.

Bulk-Reads max. 16 KiB, Timeout 20 s, leere Reads → 20 ms warten und wiederholen.

## Befehle (Bulk-OUT, ASCII)

| Befehl | Bytes | Zweck |
|---|---|---|
| Q | `1b 51 0a 80` | Geräteinfo. Antwort ≤ 0x119 Bytes, `[3]` = Protokollversion, `[0x0e]` Auflösungstabelle, `[0x0f]` max. Papier, `[0x10..0x13]` Ausrichtung (Proto>4), `[0x16]`/`[0x1d]` Flags (Proto>8) |
| K | `1b 4b 0a 80` | nur Proto>8: Einstellungen der Scan-Taste, Text ab Byte 3; enthält ggf. `D=DUP` |
| I | `1b 49 0a "R=x,y\n" "M=<mode>\n" 80` | Scanparameter. Antwort: 2 Bytes Kopf (Proto>5: 3), dann `xres,yres,?,breite_mm,max_px_x,höhe_mm,max_px_y` |
| D | `1b 44 0a "ADF\n" 80` | Papier im Einzug? Antwort `[0]==0x80` → ja |
| X | siehe unten | Scan starten |
| X (leer) | `1b 58 0a 80` | nächste Seite (nur Proto ≤ 6) |
| R | `1b 52` | Abbrechen (danach `ReadTrash`: ~1 s leerlesen) |

Modi (`MakeupColorTypeCommand`): `TEXT` (S/W), `ERRDIF` (Halbton), `GRAY64`
(Graustufen 8 Bit), `C256`, `CGRAY` (24 Bit). „24bit Color[Fast]“ = `CGRAY` + `C=JPEG`.

Startbefehl (`MakeupScanStartCmd`), Reihenfolge exakt:

```
ESC X \n
R=<xres>,<yres>\n          Auflösung aus der I-Antwort
M=<mode>\n
C=RLENGTH\n | C=NONE\n | C=JPEG\nJ=MID\n
B=<helligkeit+50>\n         0..100
N=<kontrast+50>\n           0..100
A=<l>,<t>,<r>,<b>\n         Gerätepixel
D=DUP\n | D=SIN\n           wenn Modell Duplex kann
S=NORMAL_SCAN\nP=0\n        Proto > 8
E=1\n                       Proto > 8 und Q[0x16]==1
G=0\nL=0\n                  Proto > 8 und Q[0x1d]==1
0x80
```

Scanfläche: Benutzerwerte in 0,1 mm, jede Kante −2 mm (`margin_adjust`),
px = v·res/254, Breite auf max_px_x begrenzt, bei mittigem Einzug um
`(max_px_x − r)/2` verschoben, l/r auf 16 px gerundet (`(v+8)&0xfff0`),
Länge im ADF max. 17 Zoll.

## Bilddatenstrom (Bulk-IN)

**Protokoll ≤ 6** (`ProcessMain`):

- `00` → weiße Zeile
- `84`/`85` + 2 Bytes → Duplex-Markierung (normal/umgekehrt)
- `80` Seitenende (letzte), `81` nächste Seite (Host sendet leeres `ESC X`)
- `c2` kein Papier, `c3`/`ea` Stau, `c4` Abdeckung offen, `83`/`e3` Abbruch bestätigt
- sonst `H, u16 len, data`

**Protokoll > 6** (`make_cache_block`, Header-Größe 10):

- `80` → Ende des gesamten Auftrags
- `H > c1` → Fehlerstatus wie oben (1 Byte)
- sonst `H, u16 L(=7), ext[L]` mit `ext[0..1]` = Seitennummer, `ext[3..6]` = Zeilenzahl;
  - `81/82/83/86` → Seite fertig, `84/85` → Duplex-Markierung
  - Daten-Header (`H < 0x80`): danach `u16 len, data`

Seiten können sich verschachteln (Duplex: Vorder- und Rückseite gleichzeitig).

Daten-Header (`SetupImgLineProc`): `(H & 0x1c) >> 2` = Ebene (0 Grau/S-W,
1 R, 2 G, 3 B), `H & 3 == 2` → PackBits, sonst roh. Jeder Block = eine Zeile
(bei Farbe eine Ebene einer Zeile). `0x64` = JPEG-Daten, pro Seite aneinandergehängt.
S/W: 1 Bit/Pixel, 1 = schwarz.

## Scan-Taste (brscan-skey, USB)

Jede Sekunde: Control-IN `bmRequestType=0xC0, bRequest=3, wValue=0, wIndex=0, len=255`
(Timeout im Original 200 s, ohne `claim_interface`). Ereignis wenn
`buf[1]==0x10 && buf[2]==0x03 && buf[3]==0x20`; `buf[4]`: 3 = Image,
2 = OCR, 8 = E-Mail, 5 = File.

Das Scanner-Interface hat genau zwei Endpunkte, beide Bulk (`get_endpoint` wertet nur
Endpunkt 0 und 1 aus). Einen Interrupt-Endpunkt für Tastenereignisse gibt es also
nicht; das Polling über EP0 ist der einzige Weg.

`brscan-skey` schützt den USB-Zugriff mit einem `flock` auf
`/tmp/<x>_lockfile_<y>_jhsga…`. Diese Sperre benutzt nur skey selbst, `brscan4`
kennt sie nicht. Deshalb kann skey mitten in einen SANE-Scan hinein pollen. Dieser
Treiber verwendet für Scan und Tastenabfrage dieselbe Sperre (`usbio.DeviceLock`).

## Offene Punkte (nicht an Hardware geprüft)

- Protokollversion des ADS-2100e (entscheidet zwischen beiden Stromformaten)
- welcher Status-Code bei Proto > 6 eine Seite beendet (81/82/86)
- Bedeutung von `84`/`85` für die Ausrichtung der Rückseite
- ob die Tastenabfrage sofort antwortet oder bis zum Tastendruck blockiert
