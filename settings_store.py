from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from models import (
    canonicalize_hidden_kit_entries,
    canonicalize_punch_codes_by_kit,
    ExplorerSettings,
    normalize_hidden_truck_entries,
    normalize_kit_template_entries,
)

APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / "_runtime"
SETTINGS_PATH = RUNTIME_DIR / "settings.json"


def _normalize_kit_templates(values: list[object] | None) -> list[str]:
    return normalize_kit_template_entries(values)


def load_settings() -> ExplorerSettings:
    if not SETTINGS_PATH.exists():
        return ExplorerSettings()

    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ExplorerSettings()

    kit_templates = _normalize_kit_templates(payload.get("kit_templates"))
    return ExplorerSettings(
        release_root=str(payload.get("release_root") or ExplorerSettings.release_root),
        fabrication_root=str(payload.get("fabrication_root") or ExplorerSettings.fabrication_root),
        radan_kitter_launcher=str(
            payload.get("radan_kitter_launcher") or ExplorerSettings.radan_kitter_launcher
        ),
        inventor_to_radan_entry=str(
            payload.get("inventor_to_radan_entry") or ExplorerSettings.inventor_to_radan_entry
        ),
        rpd_template_path=str(payload.get("rpd_template_path") or ""),
        template_replacements_text=str(payload.get("template_replacements_text") or ""),
        punch_codes_text=str(payload.get("punch_codes_text") or ""),
        punch_codes_by_kit=canonicalize_punch_codes_by_kit(payload.get("punch_codes_by_kit"), kit_templates),
        create_support_folders=bool(payload.get("create_support_folders", True)),
        kit_templates=kit_templates,
        hidden_trucks=normalize_hidden_truck_entries(payload.get("hidden_trucks")),
        hidden_kits=canonicalize_hidden_kit_entries(payload.get("hidden_kits"), kit_templates),
    )


def save_settings(settings: ExplorerSettings) -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = asdict(settings)
    payload["kit_templates"] = _normalize_kit_templates(settings.kit_templates)
    payload["punch_codes_by_kit"] = canonicalize_punch_codes_by_kit(
        settings.punch_codes_by_kit,
        settings.kit_templates,
    )
    payload["hidden_trucks"] = normalize_hidden_truck_entries(settings.hidden_trucks)
    payload["hidden_kits"] = canonicalize_hidden_kit_entries(settings.hidden_kits, settings.kit_templates)
    SETTINGS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return SETTINGS_PATH
