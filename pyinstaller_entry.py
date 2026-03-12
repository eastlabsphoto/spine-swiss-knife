"""Entry point for PyInstaller builds (uses absolute imports)."""

import sys
from PySide6.QtWidgets import QApplication
from spine_swiss_knife.app import SpineSwissKnifeApp
from spine_swiss_knife.style import STYLESHEET
from spine_swiss_knife.debug_log import CONSOLE_STYLE, install_error_hooks
from spine_swiss_knife.updater import UpdateChecker
from spine_swiss_knife.settings import settings
from spine_swiss_knife.i18n import set_language


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET + CONSOLE_STYLE)
    install_error_hooks()

    # Restore language from settings
    saved_lang = settings.language()
    if saved_lang and saved_lang != "en":
        set_language(saved_lang)

    window = SpineSwissKnifeApp()

    # Sync language combo with restored language
    if saved_lang == "sk":
        window._lang_combo.setCurrentIndex(1)

    window.show()

    checker = UpdateChecker(window)
    checker.update_available.connect(window.show_update_available)
    checker.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
