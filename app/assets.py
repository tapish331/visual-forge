"""Repository-global reusable asset registry."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TextIO, TypedDict, cast

import portalocker
from PIL import Image, UnidentifiedImageError

from .artifacts import atomic_write_json, fingerprint_from_json, sha256_fingerprint
from .project import JsonObject, JsonValue, utc_now_iso


DEFAULT_ASSETS_DIR = Path("assets")
ASSET_MANIFEST_NAME = "manifest.json"
ASSET_LOCK_NAME = ".visual-forge-assets.lock"
ASSET_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
PNG_MEDIA_TYPE = "image/png"


class AssetError(Exception):
    """Raised when the reusable asset registry cannot be read or updated."""


class AssetValidationResult(TypedDict):
    asset_id: str
    valid: bool
    state: str
    asset: JsonObject | None
    resolved_path: str | None
    errors: list[str]


class AssetInventory(TypedDict):
    total: int
    current_count: int
    stale_count: int
    missing_count: int
    invalid_count: int
    assets: list[AssetValidationResult]


class RegisterAssetResult(TypedDict):
    asset_id: str
    success: bool
    reused_existing: bool
    asset: JsonObject | None
    errors: list[str]


class AssetDependencies(TypedDict):
    asset_ids: list[str]
    asset_fingerprints: dict[str, JsonObject]
    asset_paths: dict[str, str]
    errors: list[str]


def build_asset_inventory(assets_dir: Path = DEFAULT_ASSETS_DIR) -> AssetInventory:
    manifest = load_asset_manifest(assets_dir)
    results = [validate_asset(asset_id, assets_dir) for asset_id in _manifest_ids(manifest)]
    return {
        "total": len(results),
        "current_count": sum(result["state"] == "current" for result in results),
        "stale_count": sum(result["state"] == "stale" for result in results),
        "missing_count": sum(result["state"] == "missing" for result in results),
        "invalid_count": sum(result["state"] == "invalid" for result in results),
        "assets": results,
    }


def register_asset(
    asset_path_value: Path,
    asset_id: str,
    version: str,
    *,
    replace: bool = False,
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> RegisterAssetResult:
    errors = _validate_registration_inputs(asset_path_value, asset_id, version, assets_dir)
    if errors:
        return _register_result(asset_id, False, False, None, errors)

    asset_path_value = asset_path_value.resolve()
    assets_root = assets_dir.resolve()
    try:
        relative_path = asset_path_value.relative_to(assets_root)
        width, height = _png_dimensions(asset_path_value)
        fingerprint = sha256_fingerprint(asset_path_value)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        return _register_result(asset_id, False, False, None, [f"Could not inspect PNG asset: {exc}"])

    assets_dir.mkdir(parents=True, exist_ok=True)
    lock_path = assets_dir / ASSET_LOCK_NAME
    try:
        lock = portalocker.Lock(lock_path, mode="a+", timeout=10, encoding="utf-8")
        with lock as handle_value:
            handle = cast(TextIO, handle_value)
            manifest = load_asset_manifest(assets_dir)
            records = _manifest_records(manifest)
            existing_index = _asset_index(records, asset_id)
            now = utc_now_iso()
            record: JsonObject = {
                "id": asset_id,
                "version": version.strip(),
                "type": "image",
                "path": relative_path.as_posix(),
                "media_type": PNG_MEDIA_TYPE,
                "width": width,
                "height": height,
                "artifact_fingerprint": fingerprint,
                "registered_at": _registered_at(records, existing_index, now),
                "updated_at": now,
            }
            if existing_index is not None:
                existing = records[existing_index]
                if _same_asset(existing, record):
                    return _register_result(asset_id, True, True, existing, [])
                if not replace:
                    return _register_result(
                        asset_id,
                        False,
                        False,
                        None,
                        [f"Asset ID already exists with different content: {asset_id}. Use --replace to update it."],
                    )
                records[existing_index] = record
            else:
                records.append(record)
            records.sort(key=lambda item: str(item.get("id", "")))
            atomic_write_json(
                assets_dir / ASSET_MANIFEST_NAME,
                {"schema_version": 1, "assets": cast(list[JsonValue], records)},
            )
            handle.flush()
            return _register_result(asset_id, True, False, record, [])
    except portalocker.exceptions.LockException:
        return _register_result(asset_id, False, False, None, ["Asset registry is busy; retry the command."])
    except (OSError, TypeError, ValueError, AssetError) as exc:
        return _register_result(asset_id, False, False, None, [f"Could not update asset registry: {exc}"])


def validate_asset(asset_id: str, assets_dir: Path = DEFAULT_ASSETS_DIR) -> AssetValidationResult:
    if not ASSET_ID_PATTERN.fullmatch(asset_id):
        return _validation_result(asset_id, False, "invalid", None, None, ["Asset ID must be lowercase snake-case."])
    try:
        manifest = load_asset_manifest(assets_dir)
    except AssetError as exc:
        return _validation_result(asset_id, False, "invalid", None, None, [str(exc)])
    record = _find_asset(_manifest_records(manifest), asset_id)
    if record is None:
        return _validation_result(asset_id, False, "missing", None, None, [f"Asset is not registered: {asset_id}"])
    errors = _validate_record(record)
    path_value = record.get("path")
    resolved: Path | None = None
    if isinstance(path_value, str) and path_value:
        try:
            resolved = _safe_asset_path(assets_dir, path_value)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        return _validation_result(asset_id, False, "invalid", record, resolved, errors)
    if resolved is None or not resolved.is_file():
        return _validation_result(
            asset_id,
            False,
            "missing",
            record,
            resolved,
            [f"Asset file is missing: {resolved or path_value}"],
        )
    expected = fingerprint_from_json(record.get("artifact_fingerprint"))
    if expected is None:
        return _validation_result(asset_id, False, "invalid", record, resolved, ["Asset fingerprint is invalid."])
    try:
        width, height = _png_dimensions(resolved)
        actual = sha256_fingerprint(resolved)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        return _validation_result(asset_id, False, "invalid", record, resolved, [f"Could not inspect PNG asset: {exc}"])
    if actual != expected or record.get("width") != width or record.get("height") != height:
        return _validation_result(
            asset_id,
            False,
            "stale",
            record,
            resolved,
            [f"Asset file no longer matches its registered fingerprint: {asset_id}"],
        )
    return _validation_result(asset_id, True, "current", record, resolved, [])


def resolve_asset_dependencies(
    asset_ids: list[str],
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> AssetDependencies:
    normalized = sorted(set(asset_ids))
    fingerprints: dict[str, JsonObject] = {}
    paths: dict[str, str] = {}
    errors: list[str] = []
    for asset_id in normalized:
        result = validate_asset(asset_id, assets_dir)
        if not result["valid"] or result["asset"] is None or result["resolved_path"] is None:
            errors.extend(result["errors"])
            continue
        fingerprint = fingerprint_from_json(result["asset"].get("artifact_fingerprint"))
        if fingerprint is None:
            errors.append(f"Asset fingerprint is invalid: {asset_id}")
            continue
        fingerprints[asset_id] = fingerprint
        paths[asset_id] = result["resolved_path"]
    return {
        "asset_ids": normalized,
        "asset_fingerprints": fingerprints,
        "asset_paths": paths,
        "errors": errors,
    }


def asset_path(asset_id: str, assets_dir: Path = DEFAULT_ASSETS_DIR) -> Path:
    result = validate_asset(asset_id, assets_dir)
    if not result["valid"] or result["resolved_path"] is None:
        raise AssetError("; ".join(result["errors"]) or f"Asset is unavailable: {asset_id}")
    return Path(result["resolved_path"])


def load_asset_manifest(assets_dir: Path = DEFAULT_ASSETS_DIR) -> JsonObject:
    manifest_path = assets_dir / ASSET_MANIFEST_NAME
    if not manifest_path.exists():
        return {"schema_version": 1, "assets": []}
    try:
        raw: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError(f"Could not read asset manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AssetError("Asset manifest must be a JSON object.")
    manifest = cast(JsonObject, raw)
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("assets"), list):
        raise AssetError("Asset manifest must use schema_version 1 and contain an assets list.")
    records = _manifest_records(manifest)
    ids = [_record_id(record) for record in records]
    if any(asset_id is None for asset_id in ids) or len(set(ids)) != len(ids):
        raise AssetError("Asset manifest contains invalid or duplicate asset IDs.")
    return manifest


def format_asset_inventory(inventory: AssetInventory) -> str:
    lines = [
        f"Assets: {inventory['total']}",
        f"Current: {inventory['current_count']}",
        f"Stale: {inventory['stale_count']}",
        f"Missing: {inventory['missing_count']}",
        f"Invalid: {inventory['invalid_count']}",
    ]
    for result in inventory["assets"]:
        path = result["resolved_path"] or "unknown"
        lines.append(f"- {result['asset_id']} ({result['state']}): {path}")
    return "\n".join(lines)


def format_asset_validation(result: AssetValidationResult) -> str:
    lines = [f"Asset: {result['asset_id']}", f"State: {result['state']}"]
    if result["resolved_path"] is not None:
        lines.append(f"Path: {result['resolved_path']}")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def format_register_asset_result(result: RegisterAssetResult) -> str:
    status = "registered" if result["success"] else "failed"
    lines = [f"Asset: {result['asset_id']}", f"Status: {status}"]
    if result["reused_existing"]:
        lines.append("Reused existing registration: yes")
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _validate_registration_inputs(path: Path, asset_id: str, version: str, assets_dir: Path) -> list[str]:
    errors: list[str] = []
    if not ASSET_ID_PATTERN.fullmatch(asset_id):
        errors.append("Asset ID must be lowercase snake-case.")
    if not version.strip():
        errors.append("Asset version must be a non-empty string.")
    if not path.is_file():
        errors.append(f"Asset file not found: {path}")
        return errors
    if path.suffix.lower() != ".png":
        errors.append("Asset file must be a PNG.")
    try:
        path.resolve().relative_to(assets_dir.resolve())
    except ValueError:
        errors.append(f"Asset file must be inside {assets_dir.resolve()}.")
    return errors


def _png_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        if image.format != "PNG":
            raise ValueError("file content is not PNG")
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    return width, height


def _manifest_records(manifest: JsonObject) -> list[JsonObject]:
    value = manifest.get("assets")
    if not isinstance(value, list):
        raise AssetError("Asset manifest assets must be a list.")
    records: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise AssetError("Asset manifest entries must be objects.")
        records.append(cast(JsonObject, item))
    return records


def _manifest_ids(manifest: JsonObject) -> list[str]:
    return sorted(asset_id for record in _manifest_records(manifest) if (asset_id := _record_id(record)) is not None)


def _validate_record(record: JsonObject) -> list[str]:
    errors: list[str] = []
    asset_id = _record_id(record)
    if asset_id is None:
        errors.append("Asset record ID must be lowercase snake-case.")
    if not isinstance(record.get("version"), str) or not str(record.get("version", "")).strip():
        errors.append("Asset version must be a non-empty string.")
    if record.get("type") != "image" or record.get("media_type") != PNG_MEDIA_TYPE:
        errors.append("Asset type must be image/png.")
    if not isinstance(record.get("path"), str) or not str(record.get("path", "")):
        errors.append("Asset path must be a non-empty string.")
    for field in ("width", "height"):
        value = record.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"Asset {field} must be a positive integer.")
    if fingerprint_from_json(record.get("artifact_fingerprint")) is None:
        errors.append("Asset artifact_fingerprint is invalid.")
    return errors


def _safe_asset_path(assets_dir: Path, relative_path: str) -> Path:
    root = assets_dir.resolve()
    resolved = (assets_dir / relative_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Asset path escapes the asset root: {relative_path}") from exc
    return resolved


def _record_id(record: JsonObject) -> str | None:
    value = record.get("id")
    return value if isinstance(value, str) and ASSET_ID_PATTERN.fullmatch(value) else None


def _find_asset(records: list[JsonObject], asset_id: str) -> JsonObject | None:
    return next((record for record in records if record.get("id") == asset_id), None)


def _asset_index(records: list[JsonObject], asset_id: str) -> int | None:
    return next((index for index, record in enumerate(records) if record.get("id") == asset_id), None)


def _registered_at(records: list[JsonObject], index: int | None, default: str) -> str:
    if index is None:
        return default
    value = records[index].get("registered_at")
    return value if isinstance(value, str) and value else default


def _same_asset(existing: JsonObject, proposed: JsonObject) -> bool:
    ignored = {"registered_at", "updated_at"}
    return {key: value for key, value in existing.items() if key not in ignored} == {
        key: value for key, value in proposed.items() if key not in ignored
    }


def _validation_result(
    asset_id: str,
    valid: bool,
    state: str,
    asset: JsonObject | None,
    path: Path | None,
    errors: list[str],
) -> AssetValidationResult:
    return {
        "asset_id": asset_id,
        "valid": valid,
        "state": state,
        "asset": asset,
        "resolved_path": str(path) if path is not None else None,
        "errors": errors,
    }


def _register_result(
    asset_id: str,
    success: bool,
    reused: bool,
    asset: JsonObject | None,
    errors: list[str],
) -> RegisterAssetResult:
    return {
        "asset_id": asset_id,
        "success": success,
        "reused_existing": reused,
        "asset": asset,
        "errors": errors,
    }
