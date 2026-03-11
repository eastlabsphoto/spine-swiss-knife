"""Entry point for: python -m spine_swiss_knife"""

import sys
from PySide6.QtWidgets import QApplication
from .app import SpineSwissKnifeApp
from .style import STYLESHEET


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = SpineSwissKnifeApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
