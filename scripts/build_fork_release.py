#!/usr/bin/env python3
"""Build a reproducible Zap2XML fork release and its plugin-repository manifests.

Usage: python3 scripts/build_fork_release.py VERSION
  e.g. python3 scripts/build_fork_release.py 2.1.4.1

The plugin is installed under the existing ``zap2xml`` slug. The release ZIP is
flat and contains only the six files Dispatcharr needs from ``src/zap2xml``.
"""

import datetime
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path


OWNER = "tylertufano"
REPO = "zap2xml_dispatcharr_plugin"
BRANCH = "tufanito"
SLUG = "zap2xml"
MIN_DISPATCHARR_VERSION = "0.34.0"
ZIP_FILES = (
    "plugin.json",
    "plugin.py",
    "zap2xml.py",
    "plugins.py",
    "__init__.py",
    "channels.db",
)
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / SLUG
RELEASE_URL = (
    f"https://github.com/{OWNER}/{REPO}/releases/download/v{{version}}/"
    f"{SLUG}-v{{version}}.zip"
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def reproducible_zip(path: Path) -> None:
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name in sorted(ZIP_FILES):
            source_path = SOURCE / name
            if not source_path.is_file():
                raise SystemExit(f"missing source file: {source_path}")
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.internal_attr = 0
            info.flag_bits = 0
            info.extra = b""
            info.comment = b""
            archive.writestr(
                info,
                source_path.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def earlier_versions(manifest_path: Path, version: str) -> list[dict]:
    if not manifest_path.exists():
        return []
    old = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_manifest = old.get("manifest", old)
    return [
        entry
        for entry in old_manifest.get("versions", [])
        if entry.get("version") != version
    ]


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", sys.argv[1]):
        raise SystemExit("usage: build_fork_release.py VERSION (four numeric parts, e.g. 2.1.4.1)")
    version = sys.argv[1]

    plugin_path = SOURCE / "plugin.py"
    plugin_source = plugin_path.read_text(encoding="utf-8")
    plugin_source, replacements = re.subn(
        r'(?m)^(\s*version\s*=\s*)"[^"]*"',
        rf'\g<1>"{version}"',
        plugin_source,
        count=1,
    )
    if replacements != 1:
        raise SystemExit("plugin.py: no class version line found")
    plugin_path.write_text(plugin_source, encoding="utf-8")

    plugin_json_path = SOURCE / "plugin.json"
    plugin_json = json.loads(plugin_json_path.read_text(encoding="utf-8"))
    plugin_json["version"] = version
    write_json(plugin_json_path, plugin_json)

    zip_path = ROOT / "dist" / f"{SLUG}-v{version}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    reproducible_zip(zip_path)
    data = zip_path.read_bytes()
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    url = RELEASE_URL.format(version=version)
    entry = {
        "version": version,
        "url": url,
        "checksum_md5": hashlib.md5(data).hexdigest(),
        "checksum_sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "last_updated": now,
        "build_timestamp": now,
        "min_dispatcharr_version": MIN_DISPATCHARR_VERSION,
    }
    description = plugin_json["description"].split("\n\n", 1)[0]
    author = plugin_json["author"]
    registry_url = f"https://github.com/{OWNER}/{REPO}"

    per_plugin_path = ROOT / "zips" / SLUG / "manifest.json"
    versions = [entry] + earlier_versions(per_plugin_path, version)
    per_plugin = {
        "generated_at": now,
        "manifest": {
            "slug": SLUG,
            "name": plugin_json["name"],
            "description": description,
            "author": author,
            "registry_url": registry_url,
            "registry_name": registry_url.removeprefix("https://github.com/"),
            "last_updated": now,
            "latest": {**entry, "latest_url": url},
            "versions": versions,
        },
    }
    write_json(per_plugin_path, per_plugin)

    root_manifest = {
        "generated_at": now,
        "manifest": {
            "registry_url": registry_url,
            "registry_name": f"{OWNER}/{REPO} (fork)",
            "root_url": f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}",
            "plugins": [
                {
                    "slug": SLUG,
                    "name": plugin_json["name"],
                    "description": description,
                    "manifest_url": f"zips/{SLUG}/manifest.json",
                    "author": author,
                    "last_updated": now,
                    "latest_version": version,
                    "latest_md5": entry["checksum_md5"],
                    "latest_sha256": entry["checksum_sha256"],
                    "latest_url": url,
                    "latest_size": entry["size"],
                    "help_url": plugin_json["help_url"],
                    "min_dispatcharr_version": MIN_DISPATCHARR_VERSION,
                }
            ],
        },
    }
    write_json(ROOT / "manifest.json", root_manifest)
    print(
        f"{zip_path.name}: {len(data)} bytes, "
        f"md5 {entry['checksum_md5']}, "
        f"sha256 {entry['checksum_sha256']}; "
        f"manifests updated; {len(versions) - 1} earlier version(s) kept"
    )


if __name__ == "__main__":
    main()
