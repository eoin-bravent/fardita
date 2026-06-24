#!/usr/bin/env python3
"""Parallel LLM backend: Google Vertex AI (Gemini) via the google-genai SDK.

Drop-in alternative to `gemini_audit.py` (the USAi.gov stdlib backend). Exposes the SAME
public interface — `audit(units, cfg, cache_dir)` and `judge_all(jobs, cfg, cache_dir)` — so
`pipeline.py` can swap to it with `LLM_PROVIDER=vertex` (or `"provider": "vertex"` in the
config) and nothing downstream (reconcile / review / apply) changes.

Single source of truth: the prompts, schemas, the threaded orchestration (`run_audit` /
`run_judge`), caching, and the token tracker all live in `gemini_audit` — this module only
supplies the Vertex transport (`_call`) + auth and a separate cache dir.

Auth: Application Default Credentials, exactly like the Java sample GSA provided — set
`GOOGLE_APPLICATION_CREDENTIALS` to the path of the service-account JSON key. Project /
location come from config (`vertex.project` / `vertex.location`), then env
(`GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION`), then the GSA sample defaults.

Caches into a SEPARATE dir (`<cache>_vertex/`) so a Vertex run never clobbers cached USAi
responses (same model id, same prompt → same filename, but a different provider).

Requires `google-genai` (see requirements-vertex.txt). The USAi backend stays stdlib-only;
this dependency is needed only when you actually select the Vertex backend.
"""
import os, time

# Reuse the shared threaded orchestration + helpers from the USAi backend; only the transport differs.
from gemini_audit import run_audit, run_judge, _schema_instruction, _extract_json

PROVIDER = "vertex"
# GSA sample defaults (VertexAiServiceAccountClient.java) — overridable via config/env.
DEFAULT_PROJECT = "prj-t-ogp-acqsplcy-mvcai"
DEFAULT_LOCATION = "us-central1"

_CLIENT = None  # created once, reused across calls


def _client(cfg):
    """Lazily build (and cache) a Vertex-backed google-genai client from ADC."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS not set — point it at the service-account JSON "
            "key GSA gave you (the same env var the Java sample uses).")
    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError(
            "google-genai not installed — the Vertex backend needs it: "
            "pip install -r requirements-vertex.txt") from e
    v = cfg.get("vertex", {})
    project = (v.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
               or os.environ.get("VERTEX_PROJECT") or DEFAULT_PROJECT)
    location = (v.get("location") or os.environ.get("GOOGLE_CLOUD_LOCATION")
                or os.environ.get("VERTEX_LOCATION") or DEFAULT_LOCATION)
    _CLIENT = genai.Client(vertexai=True, project=project, location=location)
    return _CLIENT


def _is_transient(e):
    """Rate-limit / transient server errors worth retrying with backoff."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    s = str(e).upper()
    return any(t in s for t in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE", "503", "429"))


def _usage_from_vertex(resp):
    """Pull token usage (incl. gemini-2.5 thinking tokens) from a Vertex response."""
    m = getattr(resp, "usage_metadata", None)
    if not m:
        return {"prompt": 0, "output": 0, "thinking": 0, "total": 0, "reported": False}
    g = lambda a: getattr(m, a, 0) or 0
    return {"prompt": g("prompt_token_count"), "output": g("candidates_token_count"),
            "thinking": g("thoughts_token_count"), "total": g("total_token_count"), "reported": True}

def _call(system_text, user_text, schema, cfg, retries=4):
    """One Gemini generateContent call on Vertex. Returns (parsed_json, usage_dict)."""
    from google.genai import types
    client = _client(cfg)
    g = cfg.get("gemini", {})
    model = g.get("model", "gemini-2.5-pro")
    reasoning = g.get("reasoning", True)
    budget = g.get("thinking_budget", -1)
    config = types.GenerateContentConfig(
        system_instruction=system_text,
        temperature=0,
        response_mime_type="application/json",          # force valid JSON (no fences/prose)
        thinking_config=types.ThinkingConfig(thinking_budget=budget if reasoning else 0),
    )
    contents = user_text + _schema_instruction(schema)  # fold the JSON Schema into the user turn
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            return _extract_json(resp.text), _usage_from_vertex(resp)
        except Exception as e:                          # noqa: BLE001 — SDK exc hierarchy varies
            if attempt < retries - 1 and _is_transient(e):
                time.sleep(2 ** attempt * 2)            # backoff for rate limit / transient
                continue
            raise RuntimeError(f"Vertex generate_content failed: {e}") from None
    return [], None


def _vertex_cache(cache_dir):
    """Keep Vertex responses in their own dir so they never clobber cached USAi responses."""
    return cache_dir.rstrip("/\\") + "_vertex"


# ---------- stages (delegate to the shared orchestration with this backend's transport) ----------
def audit(units, cfg, cache_dir, progress=True):
    return run_audit(units, cfg, _vertex_cache(cache_dir), _call, provider_tag="vertex|", progress=progress)

def judge_all(jobs, cfg, cache_dir, progress=True):
    return run_judge(jobs, cfg, _vertex_cache(cache_dir), _call, provider_tag="vertex|", progress=progress)
