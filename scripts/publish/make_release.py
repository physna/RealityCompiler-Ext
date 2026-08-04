# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
import zipfile
from pathlib import Path

# Configuration
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "extension.toml"
INCLUDE_FILES = ["config", "data", "docs", "physna", "premake5.lua", "README.md", "LICENSE"]
INTERNAL_FOLDER_NAME = "physna.reality_compiler"


def get_version() -> str:
    """Extract the version string from the TOML file without external libs."""
    if not CONFIG_PATH.exists():
        print(f"Error: {CONFIG_PATH} not found.")
        return "0.0.0"

    content = CONFIG_PATH.read_text()
    match = re.search(r'version\s*=\s*"([^"]+)"', content)
    if match:
        return match.group(1)
    return "0.0.0"


def create_release() -> None:
    version = get_version()
    output_path = REPO_ROOT / f"physna.reality_compiler-v{version}.zip"

    if output_path.exists():
        output_path.unlink()

    print(f"Creating {output_path.name}...")

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for item in INCLUDE_FILES:
            item_path = REPO_ROOT / item
            if not item_path.exists():
                print(f"Warning: {item} not found, skipping.")
                continue

            if item_path.is_dir():
                for root, dirs, files in item_path.walk():
                    if "__pycache__" in dirs:
                        dirs.remove("__pycache__")

                    for file_name in files:
                        file_path = root / file_name
                        arcname = Path(INTERNAL_FOLDER_NAME) / file_path.relative_to(
                            REPO_ROOT
                        )
                        zipf.write(file_path, arcname)
            else:
                arcname = Path(INTERNAL_FOLDER_NAME) / item
                zipf.write(item_path, arcname)

    print(f"Success! Created {output_path.name}")


if __name__ == "__main__":
    create_release()
