# Label Studio + Tapis OAuth2

Adds Tapis single-sign-on login to open-source Label Studio, without editing
Label Studio's own source tree — everything lives in `tapis_auth/` (a new
Django app) plus `settings_additions.py`/`urls_additions.py`, whose contents
the `Dockerfile` appends onto the *end* of Label Studio's own
`core/settings/label_studio.py` and `core/urls.py` at build time. This is a
downstream image built `FROM heartexlabs/label-studio:latest`, not a fork of
Label Studio's repo — picking up a new upstream release is just rebuilding
against the new tag, same as WebODM's own coreplugins pattern of layering
custom code on top of upstream rather than forking it.

## CI/CD

`.github/workflows/docker-build.yml` builds and pushes the image to GHCR
(`ghcr.io/in-for-disaster-analytics/label-studio-tapis-auth`) on every push to
`main` and on version tags — no local `docker push` needed. Uses the
repo-scoped `GITHUB_TOKEN`, so no extra secrets to configure for the build
itself.

**GHCR compatibility: confirmed.** Tapis Pods' own docs only mention "the
public Docker Hub," but this ecosystem already runs production Tapis Pods
from GHCR images — see `subside/tapis/register_pods.py` (`subsideapi`,
`subsideui` pods, images `ghcr.io/<owner>/subside-{api,ui}`). One real
constraint from that precedent, not documented anywhere in Tapis's own docs:
**the GHCR package must be public** — Tapis Pods pulls custom images
anonymously, so a private GHCR repo will fail to pull. This repo is public,
so that's already satisfied.

## How it differs from WebODM's existing Tapis backend

`app/auth/tapis_oauth2.py` in WebODM decodes the JWT payload directly and
checks expiry, but never verifies the token was actually signed by Tapis.
This backend fetches the tenant's RS256 public key from the Tapis Tenants API
(`GET {TAPIS_BASE_URL}/v3/tenants/{tenant_id}`) and verifies the signature
with `PyJWT` before trusting any claim. Worth backporting to WebODM's backend
too — flagging it here rather than silently fixing it there.

## 1. Register an OAuth2 client with Tapis

```bash
curl -H "X-Tapis-Token: $JWT" -H "Content-type: application/json" \
  -d '{"client_id": "label-studio", "callback_url": "https://label-studio.pods.portals.tapis.io/tapis/callback/"}' \
  https://portals.tapis.io/v3/oauth2/clients
```

This requires a Tapis account with tenant access — needs to be run by
whoever administers the Tapis tenant this deployment will use. The response
returns `client_id` and `client_key` (Tapis's name for what we call
`TAPIS_CLIENT_SECRET` below) — save both.

## 2. Environment variables

