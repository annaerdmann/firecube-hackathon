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

"""Static contract check for a Firecube plugin: public API only, core owns run/resume, no known traps.

Usage (no data, no install, seconds; stdlib only):
  python check_plugin_contract.py <plugin-project> [--json OUT] [--exclude GLOB ...] [--max-locations N]

FAIL (exit 1):
  firecube-imports        imports outside firecube.ingestor.api, firecube.core.api, firecube.ingestor.extensions
  framework-overrides     a plugin class defines run, finalize_pipeline, or a private Firecube method
                          (_aggregate_metrics, _resolve_time_dim_name, _create_batches, ...); _process_batch is
                          allowed only on a direct BaseIngestor subclass
  private-core-access     self._name / super()._name that no plugin class defines (inherited from Firecube, or
                          missing like self._write_lock on 0.1.7); self._log is a WARN
  discovery-empty-return  discover_source_files returns [], (), None, or an empty iterator (hides items from core)
  referenced-files        README, CONTRIBUTING, docs, or notebooks name a repository file that does not exist
WARN (review, exit 0):
  store-writes            attrs writes, zarr.open_group in a write mode, consolidate_metadata in plugin code
  own-ordering            threading.Condition in plugin code (core orders append writes)
  payload-hash            hashlib digests in plugin code (prefer container checksums at discovery)
  time-encoding           a Zarr append plugin that never sets encoding units
  tests-outside-repo      test fixtures resolved outside the repository
  firecube-pin            firecube requirement not pinned
  performance-claims      docs lines with timing numbers: each needs a benchmark record
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
from pathlib import Path

ALLOWED_PREFIXES = ("firecube.ingestor.api", "firecube.core.api", "firecube.ingestor.extensions")
FORBIDDEN_PUBLIC_OVERRIDES = {"run", "finalize_pipeline"}
# Private names on Firecube's ingestor classes (0.1.5 and 0.1.7). Extended at runtime from an installed firecube.
CORE_PRIVATE = {
    "_aggregate_metrics", "_bind_index_at_startup", "_cached_zarr_schema", "_check_legacy_index_record_at_startup",
    "_compile_write_intents", "_create_batches", "_emit_index_ensured_event", "_ensure_index_identity_at_startup",
    "_ensure_index_record_at_startup", "_materialize", "_process_batch", "_resolve_index_binding_at_startup",
    "_resolve_time_dim_name", "_validate_context_hook_signatures", "_validate_duckdb_persistence_contract",
    "_verify_existing_cube_batch_groups", "_verify_per_group_identity_at_startup", "_verify_per_group_identity_at_store",
    "_verify_schema_at_pod_startup", "_write_lock", "_write_gate", "_chunk_manager", "_span_recorder", "_alignment",
    "_append_order", "_log",
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist",
             ".claude", ".check_done"}
PATH_RE = re.compile(r"(?<![\w./-])((?:scripts|tools|bin|examples|docs|tests|notebooks|src)/[\w./-]+\.(?:py|sh|md|ipynb|toml|ya?ml|json|txt))\b")
PLACEHOLDER_STEMS = {"path", "path_or_file", "file", "foo", "bar", "example", "my_plugin", "your_file"}
MD_LINK_RE = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
CLAIM_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|min|minutes|h|hours)\b.*\b(?:ingest|wall|faster|speedup|run|download|takes?|in about)\b|"
                      r"\b(?:ingest|wall|faster|speedup|takes?|in about)\b.*\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|min|minutes|h|hours)\b", re.I)

results: list[dict] = []


def report(check: str, level: str, detail: str, locations: list[str] | None = None, limit: int = 8) -> None:
    locations = locations or []
    results.append({"check": check, "level": level, "detail": detail, "locations": locations})
    shown = "" if not locations else " | " + "; ".join(locations[:limit]) + (f"; +{len(locations) - limit} more" if len(locations) > limit else "")
    print(f"[{level}] {check}: {detail}{shown}")


def walk(root: Path, patterns: tuple[str, ...], excludes: list[str]) -> list[Path]:
    out = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if any(fnmatch.fnmatch(str(rel), ex) for ex in excludes):
            continue
        if any(fnmatch.fnmatch(path.name, p) for p in patterns):
            out.append(path)
    return sorted(out)


def extend_private_names() -> str:
    try:
        import firecube.ingestor.api as api  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return "static list (firecube not importable)"
    for name in ("BaseIngestor", "GenericZarrIngestor", "DirectZarrIngestor", "GenericParquetIngestor"):
        cls = getattr(api, name, None)
        if cls is not None:
            CORE_PRIVATE.update(n for n in dir(cls) if n.startswith("_") and not n.startswith("__"))
    return "static list plus installed firecube"


def is_firecube_module(name: str) -> bool:
    """True only for the firecube package itself, never a plugin package like firecube_<name>."""
    return name == "firecube" or name.startswith("firecube.")


class PluginIndex:
    """Collect class structure across the plugin's source files."""

    def __init__(self, trees: dict[Path, ast.Module]):
        self.trees = trees
        self.firecube_names: set[str] = set()
        self.firecube_module_aliases: set[str] = set()
        self.classes: dict[str, ast.ClassDef] = {}
        self.class_file: dict[str, Path] = {}
        self.defined_private: set[str] = set()
        for path, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and is_firecube_module(node.module):
                    self.firecube_names.update(a.asname or a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if is_firecube_module(a.name):
                            self.firecube_module_aliases.add(a.asname or a.name.split(".")[0])
                elif isinstance(node, ast.ClassDef):
                    self.classes[node.name] = node
                    self.class_file[node.name] = path
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("_"):
                            if item.name not in CORE_PRIVATE:
                                self.defined_private.add(item.name)
                        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                            self.defined_private.update(t.id for t in targets if isinstance(t, ast.Name) and t.id.startswith("_"))
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        for sub in ast.walk(t):
                            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self" and sub.attr.startswith("_"):
                                self.defined_private.add(sub.attr)

    def base_names(self, cls: ast.ClassDef) -> list[tuple[str, bool]]:
        """Return (name, via_firecube_module_alias) for each base, e.g. `api.GenericZarrIngestor`
        after `import firecube.ingestor.api as api` is tracked the same as a plain ImportFrom name."""
        names = []
        for b in cls.bases:
            if isinstance(b, ast.Name):
                names.append((b.id, False))
            elif isinstance(b, ast.Attribute):
                via_alias = isinstance(b.value, ast.Name) and b.value.id in self.firecube_module_aliases
                names.append((b.attr, via_alias))
        return names

    def is_ingestor(self, name: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if name in seen or name not in self.classes:
            return False
        seen.add(name)
        for base, via_alias in self.base_names(self.classes[name]):
            if (base in self.firecube_names or via_alias) and (base.endswith("Ingestor") or base.endswith("Mixin")):
                return True
            if self.is_ingestor(base, seen):
                return True
        return False


def loc(root: Path, path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')}"


def check_code(root: Path, src_files: list[Path], max_loc: int) -> None:
    trees: dict[Path, ast.Module] = {}
    for path in src_files:
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        except SyntaxError as exc:
            report("parse", "FAIL", f"{path.relative_to(root)}: {exc}")
    index = PluginIndex(trees)

    bad_imports, overrides, private_access, log_access, empty_returns = [], [], [], [], []
    store_writes, conditions, hashes = [], [], []
    encoding_units = False
    zarr_append = False
    for path, tree in trees.items():
        text = path.read_text(encoding="utf-8", errors="ignore")
        # A bare dict literal such as {"units": "days since 1970-01-01"} needs no AST context to trust;
        # a `.encoding.update(...)`/`.encoding[...] =` call is matched by AST below, statement-wide rather
        # than line-wide, so an idiomatic multi-line call (keyword arguments on their own lines) still counts.
        if re.search(r"[\"']units[\"']\s*[:=]\s*[\"'][a-z]+ since \d{4}-\d{2}-\d{2}", text):
            encoding_units = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if is_firecube_module(a.name) and not a.name.startswith(ALLOWED_PREFIXES):
                        bad_imports.append(f"{loc(root, path, node)} import {a.name}")
            elif isinstance(node, ast.ImportFrom) and node.module and is_firecube_module(node.module):
                if not node.module.startswith(ALLOWED_PREFIXES) or any(p.startswith("_") for p in node.module.split(".")):
                    bad_imports.append(f"{loc(root, path, node)} from {node.module}")
                for a in node.names:
                    if a.name.startswith("_"):
                        bad_imports.append(f"{loc(root, path, node)} private name {a.name}")
                    if a.name == "GenericZarrIngestor":
                        zarr_append = True
            elif isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__"):
                owner = node.value
                is_self = isinstance(owner, ast.Name) and owner.id == "self"
                is_super = isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name) and owner.func.id == "super"
                if is_super and node.attr in CORE_PRIVATE and node.attr != "_process_batch":
                    private_access.append(f"{loc(root, path, node)} super().{node.attr}")
                elif is_self and isinstance(node.ctx, ast.Load) and node.attr not in index.defined_private:
                    if node.attr == "_log":
                        log_access.append(loc(root, path, node))
                    elif node.attr == "_process_batch":
                        continue
                    else:
                        private_access.append(f"{loc(root, path, node)} self.{node.attr}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                dotted = ast.unparse(func)
                if name == "Condition" and "threading" in dotted:
                    conditions.append(loc(root, path, node))
                is_digest = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "hashlib") or (
                    isinstance(func, ast.Name) and name in {"sha256", "sha1", "md5", "blake2b"})
                hashes_small_input = any("json.dumps" in ast.unparse(a) or ".encode(" in ast.unparse(a) for a in node.args)
                if is_digest and not hashes_small_input:
                    hashes.append(f"{loc(root, path, node)} {dotted}")
                if name == "consolidate_metadata":
                    store_writes.append(f"{loc(root, path, node)} consolidate_metadata")
                if (name == "update" and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute)
                        and func.value.attr == "encoding"):
                    if any(kw.arg == "units" for kw in node.keywords):
                        encoding_units = True
                    if any(isinstance(a, ast.Dict) and any(
                            isinstance(k, ast.Constant) and k.value == "units" for k in a.keys) for a in node.args):
                        encoding_units = True
                if name == "update" and isinstance(func, ast.Attribute) and ast.unparse(func.value).endswith("attrs") and re.search(
                        r"root|group|store|zarr|stored", ast.unparse(func.value), re.I):
                    store_writes.append(f"{loc(root, path, node)} attrs.update")
                if name == "open_group":
                    mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
                    if isinstance(mode, ast.Constant) and mode.value in {"a", "w", "w-", "r+"}:
                        store_writes.append(f"{loc(root, path, node)} open_group(mode={mode.value!r})")
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Subscript) and ast.unparse(t.value).endswith("attrs"):
                        # dataset.attrs[...] on an in-memory xarray object is fine; flag only store-like names
                        target_text = ast.unparse(t.value)
                        if re.search(r"root|group|store|zarr|stored", target_text, re.I):
                            store_writes.append(f"{loc(root, path, node)} {target_text}[...] =")
                    if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute) and t.value.attr == "encoding"
                            and isinstance(t.slice, ast.Constant) and t.slice.value == "units"):
                        encoding_units = True
                    elif (isinstance(t, ast.Attribute) and t.attr == "encoding" and isinstance(node, ast.Assign)
                            and isinstance(node.value, ast.Dict) and any(
                                isinstance(k, ast.Constant) and k.value == "units" for k in node.value.keys)):
                        encoding_units = True

        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if not index.is_ingestor(cls.name):
                continue
            direct_base = any(name == "BaseIngestor" for name, _ in index.base_names(cls))
            for item in cls.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name in FORBIDDEN_PUBLIC_OVERRIDES:
                    overrides.append(f"{loc(root, path, item)} {cls.name}.{item.name}")
                elif item.name in CORE_PRIVATE and not (item.name == "_process_batch" and direct_base):
                    overrides.append(f"{loc(root, path, item)} {cls.name}.{item.name}")
                if item.name == "discover_source_files":
                    for sub in ast.walk(item):
                        if isinstance(sub, ast.Return):
                            v = sub.value
                            empty = (
                                v is None
                                or (isinstance(v, (ast.List, ast.Tuple, ast.Set)) and not v.elts)
                                or (isinstance(v, ast.Constant) and v.value is None)
                                or (isinstance(v, ast.Call) and ast.unparse(v) in {"iter(())", "iter([])", "list()", "tuple()", "[]"})
                            )
                            if empty:
                                empty_returns.append(f"{loc(root, path, sub)} {ast.unparse(sub)}")

    report("firecube-imports", "FAIL" if bad_imports else "PASS",
           "imports only public Firecube modules" if not bad_imports else "deep or private Firecube imports", bad_imports, max_loc)
    report("framework-overrides", "FAIL" if overrides else "PASS",
           "no run/finalize_pipeline/private Firecube method overridden" if not overrides else "framework-owned methods overridden", overrides, max_loc)
    report("private-core-access", "FAIL" if private_access else "PASS",
           "no private Firecube attribute or method used" if not private_access else "private names the plugin does not define", private_access, max_loc)
    if log_access:
        report("private-core-access", "WARN", f"self._log used {len(log_access)}x; prefer logging.getLogger(__name__)", log_access, 3)
    report("discovery-empty-return", "FAIL" if empty_returns else "PASS",
           "discovery never returns an empty result to skip work" if not empty_returns else "discover_source_files returns empty: core never sees the items", empty_returns, max_loc)
    for check, found, msg in (
        ("store-writes", store_writes, "plugin writes the store itself: group attrs are write-once; provenance belongs in arrays"),
        ("own-ordering", conditions, "threading.Condition: core orders append writes; do not add ordering machinery"),
        ("payload-hash", hashes, "payload digests: avoid hashing whole products at discovery; prefer container checksums"),
    ):
        report(check, "WARN" if found else "PASS", msg if found else "none found", found, max_loc)
    if zarr_append:
        report("time-encoding", "PASS" if encoding_units else "WARN",
               "explicit encoding units found" if encoding_units else "no explicit encoding units: set a fixed epoch for the append coordinate")


