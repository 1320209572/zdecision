"""Local operating-system browser launch boundary."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from typing import Protocol


class BrowserLauncher(Protocol):
    def open(self, url: str) -> bool:
        """Request one URL in the operating system's default browser."""

        ...


class SystemDefaultBrowserLauncher:
    def __init__(
        self,
        *,
        opener: Callable[[str], bool] | None = None,
    ) -> None:
        self._opener = opener or webbrowser.open_new_tab

    def open(self, url: str) -> bool:
        try:
            return bool(self._opener(url))
        except Exception:
            return False
