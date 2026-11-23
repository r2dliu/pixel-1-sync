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

3. **Find Photos** — Filter: *Creation Date is in the last 30 Days*; Sort by *Creation Date*,
   **Oldest First**; Limit off.
4. **Repeat with Each** (Photos):
   - **Get Details of Images** → **Name**
   - **Get Details of Images** → **File Size**
   - **Text** → `[Name]_[File Size]`
   - **Add to Variable** `NameList`
5. **Combine Text** → `NameList`, separated by **New Lines**
6. **Get Contents of URL** → `[Base]/missing`
   - Method **PUT**; Header `X-Token` = `[Token]`; Request Body **Text** = the combined text
7. **Set Variable** `Missing` = Contents of URL
8. **Split Text** → `Missing` by **New Lines** → **Set Variable** `MissingList`
9. **If** `Missing` **is** `NONE` → **Stop This Shortcut**

### Pass 2 — upload the missing items

10. **Repeat with Each** — set the input to the **Find Photos** result from step 3:
    - **Get Details of Images** → **Name**
    - **Get Details of Images** → **File Size**
    - **Text** → `[Name]_[File Size]`
    - **If** `MissingList` **contains** that **Text**:
      - **URL Encode** that **Text**
      - **Get Contents of URL** → `[Base]/upload?name=[URL Encoded Text]`
        - Method **PUT**; Header `X-Token` = `[Token]`; Request Body **File** = **Repeat Item**

Match against `MissingList` (the split list), not the raw `Missing` text — a raw-text "contains"
can match one name inside a longer one (for example `_100000` within `_1000000`).

## Automation

Shortcuts → **Automation** → **Charger → Is Connected → Run Immediately** (disable "Notify When
Run") → **Run Shortcut → Send to Pixel**. Optionally add a **Time of Day** automation as well.

iOS limits background execution time, so a large first import may stop partway; the next run's
`/missing` call resumes where it left off. For a large import, run the shortcut in the foreground
with the device on power.

## Naming

`Name_FileSize` is stable per asset (so re-runs are idempotent) and collides only if two assets
share both a filename and an exact byte size. Prepend the creation date for additional certainty.

## Deleting originals after backup (optional)

`PUT /safe_to_delete` returns the names the server has finished, wrapped as `|name|`. These are
*presumed* backed up (time-based, not verified), so confirm the cloud before relying on it. A
second shortcut can build the same names, post them to `/safe_to_delete`, collect the photos whose
`|name|` appears in the response, and pass them to **Delete Photos**. iOS retains deleted items in
Recently Deleted for 30 days.
