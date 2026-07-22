#!/usr/bin/env python3
"""Shared LLM transport for the reference-verification pass (chunker.references).

TRANSPORT ONLY — auth, retries, caching, token tracking, tolerant JSON extraction, and one
threaded batch runner. NO task logic lives here: the prompts, schemas, and PROMPT_VERSION are
in `chunker.references.prompts`, and each caller passes fully-built (system, user, schema) jobs
into `run_batch`. Two providers share the one runner:

  * USAi.gov  — OpenAI-compatible Chat Completions over stdlib urllib (Python 3.14 safe, no
                third-party deps). base_url is agency-specific (https://<agency>.usai.gov). It
                has no server-side response schema, so we fold the JSON Schema into the prompt
                and parse the reply ourselves (see extract_json).
  * Vertex AI — Google Vertex (Gemini) via the google-genai SDK; ADC auth
                (GOOGLE_APPLICATION_CREDENTIALS). Imported lazily, needed only when selected.

Behavior is ported verbatim from pipeline/gemini_audit.py + pipeline/vertex_audit.py: the two
transports, the tolerant `extract_json`, the `schema_instruction` fold-in, the `cached` scheme
(keyed by provider+model+prompt_version+stage+payload), and the token `Tracker` are unchanged.
The three near-identical run_audit / run_judge / run_classifier orchestrations are unified into
one generic `run_batch` with the SAME threading, cache key, error isolation, and progress
semantics — the only structural change, and it is task-agnostic.

cfg shape consumed here (same as the pipeline cfg):
  {"provider": "usai"|"vertex", "concurrency": int,
   "gemini": {"model", "base_url", "reasoning", "thinking_budget"},
   "vertex": {"project", "location"}}
"""
import os, json, time, re, hashlib, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

ENDPOINT_PATH = "/api/v1/chat/completions"    # USAi OpenAI-compatible chat completions
# GSA sample defaults (VertexAiServiceAccountClient.java) — overridable via config/env.
VERTEX_DEFAULT_PROJECT = "prj-t-ogp-acqsplcy-mvcai"
VERTEX_DEFAULT_LOCATION = "us-central1"


# ---------- tolerant JSON extraction ----------
def extract_json(text):
    """Parse the model's JSON, tolerating ```json fences, a junk prefix, or trailing 'extra
    data' after a complete value (seen from Vertex: e.g. an empty value then the real array)."""
    s = (text or "").strip()
    if s.startswith("```"):                              # strip ```json … ``` fences
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Collect every top-level JSON array/object in the text. Handles the 'extra data' case where
    # the reply is several concatenated values (e.g. an empty `[]` then the real array), plus any
    # junk/prose before or between them.
    dec, vals, i, n = json.JSONDecoder(), [], 0, len(s)
    while i < n:
        while i < n and s[i] not in "[{":                # skip to the next array/object
            i += 1
        if i >= n:
            break
        try:
            v, i = dec.raw_decode(s, i)
            vals.append(v)
        except json.JSONDecodeError:
            i += 1
    if not vals:
        raise ValueError(f"no JSON value found in model output: {s[:200]!r}")
    if len(vals) == 1:
        return vals[0]
    if all(isinstance(v, list) for v in vals):           # merge concatenated arrays into one
        return [item for v in vals for item in v]
    return next((v for v in vals if v), vals[0])          # else first non-empty value


def schema_instruction(schema):
    """Neither provider is asked to enforce a server-side schema uniformly (USAi can't), so we
    ask for the JSON shape in-prompt for both — keeping the folded payload (and its cache key)
    identical across providers."""
    return ("\n\nReturn ONLY a JSON value that conforms to this JSON Schema — no prose, no "
            "explanation, no markdown code fences:\n" + json.dumps(schema))


# ---------- USAi.gov transport (stdlib urllib) ----------
def _usai_body(system_text, user_text, schema, cfg):
    # Fold the schema into the user turn since USAi can't enforce it server-side.
    return {"model": cfg["gemini"]["model"],
            "temperature": 0,
            "messages": [{"role": "system", "content": system_text},
                         {"role": "user", "content": user_text + schema_instruction(schema)}]}


def _usai_base_url(cfg):
    url = (cfg.get("gemini", {}).get("base_url") or os.environ.get("USAI_BASE_URL")
           or os.environ.get("GEMINI_BASE_URL") or "").rstrip("/")
    if not url:
        raise RuntimeError("USAi base URL not set — set USAI_BASE_URL (your agency endpoint, "
                           "e.g. https://<agency>.usai.gov) in .env or gemini.base_url in config")
    return url


