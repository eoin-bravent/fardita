"""chunker.audit — completeness/correctness certificates.

Ported verbatim from pipeline/corpus_audit.py: the text-conservation audit (covered% /
accounted% / residue-by-class) and the classifier-free `section_completeness` (the provable
`missing` measure, over parsers.declared_sections). Only change: the declared_sections
dependency points at chunker.parsers instead of the old archive_adapter module.
"""
from chunker.audit.corpus_audit import (
    audit, section_completeness, dita_section_completeness,
    canon_tokens, shingles, source_segments, classify_residue,
    is_suspect_skip, DEFAULT_SKIP_FILES,
)

__all__ = ["audit", "section_completeness", "dita_section_completeness",
           "canon_tokens", "shingles", "source_segments", "classify_residue",
           "is_suspect_skip", "DEFAULT_SKIP_FILES"]