def notebook_text(path: Path) -> str:
    try:
        nb = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return ""
    return "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))


def check_docs(root: Path, excludes: list[str], max_loc: int) -> None:
    docs = [p for p in walk(root, ("*.md", "*.ipynb", "*.rst"), excludes)]
    missing, claims = [], []
    for path in docs:
        text = notebook_text(path) if path.suffix == ".ipynb" else path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in PATH_RE.finditer(line):
                rel = m.group(1).rstrip(".")
                if Path(rel).stem in PLACEHOLDER_STEMS:
                    continue
                if not (root / rel).exists() and not (path.parent / rel).exists():
                    missing.append(f"{path.relative_to(root)}:{lineno} {rel}")
            if path.suffix == ".md":
                for m in MD_LINK_RE.finditer(line):
                    target = m.group(1)
                    if re.match(r"^[a-z]+:|^/|^<|^\{", target) or "{{" in target:
                        continue
                    if Path(target).stem in PLACEHOLDER_STEMS:
                        continue
                    if not (path.parent / target).exists():
                        missing.append(f"{path.relative_to(root)}:{lineno} {target}")
                if CLAIM_RE.search(line):
                    claims.append(f"{path.relative_to(root)}:{lineno}")
    report("referenced-files", "FAIL" if missing else "PASS",
           "every referenced repository file exists" if not missing else "referenced files missing", sorted(set(missing)), max_loc)
    report("performance-claims", "WARN" if claims else "PASS",
           f"{len(claims)} lines with timing numbers: each needs a full-scale benchmark record" if claims else "none found", claims, max_loc)


