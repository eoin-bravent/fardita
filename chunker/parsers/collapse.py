"""The cross-source cosmetic seam (ARCHITECTURE §6).

`collapse_cosmetic` snaps an incoming chunk that differs only cosmetically (dash glyphs,
U.S.C./CFR spacing, run-in labels) from the store row it would abut, so archive + GitHub
combine into one clean version chain instead of a spurious boundary version. Re-exported
verbatim from _adapter.
"""
from chunker.parsers._adapter import collapse_cosmetic

__all__ = ["collapse_cosmetic"]
