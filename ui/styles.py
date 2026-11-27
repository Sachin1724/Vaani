from functools import lru_cache
try:
    from ..config import get_asset_path
except (ImportError, ValueError):
    from config import get_asset_path

_CHECK = get_asset_path("check_white.svg").replace("\\", "/")

# Website palette (docs/site.css): light = cream surfaces + slate ink + steel accent; dark = the same family inverted
THEMES = {
    "light": {
        "bg_dark": "#F5EFE6", "bg_sidebar": "#EFE8DA", "bg_card": "#EFE8DA", "bg_control": "#E8DFCA",
        "border_glass": "rgba(30, 42, 54, 0.10)", "accent_primary": "#7895B2", "accent_text": "#3E5A75",
        "accent_ring": "#7895B2", "danger": "#B42318", "success": "#15803D",
        "text_main": "#1E2A36", "text_secondary": "#45515E", "text_muted": "#56626F",  # >= 6:1 on the cards
        "tint": "rgba(30, 42, 54, 0.06)", "tint_strong": "rgba(30, 42, 54, 0.11)", "tint_line": "rgba(30, 42, 54, 0.35)",
        "button": "#DCD1B6", "button_hover": "#D0C4A6", "button_press": "#C4B795",
        "primary": "#1E2A36", "primary_text": "#F5EFE6", "primary_hover": "#2E4560", "primary_press": "#16202A",
        "chevron": "chevron_dark.svg",
    },
    "dark": {
        "bg_dark": "#141B23", "bg_sidebar": "#10161D", "bg_card": "#1B2530", "bg_control": "#243140",
        "border_glass": "rgba(245, 239, 230, 0.10)", "accent_primary": "#7895B2", "accent_text": "#9FB6CC",
        "accent_ring": "#7895B2", "danger": "#EF4444", "success": "#22C55E",
        "text_main": "#F5EFE6", "text_secondary": "rgba(245, 239, 230, 0.62)", "text_muted": "#8C99A8",
        "tint": "rgba(245, 239, 230, 0.06)", "tint_strong": "rgba(245, 239, 230, 0.12)", "tint_line": "rgba(245, 239, 230, 0.35)",
        "button": "rgba(245, 239, 230, 0.10)", "button_hover": "rgba(245, 239, 230, 0.16)", "button_press": "rgba(245, 239, 230, 0.22)",
        "primary": "#F5EFE6", "primary_text": "#1E2A36", "primary_hover": "#FFFFFF", "primary_press": "#E8DFCA",
        "chevron": "chevron.svg",
    },
}
COLORS = dict(THEMES["light"])  # the active theme; main_qss() updates it

