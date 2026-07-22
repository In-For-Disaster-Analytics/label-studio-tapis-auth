#!/usr/bin/env python3
"""Register (or update) Label Studio as a Tapis Pod on portals.tapis.io.

Adapted from subside/tapis/register_pods.py (same upsert/dry-run/recreate
conventions), collapsed to one pod since Label Studio — unlike subside's
separate API+UI split — is a single container. Adds a persistent Volume,
which subside's pods don't need but this one does (SQLite DB + uploaded
task data).

    labelstudio  ->  https://labelstudio.pods.portals.tapis.io  (:8080)

Usage:
    export TAPIS_USERNAME=... TAPIS_PASSWORD=...      # or you'll be prompted
    python tapis/register_pod.py --owner in-for-disaster-analytics
    python tapis/register_pod.py --recreate            # delete + recreate
    python tapis/register_pod.py --dry-run              # print specs, don't call Tapis

Prerequisites:
    * Image built & pushed to GHCR (.github/workflows/docker-build.yml).
    * The GHCR package must be PUBLIC — Tapis Pods pulls custom images
      anonymously; a private repo will fail to pull. (Confirmed against
      subside's existing production pods, which use the same pattern —
      see DSO-Architecture docs/architecture/cicd.md.)
    * Register the OAuth client against the pod's URL BEFORE creating the
      pod (the URL is deterministic from pod_id, so there's no
      chicken-and-egg problem) — this script does that for you unless
      --no-oauth is passed.

NOT YET LIVE-TESTED: this mirrors a proven pattern (subside's pods, already
running in production) and the Docker image itself has been verified to
boot and handle the login redirect/state/signature-verification logic
correctly (see README "What's actually been verified"), but this exact
script has not been run against a real Tapis tenant. Recommend a --dry-run
first, then a real run, then confirming the full login flow in a browser
before relying on this for actual labeling work.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from getpass import getpass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Tapis pod_id/volume_id must be lowercase alphanumeric, first char alpha —
# NO hyphens (same constraint subside's register_pods.py notes for pod_id;
# it applies to volume_id too, confirmed the hard way: create_volume() 400'd
# on "label-studio-data" while pod_id="label-studio" was never even reached).
POD_ID = "labelstudio"
VOLUME_ID = "labelstudiodata"
SECRET_KEYS = {"TAPIS_CLIENT_SECRET"}


def _load_dotenv(override: bool = False) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env", override=override)
    except ImportError:
        pass


def _pods_domain(base_url: str) -> str:
    return base_url.rstrip("/").split("://", 1)[-1]


def pod_url(base_url: str) -> str:
    return f"https://{POD_ID}.pods.{_pods_domain(base_url)}"


def build_spec(owner: str, tag: str, base_url: str) -> dict:
    callback_url = f"{pod_url(base_url)}/tapis/callback/"
    env = {
        "TAPIS_BASE_URL": base_url.rstrip("/"),
        "TAPIS_TENANT_ID": os.environ.get("TAPIS_TENANT_ID", "portals"),
        "TAPIS_CLIENT_ID": os.environ.get("TAPIS_CLIENT_ID", POD_ID),
        "TAPIS_CALLBACK_URL": callback_url,
    }
    if os.environ.get("TAPIS_CLIENT_SECRET"):
        env["TAPIS_CLIENT_SECRET"] = os.environ["TAPIS_CLIENT_SECRET"]

    return {
        "pod_id": POD_ID,
        "image": f"ghcr.io/{owner}/label-studio-tapis-auth:{tag}",
        "description": "Label Studio (Tapis SSO enabled)",
        "networking": {"default": {"protocol": "http", "port": 8080}},
        "resources": {"cpu_request": 250, "cpu_limit": 2000,
                      "mem_request": 512, "mem_limit": 3072},
        "volume_mounts": {"/label-studio/data": {"type": "tapisvolume", "source_id": VOLUME_ID}},
        "environment_variables": env,
        "time_to_stop_default": -1,  # long-running service
    }


def ensure_volume(t, *, size_limit_mb: int, dry_run: bool) -> None:
    if dry_run:
        print(f"Would ensure volume {VOLUME_ID!r} exists (size_limit={size_limit_mb}).")
        return
    try:
        t.pods.get_volume(volume_id=VOLUME_ID)
        print(f"Volume {VOLUME_ID!r} already exists.")
    except Exception:
        print(f"Creating volume {VOLUME_ID!r}...")
        t.pods.create_volume(
            volume_id=VOLUME_ID,
            description="Label Studio SQLite DB + uploaded task data",
            size_limit=size_limit_mb,
        )


def upsert_pod(t, spec: dict, *, recreate: bool, start: bool, restart: bool) -> None:
    pid = spec["pod_id"]
    exists = True
    try:
        t.pods.get_pod(pod_id=pid)
    except Exception:
        exists = False

    if exists and recreate:
        print(f"  [{pid}] deleting existing pod (--recreate)...")
        t.pods.delete_pod(pod_id=pid)
        exists = False

    if exists:
        print(f"  [{pid}] updating...")
        t.pods.update_pod(**spec)
        if restart:
            try:
                t.pods.restart_pod(pod_id=pid)
                print(f"  [{pid}] restart requested (applying env changes)")
            except Exception as exc:
                print(f"  [{pid}] restart failed: {exc}")
            return
    else:
        print(f"  [{pid}] creating...")
        t.pods.create_pod(**spec)

    if start:
        try:
            status = getattr(t.pods.get_pod(pod_id=pid), "status", None)
        except Exception:
            status = None
        if status and status != "STOPPED":
            print(f"  [{pid}] already {status}; not starting")
        else:
            try:
                t.pods.start_pod(pod_id=pid)
                print(f"  [{pid}] start requested")
            except Exception as exc:
                print(f"  [{pid}] start skipped: {exc}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Register Label Studio as a Tapis Pod.")
    parser.add_argument("--base-url", default=os.environ.get("TAPIS_BASE_URL", "https://portals.tapis.io"))
    parser.add_argument("--owner", default=os.environ.get("GHCR_OWNER", "in-for-disaster-analytics"),
                        help="GHCR owner/org for the image (ghcr.io/<owner>/label-studio-tapis-auth).")
    parser.add_argument("--image-tag", default="latest")
    parser.add_argument("--volume-size-mb", type=int, default=10240)
    parser.add_argument("--recreate", action="store_true", help="Delete + recreate instead of update.")
    parser.add_argument("--restart", action="store_true",
                        help="Restart the updated pod so env changes take effect.")
    parser.add_argument("--no-start", action="store_true", help="Create/update but don't start.")
    parser.add_argument("--dry-run", action="store_true", help="Print specs; don't call Tapis.")
    parser.add_argument("--no-oauth", action="store_true",
                        help="Skip (re)registering the OAuth client + writing .env.")
    parser.add_argument("--oauth-client-id", default=os.environ.get("TAPIS_OAUTH_CLIENT_ID", POD_ID))
    args = parser.parse_args(argv)

    _load_dotenv()
    url = pod_url(args.base_url)
    callback_url = f"{url}/tapis/callback/"

    if args.dry_run:
        spec = dict(build_spec(args.owner, args.image_tag, args.base_url))
        spec["environment_variables"] = {
            k: ("***" if k in SECRET_KEYS else v)
            for k, v in spec["environment_variables"].items()
        }
        print(json.dumps(spec, indent=2))
        print(f"\nURL once running: {url}")
        if not args.no_oauth:
            print(f"Would register OAuth client {args.oauth_client_id!r} (callback {callback_url}) "
                  f"and write its id/secret to {REPO_ROOT / '.env'}.")
        ensure_volume(None, size_limit_mb=args.volume_size_mb, dry_run=True)
        return 0

    try:
        from tapipy.tapis import Tapis
    except ImportError:
        raise SystemExit("tapipy is not installed (pip install tapipy).")

    username = os.environ.get("TAPIS_USERNAME") or input("Tapis username: ")
    password = os.environ.get("TAPIS_PASSWORD") or getpass("Tapis password: ")
    t = Tapis(base_url=args.base_url.rstrip("/"), username=username, password=password)
    t.get_tokens()

    # Register the OAuth client FIRST (URL is deterministic from pod_id, so no
    # chicken-and-egg problem) and persist its id/secret to .env, then reload
    # so build_spec forwards the matching secret into the pod.
    if not args.no_oauth:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from workflows import register_oauth_client as oauth
        cid, _secret = oauth.register_and_write_env(
            t, client_id=args.oauth_client_id, callback_url=callback_url,
            env_path=REPO_ROOT / ".env")
        print(f"OAuth client {cid!r} registered; wrote id/secret/callback to "
              f"{REPO_ROOT / '.env'} (callback {callback_url}).")
        _load_dotenv(override=True)

    ensure_volume(t, size_limit_mb=args.volume_size_mb, dry_run=False)

    spec = build_spec(args.owner, args.image_tag, args.base_url)
    leaked = sorted(k for k in SECRET_KEYS if os.environ.get(k))
    if leaked:
        print("WARNING: these secrets will be stored in the pod's environment_variables "
              "(visible to the pod owner): " + ", ".join(leaked))
        print("         For production, move them to Tapis secrets (${pods:secrets:KEY}).\n")

    upsert_pod(t, spec, recreate=args.recreate, start=not args.no_start, restart=args.restart)

    print(f"\nDone. Pod (once started): {url}")
    print(f"  Login: {url}/tapis/login/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
