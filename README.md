# pixel-1-sync

Turn a first-generation Google Pixel into an always-on relay for unlimited, original-quality
Google Photos backups — for photos and videos originating on **any** device (for example an
iPhone), with **no root** required on the Pixel.

The first-generation Pixel (sailfish / marlin) includes free, unlimited, original-quality
Google Photos storage. `pixel-1-sync` exploits that benefit for an arbitrary photo source: a
client uploads media to the Pixel over a private network, the media lands in the Pixel's Camera
folder, Google Photos backs it up as a Pixel upload, and a background cleaner removes each local
copy once it has had time to upload — so a 32 GB device can relay an unbounded library.

## Architecture

```
  source device (iOS Shortcut, script, …)
        │   HTTP over a private overlay network (e.g. Tailscale)
        ▼
  Pixel: pixel_sync_server.py  (Termux, no root)
        │   writes to /DCIM/Camera
        ▼
  Google Photos  →  backed up as a Pixel upload ("no storage used")
        │
        ▼
  background cleaner removes the local copy after a size-scaled delay
```

Only a small buffer is ever held on the device, so total library size is unbounded; the single
constraint is that one file fits the buffer. When the buffer is full the server returns `BUSY`
and the client retries later or on its next run.

## Components

| File | Role |
|------|------|
| `pixel_sync_server.py` | The upload server. Runs in Termux on the Pixel. |
| `start-server.sh` | Termux:Boot launcher; sets configuration and restarts on exit. |
| `docs/iphone-shortcut.md` | Building the iOS Shortcut client. |

## How deletion is decided

Google Photos' backup state is not readable without root, so the server does not verify backup.
Instead it deletes a file once it has had enough **online** time to have uploaded, scaled to the
file's size:

```
wait = MIN_WAIT_SEC + (file_size_bits / (UPLOAD_MBPS × 1e6)) × WAIT_SAFETY   (capped at MAX_WAIT_SEC)
```

Small files clear quickly; large videos are held proportionally longer. The wait is measured in
online time — while the device is offline the clock does not advance — so a file is never deleted
immediately after the device reconnects, before Google Photos has had a chance to upload it.

This is a time-based heuristic, not a backup guarantee. The source device should retain its
originals until the cloud copy is confirmed; local deletion on the Pixel is space management only.
A genuine, verified alternative requires root (reading Google Photos' backup database); see
[Limitations](#limitations).

## Pixel setup

Install **Termux** and **Termux:Boot** from F-Droid (the Play Store builds are outdated), then:

```sh
pkg install python termux-api curl
termux-setup-storage          # grant storage access when prompted
cp ~/storage/shared/Download/pixel_sync_server.py ~/
mkdir -p ~/.termux/boot
cp ~/storage/shared/Download/start-server.sh ~/.termux/boot/
chmod +x ~/.termux/boot/start-server.sh
```

Edit `~/.termux/boot/start-server.sh`:

- `PHOTO_TOKEN` — a long random secret (`head -c 24 /dev/urandom | base64`); the client uses the same value.
- `PHOTO_UPLOAD_MBPS` — the network's actual upload speed in Mbps (conservative).
- `MAX_PENDING_MB` — buffer cap; keep below free space and above the largest single file.

Start it and verify:

```sh
sh ~/.termux/boot/start-server.sh &
curl http://127.0.0.1:8765/ping     # -> pong (0 done, 0 in buffer, 0 MB, online=True)
```

Then:

1. Install **Tailscale**, sign in, enable **Always-on VPN**, and note the Pixel's Tailscale address.
2. Disable battery optimization for Termux, Termux:Boot, Tailscale, and Google Photos.
3. In Google Photos (signed into the account with the storage benefit), enable backup at **Original**
   quality. The Camera folder is backed up by default.
4. Keep the device on power and Wi-Fi.

## Client

See [`docs/iphone-shortcut.md`](docs/iphone-shortcut.md) for an iOS Shortcut that uploads new photos
automatically. Any client that speaks the HTTP API below works equally well.

## HTTP API

All endpoints except `/ping` require the `X-Token` header.

| Method & path | Body | Response |
|---------------|------|----------|
| `GET /ping` | — | health and counts |
| `GET /space` | — | buffer headroom in bytes |
| `GET /exists?name=<n>` | — | `yes` / `no` |
| `PUT /missing` | names, one per line | names not finished and not currently held, or `NONE` |
| `PUT /safe_to_delete` | names, one per line | finished names as `\|name\|`, or `NONE` |
| `PUT /upload?name=<n>` | file bytes | `saved` / `skipped` / `BUSY` |

A typical client first posts the batch of candidate names to `/missing`, then `PUT`s only the
returned files to `/upload`, naming each one stably (for example `creationDate_fileSize`) so
re-runs are idempotent. The server appends every completed name to a ledger, so files that have
been backed up and removed are never requested again.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PHOTO_TOKEN` | — | shared secret (required) |
| `PHOTO_DEST` | `/storage/emulated/0/DCIM/Camera` | destination folder |
| `PHOTO_LEDGER` | `~/photo_ledger.txt` | ledger of completed names |
| `PHOTO_PORT` | `8765` | listen port |
| `PHOTO_UPLOAD_MBPS` | `10` | assumed upload speed (Mbps) |
| `PHOTO_MIN_WAIT_SEC` | `180` | minimum hold per file |
| `PHOTO_WAIT_SAFETY` | `2.5` | multiple of estimated upload time |
| `PHOTO_MAX_WAIT_SEC` | `43200` | upper bound on the hold |
| `MAX_PENDING_MB` | `12000` | buffer cap (MB) |
| `CLEAN_EVERY_SEC` | `30` | cleaner interval |

## Monitoring

`GET /ping` over the overlay network reports completed count, current buffer size, and
connectivity. A steadily climbing buffer indicates Google Photos is not keeping up (slow upload,
backup disabled, or the app suspended); an unreachable endpoint indicates the device rebooted
without the launcher restarting, is offline, or is powered down.

## Large initial import

The buffer cannot overflow — excess simply waits and uploads over subsequent runs, paced by the
upload link. To accelerate a one-time import, raise `MAX_PENDING_MB` toward free space and run the
client in the foreground. Because the hold scales with file size, the buffer drains about as fast
as each file can upload, so upload bandwidth, not the cleaner, is the limit.

## Security

The server binds `0.0.0.0` and is intended to be reached over a private overlay network such as
Tailscale, which also encrypts the traffic. It is therefore also reachable on the local LAN. All
mutating endpoints require the `X-Token` secret, compared in constant time; `/ping` and `/exists`
expose only counts. Run it only on a trusted network.

## Limitations

- **Backup is not verified.** Deletion is a size- and time-based heuristic. The source device's
  originals are the backstop; confirm the cloud before deleting them.
- **Live Photos** are relayed as the still image only.
- **One file must fit the buffer.** `MAX_PENDING_MB` must exceed the largest single file.
- **Carrier-locked devices.** The storage benefit requires a first-generation Pixel; relaying from
  the Camera folder needs only a working device, not an unlocked bootloader. A rooted device with an
  unlocked bootloader enables a stronger variant — mounting external storage as internal and reading
  Google Photos' database for verified, immediate per-file deletion with an unbounded local buffer —
  which is out of scope here.

## Disclaimer

Independent project. Not affiliated with or endorsed by Google. Use in accordance with the
applicable service terms.
