from __future__ import annotations

import base64
from datetime import date, timedelta
import json
import math
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parent
FLOW_APP_DIR = APP_DIR.parent / "fabrication_flow_dashboard"


def _emit(payload: dict[str, object]) -> int:
    print(json.dumps(payload, separators=(",", ":")))
    return 0


def _status_display_text(*, released: bool, stage_label: str, status_key: str, hold_weeks: float, is_not_due: bool, blocked: bool) -> str:
    if blocked:
        return f"{stage_label} | Blocked"
    if not released:
        if hold_weeks > 0.0:
            return f"Unreleased | Hold {hold_weeks:.1f}w"
        if is_not_due:
            return "Unreleased | Not due"
        return "Unreleased"
    if status_key == "blue":
        return f"{stage_label} | Ahead"
    if status_key == "green":
        return f"{stage_label} | On track"
    if status_key == "yellow":
        return f"{stage_label} | Late"
    if status_key == "red":
        return f"{stage_label} | Critical"
    return stage_label


def _week_value_to_date_label(week_value: float, current_week: float) -> str:
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    delta_days = (float(week_value) - float(current_week)) * 7.0
    target_date = current_monday + timedelta(days=delta_days)
    return target_date.strftime("%m/%d/%y")


def _normalize_week_around_current(week_value: float, current_week: float) -> float:
    value = float(week_value)
    current = float(current_week)
    cycle = 52.0
    while (value - current) > 26.0:
        value -= cycle
    while (current - value) > 26.0:
        value += cycle
    return value


def _overlay_sort_key(row: object) -> tuple[float, str]:
    baseline_windows = getattr(row, "baseline_windows", {}) or {}
    earliest_week = min((float(start) for start, _end in baseline_windows.values()), default=math.inf)
    return (earliest_week, str(getattr(row, "row_label", "") or "").lower())


