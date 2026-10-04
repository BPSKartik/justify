"""Language pack: C, C++, C/C++ header. (Placeholder: names no units yet.)"""

from __future__ import annotations

from . import Pack


class _Pack(Pack):
    langs = ('C', 'C++', 'C/C++ header')


PACK = _Pack()
