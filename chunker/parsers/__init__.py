"""chunker.parsers — the archive/DITA parser eras, completeness manifest, and the seam.

Ported VERBATIM from pipeline/archive_adapter.py: the 10 hard-won era chunkers, the
classifier-free `declared_sections` completeness manifest, and `collapse_cosmetic` (the
cross-source cosmetic seam). The full implementation lives in `_adapter.py`; this module
is the clean public surface — the ERA registry plus the functions the ingest/audit layers
call. No logic changes (CORE PRINCIPLE: move/wrap/register, never reimplement).
"""
from chunker.parsers import _adapter
from chunker.parsers._adapter import (
    ERA_CHUNKERS as ERA,          # {era_name: chunker(edition_dir, cfg, hints) -> (rows, manifest)}
    chunk_edition_canon,          # the build()-based GitHub/eCFR DITA chunker
    collapse_cosmetic,            # the archive<->GitHub cosmetic seam (ARCHITECTURE §6)
    declared_sections,            # classifier-free publisher manifest (provable-drop check)
    classify_folder,              # era sniffing for one edition folder
    classify_companion,           # companion doc_class (mp|ig|annex|attachment|appendix|exhibit) or None
    companion_identity,           # class-prefixed companion citation <AG>-<CLASS>-<localid>
    default_cfg,                  # per-agency chunk config
    parse_meta,                   # (source_version, effective_date) for an edition folder
    derive_hints,                 # structure hints derived once from the store's floor
    load_archive_dates,           # folder -> effective_date map
    eras_path,                    # per-agency era-survey cache path
    ensure_profile,               # register a regulation profile (dates/bottom_depth)
    edition_root,                 # locate the real edition root within a folder
    REG_PROFILES,
    SOURCE,
)

__all__ = ["ERA", "chunk_edition_canon", "collapse_cosmetic", "declared_sections",
           "classify_folder", "classify_companion", "companion_identity", "default_cfg",
           "parse_meta", "derive_hints", "load_archive_dates", "eras_path",
           "ensure_profile", "edition_root", "REG_PROFILES", "SOURCE"]
