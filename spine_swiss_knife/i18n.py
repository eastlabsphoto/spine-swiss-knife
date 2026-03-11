"""Internationalization — flat-key JSON locale files with runtime switching."""

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

    def _load(self, lang: str):
        path = _LOCALES_DIR / f"{lang}.json"
        if not path.is_file():
            return
        with open(path, "r", encoding="utf-8") as f:
            self._strings = json.load(f)
        self._lang = lang

    def set_language(self, lang: str):
        if lang == self._lang:
            return
        self._load(lang)
        self.language_changed.emit()

    def tr(self, key: str, **kwargs) -> str:
        text = self._strings.get(key, key)
        if kwargs:
            try:
                text = text.format(**kwargs)
            except (KeyError, IndexError):
                pass
        return text

    @property
    def current(self) -> str:
        return self._lang


_instance = _I18n()

tr = _instance.tr
set_language = _instance.set_language
language_changed = _instance.language_changed
