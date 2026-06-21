"""Render contract-compliant visual templates."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypedDict, cast

from .assets import DEFAULT_ASSETS_DIR, resolve_asset_dependencies
from .project import JsonObject
from .templates import DEFAULT_TEMPLATES_DIR, TemplateError, discover_template_files, import_template_module
from .templates import validate_template_file


class RenderTemplateResult(TypedDict):
    template_ref: str
    template_id: str | None
    output_path: str
    success: bool
    required_assets: list[str]
    asset_fingerprints: dict[str, JsonObject]
    errors: list[str]


class TemplateParamsValidationResult(TypedDict):
    template_ref: str
    template_id: str | None
    valid: bool
    required_assets: list[str]
    asset_fingerprints: dict[str, JsonObject]
    errors: list[str]


TemplateValidator = Callable[[dict[str, object]], list[str]]
TemplateRenderer = Callable[[dict[str, object], str], None]
TemplateRequiredAssets = Callable[[dict[str, object]], list[str]]


def render_template_from_json(
    template_ref: str,
    output_path: Path,
    params_json: str,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> RenderTemplateResult:
    params, errors = parse_params_json(params_json)
    if errors:
        return _render_template_result(template_ref, None, output_path, False, [], {}, errors)
    return render_template(template_ref, output_path, params, templates_dir)


def render_template(
    template_ref: str,
    output_path: Path,
    params: dict[str, object],
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> RenderTemplateResult:
    errors: list[str] = []

    validation = validate_template_params(template_ref, params, templates_dir, assets_dir)
    if not validation["valid"]:
        return _render_template_result(
            template_ref,
            validation["template_id"],
            output_path,
            False,
            validation["required_assets"],
            validation["asset_fingerprints"],
            validation["errors"],
        )

    template_file = resolve_template_file(template_ref, templates_dir)
    if template_file is None:
        return _render_template_result(
            template_ref,
            None,
            output_path,
            False,
            [],
            {},
            [f"Template not found: {template_ref}"],
        )

    template_info = validate_template_file(template_file)
    template_id = template_info["template_id"]
    if not template_info["valid"]:
        return _render_template_result(template_ref, template_id, output_path, False, [], {}, template_info["errors"])

    try:
        module = import_template_module(template_file)
    except Exception as exc:  # noqa: BLE001 - template import failures are render result data.
        return _render_template_result(
            template_ref,
            template_id,
            output_path,
            False,
            validation["required_assets"],
            validation["asset_fingerprints"],
            [f"Could not import template: {exc}"],
        )

    render = _read_render(module)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        render(params, str(output_path))
    except Exception as exc:  # noqa: BLE001 - template render failures are render result data.
        errors.append(f"render() raised {type(exc).__name__}: {exc}")
        return _render_template_result(
            template_ref,
            template_id,
            output_path,
            False,
            validation["required_assets"],
            validation["asset_fingerprints"],
            errors,
        )

    if not output_path.exists():
        errors.append(f"Template did not create output file: {output_path}")
    elif not output_path.is_file():
        errors.append(f"Template output is not a file: {output_path}")
    elif output_path.stat().st_size == 0:
        errors.append(f"Template created an empty output file: {output_path}")

    return _render_template_result(
        template_ref,
        template_id,
        output_path,
        not errors,
        validation["required_assets"],
        validation["asset_fingerprints"],
        errors,
    )


def validate_template_params(
    template_ref: str,
    params: dict[str, object],
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
    assets_dir: Path = DEFAULT_ASSETS_DIR,
) -> TemplateParamsValidationResult:
    template_file = resolve_template_file(template_ref, templates_dir)
    if template_file is None:
        return _template_params_validation_result(
            template_ref,
            None,
            False,
            [],
            {},
            [f"Template not found: {template_ref}"],
        )

    template_info = validate_template_file(template_file)
    template_id = template_info["template_id"]
    if not template_info["valid"]:
        return _template_params_validation_result(template_ref, template_id, False, [], {}, template_info["errors"])
    if not template_info["ready"]:
        return _template_params_validation_result(
            template_ref,
            template_id,
            False,
            [],
            {},
            [f"Template is not ready: {template_id or template_ref} ({template_info['status']})"],
        )

    try:
        module = import_template_module(template_file)
    except Exception as exc:  # noqa: BLE001 - template import failures are validation result data.
        return _template_params_validation_result(
            template_ref,
            template_id,
            False,
            [],
            {},
            [f"Could not import template: {exc}"],
        )

    validate_params = _read_validate_params(module)
    errors = validate_params(params)
    required_assets: list[str] = []
    fingerprints: dict[str, JsonObject] = {}
    if not errors:
        required_assets_func = _read_required_assets(module)
        try:
            required_assets = required_assets_func(params)
        except Exception as exc:  # noqa: BLE001 - template dependency failures are validation data.
            errors.append(f"required_assets() raised {type(exc).__name__}: {exc}")
        if not isinstance(required_assets, list) or not all(isinstance(item, str) for item in required_assets):
            errors.append("required_assets(params) must return a list of asset ID strings")
            required_assets = []
    if not errors:
        dependencies = resolve_asset_dependencies(required_assets, assets_dir)
        errors.extend(dependencies["errors"])
        fingerprints = dependencies["asset_fingerprints"]
    return _template_params_validation_result(
        template_ref,
        template_id,
        not errors,
        required_assets,
        fingerprints,
        errors,
    )


def parse_params_json(params_json: str) -> tuple[dict[str, object], list[str]]:
    try:
        raw: object = json.loads(params_json)
    except json.JSONDecodeError as exc:
        return {}, [f"Invalid params JSON: {exc.msg}"]

    if not isinstance(raw, dict):
        return {}, ["Params JSON must be an object"]

    raw_params = cast(dict[object, object], raw)
    params: dict[str, object] = {}
    for key, value in raw_params.items():
        if not isinstance(key, str):
            return {}, ["Params JSON object keys must be strings"]
        params[key] = value
    return params, []


def resolve_template_file(template_ref: str, templates_dir: Path = DEFAULT_TEMPLATES_DIR) -> Path | None:
    direct_path = Path(template_ref)
    if direct_path.exists() or direct_path.suffix == ".py":
        return direct_path

    try:
        template_files = discover_template_files(templates_dir)
    except TemplateError:
        return None

    for template_file in template_files:
        template_info = validate_template_file(template_file)
        if template_info["template_id"] == template_ref or template_file.stem == template_ref:
            return template_file
    return None


def format_render_template_result(result: RenderTemplateResult) -> str:
    status = "rendered" if result["success"] else "failed"
    template = result["template_id"] or result["template_ref"]
    lines = [
        f"Template: {template}",
        f"Output: {result['output_path']}",
        f"Status: {status}",
    ]
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _read_validate_params(module: ModuleType) -> TemplateValidator:
    return cast(TemplateValidator, getattr(module, "validate_params"))


def _read_render(module: ModuleType) -> TemplateRenderer:
    return cast(TemplateRenderer, getattr(module, "render"))


def _read_required_assets(module: ModuleType) -> TemplateRequiredAssets:
    return cast(TemplateRequiredAssets, getattr(module, "required_assets"))


def _render_template_result(
    template_ref: str,
    template_id: str | None,
    output_path: Path,
    success: bool,
    required_assets: list[str],
    asset_fingerprints: dict[str, JsonObject],
    errors: list[str],
) -> RenderTemplateResult:
    return {
        "template_ref": template_ref,
        "template_id": template_id,
        "output_path": str(output_path),
        "success": success,
        "required_assets": required_assets,
        "asset_fingerprints": asset_fingerprints,
        "errors": errors,
    }


def _template_params_validation_result(
    template_ref: str,
    template_id: str | None,
    valid: bool,
    required_assets: list[str],
    asset_fingerprints: dict[str, JsonObject],
    errors: list[str],
) -> TemplateParamsValidationResult:
    return {
        "template_ref": template_ref,
        "template_id": template_id,
        "valid": valid,
        "required_assets": required_assets,
        "asset_fingerprints": asset_fingerprints,
        "errors": errors,
    }
