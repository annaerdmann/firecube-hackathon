#!/usr/bin/env python3
# Copyright (c) 2026 EUMETSAT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Inspect one Earth Observation product file; print a short summary, write the full tables to a sidecar.

Usage:
  inspect_product.py <file.nc|file.h5|archive.zip|product-dir> [--member NAME] [--var VARIABLE] [--out table.json]

Prints about twenty lines: dimensions, coordinate variables, global attributes, variable counts per group,
attribute counts per group, any attribute anywhere in the file whose name or value looks time-like (a product
that keeps its real metadata in a named sub-group, not the root, still surfaces it here), every time-like
variable with its dimensionality and monotonicity, and the valid fraction of one reference variable. The
complete per-variable table (dims, shape, dtype, fill, scale, offset, units, long name) and all attributes for
every group go to the sidecar JSON (`groups[<path>].attrs`), which the plan file can reference.

Not read-only: it writes the sidecar JSON next to the product by default (--out to change the path), and it
extracts an archive member to a temporary directory that it removes before exiting. It never modifies the
product file itself.

Run it with an environment that has xarray and a NetCDF/HDF5 engine (a Firecube plugin's `uv run python`
qualifies).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

DATA_SUFFIXES = (".nc", ".nc4", ".h5", ".hdf", ".hdf5", ".he5")
TIME_LIKE_NAME_RE = re.compile(r"time|date|start|end|coverage", re.IGNORECASE)
ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?$")


def looks_time_like(name: str, value) -> bool:
    """True when an attribute's name suggests a timestamp/coverage field, or its value parses as ISO 8601.

    Catches the case where a product's only time information is an attribute of a nested group (for
    example ``platform``/``time_coverage_start``/``time_coverage_end`` in a metadata sub-group), not a
    time-like variable at the root, which the by-variable check below never sees."""
    if TIME_LIKE_NAME_RE.search(name):
        return True
    return bool(ISO8601_RE.match(str(value).strip()))


def resolve(path: Path, member: str | None) -> tuple[Path, Path | None]:
    """Return a readable data file for a file, an archive, or a product directory, and the temporary
    extraction directory to remove when done (None when nothing was extracted)."""
    if path.is_dir():
        candidates = sorted(p for p in path.rglob("*") if p.suffix.lower() in DATA_SUFFIXES)
        if not candidates:
            sys.exit(f"no data file under {path}")
        if member:
            candidates = [p for p in candidates if member in p.name] or candidates
        if len(candidates) > 1:
            print(f"note: {len(candidates)} data files in the directory; inspecting {candidates[0].name} (use --member to pick another)")
        return candidates[0], None
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(DATA_SUFFIXES)]
            if not names:
                sys.exit(f"no data member in {path}: {zf.namelist()[:8]}")
            if member:
                names = [n for n in names if member in n] or names
            if len(names) > 1:
                print(f"note: {len(names)} data members in the archive; inspecting {names[0]} (use --member to pick another)")
            tmp = Path(tempfile.mkdtemp(prefix="inspect_"))
            return Path(zf.extract(names[0], tmp)), tmp
    return path, None


def group_paths(path: Path) -> list[str]:
    """Every group path in the file, root ("") first, walked with netCDF4 or h5netcdf's own h5py dependency,
    whichever is importable. Falls back to root only when neither library or the walk itself is available."""
    groups = [""]

    def walk(grp, prefix, children):
        for name, sub in children(grp):
            gp = f"{prefix}{name}"
            groups.append(gp)
            walk(sub, gp + "/", children)

    try:
        import netCDF4  # noqa: PLC0415

        with netCDF4.Dataset(path) as root:
            walk(root, "", lambda g: g.groups.items())
        return groups
    except Exception:  # noqa: BLE001 - fall back to h5py below
        pass
    try:
        import h5py  # noqa: PLC0415

        with h5py.File(path, "r") as root:
            walk(root, "", lambda g: ((n, s) for n, s in g.items() if isinstance(s, h5py.Group)))
        return groups
    except Exception:  # noqa: BLE001 - neither engine available; report root only
        return [""]


