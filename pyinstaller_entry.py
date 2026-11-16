"""Entry point for PyInstaller builds (uses absolute imports)."""

import sys

from spine_swiss_knife.launcher import dispatch

# Headless modes (--mcp / --cli) must run before any Qt import.
_rc = dispatch(sys.argv)
if _rc is not None:
    sys.exit(_rc)

from PySide6.QtWidgets import QApplication
from spine_swiss_knife.app import SpineSwissKnifeApp
from spine_swiss_knife.style import STYLESHEET
from spine_swiss_knife.debug_log import CONSOLE_STYLE, install_error_hooks
from spine_swiss_knife.updater import UpdateChecker


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET + CONSOLE_STYLE)
    install_error_hooks()

    window = SpineSwissKnifeApp()
    window.show()

    checker = UpdateChecker(window)
    checker.update_available.connect(window.show_update_available)
    checker.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
