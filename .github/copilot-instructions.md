# Visual Studio / GitHub Copilot Project Instructions

<!-- graphite:managed version=14 -->
## Shared Graphite Instructions

Graphite-first is required in this repo. Follow `GRAPHITE.md` before making non-trivial code changes: for cross-file questions (who-calls, where-defined, impact, data flow, structure) run the Graphite commands first; grep/glob are for literal text and filename lookups only. Fall back to manual search only after a Graphite answer proved insufficient, and say so. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.

**Stay inside this repository.** Do not read, write, or run commands in any other repo, including its graph. Findings about another repo go to its agent as a recommendation through the shared `.agent-channel/` (see its `PROTOCOL.md`); that agent decides and acts. A tool doing its designed job is a separate question from an agent's boundary. See `GRAPHITE.md` section "Repository Isolation".
<!-- graphite:managed-end -->
