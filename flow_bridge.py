from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import subprocess
import sys

from models import canonicalize_kit_name

DEFAULT_VENV_PYTHON = Path(r"C:\Tools\.venv\Scripts\python.exe")
APP_DIR = Path(__file__).resolve().parent
FLOW_APP_DIR = APP_DIR.parent / "fabrication_flow_dashboard"
FLOW_PROBE_PATH = APP_DIR / "flow_schedule_probe.py"

EXPLORER_TO_FLOW_KIT_NAME = {
    "PAINT PACK": "Body",
    "CONSOLE PACK": "Console",
    "INTERIOR PACK": "Interior",
    "EXTERIOR PACK": "Exterior",
    "CHASSIS PACK": "Chassis",
    "PUMP HOUSE": "Pumphouse",
    "PUMP COVERING": "Pump Covering",
    "PUMP MOUNTS": "Pump Mounts",
    "PUMP BRACKETS": "Pump Brackets",
    "STEP PACK": "Step Pack",
    "OPERATIONAL PANELS": "Operational Panels",
}
VALID_FLOW_STATUS_KEYS = {"red", "yellow", "green", "blue", "black", "missing"}


@dataclass(frozen=True)
class FlowKitInsight:
    flow_kit_name: str
    display_text: str
    tooltip_text: str = ""
    status_key: str = "missing"
    tracked: bool = True
    pdf_link: str = ""


@dataclass(frozen=True)
class FlowTruckInsight:
    available: bool
    truck_number: str
    summary_text: str
    issue: str = ""
    tooltip_text: str = ""
    planned_start_date: str = ""
    current_week: float | None = None
    gantt_png_bytes: bytes | None = None
    kit_insights_by_flow_name: dict[str, FlowKitInsight] = field(default_factory=dict)


def empty_flow_truck_insight(truck_number: str = "") -> FlowTruckInsight:
    clean_truck = str(truck_number or "").strip()
    return FlowTruckInsight(
        available=False,
        truck_number=clean_truck,
        summary_text="Flow: unavailable.",
        issue="unavailable",
        tooltip_text="Could not load scheduling insights from the fabrication flow dashboard.",
        planned_start_date="",
        current_week=None,
        gantt_png_bytes=None,
        kit_insights_by_flow_name={},
    )


def map_explorer_kit_to_flow_kit(kit_name: str) -> str:
    clean_name = canonicalize_kit_name(kit_name).upper()
    if not clean_name:
        return ""
    return EXPLORER_TO_FLOW_KIT_NAME.get(clean_name, "")


def _python_executable() -> str:
    if DEFAULT_VENV_PYTHON.exists():
        return str(DEFAULT_VENV_PYTHON)
    return sys.executable


def _normalize_status_key(value: object) -> str:
    clean_value = str(value or "").strip().lower()
    if clean_value in VALID_FLOW_STATUS_KEYS:
        return clean_value
    return "missing"


def _normalize_flow_pdf_link(value: object) -> str:
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    if "://" in text:
        return text
    path = Path(text)
    if not path.is_absolute():
        path = FLOW_APP_DIR / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def parse_flow_probe_payload(payload: object) -> FlowTruckInsight:
    if not isinstance(payload, dict):
        return empty_flow_truck_insight()

    raw_kits = payload.get("kits")
    kit_insights_by_flow_name: dict[str, FlowKitInsight] = {}
    if isinstance(raw_kits, list):
        for item in raw_kits:
            if not isinstance(item, dict):
                continue
            flow_kit_name = str(item.get("flow_kit_name") or item.get("kit_name") or "").strip()
            if not flow_kit_name:
                continue
            display_text = str(item.get("display_text") or "").strip() or flow_kit_name
            insight = FlowKitInsight(
                flow_kit_name=flow_kit_name,
                display_text=display_text,
                tooltip_text=str(item.get("tooltip_text") or "").strip(),
                status_key=_normalize_status_key(item.get("status_key")),
                tracked=bool(item.get("tracked", True)),
                pdf_link=_normalize_flow_pdf_link(item.get("pdf_link")),
            )
            kit_insights_by_flow_name[flow_kit_name.casefold()] = insight

    current_week = payload.get("current_week")
    try:
        parsed_current_week = float(current_week) if current_week is not None else None
    except (TypeError, ValueError):
        parsed_current_week = None

    truck_number = str(payload.get("truck_number") or "").strip()
    summary_text = str(payload.get("summary_text") or "").strip()
    issue = str(payload.get("issue") or "").strip()
    if not summary_text:
        if issue == "truck_missing" and truck_number:
            summary_text = f"Flow: {truck_number} is not in the fabrication flow dashboard."
        elif issue:
            summary_text = "Flow: unavailable."
        else:
            summary_text = "Flow: ready."

    raw_gantt_png = str(payload.get("gantt_png_base64") or "").strip()
    gantt_png_bytes: bytes | None = None
    if raw_gantt_png:
        try:
            gantt_png_bytes = base64.b64decode(raw_gantt_png.encode("ascii"))
        except Exception:
            gantt_png_bytes = None

    return FlowTruckInsight(
        available=bool(payload.get("available")),
        truck_number=truck_number,
        summary_text=summary_text,
        issue=issue,
        tooltip_text=str(payload.get("tooltip_text") or "").strip(),
        planned_start_date=str(payload.get("planned_start_date") or "").strip(),
        current_week=parsed_current_week,
        gantt_png_bytes=gantt_png_bytes,
        kit_insights_by_flow_name=kit_insights_by_flow_name,
    )


