"""Entry point for: python -m spine_swiss_knife"""

import sys
from PySide6.QtWidgets import QApplication
from .app import SpineSwissKnifeApp
from .style import STYLESHEET
from .updater import UpdateChecker


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = SpineSwissKnifeApp()
    window.show()

    # Check for updates in background
    checker = UpdateChecker(window)
    checker.update_available.connect(window.show_update_available)
    checker.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
