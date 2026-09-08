"""Scope the pinned Leo build-policy repair to its temporary checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

LEO_WORKSPACE = """minimumReleaseAge: 2880

allowBuilds:
  '@parcel/watcher': false
  esbuild: true
  figma-api-exporter: true
  svelte-preprocess: false
  unrs-resolver: false

overrides:
  esbuild: 0.27.1
  glob: 13.0.3
  'svgo@<2.8.2': 2.8.2
  uuid: 14.0.0
  vite: 6.4.3
"""
EXPORTER_COMMIT = "141c3eab4643ba9cdcbb23867785aa7df9347eec"
EXACT_RULE = f"  'figma-api-exporter@https://codeload.github.com/brave/figma-api-exporter/tar.gz/{EXPORTER_COMMIT}': true"


def repair_leo(directory):
    manifest = directory / "package.json"
    workspace = directory / "pnpm-workspace.yaml"
    if not manifest.is_file() or not workspace.is_file():
        return False
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("name") != "@brave/leo":
        return False
    dependencies = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    exporter = dependencies.get("figma-api-exporter", "")
    if not exporter.endswith(EXPORTER_COMMIT):
        raise RuntimeError("Unexpected Leo exporter revision; refusing to change build policy.")
    original = workspace.read_text(encoding="utf-8").rstrip("\n")
    expected = LEO_WORKSPACE.rstrip("\n")
    fixed = expected.replace("  figma-api-exporter: true", EXACT_RULE)
    if original == fixed:
        return False
    if original != expected:
        raise RuntimeError("Unexpected Leo build policy; refusing to change it.")
    workspace.write_bytes(fixed.encode("utf-8"))
    print("[PiP] Applied the exact-revision exporter build rule in the Leo temporary checkout.", flush=True)
    return True


def main():
    if sys.argv[1:2] in (["install"], ["i"]):
        repair_leo(Path.cwd())
    actual = os.environ["PIP_REAL_PNPM"]
    raise SystemExit(subprocess.call([actual, *sys.argv[1:]]))


if __name__ == "__main__":
    main()
