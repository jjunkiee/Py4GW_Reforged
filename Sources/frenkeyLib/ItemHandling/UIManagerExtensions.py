import Py4GW
import PyUIManager

from typing import Optional, Union

from Py4GWCoreLib.Inventory import Inventory
from Py4GWCoreLib.UIManager import UIManager
from Sources.frenkeyLib.ItemHandling.Rules.types import SalvageMode
from Py4GWCoreLib.FrameTree import Frame, FrameId

# What a click target may arrive as. GetSalvageOptions() populates its map from
# two sources - frame ids discovered by sweeping the live dialog, and registry
# Frame handles - so the click helpers must accept either shape plus the "not
# found" sentinel. See _coerce_frame().
ClickTarget = Union[Frame, int, None]


class UIManagerExtensions:
    @staticmethod
    def _coerce_frame(target: ClickTarget) -> Optional[Frame]:
        """Normalise a click target to a Frame handle, or None if there is none.

        The salvage-dialog discovery path yields raw frame ids while the
        registry path yields Frame handles, and the "nothing found" sentinel is
        the integer 0. Everything downstream wants one shape.
        """
        if target is None:
            return None
        if isinstance(target, Frame):
            return target
        try:
            frame_id = int(target)
        except (TypeError, ValueError):
            return None
        if frame_id <= 0:
            return None
        return Frame.from_id(frame_id)

    @staticmethod
    def _frame_exists(frame: ClickTarget) -> bool:
        handle = UIManagerExtensions._coerce_frame(frame)
        return handle is not None and handle.is_usable

    @staticmethod
    def IsElementVisible(frame: Frame | None) -> bool:
        """
        Check if a specific frame is open in the UI.

        Args:
            frame_id (int): The ID of the frame to check.

        Returns:
            bool: True if the frame is open, False otherwise.
        """
        return UIManagerExtensions._frame_exists(frame)

    @staticmethod
    def _find_first_visible_frame(frames: list[Frame]) -> Frame | None:
        for frame in frames:
            if UIManagerExtensions._frame_exists(frame):
                return frame
        return None

    @staticmethod
    def _click_frame(frame: ClickTarget) -> bool:
        """Activate a salvage-dialog frame with the two native UI calls."""
        handle = UIManagerExtensions._coerce_frame(frame)
        if handle is None or not handle.is_usable:
            return False

        handle.click()

        # Re-check between the two natives: the first can tear the dialog down,
        # and firing input at a frame that has just gone is exactly the kind of
        # thing the client does not survive.
        if not handle.is_usable:
            return True

        handle.mouse_action(8, 0, 0)
        return True

    @staticmethod
    def _get_confirm_salvage_window_frame() -> Optional[Frame]:
        """The visible salvage confirmation button, or None when none is up."""
        for candidate in (
            Frame(FrameId.ScreenFrame.C6.LesserSalvageWindow.SalvageWithLesserKitConfirm),
            Frame(FrameId.ScreenFrame.C6.SalvageMaterialsDialog.YesButton),
            Frame(FrameId.SalvageWindow.OptionsWindowConfirmMaterialsWindow.Confirm),
        ):
            if candidate.exists:
                return candidate
        return None

    @staticmethod
    def _get_salvage_option_entries():
        try:
            visible_entries_by_parent = Inventory._build_visible_frame_entry_map()
            _, _, option_entries = Inventory._get_salvage_choice_dialog_options(visible_entries_by_parent)
            return option_entries
        except Exception:
            return []
    
    @staticmethod
    def GetSalvageOptions() -> dict[SalvageMode, ClickTarget]:
        """Map each salvage mode to the dialog frame that selects it.

        Two sources, two shapes: the live-sweep path yields raw frame ids, the
        registry fallback yields Frame handles. Callers must go through
        _coerce_frame(); the union is spelled out in ClickTarget so that is not
        a thing you have to discover by crashing.
        """
        options: dict[SalvageMode, ClickTarget] = {}

        option_entries = UIManagerExtensions._get_salvage_option_entries()
        if option_entries:
            # Built once, not once per option: this walks the whole frame tree,
            # and the old placement did it again for every row on screen.
            children_map = Inventory._build_frame_children_map()

            for order, entry in enumerate(option_entries, start=1):
                entry["order"] = order
                path_root_frame_ids = list(entry["path_root_frame_ids"]) if "path_root_frame_ids" in entry else [int(entry["frame_id"])]
                # Reads no text: the native decoder kills the client on these
                # frames, so selection rests on the visible order of the dialog.
                entry["text"] = Inventory._collect_salvage_choice_option_text(
                    path_root_frame_ids,
                    children_map=children_map,
                    max_depth=2,
                )

            material_entry, _ = Inventory._choose_salvage_choice_dialog_option(option_entries, strategy=0)
            upgrade_entry, _ = Inventory._choose_salvage_choice_dialog_option(option_entries, strategy=1)

            if material_entry is not None:
                material_frame_id = int(material_entry["frame_id"])
                options[SalvageMode.LesserCraftingMaterials] = material_frame_id
                options[SalvageMode.RareCraftingMaterials] = material_frame_id

            if upgrade_entry is not None:
                upgrade_frame_id = int(upgrade_entry["frame_id"])
                options[SalvageMode.Prefix] = upgrade_frame_id
                options[SalvageMode.Suffix] = upgrade_frame_id
                options[SalvageMode.Inscription] = upgrade_frame_id

            if options:
                return options

        salvage_window_mod_one_id = Frame(FrameId.SalvageWindow.Options.Option1)
        salvage_window_mod_two_id = Frame(FrameId.SalvageWindow.Options.Option2)
        salvage_window_mod_three_id = Frame(FrameId.SalvageWindow.Options.Option3)
        salvage_window_materials_id = Frame(FrameId.SalvageWindow.Options.Option4)

        if salvage_window_mod_one_id.exists:
            options[SalvageMode.Prefix] = salvage_window_mod_one_id

        if salvage_window_mod_two_id.exists:
            options[SalvageMode.Suffix] = salvage_window_mod_two_id

        if salvage_window_mod_three_id.exists:
            options[SalvageMode.Inscription] = salvage_window_mod_three_id

        if salvage_window_materials_id.exists:
            options[SalvageMode.LesserCraftingMaterials] = salvage_window_materials_id
            options[SalvageMode.RareCraftingMaterials] = salvage_window_materials_id

        return options
    
    @staticmethod
    def ConfirmSalvageOption() -> bool:
        button = Frame(FrameId.SalvageWindow.Button)
        frame = Inventory._salvage_confirm()
        if not frame.exists:
            if not button.exists:
                return False
            frame = button

        return UIManagerExtensions._click_frame(frame)
    
    @staticmethod
    def CancelSalvageOption() -> bool:
        salvage_window_cancel_button_id = Frame(FrameId.SalvageWindow.CancelButton)
        if not salvage_window_cancel_button_id.exists:
            return False

        return UIManagerExtensions._click_frame(salvage_window_cancel_button_id)
    
    @staticmethod
    def SelectSalvageOptionAndSalvage(option: SalvageMode) -> bool:
        """
        Select a salvage option in the salvage window.

        Args:
            option (SalvageMode): The salvage option to select.

        Returns:
            bool: True if the option was successfully selected, False otherwise.
        """
        options = UIManagerExtensions.GetSalvageOptions()

        if option in options:
            if UIManagerExtensions._click_frame(options[option]):
                return UIManagerExtensions.ConfirmSalvageOption()
        else:
            UIManagerExtensions.CancelSalvageOption()

        return False
    
    @staticmethod
    def SelectSalvageOption(option: SalvageMode) -> bool:
        """
        Select a salvage option in the salvage window.

        Args:
            option (SalvageMode): The salvage option to select.

        Returns:
            bool: True if the option was successfully selected, False otherwise.
        """
        options = UIManagerExtensions.GetSalvageOptions()

        if option in options:
            return UIManagerExtensions._click_frame(options[option])

        return False
    
    @staticmethod
    def IsUpgradeWindowOpen() -> bool:
        upgrade_window_frame_id = Frame(FrameId.UpgradeWindow)
        return upgrade_window_frame_id.exists
    
    @staticmethod
    def IsMerchantWindowOpen() -> bool:
        merchant_window_frame_id = Frame(FrameId.Merchant)
        # merchant_window_frame_inner_id = Frame.from_hash(3613855137, [ # 0])
        # merchant_window_funds_id = Frame(FrameId.GoldText)
        # merchant_window_buy_button_id = Frame(FrameId.MerchantBuyButton)

        return merchant_window_frame_id.exists
        
    @staticmethod
    def IsCollectorOpen() -> bool:        
        merchant_buy_button = 1532320307
        crafter_craft_button = 1517397806
        exchange_collector_button = Frame(FrameId.Merchant.C0.C0.Exchange)
        sell_tab = Frame(FrameId.Merchant.C0.QuoteField)

        return exchange_collector_button.exists and not sell_tab.exists
    
    @staticmethod
    def IsSkillTrainerOpen() -> bool:     
        display_type_button_id = Frame(FrameId.SkillTrainerWindow.DisplayModeButton)
        sell_tab = Frame(FrameId.Merchant.C0.QuoteField)

        return display_type_button_id.exists and not sell_tab.exists
    
    @staticmethod
    def IsCrafterOpen() -> bool:
        crafter_craft_button_id = Frame(FrameId.CraftButton)

        return crafter_craft_button_id.exists

    @staticmethod
    def IsConfirmLesserMaterialsWindowOpen() -> bool:
        return Inventory.IsSalvageChoiceMaterialConfirmVisible() or UIManagerExtensions._get_confirm_salvage_window_frame() is not None

    @staticmethod
    def _accept_salvage_window() -> None:
        """Native AcceptSalvageWindow, shared by the three confirm helpers."""
        inventory = Inventory.inventory_instance()
        try:
            inventory.AcceptSalvageWindow()
        except Exception:
            pass

    @staticmethod
    def ConfirmLesserSalvage():
        UIManagerExtensions._accept_salvage_window()
        return UIManagerExtensions._click_frame(UIManagerExtensions._get_confirm_salvage_window_frame())

    @staticmethod
    def ConfirmModMaterialSalvage():
        UIManagerExtensions._accept_salvage_window()
        return UIManagerExtensions._click_frame(UIManagerExtensions._get_confirm_salvage_window_frame())
        
    @staticmethod
    def ConfirmModMaterialSalvageVisible():
        return Inventory.IsSalvageChoiceMaterialConfirmVisible() or UIManagerExtensions._get_confirm_salvage_window_frame() is not None
        
    @staticmethod
    def CancelLesserSalvage():
        salvage_lower_kit_no_button_id = Frame.from_hash(140452905, [ 6, 100, 4])
        salvage_lower_kit_no_button_id.click()
    
    @staticmethod
    def IsSalvageWindowOpen() -> bool:
        # _get_salvage_choice_confirm_frame_id() was refactored away; the replacement predicate
        # already covers the SalvageWindow.Button fallback this used to check itself.
        return Inventory.IsSalvageChoiceConfirmVisible()
    
    @staticmethod
    def IsSalvageWindowNoIdentifiedOpen() -> bool:
        salvage_window_salvage_button_id = Frame(FrameId.ScreenFrame.C6.LesserSalvageWindow.SalvageWithLesserKitConfirm)
        return salvage_window_salvage_button_id.exists
    
    @staticmethod
    def ConfirmSalvageWindowNoIdentified():
        UIManagerExtensions._accept_salvage_window()
        return UIManagerExtensions._click_frame(
            Frame(FrameId.ScreenFrame.C6.LesserSalvageWindow.SalvageWithLesserKitConfirm)
        )
            
    
    @staticmethod
    def AnySalvageRelatedWindowOpen() -> bool:
        return (
            UIManagerExtensions.IsSalvageWindowOpen()
            or UIManagerExtensions.IsConfirmLesserMaterialsWindowOpen()
            or UIManagerExtensions.ConfirmModMaterialSalvageVisible()
            or UIManagerExtensions.IsSalvageWindowNoIdentifiedOpen()
        )
