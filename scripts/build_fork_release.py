#!/usr/bin/env python3
"""Build a reproducible Zap2XML fork release and its plugin-repository manifests.

Usage: python3 scripts/build_fork_release.py VERSION [--out DIR] [--check]
  e.g. python3 scripts/build_fork_release.py 2.1.4.1

With no options the script packages a release: it writes the version into
``plugin.py`` and ``plugin.json`` and rewrites both manifests. ``--out`` builds
the ZIP into DIR and changes no tracked file; ``--check`` rebuilds it into
``--out`` (or a temporary directory) and fails when the result's sha256
disagrees with the checksum the committed manifests advertise for VERSION.

The plugin is installed under the existing ``zap2xml`` slug. The release ZIP is
flat and contains only the files Dispatcharr needs from ``src/zap2xml``.
"""

import argparse
import datetime
import hashlib
import json
import re
import sys
import tempfile
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
    "artwork.py",
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


def build_zip(destination: Path) -> bytes:
    """Write the release ZIP to `destination` from the committed tree; change nothing else."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    reproducible_zip(destination)
    return destination.read_bytes()


def published_checksums(version: str) -> dict[str, str]:
    """The sha256 each committed manifest advertises for `version`, keyed by where it appears."""
    checksums = {}
    per_plugin_path = ROOT / "zips" / SLUG / "manifest.json"
    if per_plugin_path.exists():
        name = str(per_plugin_path.relative_to(ROOT))
        manifest = json.loads(per_plugin_path.read_text(encoding="utf-8")).get("manifest", {})
        latest = manifest.get("latest") or {}
        if latest.get("version") == version and latest.get("checksum_sha256"):
            checksums[f"{name} latest"] = latest["checksum_sha256"]
        for entry in manifest.get("versions", []):
            if entry.get("version") == version and entry.get("checksum_sha256"):
                checksums[f"{name} versions[{version}]"] = entry["checksum_sha256"]
    root_path = ROOT / "manifest.json"
    if root_path.exists():
        name = str(root_path.relative_to(ROOT))
        manifest = json.loads(root_path.read_text(encoding="utf-8")).get("manifest", {})
        for entry in manifest.get("plugins", []):
            if entry.get("latest_version") == version and entry.get("latest_sha256"):
                checksums[f"{name} plugins[{entry.get('slug', '?')}]"] = entry["latest_sha256"]
    return checksums


def check_zip(version: str, zip_path: Path) -> None:
    """Fail unless `zip_path` hashes to what the manifests advertise for `version`."""
    data = zip_path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    expected = published_checksums(version)
    if not expected:
        print(f"check: no committed manifest lists {version}; checksum comparison skipped")
        return
    wrong = {path: sha for path, sha in expected.items() if sha != actual}
    if wrong:
        details = "\n".join(f"  {path}: {sha}" for path, sha in wrong.items())
        raise SystemExit(
            f"check: rebuilt {zip_path.name} is {actual} ({len(data)} bytes); the manifests expect:\n{details}"
        )
    print(f"check: {zip_path.name} matches {', '.join(sorted(expected))} ({actual})")


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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a reproducible Zap2XML release and its plugin-repository manifests.",
    )
    parser.add_argument("version", type=version_arg, help="four numeric parts, e.g. 2.1.4.1")
    parser.add_argument(
        "--out",
        metavar="DIR",
        help="write only the release ZIP into DIR and change no tracked file",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild the ZIP into --out (or a temporary directory) and fail when its sha256 "
        "differs from the checksum the committed manifests list for this version",
    )
    return parser.parse_args(argv)


def version_arg(value: str) -> str:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value):
        raise argparse.ArgumentTypeError("four numeric parts, e.g. 2.1.4.1")
    return value


def package(version: str) -> None:
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
    data = build_zip(zip_path)
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


def main() -> None:
    args = parse_args(sys.argv[1:])
    version = args.version

    if args.out is None and not args.check:
        package(version)
        return

    declared = json.loads((SOURCE / "plugin.json").read_text(encoding="utf-8"))["version"]
    if declared != version:
        raise SystemExit(
            f"plugin.json declares {declared}, not {version}; --out and --check build the "
            "committed tree as it stands, so pass the version already packaged there"
        )

    if args.out is not None:
        zip_path = Path(args.out) / f"{SLUG}-v{version}.zip"
        data = build_zip(zip_path)
        print(f"built {zip_path}: {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}")
        if args.check:
            check_zip(version, zip_path)
        return

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / f"{SLUG}-v{version}.zip"
        build_zip(zip_path)
        check_zip(version, zip_path)


if __name__ == "__main__":
    main()
