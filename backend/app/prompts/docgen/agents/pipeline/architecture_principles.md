═══════════════════════════════════════════════════════════
ARCHITECTURE PRINCIPLES — APPLY TO EVERY TSD SECTION
═══════════════════════════════════════════════════════════
A TSD is an engineering contract, not prose. Beyond documenting WHAT the
change does, every applicable section below must specify HOW it is built
well enough that an engineer could implement it without guessing. Apply
these principles only where the ratified technical design gives you
something concrete to specify — never invent a mechanism the design does
not describe.

1) MODULARITY & CONTRACTS
   · Name each new/changed component's responsibility and its EXACT
     injection point (the real method/class it hooks into) — never
     describe a generic "service layer."
   · State whether behaviour is config-driven or hardcoded. Prefer and
     call out configuration-driven business rules over embedded logic.
   · Note the contract/interface a component depends on, not a concrete
     implementation it directly instantiates, when the design specifies one.

2) CONCURRENCY & MECHANICAL SYMPATHY
   · If the design introduces queues, buffers, or concurrent access, state
     whether they are BOUNDED and what happens when they fill (backpressure,
     reject, block) — never leave an unbounded structure unstated.
   · Name the concurrency model the design uses (e.g. actor/pull-based,
     thread-pool, single-writer) and whether shared mutable state exists.
   · Prefer describing non-blocking, lock-free mechanisms (CAS, pull-based
     consumption) over locks/sleeps when the design specifies them.

3) AUTOSCALING & STATELESSNESS
   · State whether the component is stateless or holds local state, and if
     stateful, where that state lives and how it survives an instance
     restart or scale-in.
   · State the idempotency behaviour for any operation that can be retried
     (request keys, dedup window) so duplicate delivery under autoscaling
     jitter cannot double-apply a side effect.
   · If the design names a scaling signal (queue depth, latency, error
     rate), state it; do not default to CPU/memory-only scaling without
     comment.

4) INFRASTRUCTURE-AGNOSTIC CONFIGURATION
   · Runtime tunables (thread/pool counts, timeouts, memory limits) must be
     sourced from configuration, not hardcoded — say so explicitly and name
     the config key when the design defines one.
   · Do not assume a specific host, AZ, or instance shape; if the design is
     silent on this, do not introduce such an assumption.

5) FRAMEWORK-DRIVEN / EXTERNALIZED CONFIGURATION
   · Cross-cutting concerns (auth, messaging, storage, caching, tracing)
     should be described as using the design's shared framework/adapter,
     not a one-off implementation, when the design says so.
   · Every configuration parameter this change introduces must specify its
     default, storage location, and whether it can change WITHOUT a
     redeploy — this is already required by the Configuration section;
     apply it consistently everywhere else a config value is mentioned.

6) COST-AWARE DATA ACCESS
   · Classify each new data structure the design introduces as hot, warm,
     or cold, and name where it lives (in-process memory, cache, durable
     store) — do not describe a data structure without stating its tier.
   · State each structure's TTL/lifecycle and, if the design specifies a
     query pattern, confirm it targets indexed columns and pulls only the
     required fields rather than a broad/generic read.
   · Treat high-throughput or continuously-arriving data as a stream
     (event/queue-based access), not a store to be polled or aggregated
     in place, when the design describes it that way.

7) NON-BLOCKING INTEGRATION & BACKPRESSURE
   · For any new or changed integration (internal or wire-level), state
     whether it is synchronous or asynchronous and why that fits the
     coupling/latency needs the design describes.
   · State the delivery semantics (at-least-once / exactly-once / best
     effort) and how duplicates or out-of-order delivery are handled.
   · If the integration can experience backpressure (a slower consumer,
     a saturated downstream), state the signal and the producer's response
     to it (slow down, buffer, shed) rather than leaving it implicit.

8) FAILURE HANDLING AS A FIRST-CLASS SCENARIO
   · State the fail-open vs fail-closed posture EXACTLY as the design
     ratified it — never invert it, never leave it unstated when the
     design specifies one.
   · Distinguish business failures (rule violations) from technical
     failures (timeouts, dependency errors) using the design's own
     vocabulary; do not blend the two into one generic "error."
   · If the design specifies a circuit breaker, bulkhead, or retry policy,
     name its thresholds/parameters; do not describe resilience mechanisms
     the design does not define.

9) OBSERVABILITY
   · Every new or changed code path must specify what it makes observable:
     the metric(s) emitted (and whether each is a gauge, counter, or
     histogram), the log fields it carries, and any correlation/transaction
     ID it propagates.
   · State what a failure on this path looks like from a monitoring
     dashboard (which signal moves) so the change is diagnosable in
     production, not just in code review.
   · Never leave an exception path unaccounted for — every failure branch
     the design defines must be either logged, metered, or traced.

10) RESOURCE MANAGEMENT
   · For any resource this change opens (connection, file, socket, pool
     entry), state how its lifecycle is bounded (timeout, pool limit,
     explicit release) so it cannot leak.
   · Classify resource-access failures (timeout, exhaustion, connection
     refused) as their own category distinct from business/technical
     decline codes, when the design defines such failures.
   · Note when a resource is accessed over a secure/trusted protocol only;
     flag it explicitly if the design specifies encryption or a trusted
     channel requirement.

CONSISTENCY RULE FOR ALL OF THE ABOVE: only state what the ratified
technical design actually specifies. Where the design is silent on one of
these dimensions, do not fabricate an answer — omit it, or (if the section
allows) label it "Assumption:" per the document's existing anti-hallucination
rules. Applying a principle should never introduce a mechanism, config key,
or identifier that does not exist in the design.
