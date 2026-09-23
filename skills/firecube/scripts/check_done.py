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

"""Definition-of-done check for a Firecube plugin: one command, PASS/FAIL lines, details on disk.

Usage (from anywhere; run with the plugin project's interpreter so xarray and zarr are available):
  uv run --project <plugin-project> python check_done.py --plugin-id <id> --project <plugin-project> \
      --input-dir <dir with real products> [--ref-var VARIABLE] [--min-variables N] [--group-regex RE] \
      [--author NAME --email ADDR --license SPDX] [--fixture-env VAR ...] [--plan PATH] [--scratch DIR] [--keep]
      [--ingest-option key=value ...] [--workers N] [--skip-lifecycle]

Checks, in order: the static contract check (check_plugin_contract.py beside this file); metadata as given
(optional); tests run against real fixtures with none skipped, and again without the fixture variables (a fresh
checkout) with no errors or failures; lint; the plugin is registered; one ingestion, with files processed equal to
the products in the input; groups written, with a 1-D monotonic ordering coordinate; a second ingestion with
pipeline_batch_size=1 producing the same groups, sizes, values and attributes; one variable's finite count and sum
equal to the decoded source; then the lifecycle (Zarr, skip with --skip-lifecycle): a pipeline_workers>1 run equal
to the one-worker store, a rerun without a flag refused, a rerun with resume_existing over the same window leaving
the store equal (the state-aware skip), an extension (first half of the input, then all of it with resume_existing)
equal to the one-shot store, and a force_reingest run equal as well; a plan file with the required sections. On
firecube 0.1.5, two of these differ and are treated as the expected outcome, not a plugin defect: the
pipeline_workers>1 equality comparison is skipped (a documented version limit), and resume_existing over a fully
overlapping window is expected to raise the documented refusal instead of leaving the store equal. Prints a block
to paste into the plan's Log. Writes only a fresh subdirectory it creates for itself inside --scratch (default:
<project>/.check_done; removed after the run unless --keep); it never creates or removes --scratch itself beyond
that subdirectory. Running tests or an editable install inside the plugin project also writes uv's own .venv/ and
uv.lock there, not under --scratch.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

DATA_SUFFIXES = (".nc", ".nc4", ".h5", ".hdf", ".hdf5")
IGNORED = (".xml", ".jpg", ".jpeg", ".png", ".txt", ".md", ".json", ".py", ".pyc")
PLAN_SECTIONS = ["Operator", "Evidence", "Design", "Feasib", "Steps", "Open", "Next", "Log"]
results: list[tuple[str, bool, str]] = []
infos: list[tuple[str, str]] = []


def check(name: str, ok, detail="") -> None:
    results.append((name, bool(ok), str(detail)[:300]))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:160]}")


def skip(name: str, detail="", counted: bool = True) -> None:
    """counted=True records a not-passed result (missing evidence); counted=False is only for a documented,
    version-specific gate that is not a plugin defect and should not by itself block completion."""
    if counted:
        results.append((name, False, str(detail)[:300]))
    print(f"[SKIP] {name}: {str(detail)[:160]}")


def info(name: str, detail) -> None:
    infos.append((name, str(detail)[:300]))
    print(f"[info] {name}: {str(detail)[:160]}")


def sh(cmd, cwd=None, env=None, timeout=3600):
    """Run cmd; a missing executable or a timeout is reported as a failed run (non-zero rc, message in stderr),
    never a traceback."""
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return 127, "", f"executable not found: {exc.filename or cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(str(c) for c in cmd)}"
    return r.returncode, r.stdout, r.stderr


def products_in(input_dir: Path) -> set[str]:
    names = set()
    for p in input_dir.iterdir():
        if p.name.startswith(".") or p.suffix.lower() in IGNORED:
            continue
        stem = p.name[:-4] if p.name.endswith(".zip") else p.name
        names.add(stem)
    return names


def source_files(input_dir: Path, extract_root: Path) -> list[Path]:
    """Every data file in the input, extracting archive members that have no extracted twin into extract_root
    (a directory the caller creates and removes; this function never creates or removes it)."""
    files = {p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in DATA_SUFFIXES}
    for zp in sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() not in DATA_SUFFIXES):
        if not zipfile.is_zipfile(zp):
            continue
        base = zp.name[:-4] if zp.name.endswith(".zip") else zp.name
        if any(base in str(f) for f in files):
            continue
        with zipfile.ZipFile(zp) as zf:
            for m in zf.namelist():
                if m.lower().endswith(DATA_SUFFIXES):
                    files.add(Path(zf.extract(m, extract_root)))
    return sorted(files)


def group_paths(path: Path) -> list[str]:
    """Every group path in a NetCDF4/HDF5 file, root ("") first; root only when neither engine is importable
    or the file is flat. Mirrors inspect_product.py's group_paths."""
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
    except Exception:  # noqa: BLE001
        pass
    try:
        import h5py  # noqa: PLC0415

        with h5py.File(path, "r") as root:
            walk(root, "", lambda g: ((n, s) for n, s in g.items() if isinstance(s, h5py.Group)))
    except Exception:  # noqa: BLE001 - report root only
        pass
    return groups