def main(argv: list[str]) -> int:
    truck_number = str(argv[1] if len(argv) > 1 else "").strip()
    if not truck_number:
        return _emit(
            {
                "available": False,
                "truck_number": "",
                "issue": "truck_missing",
                "summary_text": "Flow: no truck number was provided.",
                "tooltip_text": "Expected a truck number argument.",
                "kits": [],
            }
        )

    if not FLOW_APP_DIR.exists():
        return _emit(
            {
                "available": False,
                "truck_number": truck_number,
                "issue": "dashboard_missing",
                "summary_text": "Flow: fabrication_flow_dashboard folder not found.",
                "tooltip_text": str(FLOW_APP_DIR),
                "kits": [],
            }
        )

    db_path = FLOW_APP_DIR / "fabrication_flow.db"
    if not db_path.exists():
        return _emit(
            {
                "available": False,
                "truck_number": truck_number,
                "issue": "db_missing",
                "summary_text": "Flow: fabrication_flow dashboard database not found.",
                "tooltip_text": str(db_path),
                "kits": [],
            }
        )

    sys.path.insert(0, str(FLOW_APP_DIR))
    from database import FabricationDatabase
    from gantt_overlay import (
        LASER_START_POSITION,
        STATUS_COLORS,
        WELD_NEAR_POSITION,
        OverlayRow,
        Stage,
        build_overlay_rows,
        compute_overlay_viewport,
        normalize_overlay_row_labels,
        render_overlay_png,
    )
    from models import pdf_link
    from schedule import build_schedule_insights
    from stages import stage_from_id, stage_label

    database = FabricationDatabase(db_path)
    trucks = database.load_trucks_with_kits(active_only=False)
    target_truck = next(
        (
            truck
            for truck in trucks
            if str(getattr(truck, "truck_number", "") or "").strip().casefold() == truck_number.casefold()
        ),
        None,
    )
    if target_truck is None:
        return _emit(
            {
                "available": False,
                "truck_number": truck_number,
                "issue": "truck_missing",
                "summary_text": f"Flow: {truck_number} is not in the fabrication flow dashboard.",
                "tooltip_text": str(db_path),
                "kits": [],
            }
        )

    insights = build_schedule_insights(trucks)
    kit_windows_by_name: dict[str, dict[Stage, tuple[float, float]]] = {}
    for window in insights.kit_operation_windows:
        kit_key = str(getattr(window, "kit_name", "") or "").strip().lower()
        if not kit_key:
            continue
        stage = stage_from_id(getattr(window, "stage_id", None))
        if stage not in (Stage.LASER, Stage.BEND, Stage.WELD):
            continue
        kit_windows_by_name.setdefault(kit_key, {})[stage] = (
            float(getattr(window, "start_week", 0.0) or 0.0),
            float(getattr(window, "end_week", 0.0) or 0.0),
        )

    overlay_rows = build_overlay_rows(
        trucks=[target_truck],
        schedule_insights=insights,
        max_rows=max(1, len(getattr(target_truck, "kits", [])) * 2),
    )
    truck_start_week = None
    if getattr(target_truck, "id", None) is not None:
        truck_start_week = insights.truck_planned_start_week_by_id.get(int(target_truck.id))

    if truck_start_week is not None:
        existing_overlay_names = {
            str(getattr(row, "row_label", "") or "").split("|", 1)[1].strip().casefold()
            for row in overlay_rows
            if "|" in str(getattr(row, "row_label", "") or "")
        }
        completed_rows: list[OverlayRow] = []
        for kit in getattr(target_truck, "kits", []):
            if not bool(getattr(kit, "is_active", True)):
                continue
            if stage_from_id(getattr(kit, "front_stage_id", None)) != Stage.COMPLETE:
                continue

            kit_name = str(getattr(kit, "kit_name", "") or "").strip()
            kit_key = kit_name.casefold()
            if not kit_name or kit_key in existing_overlay_names:
                continue

            base_windows = kit_windows_by_name.get(kit_key)
            if not base_windows:
                continue

            baseline_windows: dict[Stage, tuple[float, float]] = {}
            for stage, (start_week, end_week) in base_windows.items():
                normalized_start = _normalize_week_around_current(
                    float(truck_start_week) + float(start_week),
                    float(insights.current_week),
                )
                normalized_end = _normalize_week_around_current(
                    float(truck_start_week) + float(end_week),
                    float(insights.current_week),
                )
                if normalized_end < normalized_start:
                    normalized_end = normalized_start
                baseline_windows[stage] = (normalized_start, normalized_end)
            if not baseline_windows:
                continue

            truck_label = str(getattr(target_truck, "truck_number", "") or "Truck?").strip() or "Truck?"
            latest_due_week = max(float(end) for _start, end in baseline_windows.values())
            earliest_start_week = min(float(start) for start, _end in baseline_windows.values())
            completed_rows.append(
                OverlayRow(
                    row_label=f"{truck_label} | {kit_name}",
                    windows=dict(baseline_windows),
                    baseline_windows=dict(baseline_windows),
                    front_position=WELD_NEAR_POSITION,
                    back_position=LASER_START_POSITION,
                    expected_position=WELD_NEAR_POSITION,
                    front_week=latest_due_week,
                    back_week=earliest_start_week,
                    expected_week=None,
                    latest_due_week=latest_due_week,
                    released=True,
                    blocked=False,
                    blocked_reason="",
                    status_key="green",
                    status_color=STATUS_COLORS["green"],
                    is_behind=False,
                    is_not_due=False,
                )
            )

        if completed_rows:
            overlay_rows = list(overlay_rows) + completed_rows
            overlay_rows.sort(key=_overlay_sort_key)

    rows_by_kit_name = {}
    for row in overlay_rows:
        parts = str(getattr(row, "row_label", "") or "").split("|", 1)
        if len(parts) != 2:
            continue
        row_kit_name = parts[1].strip()
        if not row_kit_name:
            continue
        rows_by_kit_name[row_kit_name.casefold()] = row

    gantt_png_base64 = ""
    if overlay_rows:
        parsed_labels = [str(row.row_label or "").split(" | ", 1) for row in overlay_rows]
        shared_kit_width = max((len(parts[1].rstrip()) for parts in parsed_labels if len(parts) > 1), default=0)
        normalized_rows = normalize_overlay_row_labels(
            overlay_rows,
            truck_width=0,
            kit_width=shared_kit_width,
        )
        min_week, max_week = compute_overlay_viewport(
            rows=normalized_rows,
            current_week=float(insights.current_week),
            forward_horizon_weeks=8.0,
            side_padding_weeks=0.35,
            extend_to_latest_due_week=False,
        )
        gantt_png = render_overlay_png(
            rows=normalized_rows,
            current_week=float(insights.current_week),
            min_week=min_week,
            max_week=max_week,
            week_label=_week_value_to_date_label,
            fig_width=10.5,
            dpi=125,
            bar_height=0.58,
            fig_min_height=2.1,
            fig_height_per_row=0.24,
            y_label_size=6.0,
            x_label_size=6.0,
            x_label_text="",
            legend_size=7.0,
            dark_mode=False,
        )
        if gantt_png is not None:
            gantt_png_base64 = base64.b64encode(gantt_png).decode("ascii")

    hold_weeks_by_kit_name = {
        str(item.kit_name or "").strip().casefold(): float(item.hold_weeks)
        for item in insights.release_hold_items
        if str(item.truck_number or "").strip().casefold() == truck_number.casefold()
    }

    active_kit_count = 0
    behind_count = 0
    blocked_count = 0
    hold_count = 0
    payload_kits: list[dict[str, object]] = []

    for kit in getattr(target_truck, "kits", []):
        kit_name = str(getattr(kit, "kit_name", "") or "").strip()
        if not kit_name:
            continue

        is_active = bool(getattr(kit, "is_active", True))
        front_stage = stage_from_id(getattr(kit, "front_stage_id", int(Stage.RELEASE)))
        back_stage = stage_from_id(getattr(kit, "back_stage_id", int(Stage.RELEASE)))
        front_stage_label = stage_label(front_stage)
        back_stage_label = stage_label(back_stage)
        released = (
            str(getattr(kit, "release_state", "") or "").strip().lower() == "released"
            or front_stage > Stage.RELEASE
        )
        overlay_row = rows_by_kit_name.get(kit_name.casefold())
        hold_weeks = float(hold_weeks_by_kit_name.get(kit_name.casefold(), 0.0))
        blocked = bool(getattr(overlay_row, "blocked", False))
        blocked_reason = str(getattr(overlay_row, "blocked_reason", "") or "").strip()
        status_key = str(getattr(overlay_row, "status_key", "") or "").strip().lower()
        is_not_due = bool(getattr(overlay_row, "is_not_due", False))
        is_behind = bool(getattr(overlay_row, "is_behind", False))

        if is_active:
            active_kit_count += 1
        if blocked:
            blocked_count += 1
        if hold_weeks > 0.0:
            hold_count += 1
        if is_behind:
            behind_count += 1

        if not is_active:
            display_text = "Inactive"
            status_key = "missing"
        elif front_stage == Stage.COMPLETE:
            display_text = "Complete"
            status_key = "green"
        else:
            display_text = _status_display_text(
                released=released,
                stage_label=front_stage_label,
                status_key=status_key,
                hold_weeks=hold_weeks,
                is_not_due=is_not_due,
                blocked=blocked,
            )

        tooltip_lines = [
            f"Flow kit: {kit_name}",
            f"Release state: {str(getattr(kit, 'release_state', '') or '').strip() or 'not_released'}",
            f"Head: {front_stage_label}",
            f"Tail: {back_stage_label}",
        ]
        if hold_weeks > 0.0:
            tooltip_lines.append(f"Release hold: {hold_weeks:.1f} week(s)")
        if blocked_reason:
            tooltip_lines.append(f"Blocked: {blocked_reason}")
        if getattr(target_truck, "planned_start_date", ""):
            tooltip_lines.append(f"Truck plan start: {target_truck.planned_start_date}")

        payload_kits.append(
            {
                "flow_kit_name": kit_name,
                "display_text": display_text,
                "tooltip_text": "\n".join(tooltip_lines),
                "status_key": status_key or "missing",
                "tracked": True,
                "is_active": is_active,
                "pdf_link": pdf_link(getattr(kit, "pdf_links", "")),
            }
        )

    summary_parts: list[str] = []
    if str(getattr(target_truck, "planned_start_date", "") or "").strip():
        summary_parts.append(f"Flow plan {target_truck.planned_start_date}")
    else:
        summary_parts.append("Flow plan missing")
    summary_parts.append(f"Active flow kits {active_kit_count}")
    if hold_count:
        summary_parts.append(f"Holds {hold_count}")
    if behind_count:
        summary_parts.append(f"Behind {behind_count}")
    if blocked_count:
        summary_parts.append(f"Blocked {blocked_count}")

    tooltip_lines = [
        f"Truck: {truck_number}",
        f"Database: {db_path}",
    ]
    if str(getattr(target_truck, "planned_start_date", "") or "").strip():
        tooltip_lines.append(f"Planned start: {target_truck.planned_start_date}")
    tooltip_lines.append(f"Current week: {float(insights.current_week):.2f}")

    return _emit(
        {
            "available": True,
            "truck_number": truck_number,
            "planned_start_date": str(getattr(target_truck, "planned_start_date", "") or "").strip(),
            "current_week": float(insights.current_week),
            "summary_text": " | ".join(summary_parts),
            "tooltip_text": "\n".join(tooltip_lines),
            "gantt_png_base64": gantt_png_base64,
            "kits": payload_kits,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
