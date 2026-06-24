#!/usr/bin/env python3
"""Stage 2: blind LLM audit + optional LLM judge (USAi.gov via REST / stdlib urllib).

audit() — per unit, the model lists every reference to another part of the regulation,
          one `target` each (ranges as one normalized span), with quoted evidence.
judge() — optional: per unit, the model sees the source + parser-vs-LLM discrepancies and
          recommends a resolution + rationale for each (pre-fills the human review page).
Both cache by (model, prompt version, payload hash). No SDK (Python 3.14 safe).

USAi.gov exposes an OpenAI-compatible Chat Completions API
(`<base_url>/api/v1/chat/completions`, `Authorization: Bearer <key>`). It does NOT
support server-side structured output / response schemas, so we fold the JSON schema
into the prompt and parse the returned JSON ourselves (see _extract_json).
"""
import os, json, time, re, hashlib, urllib.request, urllib.error

PROMPT_VERSION = "v6"
# USAi is OpenAI-compatible; base_url is agency-specific (https://<agency>.usai.gov).
ENDPOINT_PATH = "/api/v1/chat/completions"

# ---------- blind audit ----------
AUDIT_SYSTEM = (
    "You audit cross-references in a government regulation. You are given the COMPLETE raw DITA "
    "XML of ONE unit of {regulation} (its citation is {citation}). Find EVERY reference it makes "
    "to another part / subpart / section / subsection / paragraph of {regulation} (this same "
    "regulation), in any of these forms:\n"
    "1. Explicit link: <xref href=\"...\">...</xref> -- the href names the target.\n"
    "2. Link + a parenthetical, e.g. <xref ...>5.202</xref>(a)(2). USUALLY the parenthetical "
    "narrows the link (-> 5.202(a)(2)) -- but DO NOT assume it; read the sentence. Sometimes it "
    "attaches to THIS section, e.g. 'the authority of 5.202 and (a)(2) of this section' means BOTH "
    "5.202 and {citation}(a)(2). Resolve each to what the text actually means.\n"
    "3. PROSE REFERENCES WITH NO <xref> LINK -- a citation written in plain text, not wrapped in a "
    "tag: 'as required by 5.207', 'see 6.302', 'under subpart 9.4', 'paragraph (b) of this section' "
    "(resolve 'this section/paragraph' against {citation}). PAY SPECIAL ATTENTION to these: automated "
    "XML-tag scanning already catches every <xref>, so references with NO tag are exactly what it "
    "misses -- they are the most valuable for you to surface. Scan the prose carefully for them.\n"
    "4. Ranges, in any phrasing -- '(a) through (f)', '1 to 3', '(a)-(d)', '52.219-3 through "
    "52.219-5'. EXPAND every range into its individual members and return ONE reference per member "
    "(do NOT emit a span like '5.203(a)-(d)'). E.g. '5.203(a) through (d)' -> four references: "
    "5.203(a), 5.203(b), 5.203(c), 5.203(d). Give each member the SAME `evidence` (the range "
    "sentence). Only expand when the members are unambiguous; if you cannot tell the sequence, "
    "report the endpoints you are sure of.\n"
    "Exclude external statutes (U.S.C., or CFR titles other than this regulation), URLs, and DITA "
    "plumbing. For each reference return one `target` citation in standard form (e.g. 5.202, "
    "5.202(a)(2), 6.302-2, subpart 9.4) and, as `evidence`, the COMPLETE sentence(s) containing the "
    "reference, quoted VERBATIM from the source, with the exact citation text that triggers this "
    "reference wrapped in « » guillemets -- e.g. 'The contracting officer shall, as required by "
    "«5.207», publicize the action.' Quote enough surrounding text to judge the reference; do not "
    "paraphrase or shorten."
)
AUDIT_SCHEMA = {
    "type": "array",
    "items": {"type": "object",
              "properties": {"target": {"type": "string"}, "evidence": {"type": "string"}},
              "required": ["target", "evidence"]},
}

# ---------- judge ----------
JUDGE_SYSTEM = (
    "You reconcile cross-reference disagreements for {regulation} unit {citation}. You are given the "
    "raw DITA XML and a numbered list of DISAGREEMENTS — each is a SINGLE atomic citation that EITHER "
    "the deterministic parser found (from prose/an expanded range, not a tagged link) OR an "
    "independent LLM found, but not both. For EACH, read the source and decide whether it is a real, "
    "correct cross-reference FROM this unit TO that target within {regulation}: choose 'accept' (the "
    "citation is correct as written), 'manual' (a real reference but the citation is wrong — put the "
    "correct citation(s) in `value`), or 'reject' (not a real reference to this regulation — external, "
    "mis-parsed, or hallucinated). Give a one-sentence `rationale`. Return one object per disagreement "
    "with its `n`."
)
JUDGE_SCHEMA = {
    "type": "array",
    "items": {"type": "object",
              "properties": {"n": {"type": "integer"},
                             "choice": {"type": "string", "enum": ["accept", "manual", "reject"]},
                             "value": {"type": "array", "items": {"type": "string"}},
                             "rationale": {"type": "string"}},
              "required": ["n", "choice", "rationale"]},
}

