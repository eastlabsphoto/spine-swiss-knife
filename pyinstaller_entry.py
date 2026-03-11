"""Entry point for PyInstaller builds (uses absolute imports)."""

import sys
from PySide6.QtWidgets import QApplication
from spine_swiss_knife.app import SpineSwissKnifeApp
from spine_swiss_knife.style import STYLESHEET
from spine_swiss_knife.updater import UpdateChecker


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = SpineSwissKnifeApp()
    window.show()

    checker = UpdateChecker(window)
    checker.update_available.connect(window.show_update_available)
    checker.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