def check_tests_and_pin(root: Path, excludes: list[str], max_loc: int) -> None:
    outside = []
    for path in walk(root / "tests", ("*.py",), excludes) if (root / "tests").exists() else []:
        depth = len(path.relative_to(root).parts) - 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            m = re.search(r"parents\[(\d+)\]", line)
            if m and "__file__" in line and int(m.group(1)) >= depth:
                outside.append(f"{path.relative_to(root)}:{lineno}")
            if re.search(r"""["'](?:/home/|/Users/|[A-Z]:\\\\)""", line):
                outside.append(f"{path.relative_to(root)}:{lineno} absolute path")
    report("tests-outside-repo", "WARN" if outside else "PASS",
           "fixtures resolved outside the repository: a fresh checkout must skip, never error" if outside else "none found", outside, max_loc)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        m = re.search(r"""["']firecube(\[[^\]]*\])?\s*([<>=!~][^"']*)?["']""", pyproject.read_text(encoding="utf-8"))
        spec = ((m.group(2) or "").strip() if m else "")
        pinned = bool(re.search(r"==|<", spec))
        found = f"firecube{spec}" if m else "firecube requirement not found"
        report("firecube-pin", "PASS" if pinned else "WARN", found + ("" if pinned else ": pin the Firecube version the plugin is tested against"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", type=Path, help="plugin project directory")
    ap.add_argument("--json", type=Path, help="write the full result list here")
    ap.add_argument("--exclude", action="append", default=[], help="glob relative to the project, repeatable")
    ap.add_argument("--max-locations", type=int, default=8)
    args = ap.parse_args()
    root = args.project.expanduser().resolve()
    if not root.is_dir():
        print(f"[FAIL] project: {root} is not a directory")
        return 1
    source = extend_private_names()
    src_root = root / "src" if (root / "src").is_dir() else root
    src_files = [p for p in walk(src_root, ("*.py",), args.exclude) if "tests" not in p.relative_to(root).parts]
    print(f"[info] project: {root.name}; {len(src_files)} source files; private names from {source}")
    if not src_files:
        report("no-source-files", "FAIL", f"no Python source files found under {src_root.relative_to(root) if src_root != root else '.'}")
    check_code(root, src_files, args.max_locations)
    check_docs(root, args.exclude, args.max_locations)
    check_tests_and_pin(root, args.exclude, args.max_locations)
    fails = sum(r["level"] == "FAIL" for r in results)
    warns = sum(r["level"] == "WARN" for r in results)
    print(f"CONTRACT {'FAIL' if fails else 'PASS'}: {fails} FAIL, {warns} WARN" + ("" if not fails else " -- see plugin-traps.md"))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