def source_figures(files: list[Path], var: str | None) -> dict:
    import numpy as np
    import xarray as xr

    tot, fin, n, size, chosen = 0.0, 0, 0, 0, var
    for f in files:
        size += f.stat().st_size
        for group in group_paths(f):
            try:
                with xr.open_dataset(f, group=group or None) as ds:
                    if chosen is None:
                        chosen = next((name for name in ds.data_vars if ds[name].ndim >= 2 and np.issubdtype(ds[name].dtype, np.floating)), None)
                    if chosen and chosen in ds:
                        a = ds[chosen].values
                        tot += float(np.nansum(a))
                        fin += int(np.isfinite(a).sum())
                        n += 1
                        break
            except Exception:  # noqa: BLE001 - try the next group
                continue
    return {"var": chosen, "files": n, "sum": tot, "finite": fin, "bytes": size}


def inspect_store(path: Path, var: str | None) -> dict:
    import numpy as np
    import xarray as xr
    import zarr

    out: dict = {}
    root = zarr.open_group(str(path), mode="r")

    def walk(g, prefix=""):
        for k, _ in g.groups():
            gp = f"{prefix}{k}"
            try:
                ds = xr.open_zarr(str(path), group=gp, consolidated=False)
                if len(ds.data_vars) == 0:
                    walk(g[k], gp + "/")
                    continue
                rec = {"sizes": {d: int(s) for d, s in ds.sizes.items()}, "nvars": len(ds.data_vars), "mono": [],
                       "coord_warnings": [], "attrs": {a: str(v) for a, v in ds.attrs.items()}}
                for c in ds.coords:
                    v = ds[c].values
                    if v.ndim == 1 and v.size > 1:
                        try:
                            if bool(np.all(np.diff(v.astype("int64")) > 0)):
                                rec["mono"].append(c)
                        except (TypeError, ValueError, OverflowError) as exc:
                            rec["coord_warnings"].append(
                                f"{c}: cannot check monotonic int64 ordering ({type(exc).__name__}: {str(exc)[:80]})"
                            )
                if var and var in ds:
                    a = ds[var].values
                    rec["sum"] = float(np.nansum(a))
                    rec["finite"] = int(np.isfinite(a).sum())
                out[gp] = rec
            except Exception as exc:  # noqa: BLE001
                out[gp] = {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}
            walk(g[k], gp + "/")

    walk(root)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin-id", required=True)
    ap.add_argument("--project", type=Path, required=True, help="plugin project directory (pyproject.toml)")
    ap.add_argument("--input-dir", type=Path, required=True, help="directory of real products")
    ap.add_argument("--ref-var", help="variable for the source-equivalence figures (default: first floating 2-D+ variable)")
    ap.add_argument("--min-variables", type=int, default=0, help="minimum data variables per group (0 = report only)")
    ap.add_argument("--group-regex", help="every group name must match, e.g. the platform identifier")
    ap.add_argument("--author")
    ap.add_argument("--email")
    ap.add_argument("--license")
    ap.add_argument("--fixture-env", action="append", default=[], help="environment variable the tests read for the fixture directory (auto-detected from the tests when omitted)")
    ap.add_argument("--plan", type=Path, help="plan file (default: *plan*.md in the project's parent)")
    ap.add_argument("--scratch", type=Path, help="where the check writes its stores (default: <project>/.check_done)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch stores")
    ap.add_argument("--ingest-option", action="append", default=[], help="extra --option key=value for every ingestion (plugin options)")
    ap.add_argument("--workers", type=int, default=4, help="pipeline_workers for the parallel run (default 4)")
    ap.add_argument("--first-part-glob", help="products for the first run of the extension check (default: the earlier half of the dates in the file names)")
    ap.add_argument("--skip-lifecycle", action="store_true", help="skip the workers/rerun/resume/extension/force runs (Parquet plugins)")
    args = ap.parse_args()

    proj = args.project.expanduser().resolve()
    input_dir = args.input_dir.expanduser().resolve()
    scratch = (args.scratch or proj / ".check_done").expanduser().resolve()
    pyproject = proj / "pyproject.toml"
    if not pyproject.exists():
        check("plugin project", False, f"no pyproject.toml in {proj}")
        return finish()
    contract = Path(__file__).with_name("check_plugin_contract.py")
    if contract.exists():
        rc, out, err = sh([sys.executable, str(contract), str(proj)])
        summary = next((line for line in out.splitlines() if line.startswith("CONTRACT")), (out + err).strip()[-160:])
        fails = [line for line in out.splitlines() if line.startswith("[FAIL]")]
        check("contract: public Firecube API only, no framework overrides, no empty discovery", rc == 0, "; ".join(f[7:90] for f in fails) or summary)
    text = pyproject.read_text(encoding="utf-8")
    check("entry point registered under the plugin id", re.search(rf"^\s*{re.escape(args.plugin_id)}\s*=", text, re.M), args.plugin_id)
    for key, val in (("author", args.author), ("email", args.email), ("licence", args.license)):
        if val:
            check(f"{key} as given by the operator", val in text, val)
    src_text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore") for p in glob.glob(str(proj / "src" / "**" / "*.py"), recursive=True))
    test_text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore") for p in glob.glob(str(proj / "tests" / "**" / "*.py"), recursive=True))
    home = str(Path.home())
    check("no absolute developer paths in code or tests", home not in src_text + test_text, "")

    env = dict(os.environ)
    fixture_vars = set(args.fixture_env) | set(re.findall(r"""environ(?:\.get)?\(\s*["']([A-Z][A-Z0-9_]+)["']""", test_text)) | set(re.findall(r"""getenv\(\s*["']([A-Z][A-Z0-9_]+)["']""", test_text))
    for var in fixture_vars:
        env[var] = str(input_dir)
    info("fixture env vars set to the input dir", sorted(fixture_vars) or "none found in tests")
    rc, out, err = sh(["uv", "run", "pytest", "-q"], cwd=proj, env=env)
    tail = (out + err).strip().splitlines()[-1] if (out + err).strip() else ""
    check("tests pass", rc == 0, tail)
    skipped = re.search(r"(\d+) skipped", tail)
    check("no test skipped", not skipped or int(skipped.group(1)) == 0, tail)
    fresh_env = {k: v for k, v in os.environ.items() if k not in fixture_vars}
    rc, out, err = sh(["uv", "run", "pytest", "-q"], cwd=proj, env=fresh_env)
    tail = (out + err).strip().splitlines()[-1] if (out + err).strip() else ""
    bad = re.search(r"(\d+) (?:failed|errors?)\b", tail)
    check("fresh checkout (fixture variables unset): no errors or failures, skips allowed", not bad and rc in (0, 5), tail)
    rc, out, err = sh(["uv", "run", "ruff", "check"], cwd=proj, env=env)
    check("ruff clean", rc == 0, (out + err).strip().splitlines()[-1] if (out + err).strip() else "")

    rc, out, err = sh(["firecube", "--version"])
    if rc != 0:
        check("firecube is on PATH (firecube --version)", False, err.strip() or out.strip() or f"exit {rc}")
        return finish()
    m = re.search(r"\d+\.\d+\.\d+", out + err)
    version = m.group(0) if m else (out + err).strip()
    info("firecube version", version)

    rc, out, _ = sh(["firecube", "plugins", "list"])
    registered = bool(re.search(rf"\b{re.escape(args.plugin_id)}\b", out))
    check("plugin registered (firecube plugins list)", registered, out.strip()[:100])
    if not registered:
        return finish()

    scratch.mkdir(parents=True, exist_ok=True)  # the user-named base directory; never removed by this script
    work = Path(tempfile.mkdtemp(dir=scratch, prefix="check_done_"))
    products = products_in(input_dir)
    info("products in input", f"{len(products)}")

    def ingest(name: str, *extra: str, source: Path | None = None, target: Path | None = None):
        target = target or work / f"{name}.zarr"
        cmd = ["firecube", "ingest", args.plugin_id, "--input-data", str(source or input_dir), "--target", f"file://{target}",
               "--product-name", args.plugin_id, "--storage-type", "local", "--storage-driver", "fsspec",
               "--output-format", "zarr", "--write-mode", "direct", "--option", "no_progress=true",
               *[x for o in args.ingest_option for x in ("--option", o)], *extra]
        rc, out, err = sh(cmd)
        found = re.search(r"Found (\d+) files", err)
        processed = re.search(r'"files_processed":\s*(\d+)', out)
        errors = [line for line in err.splitlines() if re.search(r"error|traceback|failed", line, re.I) and not line.startswith("{")]
        return rc, target, int(found.group(1)) if found else None, int(processed.group(1)) if processed else None, ("ok" if rc == 0 else (errors[-1] if errors else err.strip().splitlines()[-1] if err.strip() else ""))[:200]

    rc, native, found, processed, last = ingest("native")
    check("first ingestion succeeds", rc == 0, last)
    reached = processed if processed is not None else found
    check("files processed equal the products in the input", reached == len(products), f"found={found} processed={processed} products={len(products)}")
    if rc != 0:
        return finish(work, args.keep)

    extract_tmp = Path(tempfile.mkdtemp(dir=work, prefix="extract_"))
    try:
        files = source_files(input_dir, extract_tmp)
        src = source_figures(files, args.ref_var)
    finally:
        shutil.rmtree(extract_tmp, ignore_errors=True)
    ref = src["var"]
    n = inspect_store(native, ref)
    groups = {k: v for k, v in n.items() if "error" not in v}
    check("at least one group written", bool(groups), [k for k in n][:6])
    if groups:
        if args.group_regex:
            check("group names match the identity regex", all(re.search(args.group_regex, g) for g in groups), list(groups)[:6])
        check("1-D monotonic ordering coordinate in every group", all(v["mono"] for v in groups.values()), {g: v["mono"] for g, v in groups.items()})
        nvars = {g: v["nvars"] for g, v in groups.items()}
        if args.min_variables:
            check(f"at least {args.min_variables} variables per group", all(c >= args.min_variables for c in nvars.values()), nvars)
        else:
            info("variables per group", nvars)
        info("groups", {g: v["sizes"] for g, v in groups.items()})
        coord_warnings = {g: v["coord_warnings"] for g, v in groups.items() if v.get("coord_warnings")}
        if coord_warnings:
            info("coordinate conversion warnings", coord_warnings)
        if ref:
            cube_sum = sum(v.get("sum", 0.0) for v in groups.values())
            cube_fin = sum(v.get("finite", 0) for v in groups.values())
            same = cube_fin == src["finite"] and abs(cube_sum - src["sum"]) <= 1e-6 * max(1.0, abs(src["sum"]))
            check(f"{ref}: finite count and sum equal the decoded source", same, f"cube finite={cube_fin} sum={cube_sum:.6g} | source finite={src['finite']} sum={src['sum']:.6g} over {src['files']} files")
        else:
            skip("source-equivalence check", "no reference variable found in the store or source; pass --ref-var", counted=True)
        size = sum(p.stat().st_size for p in native.rglob("*") if p.is_file())
        info("store bytes / source bytes", f"{size} / {src['bytes']} = {size / max(1, src['bytes']):.2f}x")

    rc2, split, _, _, last2 = ingest("split", "--option", "pipeline_batch_size=1")
    check("split-batch ingestion (pipeline_batch_size=1) succeeds", rc2 == 0, last2)
    if rc2 == 0 and groups:
        s = {k: v for k, v in inspect_store(split, ref).items() if "error" not in v}
        same_groups = set(groups) == set(s)
        check("split-batch: same groups", same_groups, sorted(set(groups) ^ set(s))[:6])
        if same_groups:
            check("split-batch: same sizes", all(groups[g]["sizes"] == s[g]["sizes"] for g in groups), "")
            if ref:
                check("split-batch: same values", all(groups[g].get("finite") == s[g].get("finite") and abs(groups[g].get("sum", 0) - s[g].get("sum", 0)) <= 1e-6 * max(1.0, abs(groups[g].get("sum", 0))) for g in groups), "")
            check("split-batch: same group attributes", all(groups[g]["attrs"] == s[g]["attrs"] for g in groups), {g: "differs" for g in groups if groups[g]["attrs"] != s[g]["attrs"]})

    if not args.skip_lifecycle and groups:
        lifecycle(ingest, native, input_dir, work, args.workers, version, args.first_part_glob)

    plans = [args.plan] if args.plan else [Path(p) for p in glob.glob(str(proj.parent / "*plan*.md"))]
    plans = [p for p in plans if p and p.exists()]
    check("plan file exists", bool(plans), plans[0] if plans else "")
    if plans:
        ptxt = plans[0].read_text(encoding="utf-8")
        missing = [sec for sec in PLAN_SECTIONS if not re.search(rf"^##+\s*.*{sec}", ptxt, re.M | re.I)]
        check("plan has the required sections", not missing, f"missing: {missing}" if missing else "all present")
    return finish(work, args.keep)