def load_flow_truck_insight(
    truck_number: str,
    *,
    runner=subprocess.run,
) -> FlowTruckInsight:
    clean_truck = str(truck_number or "").strip()
    if not clean_truck:
        return empty_flow_truck_insight()
    if not FLOW_APP_DIR.exists():
        return FlowTruckInsight(
            available=False,
            truck_number=clean_truck,
            summary_text="Flow: fabrication_flow_dashboard folder not found.",
            issue="dashboard_missing",
            tooltip_text=str(FLOW_APP_DIR),
            planned_start_date="",
            current_week=None,
            gantt_png_bytes=None,
            kit_insights_by_flow_name={},
        )
    if not FLOW_PROBE_PATH.exists():
        return FlowTruckInsight(
            available=False,
            truck_number=clean_truck,
            summary_text="Flow: schedule probe is missing.",
            issue="probe_missing",
            tooltip_text=str(FLOW_PROBE_PATH),
            planned_start_date="",
            current_week=None,
            gantt_png_bytes=None,
            kit_insights_by_flow_name={},
        )

    command = [_python_executable(), str(FLOW_PROBE_PATH), clean_truck]
    try:
        completed = runner(
            command,
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        return FlowTruckInsight(
            available=False,
            truck_number=clean_truck,
            summary_text="Flow: schedule probe failed to run.",
            issue="probe_failed",
            tooltip_text=str(exc),
            planned_start_date="",
            current_week=None,
            gantt_png_bytes=None,
            kit_insights_by_flow_name={},
        )

    if int(getattr(completed, "returncode", 1)) != 0:
        stderr_text = str(getattr(completed, "stderr", "") or "").strip()
        return FlowTruckInsight(
            available=False,
            truck_number=clean_truck,
            summary_text="Flow: schedule probe failed.",
            issue="probe_failed",
            tooltip_text=stderr_text or "The schedule probe returned a non-zero exit code.",
            planned_start_date="",
            current_week=None,
            gantt_png_bytes=None,
            kit_insights_by_flow_name={},
        )

    stdout_text = str(getattr(completed, "stdout", "") or "").strip()
    if not stdout_text:
        return FlowTruckInsight(
            available=False,
            truck_number=clean_truck,
            summary_text="Flow: schedule probe returned no data.",
            issue="probe_empty",
            tooltip_text="The schedule probe completed without JSON output.",
            planned_start_date="",
            current_week=None,
            gantt_png_bytes=None,
            kit_insights_by_flow_name={},
        )

    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        return FlowTruckInsight(
            available=False,
            truck_number=clean_truck,
            summary_text="Flow: schedule probe returned unreadable data.",
            issue="probe_bad_json",
            tooltip_text=str(exc),
            planned_start_date="",
            current_week=None,
            gantt_png_bytes=None,
            kit_insights_by_flow_name={},
        )
    return parse_flow_probe_payload(payload)


def flow_kit_insight_for_explorer_kit(
    kit_name: str,
    flow_truck_insight: FlowTruckInsight | None,
) -> FlowKitInsight:
    mapped_flow_kit_name = map_explorer_kit_to_flow_kit(kit_name)
    if not mapped_flow_kit_name:
        return FlowKitInsight(
            flow_kit_name="",
            display_text="Not tracked",
            tooltip_text="This canonical nest kit is not tracked as its own scheduled kit in the fabrication flow dashboard.",
            status_key="missing",
            tracked=False,
        )

    if flow_truck_insight is None:
        return FlowKitInsight(
            flow_kit_name=mapped_flow_kit_name,
            display_text="Unavailable",
            tooltip_text="Flow scheduling insights have not been loaded yet.",
            status_key="missing",
            tracked=False,
        )

    if not flow_truck_insight.available:
        display_text = "No flow truck" if flow_truck_insight.issue == "truck_missing" else "Unavailable"
        return FlowKitInsight(
            flow_kit_name=mapped_flow_kit_name,
            display_text=display_text,
            tooltip_text=flow_truck_insight.summary_text or flow_truck_insight.tooltip_text,
            status_key="missing",
            tracked=False,
        )

    matched = flow_truck_insight.kit_insights_by_flow_name.get(mapped_flow_kit_name.casefold())
    if matched is not None:
        return matched

    return FlowKitInsight(
        flow_kit_name=mapped_flow_kit_name,
        display_text="Inactive",
        tooltip_text=f"{mapped_flow_kit_name} is not active on this truck in the fabrication flow dashboard.",
        status_key="missing",
        tracked=False,
    )


def normalize_flow_insight_for_local_release(
    flow_insight: FlowKitInsight,
    *,
    fabrication_folder_exists: bool,
    fabrication_has_files: bool,
) -> FlowKitInsight:
    if fabrication_has_files:
        return flow_insight

    display_key = str(flow_insight.display_text or "").strip().casefold()
    if not flow_insight.tracked:
        return flow_insight
    if not display_key:
        return flow_insight
    if display_key.startswith("unreleased"):
        return flow_insight
    if display_key in {"inactive", "complete", "unavailable", "no flow truck", "not tracked"}:
        return flow_insight

    local_release_text = "Not released" if fabrication_folder_exists else "W missing"
    local_status_key = "red" if (not fabrication_folder_exists or flow_insight.status_key == "red") else "yellow"
    tooltip_lines = [
        f"Local release: {local_release_text}",
        f"Flow status: {flow_insight.display_text}",
    ]
    if flow_insight.tooltip_text:
        tooltip_lines.append(flow_insight.tooltip_text)

    return replace(
        flow_insight,
        display_text=local_release_text,
        tooltip_text="\n".join(tooltip_lines),
        status_key=local_status_key,
    )
