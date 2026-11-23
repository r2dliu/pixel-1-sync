#!/data/data/com.termux/files/usr/bin/sh
#
# Termux:Boot launcher for pixel_sync_server.py.
#
# Install:
#   cp start-server.sh ~/.termux/boot/
#   chmod +x ~/.termux/boot/start-server.sh
#
# Edit the values below before first run. The same PHOTO_TOKEN must be configured in the
# upload client. Set PHOTO_UPLOAD_MBPS to the network's actual upload speed; set
# MAX_PENDING_MB below free space and above the largest single file.

termux-wake-lock

export PHOTO_TOKEN="CHANGE-ME"
export PHOTO_DEST="/storage/emulated/0/DCIM/Camera"
export PHOTO_LEDGER="$HOME/photo_ledger.txt"

export PHOTO_UPLOAD_MBPS="10"
export PHOTO_MIN_WAIT_SEC="180"
export PHOTO_WAIT_SAFETY="2.5"

export MAX_PENDING_MB="12000"

# Restart the server automatically if it exits.
while true; do
  python "$HOME/pixel_sync_server.py" >> "$HOME/pixel_sync_server.log" 2>&1
  sleep 5
done
