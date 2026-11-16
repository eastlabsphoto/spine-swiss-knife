"""Internationalization — flat-key JSON locale files with runtime switching."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal

_LOCALES_DIR = Path(__file__).parent / "locales"


class _I18n(QObject):
    language_changed = Signal()

    def __init__(self):
        super().__init__()
        self._lang = "en"
        self._strings: dict[str, str] = {}
        self._load("en")

    def _load(self, lang: str) -> bool:
        """Load a locale file; keep the current strings if it is missing/invalid."""
        path = _LOCALES_DIR / f"{lang}.json"
        if not path.is_file():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                strings = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(strings, dict):
            return False
        self._strings = strings
        self._lang = lang
        return True

    def set_language(self, lang: str):
        if lang == self._lang:
            return
        if self._load(lang):
            self.language_changed.emit()

    def tr(self, key: str, /, **kwargs) -> str:
        # `key` is positional-only so a format placeholder named {key} (passed as
        # tr("some.key", key=...)) lands in **kwargs instead of colliding with this param.
        text = self._strings.get(key, key)
        if kwargs:
            try:
                text = text.format(**kwargs)
            except (KeyError, IndexError, ValueError, AttributeError):
                pass
        return text

    @property
    def current(self) -> str:
        return self._lang


_instance = _I18n()

tr = _instance.tr
set_language = _instance.set_language
language_changed = _instance.language_changed
