from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from dialogs.import_log_dialog import ImportLogDialog
from services import (
    launch_radan_csv_import,
    radan_csv_import_lock_status,
    radan_csv_missing_symbols,
    resolve_existing_inventor_csv,
    visible_radan_sessions,
)


class RadanImportController:
    def __init__(self, window) -> None:
        self.window = window
        self._log_dialog: ImportLogDialog | None = None
        self._log_dialogs: list[ImportLogDialog] = []

    @property
    def has_running_import(self) -> bool:
        return any(not dialog.is_complete for dialog in self._log_dialogs)

    def can_close(self) -> bool:
        return not self.has_running_import

    def raise_running_dialog(self) -> ImportLogDialog | None:
        running = [dialog for dialog in self._log_dialogs if not dialog.is_complete]
        if not running:
            return None
        dialog = running[-1]
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def import_selected(self) -> None:
        window = self.window
        title = "Import CSV to RADAN"
        project_update_method = "direct-xml"
        status = window._current_status()
        if status is None:
            QMessageBox.information(window, title, "Select a kit first.")
            return
        spreadsheet_path = status.spreadsheet_match.chosen_path
        if spreadsheet_path is None:
            QMessageBox.warning(
                window,
                title,
                "This kit does not have exactly one BOM candidate, so the expected _Radan.csv path is ambiguous.",
            )
            return
        if status.paths.project_dir is None or not status.paths.project_dir.exists():
            QMessageBox.warning(
                window,
                title,
                "The L-side project folder is not available for this kit.",
            )
            return
        if status.paths.rpd_path is None or not status.paths.rpd_path.exists():
            QMessageBox.warning(
                window,
                title,
                "The L-side project file is missing for this kit.",
            )
            return
        try:
            csv_path = resolve_existing_inventor_csv(spreadsheet_path, status.paths.project_dir)
        except Exception as exc:
            QMessageBox.warning(window, title, str(exc))
            return

        output_folder = status.paths.release_kit_dir
        if output_folder is None or not output_folder.exists():
            QMessageBox.warning(
                window,
                title,
                f"The expected RADAN symbol output folder is missing:\n{output_folder}",
            )
            return
        running_import, lock_path, lock_pid = radan_csv_import_lock_status(status.paths.rpd_path)
        if running_import:
            QMessageBox.information(
                window,
                title,
                "A RADAN CSV import is already running for this project.\n\n"
                f"PID: {lock_pid}\nLock: {lock_path}",
            )
            window.log(f"RADAN CSV import already running for {status.paths.rpd_path} (PID {lock_pid}).")
            return
        try:
            radan_csv_missing_symbols(csv_path, output_folder)
        except Exception as exc:
            QMessageBox.warning(window, title, f"Could not inspect CSV symbols:\n{exc}")
            return
        allow_visible_radan = False
        try:
            visible_sessions = visible_radan_sessions()
        except Exception:
            visible_sessions = ()
        if visible_sessions:
            session_text = "\n".join(f"{pid}: {title}" for pid, title in visible_sessions[:8])
            if len(visible_sessions) > 8:
                session_text += f"\n... (+{len(visible_sessions) - 8} more)"
            choice = QMessageBox.warning(
                window,
                "Visible RADAN Sessions Are Open",
                "This import still needs RADAN automation.\n\n"
                "During symbol conversion, RADAN COM automation can redraw or disturb already-open RADAN windows, "
                "and those sessions may not be safe to keep saving afterward.\n\n"
                f"Open RADAN sessions:\n{session_text}\n\n"
                "Work requiring RADAN:\nAll CSV symbols will be rebuilt from cleaned L-side DXF working copies.\n\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if choice != QMessageBox.Yes:
                window.log(
                    "RADAN CSV import cancelled before conversion because visible RADAN sessions were open."
                )
                return
            allow_visible_radan = True
        log_dir = window._runtime_dir / "_runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(
            character if character.isalnum() else "_"
            for character in f"{status.paths.truck_number}_{status.paths.project_name}"
        ).strip("_")
        log_path = log_dir / f"radan_csv_cleaned_import_{safe_name}_{int(time.time())}.log"
        log_dialog = self._show_log(log_path)
        try:
            process = launch_radan_csv_import(
                csv_path,
                output_folder,
                project_path=status.paths.rpd_path,
                log_path=log_path,
                allow_visible_radan=allow_visible_radan,
                rebuild_symbols=True,
                preprocess_dxf_outer_profile=True,
                preprocess_dxf_tolerance=0.002,
                project_update_method=project_update_method,
                refresh_project_sheets=True,
            )
        except Exception as exc:
            log_dialog.mark_launch_failed(str(exc))
            QMessageBox.critical(window, title, str(exc))
            return
        self._write_marker(
            process=process,
            log_path=log_path,
            project_path=status.paths.rpd_path,
        )
        log_dialog.set_process(process)

        window.log(
            f"Launched RADAN CSV import for {csv_path.name} using cleaned L-side DXF working copies; "
            f"output folder is {output_folder}; project_update={project_update_method}; sheet_refresh=on."
        )

    def _show_log(self, log_path: Path) -> ImportLogDialog:
        window = self.window
        self._log_dialogs = [
            dialog
            for dialog in self._log_dialogs
            if not dialog.is_complete or dialog.isVisible()
        ]
        dialog = self._log_dialog
        if dialog is not None and dialog.is_complete:
            try:
                dialog.force_close()
            except Exception:
                pass
        dialog = ImportLogDialog(log_path, window, completion_callback=self._on_log_complete)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._log_dialog = dialog
        self._log_dialogs.append(dialog)
        return dialog

    def _marker_path(self) -> Path:
        return self.window._runtime_dir / "_runtime" / "radan_import_active.json"

    def _write_marker(
        self,
        *,
        process: subprocess.Popen[object],
        log_path: Path,
        project_path: Path,
    ) -> None:
        marker_path = self._marker_path()
        payload = {
            "pid": int(process.pid),
            "log_path": str(log_path),
            "project_path": str(project_path),
            "started_at_epoch": time.time(),
        }
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            self.window.log(f"Could not write RADAN import active marker: {exc}")

    def _clear_marker(self, *, process_id: int | None = None) -> None:
        marker_path = self._marker_path()
        if process_id is not None:
            try:
                payload = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            try:
                marker_pid = int(payload.get("pid", 0))
            except (AttributeError, TypeError, ValueError):
                marker_pid = 0
            if marker_pid and marker_pid != int(process_id):
                return
        try:
            marker_path.unlink(missing_ok=True)
        except OSError as exc:
            self.window.log(f"Could not clear RADAN import active marker: {exc}")

    def _on_log_complete(self, dialog: ImportLogDialog) -> None:
        self._clear_marker(process_id=dialog.process_id)
