"""Entry point for: python -m spine_swiss_knife"""

from __future__ import annotations

import sys

from .launcher import dispatch

_rc = dispatch(sys.argv)
if _rc is not None:
    sys.exit(_rc)

from PySide6.QtWidgets import QApplication
from .app import SpineSwissKnifeApp
from .style import STYLESHEET
from .debug_log import CONSOLE_STYLE, install_error_hooks
from .updater import UpdateChecker


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET + CONSOLE_STYLE)
    install_error_hooks()

    window = SpineSwissKnifeApp()
    window.show()

    # Check for updates in background
    checker = UpdateChecker(window)
    checker.update_available.connect(window.show_update_available)
    checker.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
