#!/usr/bin/env python3
"""Create a reviewable Nextcloud catalog update after publishing its image.

Uses Python's standard library only. Writes to a new output directory and
never modifies the source checkout, commits, pushes, or contacts TrueNAS.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys


SOURCE_VERSION = "31.0.23"
TARGET_VERSION = "31.0.24"
SOURCE_SERVER = "34.0.4"
TARGET_SERVER = "35.0.2"
IMAGE_REPOSITORY = "ghcr.io/kqmaverick/nextcloud-fpm"


def replace_once(text, pattern, replacement, label):
    result, count = re.subn(pattern, lambda _: replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected exactly one {label}; found {count}")
    return result


def encode_json_like(original_text, original, updated):
    # Preserve the repository's formatting and old property order to keep the
    # metadata diff limited to the newly added/updated Nextcloud entries.
    for indent in (4, 2):
        for ensure_ascii in (True, False):
            serialized = json.dumps(original, indent=indent, ensure_ascii=ensure_ascii)
            for suffix in ("\n", ""):
                if serialized + suffix == original_text:
                    return json.dumps(updated, indent=indent, ensure_ascii=ensure_ascii) + suffix
    raise ValueError("Unrecognized metadata JSON formatting; review it before proceeding")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepend_version_entry(original_text, key, entry, expected):
    # Older entries include trailing spaces from manual edits. Preserve the
    # existing version-index text instead of reformatting unrelated versions.
    if not original_text.startswith("{\n"):
        raise ValueError("Unexpected version-index opening; review its formatting")
    new_object = json.dumps({key: entry}, indent=4, ensure_ascii=True)
    result = new_object[:-2] + ",\n" + original_text[2:]
    if json.loads(result) != expected:
        raise ValueError("Generated version index did not match the expected metadata")
    return result


APPLY_SCRIPT = r'''param(
    [string]$CatalogRepo = 'C:\Users\kqmav\Documents\GitHub\catalog'
)
$ErrorActionPreference = 'Stop'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'update-manifest.json') -Raw | ConvertFrom-Json
$repo = (Resolve-Path -LiteralPath $CatalogRepo).Path
$target = Join-Path $repo $manifest.chart_directory
if (Test-Path -LiteralPath $target) {
    throw "The target chart directory already exists: $target. Review it before applying this update."
}
# Check every source and payload file before writing anything to the repository.
# This prevents an older artifact from overwriting newer catalog metadata.
foreach ($item in $manifest.source_files) {
    $path = Join-Path $repo $item.path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing source file: $path"
    }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) {
        throw "Source changed: $($item.path). Re-run the build against the current catalog, then download its new artifact."
    }
}
foreach ($item in $manifest.payload_files) {
    $path = Join-Path $PSScriptRoot $item.path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Incomplete artifact; missing $($item.path)"
    }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) {
        throw "Artifact checksum mismatch: $($item.path)"
    }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot $manifest.chart_directory) -Destination $target -Recurse
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'premium/nextcloud/app_versions.json') -Destination (Join-Path $repo 'premium/nextcloud/app_versions.json') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'catalog.json') -Destination (Join-Path $repo 'catalog.json') -Force
Write-Host "Prepared Nextcloud $($manifest.server_version), chart $($manifest.chart_version)."
Write-Host 'Review the changes in GitHub Desktop or git diff, then commit and open your catalog PR.'
Write-Host 'No commit, push, or TrueNAS deployment was performed.'
'''


def prepare(source, output, image, source_commit):
    if not re.fullmatch(
        re.escape(IMAGE_REPOSITORY) + r":35\.0\.2@sha256:[a-f0-9]{64}", image
    ):
        raise ValueError("Image must be the published ghcr.io/kqmaverick/nextcloud-fpm:35.0.2@sha256:<64 hex digits>")
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise ValueError("A full 40-character catalog source commit is required")
    source = source.resolve()
    output = output.resolve()
    if output.exists():
        raise ValueError(f"Output directory already exists: {output}")
    if output.is_relative_to(source):
        raise ValueError("Output must be outside the catalog source checkout")
    source_chart = source / "premium/nextcloud" / SOURCE_VERSION
    target_relative = f"premium/nextcloud/{TARGET_VERSION}"
    if (source / target_relative).exists():
        raise ValueError(f"Chart {TARGET_VERSION} already exists in the source catalog")

    catalog_path = source / "catalog.json"
    versions_path = source / "premium/nextcloud/app_versions.json"
    catalog_text = catalog_path.read_text(encoding="utf-8")
    versions_text = versions_path.read_text(encoding="utf-8")
    catalog = json.loads(catalog_text)
    versions = json.loads(versions_text)
    current = catalog["premium"]["nextcloud"]
    if current["latest_version"] != SOURCE_VERSION or current["latest_app_version"] != SOURCE_SERVER:
        raise ValueError(f"Expected the current Nextcloud chart to be {SOURCE_VERSION} / {SOURCE_SERVER}")
    if TARGET_VERSION in versions:
        raise ValueError(f"Metadata already contains {TARGET_VERSION}")
    old_entry = versions[SOURCE_VERSION]
    if old_entry["chart_metadata"]["appVersion"] != SOURCE_SERVER:
        raise ValueError("Source chart metadata has an unexpected Nextcloud version")

    chart_text = (source_chart / "Chart.yaml").read_text(encoding="utf-8")
    chart_text = replace_once(chart_text, r"^appVersion: 34\.0\.4$", "appVersion: 35.0.2", "appVersion")
    chart_text = replace_once(chart_text, r"^version: 31\.0\.23$", "version: 31.0.24", "chart version")
    values_text = (source_chart / "ix_values.yaml").read_text(encoding="utf-8")
    image_tag = image.split(IMAGE_REPOSITORY + ":", 1)[1]
    values_text = replace_once(
        values_text,
        r"\Aimage:\n  repository: ghcr\.io/kqmaverick/nextcloud-fpm\n  pullPolicy: IfNotPresent\n  tag: 34\.0\.4@sha256:[a-f0-9]{64}",
        f"image:\n  repository: {IMAGE_REPOSITORY}\n  pullPolicy: IfNotPresent\n  tag: {image_tag}",
        "Nextcloud image block",
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    new_entry = copy.deepcopy(old_entry)
    new_entry.update(version=TARGET_VERSION, human_version=f"{TARGET_SERVER}_{TARGET_VERSION}", last_update=now)
    if not old_entry["location"].endswith("/" + SOURCE_VERSION):
        raise ValueError("Unexpected source metadata location")
    new_entry["location"] = old_entry["location"].rsplit("/", 1)[0] + "/" + TARGET_VERSION
    new_entry["chart_metadata"].update(version=TARGET_VERSION, appVersion=TARGET_SERVER)
    updated_versions = {TARGET_VERSION: new_entry, **versions}
    updated_catalog = copy.deepcopy(catalog)
    updated_catalog["premium"]["nextcloud"].update(
        latest_version=TARGET_VERSION, latest_app_version=TARGET_SERVER,
        latest_human_version=f"{TARGET_SERVER}_{TARGET_VERSION}", last_update=now,
    )
    new_versions_text = prepend_version_entry(versions_text, TARGET_VERSION, new_entry, updated_versions)
    new_catalog_text = encode_json_like(catalog_text, catalog, updated_catalog)
    source_files = [catalog_path, versions_path] + sorted(p for p in source_chart.rglob("*") if p.is_file())
    if any(p.is_symlink() for p in source_chart.rglob("*")):
        raise ValueError("Unexpected symlink in the source chart")

    # Validation above completes before any output is created.
    output.mkdir(parents=True)
    try:
        target_chart = output / target_relative
        shutil.copytree(source_chart, target_chart)
        (target_chart / "Chart.yaml").write_text(chart_text, encoding="utf-8")
        (target_chart / "ix_values.yaml").write_text(values_text, encoding="utf-8")
        (output / "catalog.json").write_text(new_catalog_text, encoding="utf-8")
        (output / "premium/nextcloud/app_versions.json").write_text(new_versions_text, encoding="utf-8")
        payload = sorted(p for p in output.rglob("*") if p.is_file())
        manifest = {
            "server_version": TARGET_SERVER, "chart_version": TARGET_VERSION,
            "source_commit": source_commit, "image": image, "chart_directory": target_relative,
            "source_files": [{"path": p.relative_to(source).as_posix(), "sha256": sha256(p)} for p in source_files],
            "payload_files": [{"path": p.relative_to(output).as_posix(), "sha256": sha256(p)} for p in payload],
        }
        (output / "update-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (output / "Apply-NextcloudCatalog.ps1").write_text(APPLY_SCRIPT, encoding="utf-8")
        (output / "README.txt").write_text(
            f"Nextcloud {TARGET_SERVER} / catalog {TARGET_VERSION}\n"
            f"Catalog source commit: {source_commit}\nPublished image: {image}\n\n"
            "In your local catalog repo, pull main and create your update branch first.\n"
            "Then run this artifact's PowerShell helper from an extracted directory:\n"
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\Apply-NextcloudCatalog.ps1 "
            "-CatalogRepo 'C:\\Users\\kqmav\\Documents\\GitHub\\catalog'\n\n"
            "The helper verifies source and artifact hashes before copying the chart and metadata.\n"
            "It stops if those source files changed since the build. Re-run the workflow in that case.\n"
            "Review, commit, and open a PR using your normal catalog process.\n"
            "Take a fresh verified backup before applying the update in TrueNAS.\n"
            "Your saved TrueNAS values, including the 60 MP Imaginary limit, remain in the app configuration.\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(output)
        raise
    print(f"Prepared {len(payload)} catalog files: Nextcloud {TARGET_SERVER}, chart {TARGET_VERSION}")
    print(f"Published image: {image}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        prepare(args.catalog_root, args.output_dir, args.image, args.source_commit)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Catalog update stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
