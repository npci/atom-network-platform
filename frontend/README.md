# Frontend

The operator console for the ATOM change-management platform: a React 19 + Vite
single-page app that talks to the FastAPI backend under `../backend`.

It is the surface for the whole change lifecycle — Phase A (idea → BRD → tech
spec → XSD), Phase B (build and review), Phase C (partner collaboration), and
the certification and admin screens.

## Running it

```bash
npm ci
npm run dev
```

`npm run dev` serves on port 3000 and proxies API and WebSocket traffic to
`http://localhost:8000`, which is where `backend/run_dev.sh` listens. Point it
somewhere else with `BACKEND_ORIGIN` — the dockerised backend publishes on
18000, not 8000:

```bash
BACKEND_ORIGIN=http://localhost:18000 npm run dev
```

`./run_dev.sh` is the same thing bound to `0.0.0.0`, for reaching the dev server
from another machine.

Other scripts: `npm run build` (production bundle into `dist/`), `npm run
preview` (serve that bundle), `npm run lint`.

## The `/a2a/` base path

The app does **not** live at the origin root. The router has
`basename="/a2a"`, and production nginx redirects `/` to `/${AUTHORITY_CONTEXT}/`
— so a bare `/` renders a blank page: React mounts, the router matches no route,
and nothing is drawn. The dev server reproduces this deliberately (see
`redirectRootToA2a` in `vite.config.js`) rather than papering over it, so a path
bug shows up in development instead of at deploy time.

Browse `http://localhost:3000/a2a/`.

## Branding and labels

Nothing about the deployment's identity is hardcoded. Three build-time hooks:

| Variable | Effect | Default |
|---|---|---|
| `VITE_BRAND_NAME` | Tab title and the name rendered in the chrome | `AtOM` |
| `VITE_BRAND_LOGO_URL` | Overrides both bundled logo variants | bundled PNGs |
| `VITE_BRAND_FAVICON_URL` | Overrides the tab icon | `public/favicon.svg` |

See `src/brand.js`. The bundled favicon is deliberately wordmark-free so it
carries no organisation's name.

Interface copy is externalised the same way. `src/strings.js` holds neutral
defaults; a deployment supplies its own wording as a JSON object at build time:

```bash
VITE_LABEL_OVERRIDES='{"nav.policy":"Authority Policy"}' npm run build
```

Overrides are applied at BUILD time, not runtime, on purpose: `/api/config/ui`
resolves after first paint, so a runtime label would flash the neutral default
and then swap — which looks like a bug.

Domain vocabulary that varies per deployment (the names of the certification
sides, the partner roles, the cert role keys) is *not* set here. It comes from
the active domain pack over `/api/config/ui` — see `src/hooks/useUiConfig.js`
and `src/lib/partnerTypes.js`. Do not add a second copy of any of it to a
component.

## Deploying: the bundle is built, not mounted

**A frontend change is not live until the image is rebuilt.** Backends can be
bind-mounted; this cannot — it is a build-time bundle, so an edit under `src/`
or to an nginx template needs:

```bash
docker compose build frontend && docker compose up -d frontend
```

Skipping this is the single most common way to spend an afternoon debugging
source that is already correct while a stale artefact runs. See
`DEPLOYMENT_GUIDE.md` §11.4, which documents real occurrences of exactly this.

`Dockerfile.prod` is the air-gapped variant: it pulls every package from
`${REGISTRY}` and needs no internet access.

## Tests

There are none. `npm run build` is the gate — it fails on unresolved imports and
syntax errors. It does not catch a wrong-but-valid expression, so re-read your
own diff.