def _judge_user_text(unit_cit, discrepancies):
    """Render the disagreement list for the judge. discrepancies: [{n, target, source, evidence}]."""
    lines = [f"Unit {unit_cit}. Disagreements to resolve:"]
    for d in discrepancies:
        lines.append(f"  [{d['n']}] (found by {d['source']}) target={d['target']} | "
                     f"evidence: {d.get('evidence', '')[:300]}")
    return "\n".join(lines)

# ---------- REST (USAi.gov, OpenAI-compatible) ----------
def _schema_instruction(schema):
    """USAi has no server-side response schema, so we ask for the JSON shape in-prompt."""
    return ("\n\nReturn ONLY a JSON value that conforms to this JSON Schema — no prose, no "
            "explanation, no markdown code fences:\n" + json.dumps(schema))

def _extract_json(text):
    """Parse the model's JSON, tolerating ```json fences or stray prose around it."""
    s = text.strip()
    if s.startswith("```"):                              # strip ```json … ``` fences
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"(\[.*\]|\{.*\})", s, re.S)        # first array/object in the text
        if m:
            return json.loads(m.group(1))
        raise

def _body(system_text, user_text, schema, cfg):
    # Fold the schema into the user turn since USAi can't enforce it server-side.
    return {"model": cfg["gemini"]["model"],
            "temperature": 0,
            "messages": [{"role": "system", "content": system_text},
                         {"role": "user", "content": user_text + _schema_instruction(schema)}]}

def _base_url(cfg):
    url = (cfg.get("gemini", {}).get("base_url") or os.environ.get("USAI_BASE_URL")
           or os.environ.get("GEMINI_BASE_URL") or "").rstrip("/")
    if not url:
        raise RuntimeError("USAi base URL not set — set USAI_BASE_URL (your agency endpoint, "
                           "e.g. https://<agency>.usai.gov) in .env or gemini.base_url in config")
    return url

def _call(system_text, user_text, schema, cfg, retries=4):
    key = os.environ.get("USAI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("USAI_API_KEY (or GEMINI_API_KEY) not set in environment")
    url = _base_url(cfg) + ENDPOINT_PATH
    body = json.dumps(_body(system_text, user_text, schema, cfg)).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            content = data["choices"][0]["message"]["content"]
            return _extract_json(content)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)            # backoff for rate limit / transient
                continue
            body = e.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"USAi HTTP {e.code}: {body}") from None
    return []

def _cached(cache_dir, name, h, fn):
    os.makedirs(cache_dir, exist_ok=True)
    p = os.path.join(cache_dir, name + ".json")
    if os.path.exists(p):
        c = json.load(open(p, encoding="utf-8"))
        if c.get("hash") == h:
            return c["result"]
    result = fn()
    json.dump({"hash": h, "result": result}, open(p, "w", encoding="utf-8"))
    return result

# ---------- stages ----------
def audit(units, cfg, cache_dir, progress=True):
    """units: list of (citation, raw_dita). Returns {citation: [ {target, evidence} ]}."""
    out = {}
    for i, (cit, text) in enumerate(units, 1):
        h = hashlib.sha1(f"{cfg['gemini']['model']}|{PROMPT_VERSION}|audit|{text}".encode()).hexdigest()[:16]
        sys_t = AUDIT_SYSTEM.format(regulation=cfg["regulation"], citation=cit)
        out[cit] = _cached(cache_dir, cit.replace("/", "_"), h,
                           lambda t=text, s=sys_t: _call(s, t, AUDIT_SCHEMA, cfg))
        if progress and i % 25 == 0:
            print(f"    audited {i}/{len(units)}")
    return out

def judge(unit_cit, raw_dita, discrepancies, cfg, cache_dir):
    """discrepancies: [{n, target, source, evidence}]. Returns {n: {choice, value, rationale}}."""
    if not discrepancies:
        return {}
    user = raw_dita + "\n\n" + _judge_user_text(unit_cit, discrepancies)
    h = hashlib.sha1(f"{cfg['gemini']['model']}|{PROMPT_VERSION}|judge|{user}".encode()).hexdigest()[:16]
    sys_t = JUDGE_SYSTEM.format(regulation=cfg["regulation"], citation=unit_cit)
    recs = _cached(cache_dir, "judge_" + unit_cit.replace("/", "_"), h,
                   lambda: _call(sys_t, user, JUDGE_SCHEMA, cfg))
    return {r["n"]: r for r in recs if "n" in r}
