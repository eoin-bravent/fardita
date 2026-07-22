"""Companion-document classification + identity (Decision A / COMPANION_DOCS §5-6).

`classify_companion(name, title, citation) -> doc_class | None` is the single seam that
decides regulation vs companion (mp|ig|annex|attachment|appendix|exhibit), consolidating the
old `_dtt_is_companion` title check and the farsite skip regex. `companion_identity` builds
the class-prefixed, collision-proof citation `<AG>-<CLASS>-<localid>`. Re-exported verbatim
from _adapter.
"""
from chunker.parsers._adapter import classify_companion, companion_identity

__all__ = ["classify_companion", "companion_identity"]
