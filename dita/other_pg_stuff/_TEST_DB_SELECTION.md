# Test DB selection — FAR Subpart 5.2 cluster ("Synopses of Proposed Contract Actions")

A self-contained, intuitive cluster of 6 core units (+2 optional) chosen because the
cross-references form a closed, multi-hop web with the exact ambiguity and range
patterns we want to test chunking/retrieval against.

## Units

### Core (6) — upload these
| File | `id` | Title |
|------|------|-------|
| `5.201.dita` | `FAR_5_201` | General |
| `5.202.dita` | `FAR_5_202` | Exceptions |
| `5.203.dita` | `FAR_5_203` | Publicizing and response time |
| `5.205.dita` | `FAR_5_205` | Special situations |
| `5.207.dita` | `FAR_5_207` | Preparation and transmittal of synopses |
| `12.603.dita` | `FAR_12_603` | Streamlined solicitation for commercial products/services |

### Optional (2) — add to make two dangling ambiguous hops resolve to real text
| File | `id` | Title | Why |
|------|------|-------|-----|
| `5.101.dita` | `FAR_5_101` | Methods of disseminating information | targets of `5.101(a)(1)`, `5.101(a)(2)`, `5.101(b)` |
| `6.302-2.dita` | `FAR_6_302_2` | Unusual and compelling urgency | target of the `5.202(a)(2)` exception chain |

## Internal reference graph (edges that stay inside the core set)

```
12.603 ──► 5.203 ──► 5.201 ──► 5.202 ──► 5.203   (cycle, ≥3 hops)
   │          │         │         └────► 5.205
   │          │         └────► 5.205
   │          ├────► 5.202 (a)(2)
   │          └────► 6.302  (out, optional)
   └────► 5.207
5.205 ──► 5.201, 5.202
5.202 ──► 5.205, 5.207, 2.101, 6.302-1/-2/-3/-5/-7, 16.505, Subpart 25.4
5.207 ──► (mostly clause refs 52.225-x, subparts)
```

Every core file both **references** and **is referenced by** another core file, so any
chunking that breaks a link is observable.

## Requirement coverage

### (1)(2) Multi-hop chains ≥3 deep (all inside the set)
- `12.603(a)` → `5.203` → `5.201` → `5.202`
- `5.203(g)` → `5.202(a)(2)` → `5.203` (and `5.202` → `5.201`)
- `5.205(a)` → `5.201` → `5.202`

### (3) Explicit references in the XML (`<xref>` tags) — present throughout, e.g.
- `5.203.dita:11` → `<xref href="5.201.dita#FAR_5_201">5.201</xref>`
- `12.603.dita:20` → `<xref href="5.203.dita#FAR_5_203">5.203</xref>`

### (3.i / 4) Ambiguous links — `href` points to the section, visible text points deeper
| Where | XML | Link target | Text means |
|-------|-----|-------------|------------|
| `5.203.dita:72` | `<xref href="#FAR_5_203">5.203</xref>(b)` | 5.203 | 5.203**(b)** |
| `5.203.dita:119` | `<xref href="5.202.dita#FAR_5_202">5.202</xref>(a)(2)` | 5.202 | 5.202**(a)(2)** |
| `5.201.dita:45` | `<xref href="5.101.dita#FAR_5_101">5.101</xref>(a)(1)` | 5.101 | 5.101**(a)(1)** |
| `5.202.dita:83` | `<xref href="16.505.dita#FAR_16_505">16.505</xref>(a)(4)` | 16.505 | 16.505**(a)(4)** |
| `5.202.dita:111` | `<xref href="6.302-1.dita#FAR_6_302_1">6.302-1</xref>(a)(2)(i)` | 6.302-1 | 6.302-1**(a)(2)(i)** (3 levels) |
| `5.202.dita:63` | `<xref href="5.205.dita#FAR_5_205">5.205</xref>(f)` | 5.205 | 5.205**(f)** |
| `5.205.dita:88` | `<xref href="5.101.dita#FAR_5_101">5.101</xref>(a)(2)` | 5.101 | 5.101**(a)(2)** |
| `5.205.dita:190` | `<xref href="7.107-5.dita#FAR_7_107_5">7.107-5</xref>(c) and (d)` | 7.107-5 | 7.107-5**(c)+(d)** |
| `12.603.dita:205` | `…>5.203</xref>(b) (but see …>5.203</xref>(h))` | 5.203 | 5.203**(b)** and 5.203**(h)** |

### (2.ii / 4) Range references (prose, no tag — must be resolved by text parsing)
- `5.203.dita:114` — "specified in paragraphs **(a) through (d)** of this section"
- `5.201.dita:20` — "as specified in **paragraph (b)** of this section"
- `5.205.dita:91` — "not required to be synopsized under **paragraph (d)(1)** of this section"

## Sample questions (multi-hop retrieval)

1. **"How many days before issuing a solicitation must an agency publish the notice,
   and can that be shortened for commercial items?"**
   → `5.203(a)` (15 days) → exception routes to `12.603` (combined synopsis/solicitation)
   and `5.203(a)(1)`.

2. **"A notice wasn't actually published on time. Which response-time rules still apply,
   and what authority lets the CO proceed anyway?"**
   → `5.203(g)` ("paragraphs (a) through (d)" still mandatory) → `5.202(a)(2)`
   → `6.302-2` (unusual and compelling urgency). *Tests the range + the ambiguous
   `5.202(a)(2)` link both resolving.*

3. **"When do I NOT have to synopsize at all, and which of those exceptions ties back to
   the urgency justification?"**
   → `5.202` (list of exceptions) → `5.202(a)(2)` → `6.302-2`.

4. **"For a sole-source 8(a) competitive acquisition, what must the synopsis say, and
   where is that exception cross-referenced?"**
   → `5.202(4)` cites `5.205(f)` → `5.205(f)` (8(a) synopsis contents).
   *Tests the ambiguous `5.205(f)` link.*

5. **"What response time applies to a combined synopsis/solicitation for a commercial
   item?"**
   → `12.603(c)(3)(ii)` → `5.203(b)` (but see `5.203(h)`). *Two ambiguous same-target
   links in one sentence.*

6. **"What's the minimum response time for an R&D acquisition above the SAT, and does
   advance-notice R&D still need a synopsis?"**
   → `5.203(e)` (45 days) + `5.205(a)` → `5.201` unless `5.202` exception applies.