def _usage_from_usai(data):
    """Pull token usage from an OpenAI-compatible response (absent on some gateways)."""
    u = data.get("usage") or {}
    if not u:
        return {"prompt": 0, "output": 0, "thinking": 0, "total": 0, "reported": False}
    details = u.get("completion_tokens_details") or {}
    return {"prompt": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0),
            "thinking": details.get("reasoning_tokens", 0), "total": u.get("total_tokens", 0),
            "reported": True}


def call_usai(system_text, user_text, schema, cfg, retries=4):
    """One USAi chat-completion. Returns (parsed_json, usage_dict). usage_dict.reported is
    False if the gateway omits usage."""
    key = os.environ.get("USAI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("USAI_API_KEY (or GEMINI_API_KEY) not set in environment")
    url = _usai_base_url(cfg) + ENDPOINT_PATH
    body = json.dumps(_usai_body(system_text, user_text, schema, cfg)).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            content = data["choices"][0]["message"]["content"]
            return extract_json(content), _usage_from_usai(data)
        except urllib.error.HTTPError as e:            # HTTPError is a subclass of URLError — catch it first
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)            # backoff for rate limit / transient
                continue
            errbody = e.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"USAi HTTP {e.code}: {errbody}") from None
        except (urllib.error.URLError, TimeoutError) as e:   # connect/read timeout or conn reset: retry, then give up
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"USAi request failed after {retries} attempts: {e}") from None
    return [], None


# ---------- Vertex AI transport (google-genai; ADC) ----------
_VERTEX_CLIENT = None  # created once, reused across calls


def _vertex_client(cfg):
    """Lazily build (and cache) a Vertex-backed google-genai client from ADC."""
    global _VERTEX_CLIENT
    if _VERTEX_CLIENT is not None:
        return _VERTEX_CLIENT
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS not set — point it at the service-account JSON "
            "key GSA gave you (the same env var the Java sample uses).")
    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError(
            "google-genai not installed — the Vertex backend needs it: "
            "pip install google-genai") from e
    v = cfg.get("vertex", {})
    project = (v.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
               or os.environ.get("VERTEX_PROJECT") or VERTEX_DEFAULT_PROJECT)
    location = (v.get("location") or os.environ.get("GOOGLE_CLOUD_LOCATION")
                or os.environ.get("VERTEX_LOCATION") or VERTEX_DEFAULT_LOCATION)
    _VERTEX_CLIENT = genai.Client(vertexai=True, project=project, location=location)
    return _VERTEX_CLIENT


