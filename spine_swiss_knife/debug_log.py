"""In-app debug console — shows errors live in a panel at the bottom of the window.

Captures unhandled exceptions and stderr, displays them in a QTextEdit so
users without source code can see (and copy) what went wrong.
"""

import sys
import traceback

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QLabel,
    QApplication,
)
from PySide6.QtCore import Qt, QObject, Signal


class _ErrorSignal(QObject):
    """Bridge to emit error text from any thread into the Qt main thread."""
    error = Signal(str)


_signal = _ErrorSignal()


class DebugConsole(QWidget):
    """Collapsible error console panel. Add to the bottom of your main layout."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("debugConsole")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header bar (always visible)
        header = QWidget()
        header.setObjectName("debugHeader")
        header.setCursor(Qt.PointingHandCursor)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)

        self._title = QLabel("Console")
        self._title.setObjectName("debugTitle")
        header_layout.addWidget(self._title)

        self._badge = QLabel("")
        self._badge.setObjectName("debugBadge")
        self._badge.hide()
        header_layout.addWidget(self._badge)

        header_layout.addStretch()

        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setObjectName("debugCopyBtn")
        self._copy_btn.setFixedHeight(22)
        self._copy_btn.clicked.connect(self._copy)
        header_layout.addWidget(self._copy_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("debugCopyBtn")
        self._clear_btn.setFixedHeight(22)
        self._clear_btn.clicked.connect(self._clear)
        header_layout.addWidget(self._clear_btn)

        layout.addWidget(header)
        header.mousePressEvent = lambda e: self._toggle()

        # Log area (collapsible)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setObjectName("debugLog")
        self._log.setMaximumHeight(150)
        self._log.hide()
        layout.addWidget(self._log)

        self._expanded = False
        self._error_count = 0

        # Connect signal for thread-safe appending
        _signal.error.connect(self._append_error)

    def _toggle(self):
        self._expanded = not self._expanded
        self._log.setVisible(self._expanded)
        if self._expanded:
            self._badge.hide()

    def _append_error(self, text: str):
        self._log.append(text)
        self._error_count += 1
        if not self._expanded:
            self._badge.setText(str(self._error_count))
            self._badge.show()
        # Auto-scroll to bottom
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _copy(self):
        text = self._log.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def _clear(self):
        self._log.clear()
        self._error_count = 0
        self._badge.hide()


# Stylesheet fragment — append to main stylesheet
CONSOLE_STYLE = """
#debugConsole {
    background: transparent;
}
#debugHeader {
    background-color: #151524;
    border-top: 1px solid #2a2a3c;
}
#debugTitle {
    color: #9ea8be;
    font-size: 11px;
    font-weight: bold;
    background: transparent;
}
#debugBadge {
    background-color: #f38ba8;
    color: #1a1a2a;
    font-size: 10px;
    font-weight: bold;
    border-radius: 8px;
    padding: 1px 6px;
    min-width: 16px;
}
#debugCopyBtn {
    background-color: #2a2a3c;
    color: #9ea8be;
    border: 1px solid #3a3a50;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 11px;
}
#debugCopyBtn:hover {
    color: #d0d8e8;
    border-color: #6ec072;
}
#debugLog {
    background-color: #111120;
    color: #f38ba8;
    border: none;
    font-family: "SF Mono", "Consolas", "Courier New", monospace;
    font-size: 11px;
    padding: 6px;
}
"""


def install_error_hooks():
    """Install global exception hook that routes errors to the debug console.

    Call once at startup after QApplication is created.
    """
    _original_hook = sys.excepthook
    _original_stderr = sys.stderr

    def _exception_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            _original_hook(exc_type, exc_value, exc_tb)
            return
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _signal.error.emit(tb_text)
        # Also print to original stderr so terminal still shows it
        if _original_stderr:
            try:
                _original_stderr.write(tb_text)
                _original_stderr.flush()
            except Exception:
                pass

    sys.excepthook = _exception_hook
