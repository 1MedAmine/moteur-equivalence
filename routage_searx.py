# -*- coding: utf-8 -*-
"""Rotation B2 séquentielle entre les sept moteurs SearXNG approuvés."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field


ENGINE_SHORTCUTS = ("bi", "ddg", "goc", "nvr", "szn", "qw", "sp")
_BANG_TOKEN = re.compile(r"(?<!\S)![A-Za-z0-9_.-]+(?=\s|$)")


@dataclass
class EngineRotation:
    shortcuts: tuple[str, ...]
    _index: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.shortcuts or len(set(self.shortcuts)) != len(self.shortcuts):
            raise ValueError("Les raccourcis moteur doivent être uniques et non vides.")

    def next_sequence(self) -> tuple[str, ...]:
        with self._lock:
            start = self._index
            self._index = (self._index + 1) % len(self.shortcuts)
        return self.shortcuts[start:] + self.shortcuts[:start]


def build_bang_query(shortcut: str, query: str) -> str:
    cleaned = " ".join(_BANG_TOKEN.sub("", query).split())
    return f"!{shortcut} {cleaned}".rstrip()
