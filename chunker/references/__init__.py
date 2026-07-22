"""chunker.references — the reference-verification pass (post-ingest, offline).

Four stages over the versioned store, extended to ALL agencies + ALL storage eras:

  extract   — the deterministic parser refs already on each row (cross_references /
              external_references, produced at chunk time by chunker.parsers / extract_json)
              PLUS a blind LLM pass over the row's own text that lists every reference
              independently (its value: untagged prose refs the markup scan misses).
  reconcile — symmetric per-unit set comparison of parser vs LLM into one ledger, each atomic
              target tagged corroborated | parser_explicit | parser_inferred | llm_only.
  judge     — optional LLM pass over the disagreements (accept/reject/manual).
  apply     — write accepted refs back into the store rows + stamp refs_verified_from.

Task logic (prompts, schemas, PROMPT_VERSION) lives in `prompts`; the transport is the
task-agnostic `chunker.llm.client`. The pass never runs in the `build` critical path — it is
a separate `chunker.cli references` verb that audits the store's flattened `text` per unit,
oldest edition first, so each distinct row version is audited exactly once."""