def _is_transient(e):
    """Rate-limit / transient server errors worth retrying with backoff."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    s = str(e).upper()
    return any(t in s for t in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE",
                                "TIMEOUT", "TIMED OUT", "CONNECTION RESET", "503", "429"))


def _usage_from_vertex(resp):
    """Pull token usage (incl. gemini-2.5 thinking tokens) from a Vertex response."""
    m = getattr(resp, "usage_metadata", None)
    if not m:
        return {"prompt": 0, "output": 0, "thinking": 0, "total": 0, "reported": False}
    g = lambda a: getattr(m, a, 0) or 0
    return {"prompt": g("prompt_token_count"), "output": g("candidates_token_count"),
            "thinking": g("thoughts_token_count"), "total": g("total_token_count"), "reported": True}


def call_vertex(system_text, user_text, schema, cfg, retries=4):
    """One Gemini generateContent call on Vertex. Returns (parsed_json, usage_dict)."""
    from google.genai import types
    client = _vertex_client(cfg)
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
    contents = user_text + schema_instruction(schema)   # fold the JSON Schema into the user turn
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            return extract_json(resp.text), _usage_from_vertex(resp)
        except Exception as e:                          # noqa: BLE001 — SDK exc hierarchy varies
            if attempt < retries - 1 and _is_transient(e):
                time.sleep(2 ** attempt * 2)            # backoff for rate limit / transient
                continue
            raise RuntimeError(f"Vertex generate_content failed: {e}") from None
    return [], None


def transport_for(cfg):
    """Pick the transport by provider. Returns (call, provider_tag, cache_suffix):
      * provider_tag folds into the cache key so a Vertex response never satisfies a USAi
        lookup (same model id + prompt otherwise collide).
      * cache_suffix keeps Vertex responses in their own dir (<cache>_vertex/) for the same
        reason at the filesystem level."""
    provider = (cfg.get("provider") or "usai").lower()
    if provider in ("vertex", "vertexai", "gcp"):
        return call_vertex, "vertex|", "_vertex"
    return call_usai, "", ""


# ---------- response cache (only real API calls are cached; a failure leaves it untouched) ----------
def cached(cache_dir, name, h, fn):
    os.makedirs(cache_dir, exist_ok=True)
    p = os.path.join(cache_dir, name + ".json")
    if os.path.exists(p):
        c = json.load(open(p, encoding="utf-8"))
        if c.get("hash") == h:
            return c["result"]
    result = fn()
    json.dump({"hash": h, "result": result}, open(p, "w", encoding="utf-8"))
    return result


# ---------- token usage tracker (thread-safe; counts only real API calls, not cache hits) ----------
class Tracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.records = []                              # [{stage, unit, prompt, output, thinking, total, reported}]

    def reset(self):
        with self._lock:
            self.records = []

    def record(self, stage, unit, usage):
        with self._lock:
            self.records.append({"stage": stage, "unit": unit, **usage})

    def summary(self, stages=("audit", "judge")):
        with self._lock:
            recs = list(self.records)
        keys = ("calls", "prompt", "thinking", "output", "total", "reported")
        out = {}
        seen = list(stages) + sorted({r["stage"] for r in recs} - set(stages))
        for st in seen:
            rs = [r for r in recs if r["stage"] == st]
            out[st] = {"calls": len(rs), "prompt": sum(r.get("prompt", 0) for r in rs),
                       "thinking": sum(r.get("thinking", 0) for r in rs),
                       "output": sum(r.get("output", 0) for r in rs),
                       "total": sum(r.get("total", 0) for r in rs),
                       "reported": sum(1 for r in rs if r.get("reported"))}
        out["total"] = {k: sum(out[st][k] for st in seen) for k in keys}
        out["per_unit"] = recs
        return out


TRACKER = Tracker()


def _concurrency(cfg):
    return max(1, int(cfg.get("concurrency", 8)))


# ---------- generic threaded batch runner (task-agnostic) ----------
def run_batch(jobs, cfg, cache_dir, *, stage, prompt_version, progress=True):
    """Run a batch of independent LLM jobs concurrently. Generic over the task — the caller
    (chunker.references.*) supplies the prompts.

    jobs: iterable of dicts:
      key        — result-dict key (e.g. a citation, or a unit citation)
      cache_name — filename stem under cache_dir (must be collision-free across keys)
      system     — the system instruction (already .format()-ed by the caller)
      user       — the user content (raw unit text, or raw + rendered discrepancies)
      schema     — the JSON Schema, folded into the user turn by the transport
      coerce     — OPTIONAL fn(parsed)->stored: cleans the parsed value before it is cached
                   (e.g. keep only dict items); default identity
      payload    — OPTIONAL string hashed into the cache key; default = `user`

    Returns {key: result_or_None}. Threaded (cfg['concurrency']), cached per
    (provider_tag, model, prompt_version, stage, payload), token-tracked. One job's failure
    isolates to None (cache untouched -> a later re-run retries just that job) instead of
    aborting the batch — callers map None to their own empty default."""
    call, provider_tag, suffix = transport_for(cfg)
    cdir = (cache_dir.rstrip("/\\") + suffix) if suffix else cache_dir
    model = cfg["gemini"]["model"]
    jobs = list(jobs)
    n, out = len(jobs), {}

    def work(job):
        payload = job.get("payload", job["user"])
        h = hashlib.sha1(f"{provider_tag}{model}|{prompt_version}|{stage}|{payload}"
                         .encode()).hexdigest()[:16]
        coerce = job.get("coerce") or (lambda x: x)

        def fn():
            res, usage = call(job["system"], job["user"], job["schema"], cfg)
            if usage:
                TRACKER.record(stage, job["key"], usage)
            return coerce(res)
        return job["key"], cached(cdir, job["cache_name"], h, fn)

    with ThreadPoolExecutor(max_workers=_concurrency(cfg)) as ex:
        futs = {ex.submit(work, j): j for j in jobs}
        for done, f in enumerate(as_completed(futs), 1):
            job = futs[f]
            try:
                k, res = f.result()
                out[k] = res
            except Exception as e:                     # one job's failure must NOT abort the batch
                out[job["key"]] = None                 # cache untouched -> a later re-run retries just this job
                print(f"    WARNING: {stage} failed for {job['key']} — "
                      f"{type(e).__name__}: {e}; leaving it to the caller's default")
            if progress and (done % 10 == 0 or done == n):
                print(f"    {stage} {done}/{n}")
    return out
