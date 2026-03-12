"""Entry point for: python -m spine_swiss_knife"""

import sys
from PySide6.QtWidgets import QApplication
from .app import SpineSwissKnifeApp
from .style import STYLESHEET
from .debug_log import CONSOLE_STYLE, install_error_hooks
from .updater import UpdateChecker
from .settings import settings
from .i18n import set_language


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

    # Check for updates in background
    checker = UpdateChecker(window)
    checker.update_available.connect(window.show_update_available)
    checker.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
