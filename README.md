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

## Deploying as a Tapis Pod

```bash
export TAPIS_USERNAME=...   TAPIS_PASSWORD=...   # or you'll be prompted
python tapis/register_pod.py --owner in-for-disaster-analytics --dry-run   # inspect first
python tapis/register_pod.py --owner in-for-disaster-analytics             # then for real
```

`tapis/register_pod.py` (mirroring `subside/tapis/register_pods.py`'s
conventions exactly — upsert/`--recreate`/`--dry-run`) does the whole
sequence in the right order: registers the OAuth2 client against the pod's
URL first (deterministic from `pod_id`, no chicken-and-egg problem), writes
the resulting `TAPIS_CLIENT_ID`/`TAPIS_CLIENT_SECRET` to a local `.env`
(gitignored — never commit it), creates the persistent `label-studio-data`
Volume if it doesn't exist, then creates or updates the pod itself.

Requires a Tapis account with tenant access. **This exact script has not
been run against a real Tapis tenant yet** — see "What's actually been
verified" below for what has and hasn't been exercised.

## Environment variables

| Variable | Value |
|---|---|
| `TAPIS_BASE_URL` | `https://portals.tapis.io` (this ecosystem's actual tenant base — see `subside`) |
| `TAPIS_TENANT_ID` | the Tapis tenant this client was registered under |
| `TAPIS_CLIENT_ID` | written to `.env` by `register_pod.py` |
| `TAPIS_CLIENT_SECRET` | written to `.env` by `register_pod.py` (Tapis calls this `client_key`) |
| `TAPIS_CALLBACK_URL` | written to `.env` by `register_pod.py`; must exactly match the registered callback |

## Local build and run (dev/testing only)

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
running that full round-trip after `tapis/register_pod.py` has actually been
run against a real Tapis tenant, before relying on this for actual labeling
work.

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
