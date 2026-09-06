// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { t } from '../strings'
// Streaming dummy Maven build + deploy log generator for the demo flow on
// Phase B's Build + Deploy panel. Mounted only when the URL has `?demo=1`
// (or `localStorage.demoBuild === '1'`, or `window.__DEMO_MODE__ === true`).
//
// Schedule pacing — totals ~30s with bursty cadence so the audience sees
// distinct phases (deps download → compile → tests → package → deploy):
//
//   0–2 s     scan + module list           5 lines
//   2–6 s     dependency download           ~16 lines (fast burst)
//   6–12 s    compilation                   ~9 lines
//   12–22 s   tests                         ~24 lines (one class at a time)
//   22–26 s   package                       ~6 lines
//   26–28 s   Maven reactor summary         ~7 lines
//   28–30 s   deploy + start services       ~9 lines
//
// Lines are static strings — no template values from the change request so
// the demo plays back identically every time.

// The demo stack profile. These are the ONLY domain-bearing parts of the
// transcript — the shape (Maven reactor -> download -> compile -> test ->
// package -> scp -> systemctl) is generic and is what the demo is actually
// showing. Resolved once at module load, so the header's invariant holds: the
// values are build-time constants and the demo still plays back identically
// every time.
//
// Column alignment in the reactor/summary lines was hand-padded for the network
// names; a profile whose names differ in length will sit slightly ragged. That
// is cosmetic and only visible in demo mode.
const APP_DESC     = t('demo.stack.appDesc')
const CORE_DESC    = t('demo.stack.coreDesc')
const GROUP        = t('demo.stack.groupId')
// The same coordinate in Maven PATH form; the dotted token does not match it.
const GROUP_PATH   = t('demo.stack.groupPath')
const REGISTRY     = t('demo.stack.registry')
const APP          = t('demo.stack.appModule')
const DEP_PROTOCOL = t('demo.stack.depProtocol')
const DEP_COMMON   = t('demo.stack.depCommon')
const APP_DIR      = t('demo.stack.appDir')
const APP_UNIT     = t('demo.stack.appUnit')
const CORE         = t('demo.stack.coreModule')
const REACTOR      = t('demo.stack.reactor')

/** @typedef {{ at: number, text: string }} DemoLine */

