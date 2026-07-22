#!/usr/bin/env python3
"""Build the LLM cfg dict the references pass hands to chunker.llm.client — per agency.

There is no pipeline.config.json in chunker. Precedence (low -> high):
  DEFAULTS  <  chunker/references.config.json (optional)  <  .env (repo root)  <  environment
              <  explicit call args.
`regulation` is the agency code (drives the prompt's {regulation}). Env-var names match the
pipeline (USAI_BASE_URL / USAI_API_KEY / LLM_PROVIDER / GEMINI_MODEL / GOOGLE_APPLICATION_CREDENTIALS
/ …), so an existing .env keeps working. Secrets (API keys, ADC path) are read from the
environment at call time by the transport — never stored in cfg or written to disk.
"""
import os
import json

from chunker import paths

DEFAULTS = {
    "provider": "usai",                                   # "usai" (stdlib REST) or "vertex"
    "concurrency": 8,                                     # parallel LLM calls per run
    "gemini": {"model": "gemini-2.5-pro", "base_url": "", "reasoning": True,
               "thinking_budget": -1, "judge": False},
    "vertex": {"project": "", "location": ""},
    # Public Gemini 2.5 Pro rates (<=200k ctx); thinking bills at the output rate. 0 hides the $ figure.
    "pricing": {"input_per_1m": 1.25, "output_per_1m": 10.0, "currency": "USD"},
}

_DOTENV_LOADED = False


def load_dotenv(path):
    """Load KEY=VALUE lines into the environment (real env always wins). Honors surrounding
    quotes and strips an inline ' # comment' on unquoted values."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ("'", '"'):                           # quoted: take the quoted span verbatim
            q = v[0]; end = v.find(q, 1)
            v = v[1:end] if end != -1 else v[1:]
        else:                                             # unquoted: a ' #' / '\t#' begins a trailing comment
            for i in range(1, len(v)):
                if v[i] == "#" and v[i - 1] in " \t":
                    v = v[:i]; break
            v = v.strip()
        os.environ.setdefault(k.strip(), v)


def _ensure_dotenv():
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        load_dotenv(os.path.join(paths.ROOT, ".env"))
        _DOTENV_LOADED = True


def build_cfg(agency, *, provider=None, concurrency=None, model=None, judge=None):
    """Assemble the cfg dict for one agency's reference pass."""
    _ensure_dotenv()
    cfg = json.loads(json.dumps(DEFAULTS))                # deep copy

    # optional file overlay
    cfgpath = os.path.join(paths.PKG, "references.config.json")
    if os.path.exists(cfgpath):
        user = json.load(open(cfgpath, encoding="utf-8"))
        cfg.update({k: v for k, v in user.items() if k not in ("gemini", "vertex", "pricing")})
        for sub in ("gemini", "vertex", "pricing"):
            cfg[sub].update(user.get(sub, {}))

    # environment overlay (matches the pipeline's env names)
    if os.environ.get("LLM_PROVIDER"):
        cfg["provider"] = os.environ["LLM_PROVIDER"]
    if os.environ.get("LLM_CONCURRENCY"):
        cfg["concurrency"] = int(os.environ["LLM_CONCURRENCY"])
    if os.environ.get("GEMINI_MODEL"):
        cfg["gemini"]["model"] = os.environ["GEMINI_MODEL"]
    if os.environ.get("USAI_BASE_URL") or os.environ.get("GEMINI_BASE_URL"):
        cfg["gemini"]["base_url"] = os.environ.get("USAI_BASE_URL") or os.environ["GEMINI_BASE_URL"]
    if os.environ.get("GEMINI_REASONING"):
        cfg["gemini"]["reasoning"] = os.environ["GEMINI_REASONING"].lower() in ("1", "true", "yes", "on")
    if os.environ.get("GEMINI_THINKING_BUDGET"):
        cfg["gemini"]["thinking_budget"] = int(os.environ["GEMINI_THINKING_BUDGET"])
    if os.environ.get("GEMINI_JUDGE"):
        cfg["gemini"]["judge"] = os.environ["GEMINI_JUDGE"].lower() in ("1", "true", "yes", "on")
    for ev, k in {"VERTEX_PROJECT": "project", "GOOGLE_CLOUD_PROJECT": "project",
                  "VERTEX_LOCATION": "location", "GOOGLE_CLOUD_LOCATION": "location"}.items():
        if os.environ.get(ev):
            cfg["vertex"][k] = os.environ[ev]

    # explicit call args (highest precedence)
    if provider is not None:
        cfg["provider"] = provider
    if concurrency is not None:
        cfg["concurrency"] = concurrency
    if model is not None:
        cfg["gemini"]["model"] = model
    if judge is not None:
        cfg["gemini"]["judge"] = judge

    cfg["regulation"] = agency
    return cfg