MAIN_WINDOW_QSS = """
QMainWindow, QWidget#Content {{ background-color: {bg_dark}; }}
QWidget {{ color: {text_main}; font-family: {font}; font-size: 13px; }}
QLabel {{ background: transparent; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QToolTip {{ background: {bg_card}; color: {text_main}; border: 1px solid {border_glass}; padding: 4px 8px; }}

/* Sidebar */
QFrame#Sidebar {{ background-color: {bg_sidebar}; border: none; border-right: 1px solid {border_glass}; }}
QLabel#AppName {{ font-size: 15px; font-weight: 600; }}
QLabel#NavCaption {{ color: {text_muted}; font-size: 11px; font-weight: 600; padding: 12px 10px 4px 10px; }}
QLabel#HotkeyHint {{ color: {text_muted}; font-size: 11.5px; padding: 6px 10px; }}
QPushButton[nav="true"] {{
    background: transparent; color: {text_main}; text-align: left;
    padding: 0 10px; height: 32px; border-radius: 8px; font-weight: 500;
}}
QPushButton[nav="true"]:hover {{ background: {tint}; }}
QPushButton[nav="true"]:checked {{ background: {tint_strong}; font-weight: 600; }}

/* Page chrome */
QLabel#PageTitle, QLabel#AppName {{ font-family: {display}; }}
QLabel#PageTitle {{ font-size: 26px; font-weight: 600; }}
QLabel#PageSub {{ color: {text_secondary}; font-size: 13px; }}
QLabel#SectionHeader {{ color: {text_secondary}; font-size: 11px; font-weight: 600; padding-left: 4px; }}
QLabel#SectionFooter {{ color: {text_muted}; font-size: 11.5px; padding: 0 4px; }}
QLabel#Empty {{ color: {text_muted}; font-size: 13px; }}

/* Grouped lists */
QFrame#Group {{ background-color: {bg_card}; border: 1px solid {border_glass}; border-radius: 12px; }}
QFrame#Sep {{ background: {border_glass}; border: none; max-height: 1px; min-height: 1px; }}
QLabel#RowLabel {{ font-size: 13.5px; }}
QLabel#RowSub {{ color: {text_secondary}; font-size: 12.5px; }}
QLabel#RowMeta {{ color: {text_muted}; font-size: 12.5px; }}
QLabel#RowValue {{ color: {text_secondary}; font-size: 13px; }}
QLabel#RowValue[state="ok"] {{ color: {success}; }}
QLabel#RowValue[state="info"] {{ color: {accent_text}; }}
QLabel#RowValue[state="off"] {{ color: {text_muted}; }}
QLabel#MetricValue {{ font-size: 28px; font-weight: 600; }}

/* Badges */
QLabel[badge] {{ border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 600; background: {tint_strong}; color: {text_secondary}; }}
QLabel[badge="active"] {{ background: rgba(34, 197, 94, 0.16); color: {success}; }}
QLabel[badge="accent"] {{ background: rgba(120, 149, 178, 0.22); color: {accent_text}; }}

/* Hotkey recorder */
QFrame#HotkeyBox {{ background: {tint}; border: 1px solid {border_glass}; border-radius: 8px; }}
QFrame#HotkeyBox[recording="true"] {{ border: 2px solid {accent_primary}; background: rgba(120, 149, 178, 0.10); }}
QLabel#KeyCap {{ background: {bg_control}; color: {text_main}; border-radius: 6px; border-bottom: 2px solid {tint_line};
    padding: 2px 8px; font-size: 12px; font-weight: 600; }}
QLabel#KeyHint {{ color: {text_secondary}; font-size: 12px; background: transparent; border: none; }}
QLabel#KeyHint[error="true"] {{ color: {danger}; }}

/* Buttons: secondary by default, primary = ink fill (the website's button), danger = tinted red */
QPushButton {{
    background-color: {button}; color: {text_main}; border: none; border-radius: 8px;
    padding: 0 14px; height: 32px; font-weight: 500;
}}
QPushButton:hover {{ background-color: {button_hover}; }}
QPushButton:pressed {{ background-color: {button_press}; }}
QPushButton:disabled {{ color: {text_muted}; background-color: {tint}; }}
QPushButton[small="true"] {{ height: 28px; padding: 0 10px; font-size: 12.5px; }}
QPushButton#PrimaryButton {{ background: {primary}; color: {primary_text}; }}
QPushButton#PrimaryButton:hover {{ background: {primary_hover}; }}
QPushButton#PrimaryButton:pressed {{ background-color: {primary_press}; }}
QPushButton#PrimaryButton:disabled {{ background-color: {tint_strong}; color: {text_muted}; }}
QPushButton#DangerButton {{ background-color: rgba(239, 68, 68, 0.14); color: {danger}; }}
QPushButton#DangerButton:hover {{ background-color: rgba(239, 68, 68, 0.24); }}
QPushButton[small="true"]:checked {{ background: {primary}; color: {primary_text}; }}
QPushButton::menu-indicator {{ image: none; width: 0; }}

/* Transcribe */
QFrame#DropZone {{ background: {bg_card}; border: 1.5px dashed {tint_line}; border-radius: 12px; }}
QLabel#DropTitle {{ font-family: {display}; font-size: 18px; font-weight: 600; }}
QLabel#CaptionPreview {{ background: #0F141A; color: #F5EFE6; border-radius: 8px; padding: 8px 12px; font-size: 14px; }}
QLabel#TimeCode {{ color: {text_secondary}; font-family: Consolas, monospace; font-size: 12px; }}
QFrame#FixBar {{ background: rgba(120, 149, 178, 0.18); border-radius: 8px; }}
QFrame#ActionBar {{ background: {bg_card}; border: 1px solid {border_glass}; border-radius: 12px; }}

/* Inputs */
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
    background-color: {bg_control}; border: 1px solid transparent; border-radius: 8px;
    padding: 0 10px; height: 30px; selection-background-color: {accent_primary};
}}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{ border: 1px solid {accent_ring}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {bg_card}; border: 1px solid {border_glass}; border-radius: 8px;
    selection-background-color: {primary}; selection-color: {primary_text}; outline: 0; padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {tint_strong}; }}
QSlider::sub-page:horizontal {{ background: {accent_primary}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 18px; height: 18px; margin: -7px 0; border-radius: 9px; background: #fff;
    border: 1px solid {border_glass}; }}
QProgressBar {{ background: {tint_strong}; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }}
QProgressBar::chunk {{ background: {accent_primary}; border-radius: 2px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 9px; border: 1.5px solid {tint_line}; }}
QCheckBox::indicator:checked {{ background: {accent_primary}; border-color: {accent_primary}; image: url({check}); }}

/* Tables and lists */
QTableWidget {{ background: transparent; border: none; gridline-color: transparent; outline: 0;
    selection-background-color: rgba(120, 149, 178, 0.28); selection-color: {text_main}; }}
QTableWidget::item {{ border-bottom: 1px solid {border_glass}; padding: 0 8px; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: transparent; color: {text_muted}; padding: 10px 11px; border: none;
    border-bottom: 1px solid {border_glass}; font-size: 12px; font-weight: 500; text-align: left; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
QListWidget {{ background: transparent; border: none; outline: 0; padding: 6px; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 8px; margin-bottom: 2px; }}
QListWidget::item:selected {{ background: rgba(120, 149, 178, 0.28); color: {text_main}; }}
QListWidget::item:hover:!selected {{ background: {tint}; }}
QTextBrowser {{ background: transparent; border: none; }}
QSplitter::handle {{ background: {border_glass}; width: 1px; }}
QSplitter#Gutter::handle {{ background: transparent; }}

/* Scrollbar */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {tint_line}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {text_muted}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ height: 0; }}

QMessageBox, QInputDialog {{ background-color: {bg_card}; }}
"""


@lru_cache(maxsize=2)
def _qss(theme: str) -> str:
    from .floating_pill import ui_font_family
    t = THEMES[theme]
    chevron = get_asset_path(t["chevron"]).replace("\\", "/")
    # one resolved family per role: a fallback list of uninstalled fonts makes Qt search the font database on
    # every widget polish (~19 s to build the dashboard). Body text is Inter (built for 12-14 px screen text;
    # General Sans renders thin and cramped that small on Windows); titles keep the website's General Sans.
    ui_font_family()  # registers the bundled faces
    return MAIN_WINDOW_QSS.format(**{**t, "chevron": chevron, "check": _CHECK, "font": "'Inter'",
                                     "display": f"'{ui_font_family()}'"})


def main_qss(theme: str = "light") -> str:
    """The dashboard stylesheet for "light" or "dark"; also makes that theme the active COLORS."""
    theme = theme if theme in THEMES else "light"
    COLORS.clear()
    COLORS.update(THEMES[theme])
    return _qss(theme)
