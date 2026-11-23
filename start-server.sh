#!/data/data/com.termux/files/usr/bin/sh
#
# Termux:Boot launcher for pixel_sync_server.py.
#
# Install:
#   cp start-server.sh ~/.termux/boot/
#   chmod +x ~/.termux/boot/start-server.sh
#
# Edit the values below before first run (the server refuses to start with the
# placeholder token). The same PHOTO_TOKEN must be configured in the
# upload client. Set PHOTO_UPLOAD_MBPS to the network's actual upload speed; set
# PHOTO_MAX_PENDING_MB below free space and above the largest single file.

termux-wake-lock

export PHOTO_TOKEN="CHANGE-ME"
export PHOTO_DEST="/storage/emulated/0/DCIM/Camera"
export PHOTO_LEDGER="$HOME/photo_ledger.txt"

export PHOTO_UPLOAD_MBPS="10"
export PHOTO_MIN_WAIT_SEC="180"
export PHOTO_WAIT_SAFETY="2.5"

export PHOTO_MAX_PENDING_MB="12000"

LOG="$HOME/pixel_sync_server.log"
MAX_LOG_KB=5000

# Rotate the log: past MAX_LOG_KB, keep one previous copy and empty the current file.
# Copy-then-truncate is safe while the server runs because it only ever appends.
while true; do
  if [ -f "$LOG" ] && [ "$(du -k "$LOG" | cut -f1)" -gt "$MAX_LOG_KB" ]; then
    cp "$LOG" "$LOG.1" && : > "$LOG"
  fi
  sleep 600
done &

# Restart the server automatically if it exits.
while true; do
  python "$HOME/pixel_sync_server.py" >> "$LOG" 2>&1
  sleep 5
done
