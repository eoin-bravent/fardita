"""Classifier-free completeness (ARCHITECTURE §4/§7).

`declared_sections(edition_dir, era)` is the publisher's own manifest: the section numbers
the source declared with real body text, discovered by the era's own splitter. Every one
must produce a row, else it is a provable drop (the `missing` measure). Re-exported verbatim
from _adapter; the covered%/accounted% certificates themselves are computed in the audit
layer over this manifest.
"""
from chunker.parsers._adapter import declared_sections, edition_root

__all__ = ["declared_sections", "edition_root"]
