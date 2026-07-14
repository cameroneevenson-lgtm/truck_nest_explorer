from __future__ import annotations

from models import normalize_hidden_truck_entries, normalize_truck_order_entries
from settings_store import save_settings


class TruckOrderingController:
    """Owns fabrication-truck ordering/hiding persistence and the small bits
    of UI state that reflect it (the up/down reorder buttons and the "Show
    Hidden" button label/tooltip).

    Extracted out of MainWindow, which used to inline all of this directly.
    Kept as a small composed helper - mirroring the existing
    PacketBuildController/FullFlowController/BlockTransferController pattern
    already used in this file - rather than trying to also absorb the
    broader truck-list/status-cache state that a couple of these methods
    reach back into via `window`.
    """

    def __init__(self, window) -> None:
        self.window = window

    def persist_truck_order(self) -> None:
        window = self.window
        window.settings.truck_order = normalize_truck_order_entries(window._all_trucks)
        save_settings(window.settings)

    def refresh_order_buttons(self) -> None:
        window = self.window
        row = window.truck_list.currentRow()
        count = window.truck_list.count()
        window.move_truck_up_button.setEnabled(count > 0 and row > 0)
        window.move_truck_down_button.setEnabled(count > 0 and 0 <= row < count - 1)

    def refresh_show_hidden_button(self) -> None:
        window = self.window
        hidden_count = len(normalize_hidden_truck_entries(window.settings.hidden_trucks))
        showing_hidden = window.show_hidden_trucks_button.isChecked()
        if hidden_count == 0 and showing_hidden:
            window.show_hidden_trucks_button.blockSignals(True)
            window.show_hidden_trucks_button.setChecked(False)
            window.show_hidden_trucks_button.blockSignals(False)
            showing_hidden = False
        label_prefix = "Hide Hidden" if showing_hidden else "Show Hidden"
        window.show_hidden_trucks_button.setText(f"{label_prefix} ({hidden_count})")
        if hidden_count:
            window.show_hidden_trucks_button.setEnabled(True)
            window.show_hidden_trucks_button.setToolTip(
                f"{hidden_count} hidden truck(s). Toggle to {'hide' if showing_hidden else 'show'} them in the truck list."
            )
            return
        if showing_hidden:
            window.show_hidden_trucks_button.setEnabled(True)
            window.show_hidden_trucks_button.setToolTip("No trucks are hidden right now.")
            return
        window.show_hidden_trucks_button.setEnabled(False)
        window.show_hidden_trucks_button.setToolTip("No trucks are hidden right now.")

    def move_selected_truck(self, direction: int) -> None:
        window = self.window
        current_row = window.truck_list.currentRow()
        if current_row < 0:
            return
        target_row = current_row + direction
        visible_trucks = window._visible_truck_numbers()
        if target_row < 0 or target_row >= len(visible_trucks):
            return

        current_truck = visible_trucks[current_row]
        target_truck = visible_trucks[target_row]
        try:
            all_current_index = next(
                index
                for index, truck_number in enumerate(window._all_trucks)
                if truck_number.casefold() == current_truck.casefold()
            )
            all_target_index = next(
                index
                for index, truck_number in enumerate(window._all_trucks)
                if truck_number.casefold() == target_truck.casefold()
            )
        except StopIteration:
            return

        window._all_trucks[all_current_index], window._all_trucks[all_target_index] = (
            window._all_trucks[all_target_index],
            window._all_trucks[all_current_index],
        )
        self.persist_truck_order()
        window._apply_truck_filter()
        window._select_truck(current_truck)
        window.log(f"Updated fabrication truck order: {current_truck}")
