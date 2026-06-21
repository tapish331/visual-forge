"""Render contract-compliant visual templates."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypedDict, cast

from .templates import DEFAULT_TEMPLATES_DIR, TemplateError, discover_template_files, import_template_module
from .templates import validate_template_file


class RenderTemplateResult(TypedDict):
    template_ref: str
    template_id: str | None
    output_path: str
    success: bool
    errors: list[str]


class TemplateParamsValidationResult(TypedDict):
    template_ref: str
    template_id: str | None
    valid: bool
    errors: list[str]


TemplateValidator = Callable[[dict[str, object]], list[str]]
TemplateRenderer = Callable[[dict[str, object], str], None]


def render_template_from_json(
    template_ref: str,
    output_path: Path,
    params_json: str,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> RenderTemplateResult:
    params, errors = parse_params_json(params_json)
    if errors:
        return _render_template_result(template_ref, None, output_path, False, errors)
    return render_template(template_ref, output_path, params, templates_dir)


def render_template(
    template_ref: str,
    output_path: Path,
    params: dict[str, object],
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> RenderTemplateResult:
    errors: list[str] = []

    validation = validate_template_params(template_ref, params, templates_dir)
    if not validation["valid"]:
        return _render_template_result(template_ref, validation["template_id"], output_path, False, validation["errors"])

    template_file = resolve_template_file(template_ref, templates_dir)
    if template_file is None:
        return _render_template_result(
            template_ref,
            None,
            output_path,
            False,
            [f"Template not found: {template_ref}"],
        )

    template_info = validate_template_file(template_file)
    template_id = template_info["template_id"]
    if not template_info["valid"]:
        return _render_template_result(template_ref, template_id, output_path, False, template_info["errors"])

    try:
        module = import_template_module(template_file)
    except Exception as exc:  # noqa: BLE001 - template import failures are render result data.
        return _render_template_result(
            template_ref,
            template_id,
            output_path,
            False,
            [f"Could not import template: {exc}"],
        )

    render = _read_render(module)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        render(params, str(output_path))
    except Exception as exc:  # noqa: BLE001 - template render failures are render result data.
        errors.append(f"render() raised {type(exc).__name__}: {exc}")
        return _render_template_result(template_ref, template_id, output_path, False, errors)

    if not output_path.exists():
        errors.append(f"Template did not create output file: {output_path}")
    elif not output_path.is_file():
        errors.append(f"Template output is not a file: {output_path}")
    elif output_path.stat().st_size == 0:
        errors.append(f"Template created an empty output file: {output_path}")

    return _render_template_result(template_ref, template_id, output_path, not errors, errors)


def validate_template_params(
    template_ref: str,
    params: dict[str, object],
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> TemplateParamsValidationResult:
    template_file = resolve_template_file(template_ref, templates_dir)
    if template_file is None:
        return _template_params_validation_result(
            template_ref,
            None,
            False,
            [f"Template not found: {template_ref}"],
        )

    template_info = validate_template_file(template_file)
    template_id = template_info["template_id"]
    if not template_info["valid"]:
        return _template_params_validation_result(template_ref, template_id, False, template_info["errors"])

    try:
        module = import_template_module(template_file)
    except Exception as exc:  # noqa: BLE001 - template import failures are validation result data.
        return _template_params_validation_result(
            template_ref,
            template_id,
            False,
            [f"Could not import template: {exc}"],
        )

    validate_params = _read_validate_params(module)
    errors = validate_params(params)
    return _template_params_validation_result(template_ref, template_id, not errors, errors)


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


def _render_template_result(
    template_ref: str,
    template_id: str | None,
    output_path: Path,
    success: bool,
    errors: list[str],
) -> RenderTemplateResult:
    return {
        "template_ref": template_ref,
        "template_id": template_id,
        "output_path": str(output_path),
        "success": success,
        "errors": errors,
    }


def _template_params_validation_result(
    template_ref: str,
    template_id: str | None,
    valid: bool,
    errors: list[str],
) -> TemplateParamsValidationResult:
    return {
        "template_ref": template_ref,
        "template_id": template_id,
        "valid": valid,
        "errors": errors,
    }
