from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from models import (
    DEFAULT_KIT_TEMPLATES,
    canonicalize_client_numbers_by_truck,
    canonicalize_hidden_kit_entries,
    canonicalize_notes_by_kit,
    canonicalize_punch_codes_by_kit,
    ExplorerSettings,
    normalize_hidden_truck_entries,
    normalize_kit_template_entries,
    normalize_truck_order_entries,
)

APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / "_runtime"
SETTINGS_PATH = RUNTIME_DIR / "settings.json"
DEFAULT_TEMPLATE_PATH = APP_DIR / "Template" / "Template.rpd"


def _normalize_kit_templates(values: list[object] | None) -> list[str]:
    return normalize_kit_template_entries(values)


def _default_rpd_template_path() -> str:
    if DEFAULT_TEMPLATE_PATH.exists():
        return str(DEFAULT_TEMPLATE_PATH)
    return ""


def load_settings() -> ExplorerSettings:
    default_settings = ExplorerSettings(rpd_template_path=_default_rpd_template_path())
    if not SETTINGS_PATH.exists():
        return default_settings

    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_settings

    kit_templates = _normalize_kit_templates(DEFAULT_KIT_TEMPLATES)
    return ExplorerSettings(
        release_root=default_settings.release_root,
        fabrication_root=default_settings.fabrication_root,
        dashboard_launcher=default_settings.dashboard_launcher,
        radan_kitter_launcher=default_settings.radan_kitter_launcher,
        inventor_to_radan_entry=default_settings.inventor_to_radan_entry,
        rpd_template_path=default_settings.rpd_template_path,
        template_replacements_text="",
        punch_codes_text=str(payload.get("punch_codes_text") or ""),
        punch_codes_by_kit=canonicalize_punch_codes_by_kit(payload.get("punch_codes_by_kit"), kit_templates),
        notes_by_kit=canonicalize_notes_by_kit(payload.get("notes_by_kit"), kit_templates),
        client_numbers_by_truck=canonicalize_client_numbers_by_truck(payload.get("client_numbers_by_truck")),
        create_support_folders=default_settings.create_support_folders,
        kit_templates=kit_templates,
        truck_order=normalize_truck_order_entries(payload.get("truck_order")),
        hidden_trucks=normalize_hidden_truck_entries(payload.get("hidden_trucks")),
        hidden_kits=canonicalize_hidden_kit_entries(payload.get("hidden_kits"), kit_templates),
    )


def save_settings(settings: ExplorerSettings) -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    default_settings = ExplorerSettings(rpd_template_path=_default_rpd_template_path())
    payload = asdict(settings)
    payload["release_root"] = default_settings.release_root
    payload["fabrication_root"] = default_settings.fabrication_root
    payload["dashboard_launcher"] = default_settings.dashboard_launcher
    payload["radan_kitter_launcher"] = default_settings.radan_kitter_launcher
    payload["inventor_to_radan_entry"] = default_settings.inventor_to_radan_entry
    payload["rpd_template_path"] = default_settings.rpd_template_path
    payload["template_replacements_text"] = ""
    payload["create_support_folders"] = default_settings.create_support_folders
    payload["kit_templates"] = _normalize_kit_templates(DEFAULT_KIT_TEMPLATES)
    payload["punch_codes_by_kit"] = canonicalize_punch_codes_by_kit(
        settings.punch_codes_by_kit,
        settings.kit_templates,
    )
    payload["notes_by_kit"] = canonicalize_notes_by_kit(
        settings.notes_by_kit,
        settings.kit_templates,
    )
    payload["client_numbers_by_truck"] = canonicalize_client_numbers_by_truck(
        settings.client_numbers_by_truck
    )
    payload["truck_order"] = normalize_truck_order_entries(settings.truck_order)
    payload["hidden_trucks"] = normalize_hidden_truck_entries(settings.hidden_trucks)
    payload["hidden_kits"] = canonicalize_hidden_kit_entries(settings.hidden_kits, settings.kit_templates)
    SETTINGS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return SETTINGS_PATH