def short(value, width: int = 60) -> str:
    s = str(value).replace("\n", " ")
    return s if len(s) <= width else s[: width - 3] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("--member", help="substring selecting the data file inside an archive or directory")
    ap.add_argument("--var", help="reference variable for the valid-fraction check (default: first floating or scaled 2-D+ variable)")
    ap.add_argument("--out", type=Path, help="sidecar JSON path (default: next to the product, <name>.inspect.json)")
    args = ap.parse_args()

    try:
        import numpy as np
        import xarray as xr
    except ImportError as exc:  # pragma: no cover - environment guard
        sys.exit(f"needs xarray and numpy in this interpreter ({exc}); run with the plugin's `uv run python`")

    def variable_table(ds) -> list[dict]:
        table = []
        for name in ds.variables:
            v = ds[name]
            a = v.attrs
            table.append({"name": name, "dims": list(v.dims), "shape": [int(s) for s in v.shape], "dtype": str(v.dtype),
                          "fill": short(a.get("_FillValue", v.encoding.get("_FillValue", ""))), "scale_factor": short(a.get("scale_factor", "")),
                          "add_offset": short(a.get("add_offset", "")), "units": short(a.get("units", "")), "long_name": short(a.get("long_name", ""), 80),
                          "attrs": {k: short(val, 200) for k, val in a.items()}})
        return table

    data, extract_tmp = resolve(args.path.expanduser().resolve(), args.member)
    try:
        out = args.out or (args.path.expanduser().resolve().with_suffix("").parent / f"{args.path.name}.inspect.json")

        group_tables: dict[str, list[dict]] = {}
        group_meta: dict[str, dict] = {}
        for g in group_paths(data):
            try:
                with xr.open_dataset(data, group=g or None, decode_times=False, mask_and_scale=False) as gds:
                    group_tables[g] = variable_table(gds)
                    group_meta[g] = {"dims": {k: int(s) for k, s in gds.sizes.items()}, "coords": list(gds.coords),
                                      "global_attrs": {k: short(v, 400) for k, v in gds.attrs.items()}}
            except Exception as exc:  # noqa: BLE001 - one bad group must not stop the inspection
                print(f"note: could not open group {g or '/'!r} ({type(exc).__name__}: {short(exc, 80)})")

        # the primary group drives the summary below: the first (root-first) group that has variables
        primary = next((g for g in group_tables if group_tables[g]), next(iter(group_tables), ""))
        table = group_tables.get(primary, [])
        ds = xr.open_dataset(data, group=primary or None, decode_times=False, mask_and_scale=False)
        sidecar = {"file": str(data), "size_bytes": os.path.getsize(data), "primary_group": primary or "/",
                   "dims": group_meta.get(primary, {}).get("dims", {}), "coords": group_meta.get(primary, {}).get("coords", []),
                   "global_attrs": group_meta.get(primary, {}).get("global_attrs", {}), "variables": table,
                   "groups": {(g or "/"): {"variables": t, "attrs": group_meta.get(g, {}).get("global_attrs", {})}
                              for g, t in group_tables.items()}}

        # --- summary ---
        print(f"file: {data.name}  ({os.path.getsize(data) / 1e6:.2f} MB)")
        per_group = {(g or "/"): len(t) for g, t in group_tables.items()}
        print(f"groups: {len(group_tables)} | variables per group: {per_group}")
        if primary:
            print(f"primary group (most variables; use --member or open a specific group otherwise): {primary}")
        print(f"dims: {dict(sidecar['dims'])}")
        print("coords: " + (", ".join(f"{c}{tuple(ds[c].dims)} {ds[c].dtype}" for c in ds.coords) or "none declared"))
        attrs = list(ds.attrs.items())
        print(f"global attrs ({len(attrs)}): " + "; ".join(f"{k}={short(v, 40)}" for k, v in attrs[:12]) + (" ..." if len(attrs) > 12 else ""))
        attr_counts = {(g or "/"): len(meta.get("global_attrs", {})) for g, meta in group_meta.items()}
        print(f"attributes per group: {attr_counts}")
        by_ndim: dict[int, int] = {}
        dtypes: dict[str, int] = {}
        for row in table:
            by_ndim[len(row["dims"])] = by_ndim.get(len(row["dims"]), 0) + 1
            dtypes[row["dtype"]] = dtypes.get(row["dtype"], 0) + 1
        print(f"variables (primary group): {len(table)} | by ndim {dict(sorted(by_ndim.items()))} | dtypes {dtypes}")
        twod = [r for r in table if len(r["dims"]) == 2]
        if twod:
            shapes = {tuple(r["shape"]) for r in twod}
            print(f"2-D variables: {len(twod)} on dims {sorted({tuple(r['dims']) for r in twod})} shapes {sorted(shapes)}")

        # time-like variables: name or units; a non-numeric (string date, object) variable is reported, never crashes the run
        timeish = [n for n in ds.variables if "time" in n.lower() or "since" in str(ds[n].attrs.get("units", "")).lower()]
        for n in timeish:
            try:
                v = ds[n]
                line = f"time-like {n}{tuple(v.dims)} {v.dtype} units={short(v.attrs.get('units', ''), 40)}"
                if v.dtype.kind not in "iuf":
                    print(line + " | non-numeric: monotonicity not checked")
                    continue
                vals = np.asarray(v.values)
                fill = v.attrs.get("_FillValue", v.encoding.get("_FillValue"))
                valid = np.ones(vals.shape, bool) if fill is None else vals != fill
                line += f" fill={short(fill, 20)}"
                if vals.ndim >= 1 and vals.size > 1:
                    if vals.ndim == 1:
                        d = np.diff(vals[valid].astype("float64"))
                        line += f" | 1-D monotonic={bool(np.all(d > 0)) if d.size else 'n/a'}"
                    else:
                        masked = np.where(valid, vals.astype("float64"), np.nan)
                        row_mean = np.nanmean(masked, axis=1)
                        d = np.diff(row_mean[~np.isnan(row_mean)])
                        spread = np.nanmax(np.nanmax(masked, axis=1) - np.nanmin(masked, axis=1))
                        line += f" | per-row mean monotonic={bool(np.all(d > 0)) if d.size else 'n/a'} | max spread within a row={spread:g}"
                print(line)
            except Exception as exc:  # noqa: BLE001 - report and move on; one variable must not abort the sidecar write
                print(f"time-like {n}: could not inspect ({type(exc).__name__}: {short(exc, 80)})")
        if not timeish:
            print("time-like variables: none found by name or units")

        # time-like attributes, in any group (not only the primary one): the only time information a
        # product carries may be an attribute of a nested metadata group rather than a variable.
        time_like_attrs = [(g or "/", k, v) for g, meta in group_meta.items()
                           for k, v in meta.get("global_attrs", {}).items() if looks_time_like(k, v)]
        if time_like_attrs:
            for g, k, v in time_like_attrs:
                print(f"time-like attributes: {g}:{k}={short(v, 60)}")
        else:
            print("time-like attributes: none found")

        # reference variable valid fraction
        ref = args.var
        if not ref:
            for row in table:
                if len(row["dims"]) >= 2 and (row["dtype"].startswith("float") or row["scale_factor"]):
                    ref = row["name"]
                    break
        if ref and ref in ds:
            try:
                v = ds[ref]
                if v.dtype.kind not in "iuf":
                    print(f"reference {ref}{tuple(v.dims)} {v.dtype}: non-numeric, valid fraction not computed")
                else:
                    vals = np.asarray(v.values)
                    fill = v.attrs.get("_FillValue", v.encoding.get("_FillValue"))
                    valid = np.isfinite(vals.astype("float64")) if fill is None else (vals != fill) & np.isfinite(vals.astype("float64"))
                    print(f"reference {ref}{tuple(v.dims)} {v.dtype} fill={short(fill, 20)} scale={short(v.attrs.get('scale_factor', ''), 12)} valid fraction={valid.mean():.3f}")
            except Exception as exc:  # noqa: BLE001 - report and move on; must not abort the sidecar write
                print(f"reference {ref}: could not compute valid fraction ({type(exc).__name__}: {short(exc, 80)})")
        elif ref:
            print(f"reference {ref}: not found in the primary group ({primary or '/'})")
        ds.close()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(sidecar, indent=1), encoding="utf-8")
        total_vars = sum(len(t) for t in group_tables.values())
        print(f"full tables ({total_vars} variables across {len(group_tables)} groups, all attributes): {out}")
        return 0
    finally:
        if extract_tmp is not None:
            shutil.rmtree(extract_tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
