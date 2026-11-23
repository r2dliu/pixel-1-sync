# iOS Shortcut client

An iOS Shortcut that uploads new photos and videos to a `pixel-1-sync` server. It runs in two
passes: first it asks the server which items are missing, then it uploads only those. Files that
hit a full buffer return `BUSY` and are retried on a later run, so progress is resumable and
idempotent.

## Prerequisites

- **Tailscale** installed on the iPhone and signed into the same network as the Pixel.
- The Pixel's Tailscale address and the server's `PHOTO_TOKEN`.
- Google Photos backup **disabled** on the iPhone (so its own uploads do not consume quota).

## Build

Create a shortcut named **Send to Pixel**.

### Configuration

1. **Text** `http://PIXEL_ADDRESS:8765` → **Set Variable** `Base`
2. **Text** `TOKEN` → **Set Variable** `Token`

### Pass 1 — determine what is missing

3. **Find Photos** — no filter; Sort by *Creation Date*, **Latest First**; **Limit** on, **500**
   photos. (A date filter such as *is in the last 30 days* is unreliable in Shortcuts.)
4. **Repeat with Each** (Photos):
   - **Get Details of Images** → **Name**
   - **Get Details of Images** → **Creation Date**
   - **Format Date** → that date, Format **Custom** `yyyyMMdd_HHmmss`
   - **Text** → `[Name]_[Formatted Date]`
   - **Add to Variable** `NameList`
5. **Combine Text** → `NameList`, separated by **New Lines**
6. **Get Contents of URL** → `[Base]/missing`
   - Method **PUT**; Header `X-Token` = `[Token]`; Request Body **File** = the combined text
7. **Set Variable** `Missing` = Contents of URL

### Pass 2 — upload the missing items

8. **Repeat with Each** — set the input to the **Find Photos** result from step 3:
    - **Get Details of Images** → **Name**
    - **Get Details of Images** → **Creation Date**
    - **Format Date** → that date, Format **Custom** `yyyyMMdd_HHmmss`
    - **Text** → `[Name]_[Formatted Date]`
    - **If** `Missing` **contains** that **Text**:
      - **URL Encode** that **Text**
      - **Get Contents of URL** → `[Base]/upload?name=[URL Encoded Text]`
        - Method **PUT**; Header `X-Token` = `[Token]`; Request Body **File** = **Repeat Item**

A "contains" match can occasionally hit one name inside a longer one. That only causes an extra
upload, which the server answers with `skipped`. When nothing is missing the response is `NONE`
and pass 2 uploads nothing.

`tools/build_shortcuts.py` generates this shortcut (and the deletion one below) as files; see its
header for how to sign and import them.

## Automation

Shortcuts → **Automation** → **Charger → Is Connected → Run Immediately** (disable "Notify When
Run") → **Run Shortcut → Send to Pixel**. Optionally add a **Time of Day** automation as well.

iOS limits background execution time, which is why this shortcut checks only the 500 newest
photos. For the first import, use a copy with **Limit** turned off (the generator builds it as
**Send All to Pixel**) and run it by hand, in the foreground, with the device on power. Run it
again until nothing more uploads; each run's `/missing` call resumes where the last stopped.

## Naming

`Name_yyyyMMdd_HHmmss` (e.g. `IMG_1234_20261123_142501`) is stable per asset, so re-runs are
idempotent. The filename alone is not enough: the camera counter wraps after `IMG_9999`, and a
repeated name would be skipped as already done and later reported as safe to delete. Two assets
collide only if they share both a filename and the second they were taken.

The date is formatted in the phone's current time zone, so after travelling an older photo may get
a new name and be uploaded once more. That costs bandwidth, never a photo.

## Deleting originals after backup (optional)

`PUT /safe_to_delete` returns the names the server has finished, wrapped as `|name|`. These are
*presumed* backed up (time-based, not verified), so confirm the cloud before relying on it. A
second shortcut can build the same names, post them to `/safe_to_delete`, collect the photos whose
`|name|` appears in the response, and pass them to **Delete Photos**. The generated **Delete
Backed-Up Photos** does this over the whole library (no Limit), so run it by hand. iOS asks for
confirmation and retains deleted items in Recently Deleted for 30 days.
