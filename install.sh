#!/bin/sh
# Installation auf Raspberry Pi OS (Bookworm/Trixie, armhf oder arm64)
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)

apt-get install -y python3 python3-usb python3-pil python3-img2pdf python3-pikepdf tesseract-ocr tesseract-ocr-osd

getent group scanner >/dev/null || groupadd --system scanner
id scanner >/dev/null 2>&1 || useradd --system --gid scanner --home-dir /nonexistent --shell /usr/sbin/nologin scanner

install -d /opt/brother-ads
rm -rf /opt/brother-ads/brother_ads
cp -r "$HERE/brother_ads" /opt/brother-ads/
chmod -R u=rwX,go=rX /opt/brother-ads
cat > /usr/local/bin/brother-ads <<'WRAP'
#!/bin/sh
PYTHONPATH=/opt/brother-ads exec python3 -m brother_ads "$@"
WRAP
chmod 755 /usr/local/bin/brother-ads
install -m 755 "$HERE/packaging/brother-ads-postscan" /usr/local/bin/brother-ads-postscan

install -m 644 "$HERE/packaging/60-brother-ads.rules" /etc/udev/rules.d/60-brother-ads.rules
install -m 644 "$HERE/packaging/brother-ads-buttons.service" /etc/systemd/system/brother-ads-buttons.service
[ -f /etc/brother-ads.conf ] || install -m 644 "$HERE/packaging/brother-ads.conf" /etc/brother-ads.conf
install -d -o scanner -g scanner -m 2775 /srv/scans

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=04f9 || true
udevadm settle || true
systemctl daemon-reload
systemctl enable brother-ads-buttons.service
# Startet ab jetzt automatisch beim Einstecken; ist der Scanner schon dran, gleich starten.
[ -e /dev/brother_ads ] && systemctl restart brother-ads-buttons.service || true
echo "Fertig. Test: sudo -u scanner brother-ads info"
