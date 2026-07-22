"""chunker.llm — shared LLM transport for the reference-verification pass.

`client` is TRANSPORT ONLY (auth / retries / caching / token tracking / a threaded batch
runner). It carries no task logic: prompts, schemas, and PROMPT_VERSION live in
`chunker.references.prompts`, and each caller hands fully-built (system, user, schema) jobs
to `client.run_batch`. Two providers (USAi.gov stdlib REST, Vertex AI via google-genai)
share one runner; the provider is chosen from cfg."""
from chunker.llm import client

__all__ = ["client"]
