# Windrose pressure baker

Publishes a free, global **GFS mean-sea-level pressure** grid the app slices locally — which
**eliminates the Open-Meteo free-tier 429 stall** on the Pro pressure map (no per-pan network, no
paid key, no in-app GRIB decoder). The app side already ships (behind `AppConfig.useGFSPressure`).

## What it does
Every 6h a GitHub Action fetches PRMSL from NOAA's AWS Open Data mirror (byte-range, no key),
decodes GRIB2 with `eccodes` **server-side**, quantizes to int16 hPa×10, stacks 13 forecast frames
(now→+72h), and commits one gzipped `WPRS` binary + a manifest to this repo → served by GitHub Pages.
The app downloads it once per session, caches by ETag, holds it in RAM, and slices any viewport with
zero network. Proven: a real baked file renders correct isobars/H-L in the app (incl. lon-wrap).

- **Not a backend.** It's CI publishing a static asset to a CDN — the same pattern `airports.json`
  already uses and App Review accepts.
- **Fail-safe.** A bad cycle leaves the last-good file live; the app independently falls back to
  Open-Meteo when the file is missing or `modelRunUnix` is >18h old.

## Deploy (one-time, in the `dormatthew/windrose-pressure` repo)
1. Copy `bake.py` to the repo root, and `bake-pressure.yml` to `.github/workflows/`.
2. Ensure GitHub Pages serves this repo (Settings → Pages). Output lands in `data/`, served at
   `https://dormatthew.github.io/windrose-pressure/data/pressure_gfs.bin.gz` (adjust the path/host to
   match your Pages setup, and set the same URL in the app's `AppConfig.gfsPressureURL`).
3. Run the workflow once manually (Actions → bake-pressure → *Run workflow*) to publish the first file.
4. Confirm the file is reachable (open the URL) and the manifest looks right.
5. **In the app:** point `AppConfig.gfsPressureURL` at the live URL, QA on the 17.4 sim with the flag
   forced on (`-SimGFSPressure <url>` or flip `useGFSPressure`), then flip `useGFSPressure = true` and
   ship. Open-Meteo stays wired as the automatic fallback — this cutover is fully reversible.

## Tuning
- `RES` in `bake.py`: `0p50` (720×361, ~2.7 MB gz, crisper — default) or `1p00` (360×181, ~0.58 MB gz,
  lighter). The app reads grid dims from the file header, so **no app change** is needed to switch.
- If MAU grows past ~20–30k, serve via **jsDelivr** (`cdn.jsdelivr.net/gh/<user>/<repo>@latest/data/...`)
  to sidestep GitHub Pages' 100 GB/mo soft bandwidth limit — a one-line change to `gfsPressureURL`.

## Attribution
Data is **NOAA/NCEP GFS** (public domain). The app shows a "Data: NOAA GFS" credit on the pressure tab.

## Local test
```
pip install eccodes
OUT_DIR=data python bake.py        # writes data/pressure_gfs.bin.gz + data/pressure_manifest.json
```
(The Swift byte layout lives in `Windrose/Services/PressureGridStore.swift` — a 46-byte little-endian
header. `bake.py` asserts the layout on every run; keep the two in lockstep.)