/** @type {DemoLine[]} */
const DEMO_LINES = [
  // 0–2s — scan + reactor build order
  { at:    50, text: '[INFO] Scanning for projects...' },
  { at:   320, text: '[INFO] ------------------------------------------------------------------------' },
  { at:   380, text: '[INFO] Reactor Build Order:' },
  { at:   440, text: '[INFO]' },
  { at:   500, text: `[INFO] ${CORE}                                                          [jar]` },
  { at:   560, text: `[INFO] ${APP}                                                       [jar]` },
  { at:   620, text: `[INFO] ${REACTOR}                                                         [pom]` },
  { at:   680, text: '[INFO]' },
  { at:   750, text: `[INFO] ---------------------< ${GROUP}:${CORE} >----------------------` },
  { at:   810, text: `[INFO] Building ${CORE} 2.4.7-SNAPSHOT                                  [1/3]` },
  { at:   870, text: `[INFO]   from ${CORE}/pom.xml` },
  { at:   930, text: '[INFO] --------------------------------[ jar ]---------------------------------' },

  // 2–6s — dependency downloads
  { at:  1450, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/org/springframework/boot/spring-boot-starter-web/3.2.0/spring-boot-starter-web-3.2.0.pom` },
  { at:  1820, text: `[INFO] Downloaded from ${REGISTRY}: spring-boot-starter-web-3.2.0.pom (4.7 kB at 23 kB/s)` },
  { at:  2080, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/org/springframework/boot/spring-boot-starter-data-jpa/3.2.0/spring-boot-starter-data-jpa-3.2.0.pom` },
  { at:  2320, text: `[INFO] Downloaded from ${REGISTRY}: spring-boot-starter-data-jpa-3.2.0.pom (4.1 kB at 28 kB/s)` },
  { at:  2540, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/org/springframework/boot/spring-boot-starter-validation/3.2.0/spring-boot-starter-validation-3.2.0.jar` },
  { at:  2820, text: `[INFO] Downloaded from ${REGISTRY}: spring-boot-starter-validation-3.2.0.jar (3.7 kB at 21 kB/s)` },
  { at:  3120, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/org/postgresql/postgresql/42.7.1/postgresql-42.7.1.jar` },
  { at:  3540, text: `[INFO] Downloaded from ${REGISTRY}: postgresql-42.7.1.jar (1.0 MB at 1.9 MB/s)` },
  { at:  3760, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/io/jsonwebtoken/jjwt-impl/0.12.5/jjwt-impl-0.12.5.jar` },
  { at:  3960, text: `[INFO] Downloaded from ${REGISTRY}: jjwt-impl-0.12.5.jar (180 kB at 880 kB/s)` },
  { at:  4180, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/${GROUP_PATH}/${DEP_COMMON}/1.8.3/${DEP_COMMON}-1.8.3.jar` },
  { at:  4520, text: `[INFO] Downloaded from ${REGISTRY}: ${DEP_COMMON}-1.8.3.jar (412 kB at 1.2 MB/s)` },
  { at:  4860, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/${GROUP_PATH}/${DEP_PROTOCOL}/3.1.0/${DEP_PROTOCOL}-3.1.0.jar` },
  { at:  5240, text: `[INFO] Downloaded from ${REGISTRY}: ${DEP_PROTOCOL}-3.1.0.jar (788 kB at 2.1 MB/s)` },
  { at:  5520, text: `[INFO] Downloading from ${REGISTRY}: https://nexus.example.internal/repository/maven-public/com/hazelcast/hazelcast/5.3.6/hazelcast-5.3.6.jar` },
  { at:  5980, text: `[INFO] Downloaded from ${REGISTRY}: hazelcast-5.3.6.jar (12.4 MB at 27 MB/s)` },

  // 6–12s — compilation
  { at:  6280, text: '[INFO]' },
  { at:  6420, text: `[INFO] --- maven-resources-plugin:3.3.1:resources (default-resources) @ ${CORE} ---` },
  { at:  6680, text: '[INFO] Copying 14 resources from src/main/resources to target/classes' },
  { at:  7080, text: '[INFO]' },
  { at:  7220, text: `[INFO] --- maven-compiler-plugin:3.12.1:compile (default-compile) @ ${CORE} ---` },
  { at:  7480, text: '[INFO] Changes detected — recompiling the module! :source' },
  { at:  7920, text: '[INFO] Compiling 184 source files with javac [debug release 17] to target/classes' },
  { at:  9420, text: '[INFO] Compiled 184 source files in 1.49 s' },
  { at:  9680, text: '[INFO]' },
  { at:  9820, text: `[INFO] --- maven-resources-plugin:3.3.1:testResources (default-testResources) @ ${CORE} ---` },
  { at: 10080, text: '[INFO] Copying 6 resources from src/test/resources to target/test-classes' },
  { at: 10380, text: '[INFO]' },
  { at: 10520, text: `[INFO] --- maven-compiler-plugin:3.12.1:testCompile (default-testCompile) @ ${CORE} ---` },
  { at: 10780, text: '[INFO] Compiling 47 source files with javac [debug release 17] to target/test-classes' },
  { at: 11820, text: '[INFO] Compiled 47 source files in 1.04 s' },

  // 12–22s — tests
  { at: 12080, text: '[INFO]' },
  { at: 12220, text: `[INFO] --- maven-surefire-plugin:3.2.5:test (default-test) @ ${CORE} ---` },
  { at: 12340, text: '[INFO] Using auto detected provider org.apache.maven.surefire.junitplatform.JUnitPlatformProvider' },
  { at: 12480, text: '[INFO]' },
  { at: 12600, text: '[INFO] -------------------------------------------------------' },
  { at: 12720, text: '[INFO]  T E S T S' },
  { at: 12840, text: '[INFO] -------------------------------------------------------' },
  { at: 13180, text: `[INFO] Running ${GROUP}.core.escrow.model.EscrowStateMachineTest` },
  { at: 13680, text: `[INFO] Tests run: 9, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.487 s -- in ${GROUP}.core.escrow.model.EscrowStateMachineTest` },
  { at: 13920, text: `[INFO] Running ${GROUP}.core.escrow.service.HoldRefGeneratorTest` },
  { at: 14460, text: `[INFO] Tests run: 6, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.521 s -- in ${GROUP}.core.escrow.service.HoldRefGeneratorTest` },
  { at: 14760, text: `[INFO] Running ${GROUP}.core.escrow.service.ConditionalPaymentServiceTest` },
  { at: 15480, text: `[INFO] Tests run: 14, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.704 s -- in ${GROUP}.core.escrow.service.ConditionalPaymentServiceTest` },
  { at: 15780, text: `[INFO] Running ${GROUP}.core.attester.AttesterRegistryTest` },
  { at: 16380, text: `[INFO] Tests run: 8, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.593 s -- in ${GROUP}.core.attester.AttesterRegistryTest` },
  { at: 16660, text: `[INFO] Running ${GROUP}.core.dispute.DisputeFreezeHandlerTest` },
  { at: 17280, text: `[INFO] Tests run: 7, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.610 s -- in ${GROUP}.core.dispute.DisputeFreezeHandlerTest` },
  { at: 17560, text: `[INFO] Running ${GROUP}.core.config.HazelcastClusterConfigTest` },
  { at: 18140, text: `[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.561 s -- in ${GROUP}.core.config.HazelcastClusterConfigTest` },
  { at: 18420, text: `[INFO] Running ${GROUP}.core.security.JwtAuthFilterTest` },
  { at: 19140, text: `[INFO] Tests run: 11, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.708 s -- in ${GROUP}.core.security.JwtAuthFilterTest` },
  { at: 19420, text: `[INFO] Running ${GROUP}.core.integration.ConditionalPaymentControllerIT` },
  { at: 20520, text: `[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1.082 s -- in ${GROUP}.core.integration.ConditionalPaymentControllerIT` },
  { at: 20780, text: '[INFO]' },
  { at: 20880, text: '[INFO] Results:' },
  { at: 20960, text: '[INFO]' },
  { at: 21080, text: '[INFO] Tests run: 71, Failures: 0, Errors: 0, Skipped: 0' },

  // 22–26s — package
  { at: 21680, text: '[INFO]' },
  { at: 21820, text: `[INFO] --- maven-jar-plugin:3.3.0:jar (default-jar) @ ${CORE} ---` },
  { at: 22240, text: `[INFO] Building jar: target/${CORE}-2.4.7-SNAPSHOT.jar` },
  { at: 22660, text: '[INFO]' },
  { at: 22800, text: `[INFO] --- spring-boot-maven-plugin:3.2.0:repackage (repackage) @ ${CORE} ---` },
  { at: 23420, text: `[INFO] Replacing main artifact target/${CORE}-2.4.7-SNAPSHOT.jar with repackaged archive (78.4 MB), original copy at target/${CORE}-2.4.7-SNAPSHOT.jar.original` },
  { at: 23840, text: '[INFO]' },
  { at: 24080, text: `[INFO] --------------------< ${GROUP}:${APP} >--------------------` },
  { at: 24220, text: `[INFO] Building ${APP} 4.1.2-SNAPSHOT                                  [2/3]` },
  { at: 25080, text: '[INFO] Compiled 312 source files in 1.94 s' },
  { at: 25640, text: '[INFO] Tests run: 96, Failures: 0, Errors: 0, Skipped: 0' },
  { at: 26020, text: `[INFO] Building jar: target/${APP}-4.1.2-SNAPSHOT.jar` },
  { at: 26380, text: `[INFO] Replacing main artifact target/${APP}-4.1.2-SNAPSHOT.jar with repackaged archive (91.1 MB)` },

  // 26–28s — reactor summary
  { at: 26680, text: '[INFO] ------------------------------------------------------------------------' },
  { at: 26780, text: `[INFO] Reactor Summary for ${REACTOR} 1.0.0:` },
  { at: 26880, text: '[INFO]' },
  { at: 26980, text: `[INFO] ${CORE} ........................................... SUCCESS [ 16.142 s]` },
  { at: 27080, text: `[INFO] ${APP} ........................................ SUCCESS [ 10.418 s]` },
  { at: 27180, text: `[INFO] ${REACTOR} .......................................... SUCCESS [  0.214 s]` },
  { at: 27280, text: '[INFO] ------------------------------------------------------------------------' },
  { at: 27360, text: '[INFO] BUILD SUCCESS' },
  { at: 27440, text: '[INFO] ------------------------------------------------------------------------' },
  { at: 27520, text: '[INFO] Total time:  26.774 s' },
  { at: 27600, text: '[INFO] Finished at: ' },

  // 28–30s — deploy + service start
  { at: 28000, text: '' },
  { at: 28080, text: '── Deploy ──────────────────────────────────────────────────────────────' },
  { at: 28220, text: `$ scp target/${CORE}-2.4.7-SNAPSHOT.jar deploy@app-prod-01.example.internal:/opt/${CORE}/releases/` },
  { at: 28680, text: `${CORE}-2.4.7-SNAPSHOT.jar                                   100%   78MB  86.2MB/s   00:00` },
  { at: 28840, text: `$ scp target/${APP}-4.1.2-SNAPSHOT.jar deploy@app-prod-01.example.internal:/opt/${APP_DIR}/releases/` },
  { at: 29260, text: `${APP}-4.1.2-SNAPSHOT.jar                                100%   91MB  82.7MB/s   00:01` },
  { at: 29380, text: `$ ssh deploy@app-prod-01.example.internal "ln -sf /opt/${CORE}/releases/${CORE}-2.4.7-SNAPSHOT.jar /opt/${CORE}/current.jar && systemctl restart ${CORE}"` },
  { at: 29560, text: `● ${CORE}.service - ${CORE_DESC}` },
  { at: 29640, text: '   Active: active (running) since ' + new Date().toUTCString().slice(5, 25) + '; 0s ago' },
  { at: 29720, text: '   Main PID: 18472 (java)' },
  { at: 29800, text: `$ ssh deploy@app-prod-01.example.internal "ln -sf /opt/${APP_DIR}/releases/${APP}-4.1.2-SNAPSHOT.jar /opt/${APP_DIR}/current.jar && systemctl restart ${APP_UNIT}"` },
  { at: 29880, text: `● ${APP_UNIT}.service - ${APP_DESC}` },
  { at: 29940, text: '   Active: active (running) since ' + new Date().toUTCString().slice(5, 25) + '; 0s ago' },
  { at: 29980, text: '   Main PID: 18519 (java)' },
  { at: 30000, text: '── Deploy complete. All services healthy. ──' },
]

/** Total wall-clock duration of the canned demo, milliseconds. */
export const DEMO_DURATION_MS = 30_500

/**
 * Run the canned demo. Calls `onLine(text)` for each line at its scheduled
 * offset. Returns a Promise that resolves once the final line has fired.
 * Aborts cleanly if the caller signals via `signal` (an AbortSignal).
 *
 * The schedule is wall-clock; the loop sleeps until the next line's `at`
 * offset rather than between lines, so jitter from setTimeout doesn't
 * accumulate.
 */
export async function streamDemoBuildLogs({ onLine, signal } = {}) {
  const start = Date.now()
  for (const entry of DEMO_LINES) {
    if (signal?.aborted) return
    const dueAt = start + entry.at
    const wait  = Math.max(0, dueAt - Date.now())
    if (wait > 0) {
      await new Promise(resolve => {
        const t = setTimeout(resolve, wait)
        signal?.addEventListener('abort', () => { clearTimeout(t); resolve() }, { once: true })
      })
    }
    if (signal?.aborted) return
    if (typeof onLine === 'function') onLine(entry.text)
  }
}

/**
 * Random lowercase-hex string of `len` characters, from the Web Crypto CSPRNG.
 *
 * These ids only label demo build runs, but Math.random() is a predictable,
 * non-cryptographic PRNG and the idiom tends to get copied into code where that
 * matters. Using crypto.getRandomValues keeps the pattern safe by default.
 */
function randomHex(len) {
  const bytes = new Uint8Array(Math.ceil(len / 2))
  crypto.getRandomValues(bytes)
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('').slice(0, len)
}

/** Build a fake "success" BuildRun payload shaped like the real backend response. */
export function buildDemoResult({ coreBranch, appBranch, allLines }) {
  const completedAt = new Date()
  const triggeredAt = new Date(completedAt.getTime() - DEMO_DURATION_MS)
  const shortId = 'demo' + randomHex(6)
  // The scp progress lines are the only ones starting with an artifact name, and
  // they split build/deploy/startup. Derive the test from the profile: hardcoding
  // 'network-' would silently mis-partition the panes under any other stack profile.
  const isArtifactLine = l => l.startsWith(`${CORE}-`) || l.startsWith(`${APP}-`)
  const buildLog = allLines.filter(l => !l.startsWith('── Deploy') && !l.startsWith('$ ') && !isArtifactLine(l) && !l.startsWith('● ') && !l.startsWith('   ')).join('\n')
  const deployLog = allLines.filter(l => l.startsWith('$ ') || isArtifactLine(l) || l.startsWith('── Deploy complete')).join('\n')
  const startupLog = allLines.filter(l => l.startsWith('● ') || l.startsWith('   ')).join('\n')
  return {
    id:            shortId + '-' + randomHex(8),
    status:        'success',
    host:          'app-prod-01.example.internal',
    core_branch:   coreBranch || 'master',
    app_branch:   appBranch || 'master',
    triggered_at:  triggeredAt.toISOString(),
    completed_at:  completedAt.toISOString(),
    deployed_artifacts: [
      { path: `${CORE}/target/${CORE}-2.4.7-SNAPSHOT.jar`,       dest: `/opt/${CORE}/releases/${CORE}-2.4.7-SNAPSHOT.jar` },
      { path: `${APP}/target/${APP}-4.1.2-SNAPSHOT.jar`, dest: `/opt/${APP_DIR}/releases/${APP}-4.1.2-SNAPSHOT.jar` },
    ],
    services_started: [
      { name: `${CORE}.service`, pid: 18472 },
      { name: `${APP_UNIT}.service`,  pid: 18519 },
    ],
    build_log:   buildLog,
    deploy_log:  deployLog,
    startup_log: startupLog,
  }
}

/** True if any of the supported flags is set: ?demo=1 URL param,
 *  localStorage.demoBuild === '1', or window.__DEMO_MODE__ === true. */
export function isDemoBuildMode() {
  if (typeof window === 'undefined') return false
  if (window.__DEMO_MODE__ === true) return true
  try {
    if (new URLSearchParams(window.location.search).get('demo') === '1') return true
    if (window.localStorage?.getItem('demoBuild') === '1') return true
  } catch { /* localStorage in private browsing throws — ignore */ }
  return false
}
