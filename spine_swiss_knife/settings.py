"""Persistent application settings backed by QSettings."""

from PySide6.QtCore import QSettings

_ORG = "GreentubeSK"
_APP = "SpineSwissKnife"


class _Settings:
    def __init__(self):
        self._qs = QSettings(_ORG, _APP)

    # -- Spine executable --
    def spine_executable(self) -> str:
        return self._qs.value("spine_executable", "", type=str)

    def set_spine_executable(self, path: str):
        self._qs.setValue("spine_executable", path)

    # -- Last mode ("spine" / "json") --
    def last_mode(self) -> str:
        return self._qs.value("last_mode", "", type=str)

    def set_last_mode(self, mode: str):
        self._qs.setValue("last_mode", mode)

    # -- Last .spine file path --
    def last_spine_file(self) -> str:
        return self._qs.value("last_spine_file", "", type=str)

    def set_last_spine_file(self, path: str):
        self._qs.setValue("last_spine_file", path)

    # -- Language --
    def language(self) -> str:
        return self._qs.value("language", "en", type=str)

    def set_language(self, lang: str):
        self._qs.setValue("language", lang)


settings = _Settings()
