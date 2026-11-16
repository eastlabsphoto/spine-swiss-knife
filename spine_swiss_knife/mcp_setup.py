"""Tab with ready-to-copy settings for connecting Claude (MCP) to this app."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTabWidget, QApplication,
)

from .i18n import tr, language_changed
from .launcher import (
    claude_code_command, claude_desktop_config_path,
    claude_desktop_config_snippet,
)


class McpSetupTab:

    def __init__(self, tabs: QTabWidget, get_config):
        self._tabs = tabs
        self._get_config = get_config

        self._page = QWidget()
        tabs.addTab(self._page, tr("mcp.tab"))
        layout = QVBoxLayout(self._page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(6)

        self._info = QLabel(tr("mcp.info"))
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._desktop_label = QLabel()
        self._desktop_label.setWordWrap(True)
        layout.addWidget(self._desktop_label)
        self._desktop_text = QPlainTextEdit(claude_desktop_config_snippet())
        self._desktop_text.setReadOnly(True)
        self._desktop_text.setFixedHeight(140)
        layout.addWidget(self._desktop_text)
        row = QHBoxLayout()
        self._desktop_btn = QPushButton(tr("mcp.copy"))
        self._desktop_btn.clicked.connect(
            lambda: self._copy(self._desktop_text.toPlainText()))
        row.addWidget(self._desktop_btn)
        row.addStretch()
        layout.addLayout(row)

        self._code_label = QLabel()
        self._code_label.setWordWrap(True)
        layout.addWidget(self._code_label)
        self._code_text = QPlainTextEdit(claude_code_command())
        self._code_text.setReadOnly(True)
        self._code_text.setFixedHeight(60)
        layout.addWidget(self._code_text)
        row2 = QHBoxLayout()
        self._code_btn = QPushButton(tr("mcp.copy"))
        self._code_btn.clicked.connect(
            lambda: self._copy(self._code_text.toPlainText()))
        row2.addWidget(self._code_btn)
        row2.addStretch()
        layout.addLayout(row2)

        self._note = QLabel(tr("mcp.note"))
        self._note.setWordWrap(True)
        layout.addWidget(self._note)
        layout.addStretch()

        self._retranslate()
        language_changed.connect(self._retranslate)

    @staticmethod
    def _copy(text: str):
        QApplication.clipboard().setText(text)

    def _retranslate(self):
        idx = self._tabs.indexOf(self._page)
        if idx >= 0:
            self._tabs.setTabText(idx, tr("mcp.tab"))
        self._info.setText(tr("mcp.info"))
        self._desktop_label.setText(
            tr("mcp.desktop_label", path=claude_desktop_config_path()))
        self._code_label.setText(tr("mcp.code_label"))
        self._desktop_btn.setText(tr("mcp.copy"))
        self._code_btn.setText(tr("mcp.copy"))
        self._note.setText(tr("mcp.note"))
