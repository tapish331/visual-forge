"""Template contract validation and inventory."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TypedDict, cast

from .project import JsonObject, JsonValue


ALLOWED_OUTPUT_TYPES = {"png", "png_sequence", "mp4"}
ALLOWED_TEMPLATE_STATUSES = {"draft", "ready"}
DEFAULT_TEMPLATES_DIR = Path("templates")
CAPABILITY_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class TemplateError(Exception):
    """Raised when template inventory cannot be completed."""


class TemplateInfo(TypedDict):
    path: str
    name: str
    valid: bool
    template_id: str | None
    template_version: str | None
    output_type: str | None
    status: str
    ready: bool
    capabilities: list[str]
    metadata: JsonObject
    errors: list[str]


class TemplateInventory(TypedDict):
    total: int
    valid_count: int
    invalid_count: int
    ready_count: int
    draft_count: int
    templates: list[TemplateInfo]


class TemplateScaffoldResult(TypedDict):
    template_id: str
    capability: str
    path: str
    success: bool
    errors: list[str]


def build_inventory(templates_dir: Path = DEFAULT_TEMPLATES_DIR) -> TemplateInventory:
    files = discover_template_files(templates_dir)
    templates = [validate_template_file(path) for path in files]
    valid_count = sum(1 for template in templates if template["valid"])
    return {
        "total": len(templates),
        "valid_count": valid_count,
        "invalid_count": len(templates) - valid_count,
        "ready_count": sum(1 for template in templates if template["ready"]),
        "draft_count": sum(1 for template in templates if template["valid"] and template["status"] == "draft"),
        "templates": templates,
    }


def discover_template_files(templates_dir: Path = DEFAULT_TEMPLATES_DIR) -> list[Path]:
    if not templates_dir.exists():
        raise TemplateError(f"Missing templates directory: {templates_dir}")
    if not templates_dir.is_dir():
        raise TemplateError(f"Templates path is not a directory: {templates_dir}")

    return sorted(
        path
        for path in templates_dir.glob("*.py")
        if path.name != "__init__.py" and not path.name.startswith("_")
    )


def validate_template_file(template_file: Path) -> TemplateInfo:
    errors: list[str] = []
    metadata: JsonObject = {}
    template_id: str | None = None
    template_version: str | None = None
    output_type: str | None = None
    status = "ready"
    capabilities: list[str] = []

    if not template_file.exists():
        errors.append(f"Template file not found: {template_file}")
        return _template_info(template_file, False, template_id, template_version, output_type, status, capabilities, metadata, errors)
    if not template_file.is_file():
        errors.append(f"Template path is not a file: {template_file}")
        return _template_info(template_file, False, template_id, template_version, output_type, status, capabilities, metadata, errors)
    if template_file.suffix != ".py":
        errors.append(f"Template file must be a Python file: {template_file}")
        return _template_info(template_file, False, template_id, template_version, output_type, status, capabilities, metadata, errors)

    try:
        module = import_template_module(template_file)
    except Exception as exc:  # noqa: BLE001 - template import failures are inventory data.
        errors.append(f"Could not import template: {exc}")
        return _template_info(template_file, False, template_id, template_version, output_type, status, capabilities, metadata, errors)

    template_id = _read_string_attr(module, "TEMPLATE_ID", errors)
    template_version = _read_string_attr(module, "TEMPLATE_VERSION", errors)
    output_type = _read_string_attr(module, "OUTPUT_TYPE", errors)
    status_value: object = getattr(module, "TEMPLATE_STATUS", "ready")
    if not isinstance(status_value, str) or status_value not in ALLOWED_TEMPLATE_STATUSES:
        errors.append("TEMPLATE_STATUS must be 'draft' or 'ready' when provided")
    else:
        status = status_value
    if output_type is not None and output_type not in ALLOWED_OUTPUT_TYPES:
        errors.append(
            "OUTPUT_TYPE must be one of "
            + ", ".join(sorted(ALLOWED_OUTPUT_TYPES))
            + f"; got {output_type!r}"
        )

    metadata_func = _read_callable(module, "metadata", errors)
    validate_params_func = _read_callable(module, "validate_params", errors)
    required_assets_func = _read_callable(module, "required_assets", errors)
    _read_callable(module, "render", errors)

    if metadata_func is not None:
        metadata = _call_metadata(metadata_func, errors)
        capabilities = _read_capabilities(metadata, errors)
    if validate_params_func is not None:
        _call_string_list_function(validate_params_func, "validate_params", errors)
    if required_assets_func is not None:
        _call_string_list_function(required_assets_func, "required_assets", errors)

    return _template_info(
        template_file,
        not errors,
        template_id,
        template_version,
        output_type,
        status,
        capabilities,
        metadata,
        errors,
    )


def import_template_module(template_file: Path) -> ModuleType:
    resolved = template_file.resolve()
    module_name = f"_visual_forge_template_{resolved.stem}_{abs(hash(str(resolved)))}"
    loader = importlib.machinery.SourceFileLoader(module_name, str(resolved))
    spec = importlib.util.spec_from_loader(module_name, loader)
    if spec is None:
        raise TemplateError(f"Could not create import spec for {template_file}")

    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def format_inventory(inventory: TemplateInventory) -> str:
    lines = [
        f"Templates: {inventory['total']}",
        f"Valid: {inventory['valid_count']}",
        f"Invalid: {inventory['invalid_count']}",
        f"Ready: {inventory['ready_count']}",
        f"Draft: {inventory['draft_count']}",
    ]
    for template in inventory["templates"]:
        status = "valid" if template["valid"] else "invalid"
        template_id = template["template_id"] or template["name"]
        output_type = template["output_type"] or "unknown"
        version = template["template_version"] or "unknown"
        lines.append(
            f"- {template_id} ({status}) {output_type} v{version} "
            f"[{template['status']}]: {template['path']}"
        )
        for error in template["errors"]:
            lines.append(f"  error: {error}")
    return "\n".join(lines)


def format_template_validation(template: TemplateInfo) -> str:
    status = "valid" if template["valid"] else "invalid"
    template_id = template["template_id"] or template["name"]
    lines = [f"Template: {template_id}", f"Path: {template['path']}", f"Status: {status}"]
    if template["template_version"] is not None:
        lines.append(f"Version: {template['template_version']}")
    if template["output_type"] is not None:
        lines.append(f"Output type: {template['output_type']}")
    lines.append(f"Template status: {template['status']}")
    for error in template["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _template_info(
    template_file: Path,
    valid: bool,
    template_id: str | None,
    template_version: str | None,
    output_type: str | None,
    status: str,
    capabilities: list[str],
    metadata: JsonObject,
    errors: list[str],
) -> TemplateInfo:
    return {
        "path": str(template_file),
        "name": template_file.stem,
        "valid": valid,
        "template_id": template_id,
        "template_version": template_version,
        "output_type": output_type,
        "status": status,
        "ready": valid and status == "ready",
        "capabilities": capabilities,
        "metadata": metadata,
        "errors": errors,
    }


def scaffold_template(
    template_id: str,
    capability: str,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> TemplateScaffoldResult:
    errors: list[str] = []
    if not CAPABILITY_PATTERN.fullmatch(template_id):
        errors.append("Template ID must be lowercase snake-case.")
    if not CAPABILITY_PATTERN.fullmatch(capability):
        errors.append("Capability must be lowercase snake-case.")
    path = templates_dir / f"{template_id}.py"
    if path.exists():
        errors.append(f"Template already exists: {path}")
    if errors:
        return _scaffold_result(template_id, capability, path, False, errors)

    source = _template_scaffold_source(template_id, capability)
    try:
        templates_dir.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(source)
    except FileExistsError:
        return _scaffold_result(template_id, capability, path, False, [f"Template already exists: {path}"])
    except OSError as exc:
        return _scaffold_result(template_id, capability, path, False, [f"Could not create template: {exc}"])
    return _scaffold_result(template_id, capability, path, True, [])


def format_template_scaffold(result: TemplateScaffoldResult) -> str:
    lines = [
        f"Template: {result['template_id']}",
        f"Capability: {result['capability']}",
        f"Path: {result['path']}",
        f"Status: {'draft created' if result['success'] else 'failed'}",
    ]
    for error in result["errors"]:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _template_scaffold_source(template_id: str, capability: str) -> str:
    return f'''"""Draft Visual Forge template for {capability}."""

from __future__ import annotations


TEMPLATE_ID = "{template_id}"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_STATUS = "draft"
OUTPUT_TYPE = "png"


def metadata() -> dict[str, object]:
    return {{
        "name": "{template_id.replace('_', ' ').title()}",
        "description": "Draft template for {capability}.",
        "capabilities": ["{capability}"],
    }}


def validate_params(params: dict[str, object]) -> list[str]:
    _ = params
    return []


def required_assets(params: dict[str, object]) -> list[str]:
    _ = params
    return []


def render(params: dict[str, object], output_path: str) -> None:
    _ = params, output_path
    raise NotImplementedError("Complete this template and set TEMPLATE_STATUS to ready.")
'''


def _scaffold_result(
    template_id: str,
    capability: str,
    path: Path,
    success: bool,
    errors: list[str],
) -> TemplateScaffoldResult:
    return {
        "template_id": template_id,
        "capability": capability,
        "path": str(path),
        "success": success,
        "errors": errors,
    }


def _read_string_attr(module: ModuleType, name: str, errors: list[str]) -> str | None:
    value: object = getattr(module, name, None)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{name} must be a non-empty string")
        return None
    return value


def _read_callable(module: ModuleType, name: str, errors: list[str]) -> Callable[..., object] | None:
    value: object = getattr(module, name, None)
    if not callable(value):
        errors.append(f"{name} must be callable")
        return None
    return value


def _read_capabilities(metadata: JsonObject, errors: list[str]) -> list[str]:
    value = metadata.get("capabilities")
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("metadata()['capabilities'] must be a list of lowercase snake-case strings")
        return []
    capabilities: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not CAPABILITY_PATTERN.fullmatch(item):
            errors.append("metadata()['capabilities'] must contain only lowercase snake-case strings")
            continue
        capabilities.add(item)
    return sorted(capabilities)


def _call_metadata(func: Callable[..., object], errors: list[str]) -> JsonObject:
    try:
        result = func()
    except Exception as exc:  # noqa: BLE001 - template validation reports callable failures.
        errors.append(f"metadata() raised {type(exc).__name__}: {exc}")
        return {}

    if not isinstance(result, dict):
        errors.append("metadata() must return a dictionary")
        return {}

    raw_metadata = cast(dict[object, object], result)
    metadata: JsonObject = {}
    for key, value in raw_metadata.items():
        if not isinstance(key, str):
            errors.append("metadata() keys must be strings")
            continue
        if not _is_json_value(value):
            errors.append(f"metadata()[{key!r}] must be JSON-compatible")
            continue
        metadata[key] = _to_json_value(value)
    return metadata


def _call_string_list_function(func: Callable[..., object], name: str, errors: list[str]) -> None:
    try:
        result = func({})
    except Exception as exc:  # noqa: BLE001 - template validation reports callable failures.
        errors.append(f"{name}({{}}) raised {type(exc).__name__}: {exc}")
        return

    if not isinstance(result, list):
        errors.append(f"{name}({{}}) must return a list of strings")
        return

    items = cast(list[object], result)
    if not all(isinstance(item, str) for item in items):
        errors.append(f"{name}({{}}) must return a list of strings")


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        raw_dict = cast(dict[object, object], value)
        return all(isinstance(key, str) and _is_json_value(item) for key, item in raw_dict.items())
    return False


def _to_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_to_json_value(item) for item in cast(list[object], value)]
    raw_dict = cast(dict[object, object], value)
    return {cast(str, key): _to_json_value(item) for key, item in raw_dict.items()}