def same_store(a: Path, b: Path) -> tuple[bool, str]:
    rc, out, err = sh(["firecube", "zarr", "compare", f"file://{a}", f"file://{b}", "--storage-type", "local",
                       "--storage-driver", "fsspec"])
    lines = (out + err).strip().splitlines()
    return rc == 0, (lines[-1] if lines else f"exit {rc}")[:160]


def first_part(entries: list[Path], glob_pattern: str | None) -> list[Path]:
    """Products for the first extension run, split on a whole time unit (a date), never inside one."""
    if glob_pattern:
        return [p for p in entries if fnmatch.fnmatch(p.name, glob_pattern)]
    dates = {p: m.group(1) for p in entries if (m := re.search(r"(?<!\d)((?:19|20)\d{6})(?:T\d{4,6})?", p.name))}
    ordered = sorted(set(dates.values()))
    if len(ordered) < 2 or len(dates) != len(entries):
        return []
    keep = set(ordered[: len(ordered) // 2])
    return [p for p in entries if dates[p] in keep]


def lifecycle(ingest, native: Path, input_dir: Path, scratch: Path, workers: int, version: str = "", glob_pattern: str | None = None) -> None:
    """Parallel run, reruns, extension and force against the one-shot store (firecube's resume contract).
    On firecube 0.1.7 (the default assumed here) all four are expected to succeed: a pipeline_workers>1 run stays
    value-equal to the one-worker store through the ordered write gate, and resume_existing=true over a fully
    overlapping window takes the state-aware skip and leaves the store equal rather than refusing. If
    firecube --version instead reports 0.1.5, this function falls back to that version's two documented limits
    (plugin-traps.md): order-dependent append plugins should stay at pipeline_workers=1, so the equality
    comparison is skipped rather than run, and resume_existing=true over a fully overlapping window is expected to
    raise the documented refusal instead of leaving the store equal; both are the expected 0.1.5 outcome, not a
    plugin defect."""
    is_015 = version.startswith("0.1.5")
    rc, par, _, _, last = ingest("workers", "--option", f"pipeline_workers={workers}")
    check(f"pipeline_workers={workers} ingestion succeeds", rc == 0, last)
    if rc == 0:
        if is_015:
            skip(f"pipeline_workers={workers} store equals the one-worker store (zarr compare)",
                 "0.1.5: keep pipeline_workers=1 for order-dependent append plugins; equality comparison skipped (plugin-traps.md)",
                 counted=False)
        else:
            ok, detail = same_store(native, par)
            check(f"pipeline_workers={workers} store equals the one-worker store (zarr compare)", ok, detail)

    rerun = scratch / "rerun.zarr"
    shutil.copytree(native, rerun)
    rc, _, _, _, last = ingest("rerun", target=rerun)
    check("rerun without a flag is refused (ResumeConflictError)", rc != 0 and re.search(r"existing entries|resume_existing|ResumeConflict", last, re.I), last)
    rc, _, _, _, last = ingest("rerun", "--option", "resume_existing=true", target=rerun)
    if is_015:
        refused = rc != 0 and bool(re.search(r"Refusing overlapping resume append", last, re.I))
        check("rerun with resume_existing=true over the same window (expected refusal on 0.1.5)", refused, last)
    else:
        check("rerun with resume_existing=true succeeds", rc == 0, last)
        if rc == 0:
            ok, detail = same_store(native, rerun)
            check("rerun with resume_existing=true leaves the store equal", ok, detail)

    entries = sorted(p for p in input_dir.iterdir() if not p.name.startswith(".") and p.suffix.lower() not in IGNORED)
    head = first_part(entries, glob_pattern)
    if head and len(head) < len(entries):
        part = scratch / "input_first_part"
        part.mkdir()
        for p in head:
            (part / p.name).symlink_to(p)
        ext = scratch / "extend.zarr"
        rc, _, _, _, last = ingest("extend", source=part, target=ext)
        check("extension: first half of the input ingests", rc == 0, last)
        if rc == 0:
            rc, _, _, _, last = ingest("extend", "--option", "resume_existing=true", target=ext)
            check("extension: full input with resume_existing=true succeeds", rc == 0, last)
            if rc == 0:
                ok, detail = same_store(native, ext)
                check("extension store equals the one-shot store", ok, detail)
    else:
        info("extension", "skipped: no whole-time-unit split found; pass --first-part-glob")

    forced = scratch / "force.zarr"
    shutil.copytree(native, forced)
    rc, _, _, _, last = ingest("force", "--option", "force_reingest=true", target=forced)
    check("force_reingest=true succeeds", rc == 0, last)
    if rc == 0:
        ok, detail = same_store(native, forced)
        check("force_reingest store equals the one-shot store", ok, detail)


def finish(scratch: Path | None = None, keep: bool = False) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"DONE {passed}/{len(results)} checks passed" + ("" if passed == len(results) else " -- not done: fix the FAIL lines, then rerun"))
    print("paste the lines above into the plan's Log with the command that produced them")
    if scratch and not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
