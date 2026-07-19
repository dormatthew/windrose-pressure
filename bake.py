#!/usr/bin/env python3
"""Windrose pressure baker — publishes ONE compact global GFS mean-sea-level pressure grid
for the app to slice locally (killing the per-pan Open-Meteo 429 stall).

Runs in a GitHub Action every 6h. Fetches PRMSL from NOAA's AWS Open Data mirror via .idx
byte-ranges (primary) with NOMADS grib-filter as fallback, decodes GRIB2 with eccodes
(SERVER-SIDE only — the app never touches GRIB2), quantizes to int16 hPa*10, stacks the
forecast frames, and writes `data/pressure_gfs.bin.gz` + `data/pressure_manifest.json`.

Discipline: write to a temp path, validate (magic + hPa range), then atomically move into
place. On any failure it exits non-zero WITHOUT touching the live file, so the last-good
grid keeps serving (the app also independently falls back to Open-Meteo if the file is stale).

Deps (installed in the workflow): eccodes  (pip install eccodes)
"""
import urllib.request, urllib.error, struct, gzip, io, os, sys, json, datetime, tempfile, shutil
import eccodes

RES = "0p50"                     # 0p50 = 720x361 (~2.7MB gz) | 1p00 = 360x181 (~580KB gz)
FHRS = list(range(0, 73, 6))     # f000..f072 → 13 six-hourly frames (now → +72h)
OUT_DIR = os.environ.get("OUT_DIR", "data")
AWS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
NOMADS = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_{r}.pl"
UA = {"User-Agent": "windrose-pressure-baker/1 (+https://mwsupplylimited.github.io/windrose)"}


def http(url, rng=None, timeout=90):
    req = urllib.request.Request(url, headers=dict(UA))
    if rng:
        req.add_header("Range", f"bytes={rng}")
    return urllib.request.urlopen(req, timeout=timeout).read()


def latest_run():
    """Newest cycle whose f072 .idx exists on AWS (i.e. a complete run)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cyc = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 6) * 6)
    for back in range(5):
        c = cyc - datetime.timedelta(hours=6 * back)
        d, hh = c.strftime("%Y%m%d"), c.strftime("%H")
        try:
            http(f"{AWS}/gfs.{d}/{hh}/atmos/gfs.t{hh}z.pgrb2.{RES}.f072.idx", timeout=30)
            return d, hh, c
        except Exception:
            continue
    raise SystemExit("baker: no complete GFS run in the last 24h")


def prmsl_from_aws(d, hh, fh):
    grib = f"{AWS}/gfs.{d}/{hh}/atmos/gfs.t{hh}z.pgrb2.{RES}.f{fh:03d}"
    idx = http(grib + ".idx", timeout=30).decode()
    lines = idx.strip().split("\n")
    for i, ln in enumerate(lines):
        f = ln.split(":")
        if len(f) > 4 and f[3] == "PRMSL" and f[4] == "mean sea level":
            start = int(f[1])
            end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else ""
            return http(grib, rng=f"{start}-{end}")
    raise RuntimeError(f"PRMSL not in idx for f{fh:03d}")


def prmsl_from_nomads(d, hh, fh):
    r = RES.replace("p", ".")  # 0p50 -> 0.50 ... nomads uses 0p25/0p50/1p00 in the leftvar too; keep simple
    url = (NOMADS.format(r=RES) + f"?file=gfs.t{hh}z.pgrb2.{RES}.f{fh:03d}"
           "&lev_mean_sea_level=on&var_PRMSL=on&dir=%2Fgfs.{d}%2F{hh}%2Fatmos".format(d=d, hh=hh))
    return http(url)


def decode(grib_bytes):
    gid = eccodes.codes_new_from_message(grib_bytes)
    try:
        Ni = eccodes.codes_get(gid, "Ni")
        Nj = eccodes.codes_get(gid, "Nj")
        vals = eccodes.codes_get_values(gid)   # Pa, GFS scan order N->S, W->E, lon 0..359
        return Ni, Nj, vals
    finally:
        eccodes.codes_release(gid)


def bake():
    d, hh, cyc = latest_run()
    print(f"baker: gfs.{d}/{hh}z ({cyc:%Y-%m-%d %H}Z), res {RES}")
    frames, Ni, Nj = [], None, None
    for fh in FHRS:
        try:
            rec = prmsl_from_aws(d, hh, fh)
        except Exception as e:
            print(f"  f{fh:03d}: AWS failed ({e}); NOMADS fallback")
            rec = prmsl_from_nomads(d, hh, fh)
        ni, nj, vals = decode(rec)
        Ni, Nj = ni, nj
        hpa10 = [max(-32768, min(32767, int(round(v / 100.0 * 10)))) for v in vals]
        lo, hi = min(hpa10) / 10.0, max(hpa10) / 10.0
        if not (800 <= lo <= 1100 and 800 <= hi <= 1100):
            raise SystemExit(f"baker: insane pressure f{fh:03d} {lo}-{hi} hPa — aborting, keep last good")
        frames.append(hpa10)
        print(f"  f{fh:03d}: {ni}x{nj}, {lo:.0f}-{hi:.0f} hPa")

    # WPRS binary (46-byte header, little-endian; MUST match PressureGridStore.swift)
    buf = io.BytesIO()
    buf.write(b"WPRS")
    buf.write(struct.pack("<BB", 1, 0))                 # version, pad
    buf.write(struct.pack("<HHHH", Ni, Nj, len(frames), 0))
    buf.write(struct.pack("<ffff", 0.0, 90.0, 0.5 if RES == "0p50" else 1.0,
                          -0.5 if RES == "0p50" else -1.0))
    buf.write(struct.pack("<III", int(cyc.timestamp()), 21600, int(cyc.timestamp())))
    buf.write(struct.pack("<I", 0))                     # pad → header = 46 bytes
    for fr in frames:
        buf.write(struct.pack(f"<{len(fr)}h", *fr))
    raw = buf.getvalue()
    assert len(raw) == 46 + Ni * Nj * len(frames) * 2, "header/layout drift"
    gz = gzip.compress(raw, 9)

    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=OUT_DIR, suffix=".tmp")
    tmp.write(gz); tmp.close()
    shutil.move(tmp.name, os.path.join(OUT_DIR, "pressure_gfs.bin.gz"))
    manifest = {"run": f"{d}{hh}", "modelRunUnix": int(cyc.timestamp()),
                "frames": len(frames), "res": RES, "nLon": Ni, "nLat": Nj,
                "bytes": len(gz), "bakedAtUnix": int(datetime.datetime.now(datetime.timezone.utc).timestamp())}
    with open(os.path.join(OUT_DIR, "pressure_manifest.json"), "w") as f:
        json.dump(manifest, f)
    print(f"baker: wrote {len(gz)/1024/1024:.2f} MB gz ({len(frames)} frames @ {Ni}x{Nj})")


if __name__ == "__main__":
    bake()
