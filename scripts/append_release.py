#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
import argparse
import datetime
import hashlib
import json
import os
import re


def derive_channel(version: str) -> str:
    """Derive the release channel natively from version semantics."""
    v_lower = version.lower()
    if "alpha" in v_lower or "alfa" in v_lower:
        return "alpha"
    if "beta" in v_lower or "rc" in v_lower or "dev" in v_lower:
        return "beta"
    return "stable"


def get_sha256(filepath: str) -> str:
    """Calculate SHA256 inline sequentially."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append an OS release payload natively to the firmware JSON manifest."
    )
    parser.add_argument("filepath", help="Path to the physical firmware ZIP archive")
    parser.add_argument(
        "--manifest", required=True, help="Path to the JSON manifest schema file"
    )
    parser.add_argument(
        "--url",
        help="Direct URL to host the zip (default: auto-generated based on filename)",
    )
    parser.add_argument(
        "--notes",
        help="Release notes markdown payload (default: contents of <filepath>.zip → .md)",
        default=None,
    )
    parser.add_argument(
        "--fastboot",
        action="store_true",
        help="Toggle the fastboot initialization protocol requirement",
    )

    args = parser.parse_args()

    if not os.path.exists(args.filepath):
        print(f"Error: Target artifact {args.filepath} not found on disk.")
        return

    if args.notes is None:
        notes_path = re.sub(r"\.zip$", ".md", args.filepath, flags=re.IGNORECASE)
        if os.path.exists(notes_path):
            with open(notes_path, "r", encoding="utf-8") as nf:
                args.notes = nf.read()
        else:
            args.notes = "Stable release automatically built by CI."

    basename = os.path.basename(args.filepath)

    clean_name = basename.replace(".zip", "")

    # Strip known wrapping formatting (e.g. "smhub_os_v1.0.0.dev3", "v0.9.9")
    m = re.search(r"v?(\d+\.\d+\.\d+(?:[-.a-zA-Z0-9]*))$", clean_name, re.IGNORECASE)

    if m:
        version = m.group(1)
    else:
        # Blind fallback
        version = clean_name
        if version.startswith("smhub_os_v"):
            version = version[10:]
        elif version.startswith("v"):
            version = version[1:]

    channel = derive_channel(version)
    size_bytes = os.path.getsize(args.filepath)
    sha256 = get_sha256(args.filepath)

    url = args.url or f"https://updates.smlight.tech/firmware/smhub/os/{basename}"

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    new_release = {
        "version": version,
        "channel": channel,
        "released_at": now_iso,
        "notes": args.notes,
        "notes_url": url.replace(".zip", ".md"),
        "requires_flasher": ">=0.1.0",
        "artifacts": {
            "firmware": {"url": url, "size_bytes": size_bytes, "sha256": sha256}
        },
    }

    if args.fastboot:
        new_release["fastboot"] = True

    try:
        with open(args.manifest) as f:
            data = json.load(f)
    except FileNotFoundError:
        # Bootstrap default empty root
        data = {
            "$schema": "https://updates.smlight.tech/firmware/smhub/schema/smhub-firmware-v1.json",
            "manifest_version": 1,
            "generated_at": now_iso,
            "product": "smhub",
            "releases": [],
        }

    if "releases" not in data:
        data["releases"] = []

    # Update existing if version already exists locally
    existing_idx = next(
        (i for i, r in enumerate(data["releases"]) if r.get("version") == version), None
    )

    if existing_idx is not None:
        data["releases"][existing_idx] = new_release
        print(f"Updated explicitly existing release '{version}' inside manifest.")
    else:
        data["releases"].insert(0, new_release)
        print(
            f"Appended new raw release '{version}' mapped to channel '{channel}' into manifest."
        )

    # Ensure all releases have a valid timestamp to prevent sort errors
    for r in data["releases"]:
        if not r.get("released_at"):
            r["released_at"] = now_iso

    from packaging.version import Version

    # Universally enforce newest-to-top chronological sorting natively via SemVer
    def sort_key(r):
        try:
            return Version(r.get("version", "0.0.0"))
        except Exception:
            return Version("0.0.0")

    data["releases"].sort(key=sort_key, reverse=True)

    data["generated_at"] = now_iso

    with open(args.manifest, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
