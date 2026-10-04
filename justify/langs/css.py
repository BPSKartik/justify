"""Language pack: CSS, SCSS. (Placeholder: names no units yet.)"""

from __future__ import annotations

from . import Pack


class _Pack(Pack):
    langs = ('CSS', 'SCSS')


PACK = _Pack()
