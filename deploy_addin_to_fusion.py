#!/usr/bin/env python3

"""
Deploy the local MCPserve add-in into Fusion 360's user AddIns directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SOURCE_DIR = REPO_ROOT / "MCPserve"
FUSION_ADDINS_DIR = Path.home() / "AppData" / "Roaming" / "Autodesk" / "Autodesk Fusion 360" / "API" / "AddIns"
TARGET_DIR = FUSION_ADDINS_DIR / "MCPserve"


def ignore_filter(directory: str, names: list[str]) -> set[str]:
    ignored = {".vscode", "__pycache__", "mcp_comm"}
    return {name for name in names if name in ignored}


def main() -> int:
    if not SOURCE_DIR.exists():
        raise SystemExit(f"Source add-in directory not found: {SOURCE_DIR}")

    FUSION_ADDINS_DIR.mkdir(parents=True, exist_ok=True)

    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)

    shutil.copytree(SOURCE_DIR, TARGET_DIR, ignore=ignore_filter)

    manifest = TARGET_DIR / "MCPserve.manifest"
    if not manifest.exists():
        raise SystemExit(f"Deployment failed: missing manifest at {manifest}")

    print(f"Deployed add-in to: {TARGET_DIR}")
    print("Next step: open Fusion 360, go to Tools -> Scripts and Add-Ins, and run MCPserve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
