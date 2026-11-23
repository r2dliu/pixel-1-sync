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

Small files clear quickly; large videos are held proportionally longer. A file is deleted only once
it is at least `wait` old **and** the device has been continuously online for at least `wait`;
reconnecting restarts that clock, so a file is never deleted immediately after the device
reconnects, before Google Photos has had a chance to upload it.

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
- `PHOTO_MAX_PENDING_MB` — buffer cap; keep below free space and above the largest single file.

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

### iPhone permissions

If a shortcut fails with *"This action is trying to share … Photos items, which is not allowed"*,
iOS is blocking photo data (including names derived from photos) from leaving the device. Allow it:

1. **Settings → Apps → Shortcuts → Advanced** (older iOS: **Settings → Shortcuts → Advanced**):
   turn on **Allow Sharing Large Amounts of Data**. Pass 1 sends every photo's name in one request.
2. **Settings → Privacy & Security → Photos → Shortcuts**: choose **Full Access**. With *Limited*,
   Find Photos sees only the photos you picked.
3. Run the shortcut again and answer **Always Allow** when it asks to send items to the Pixel's
   address. If you previously chose *Don't Allow*, open the shortcut, tap **ⓘ → Privacy**, and
   reset or allow that address.

## HTTP API

All endpoints except `/ping` require the `X-Token` header. The `PUT` endpoints also accept `POST`.

| Method & path | Body | Response |
|---------------|------|----------|
| `GET /ping` | — | health and counts |
| `GET /space` | — | buffer headroom in bytes |
| `GET /exists?name=<n>` | — | `yes` / `no` |
| `PUT /missing` | names, one per line | names not finished and not currently held, or `NONE` |
| `PUT /safe_to_delete` | names, one per line | finished names as `\|name\|`, or `NONE` |
| `PUT /upload?name=<n>` | file bytes | `saved` / `skipped` / `BUSY`; `413` if larger than the buffer, `415` if not a recognized photo or video |

A typical client first posts the batch of candidate names to `/missing`, then `PUT`s only the
returned files to `/upload`, naming each one stably (for example `IMG_1234_20261123_142501`, filename plus creation time) so
re-runs are idempotent. Names are compared by *identity*: sanitized, with any known media
extension removed, so `IMG_1.HEIC` and `IMG_1` refer to the same item. The server appends every completed name to a ledger, so files that have
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
| `PHOTO_MAX_PENDING_MB` | `12000` | buffer cap (MB) |
| `PHOTO_CLEAN_EVERY_SEC` | `30` | cleaner interval (s) |
| `PHOTO_FORCE_ONLINE` | — | `1` skips the connectivity probe (testing) |

## Development

```sh
python -m unittest discover tests
```

## Monitoring

`GET /ping` over the overlay network reports completed count, current buffer size, and
connectivity. A steadily climbing buffer indicates Google Photos is not keeping up (slow upload,
backup disabled, or the app suspended); an unreachable endpoint indicates the device rebooted
without the launcher restarting, is offline, or is powered down.

## Large initial import

The buffer cannot overflow — excess simply waits and uploads over subsequent runs, paced by the
upload link. To accelerate a one-time import, raise `PHOTO_MAX_PENDING_MB` toward free space and run the
client in the foreground. Because the hold scales with file size, the buffer drains about as fast
as each file can upload, so upload bandwidth, not the cleaner, is the limit.

## Security

The server binds `0.0.0.0` and is intended to be reached over a private overlay network such as
Tailscale, which also encrypts the traffic. It is therefore also reachable on the local LAN. Every
endpoint except `/ping` requires the `X-Token` secret, compared in constant time; `/ping` exposes
only counts. Run it only on a trusted network.

## Limitations

- **Backup is not verified.** Deletion is a size- and time-based heuristic. The source device's
  originals are the backstop; confirm the cloud before deleting them.
- **Live Photos** are relayed as the still image only.
- **One file must fit the buffer.** `PHOTO_MAX_PENDING_MB` must exceed the largest single file;
  larger uploads are rejected with `413`.
- **The cleaner manages the whole destination folder.** Every non-hidden file in `PHOTO_DEST` is
  subject to deletion, including photos taken with the Pixel's own camera when `PHOTO_DEST` is the
  Camera folder. Don't use the Pixel as a camera, or point `PHOTO_DEST` at a dedicated folder that
  Google Photos backs up.
- **Carrier-locked devices.** The storage benefit requires a first-generation Pixel; relaying from
  the Camera folder needs only a working device, not an unlocked bootloader. A rooted device with an
  unlocked bootloader enables a stronger variant — mounting external storage as internal and reading
  Google Photos' database for verified, immediate per-file deletion with an unbounded local buffer —
  which is out of scope here.

## Disclaimer

Independent project. Not affiliated with or endorsed by Google. Use in accordance with the
applicable service terms.
