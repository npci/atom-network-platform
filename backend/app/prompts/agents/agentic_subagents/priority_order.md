

GOVERNING PRIORITY ORDER (SDLC review gaps 1/2/3/6 — closes "no priority ordering governs conflict resolution"). When two of the directives above genuinely conflict (e.g. completeness vs. smallest-change, or a performance shortcut vs. a security check), resolve the conflict using this order, HIGHEST first:
  1. MODULARITY — layering, contracts, and module boundaries. Do not leak a concern (validation, persistence, security) into a layer that does not own it, even to save a line of code.
  2. SECURITY — auth/authz checks, input validation, secret handling, trust boundaries. Never skip or weaken a security check to make a change smaller or faster.
  3. THROUGHPUT — non-blocking I/O, bounded concurrency, connection/resource reuse, avoiding N+1 access patterns.
  4. OBSERVABILITY — structured logging, correlation IDs, metrics/telemetry for the paths you touch.
  5. REMAINING — everything else (style, minor efficiency, cosmetic naming).
A directive lower in this order may be sacrificed to satisfy one higher in it — but ONLY with an explicit one-line trade-off note in your final summary naming which principle you deprioritized and why. Never silently drop a higher principle to satisfy a lower one.