| Variable | Value |
|---|---|
| `TAPIS_BASE_URL` | `https://portals.tapis.io` (this ecosystem's actual tenant base — see `subside`) |
| `TAPIS_TENANT_ID` | the Tapis tenant this client was registered under |
| `TAPIS_CLIENT_ID` | from step 1 |
| `TAPIS_CLIENT_SECRET` | the `client_key` value from step 1 |
| `TAPIS_CALLBACK_URL` | must exactly match the `callback_url` registered in step 1 |

## 3. Build and run

```bash
docker build -t label-studio-tapis .
docker run -p 8080:8080 \
  -e TAPIS_BASE_URL=... -e TAPIS_TENANT_ID=... \
  -e TAPIS_CLIENT_ID=... -e TAPIS_CLIENT_SECRET=... \
  -e TAPIS_CALLBACK_URL=... \
  -v label-studio-data:/label-studio/data \
  label-studio-tapis
```

Login flow: `/tapis/login/` → redirects to Tapis → user authorizes →
Tapis redirects to `/tapis/callback/` → token is verified → Django session
established → redirect to whatever `?next=` was on the login link (defaults
to `/`).

## As a Tapis Pod

```python
t.pods.create_pod(
    pod_id="label-studio",
    image="ghcr.io/in-for-disaster-analytics/label-studio-tapis-auth:latest",
    environment_variables={
        "TAPIS_BASE_URL": "https://portals.tapis.io",
        "TAPIS_TENANT_ID": "portals",
        "TAPIS_CLIENT_ID": "label-studio",
        "TAPIS_CLIENT_SECRET": "<client_key from step 1>",
        "TAPIS_CALLBACK_URL": "https://label-studio.pods.portals.tapis.io/tapis/callback/",
    },
    volume_mounts={"/label-studio/data": {"type": "tapisvolume", "source_id": "label-studio-data"}},
    networking={"default": {"protocol": "http", "port": 8080}},
    resources={"cpu_request": 250, "cpu_limit": 2000, "mem_request": 512, "mem_limit": 3072},
)
```

Register the OAuth2 client (step 1) with the pod's URL *before* creating the
pod — the subdomain is deterministic from `pod_id`
(`https://label-studio.pods.portals.tapis.io`), so there's no chicken-and-egg
problem if the client is registered first. Same ordering `subside`'s own
`register_pods.py` uses.

Two things to decide when actually deploying:

- **Database**: Label Studio defaults to SQLite in its data volume. If we
  want it sharing the same Postgres+pgvector Pod already planned for the
  embeddings store (different database, same instance), that's a
  `DJANGO_DB=postgresql` + connection env vars change in `settings_overlay.py`
  — not done here since it depends on that Pod actually existing first.
- **Reverse proxy under WebODM's own domain** (true Level 2, not just Tapis
  login): a separate step on WebODM's nginx config, not part of this repo.

## What's actually been verified

Built and booted (`docker build` + `docker run`) against the real
`heartexlabs/label-studio` image with dummy Tapis credentials:

- Django starts cleanly with the appended settings/urls (this caught a real
  bug on the first attempt — see "Django settings gotcha" below).
- `GET /tapis/login/` returns a correct 302 to `{TAPIS_BASE_URL}/v3/oauth2/authorize`
  (tested with `tacc.tapis.io` as `TAPIS_BASE_URL`; production should use
  `portals.tapis.io`) with the right `client_id`, URL-encoded `redirect_uri`,
  random `state`, and a session cookie to track it.
- `GET /tapis/callback/` correctly rejects missing/mismatched state (400).
- `TapisOAuth2Backend._verify_token()` was called directly with a forged
  token (valid structure, fabricated payload, garbage signature) — it made a
  real call to `tacc.tapis.io`'s Tenants API (used here only because it was
  a convenient, reachable Tapis tenant for testing the verification logic
  itself — production should target `portals.tapis.io`, this ecosystem's
  actual tenant), fetched the real public key, and correctly rejected the
  forgery.

**Not yet verified**: the full authorize → user login at Tapis → callback →
token exchange round-trip, since that requires a real registered OAuth2
client and a real user going through Tapis's own login UI — only reachable
by whoever administers the Tapis tenant this will run under. Recommend
running that full round-trip once a real client is registered (step 1)
before relying on this for actual labeling work.

## Django settings gotcha (hit during testing, now fixed)

The first version of this used a separate `settings_overlay.py` doing
`from core.settings.label_studio import *`, with `DJANGO_SETTINGS_MODULE`
pointed at it. That crashed on boot: `core/settings/label_studio.py` calls
`sentry.init_sentry()` partway through its own top-level code, which reads
back `django.conf.settings.SENTRY_DSN` — Django's lazy-settings recursion
tolerance only works if the *same* module is being re-entered (it finds
`SENTRY_DSN` already defined earlier in that file's own partial execution);
it breaks once a separate wrapper module is involved. Fixed by appending our
additions directly onto the tail of `core/settings/label_studio.py` and
`core/urls.py` at Docker build time instead of introducing new
`DJANGO_SETTINGS_MODULE`/`ROOT_URLCONF` values — see `Dockerfile`.
