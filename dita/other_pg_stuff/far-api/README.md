# far-api — Spring AI + Neon FAR retrieval

A minimal Spring Boot app: keyword retrieval over the FAR (in Neon) + Claude
answering grounded in the excerpts, plus graph-neighbour endpoints.

## Prerequisites
- **JDK 21**  (`winget install EclipseAdoptium.Temurin.21.JDK`)
- **Maven**   (`winget install Apache.Maven`) — or generate the wrapper (below)
- VS Code / Cursor with the **Extension Pack for Java** + **Spring Boot Extension Pack**
- Env vars set:
  - `ANTHROPIC_API_KEY`  (from console.anthropic.com)
  - `NEON_PASSWORD`      (your Neon db password)

## Run
```bash
# PowerShell: set env for the session
$env:ANTHROPIC_API_KEY="sk-ant-..."
$env:NEON_PASSWORD="npg_..."

mvn spring-boot:run
```
No Maven installed? After installing the JDK you can still get a wrapper with a
one-time `mvn -N wrapper:wrapper` (needs Maven once), or just use the **Run**
button in the VS Code Spring Boot Dashboard.

## Try it
```
GET http://localhost:8080/ask?q=When must an agency publicize a synopsis?
GET http://localhost:8080/search?q=simplified acquisition threshold&k=5
GET http://localhost:8080/refs?id=FAR_5_203_g
```

## What's wired
- `FarRetriever` — JdbcTemplate; `keywordSearch` (tsvector) + `references` (graph 1-hop).
- `FarRag`       — retrieves excerpts, asks Claude to answer citing far_address.
- `FarController`— `/ask`, `/search`, `/refs`.

## Next steps (in the code as TODOs)
1. **Vector lane** — embed the query with **BAAI/bge-small-en-v1.5** (same model as
   ingest!) and run `embedding <=> ?::vector`; fuse with keyword via RRF.
2. **Graph context** — feed `references()` neighbours into the prompt as
   "referenced authority".

## Version note
If the build complains about versions, bump `spring-boot-starter-parent` and
`spring-ai.version` in `pom.xml` to the current stable pair (what start.spring.io
would pick).
