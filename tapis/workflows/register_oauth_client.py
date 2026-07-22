#!/usr/bin/env python3
"""Register (or update) the Label Studio Tapis OAuth2 client.

Adapted directly from subside/tapis/workflows/register_oauth_client.py —
same idempotent create-or-update-in-place behavior, same non-rotation
guarantee (an existing client's callback is updated, but its client_key is
read back rather than rotated, so nothing already holding it is stranded).

Usage:
    export TAPIS_USERNAME=... TAPIS_PASSWORD=...   # or you'll be prompted
    python tapis/workflows/register_oauth_client.py \
        --callback-url https://label-studio.pods.portals.tapis.io/tapis/callback/ \
        --client-id label-studio
"""

from __future__ import annotations

import argparse
import os
from getpass import getpass
from pathlib import Path

# label-studio-tapis-auth/.env — parents: [0]=workflows, [1]=tapis, [2]=repo root.
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

CLIENT_ID_KEY = "TAPIS_CLIENT_ID"
CLIENT_SECRET_KEY = "TAPIS_CLIENT_SECRET"  # Tapis calls this client_key; our
                                            # backend.py/views.py call it
                                            # TAPIS_CLIENT_SECRET — same value.
CALLBACK_KEY = "TAPIS_CALLBACK_URL"


def write_env_vars(env_path: Path, updates: dict[str, str]) -> None:
    """Insert or replace ``KEY=value`` lines in ``env_path`` in place."""
    env_path = Path(env_path)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        key = stripped.split("=", 1)[0].strip() if ("=" in stripped and not stripped.startswith("#")) else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n")


def _is_exists_conflict(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "already exists" in msg or "uniqueness" in msg


def register_oauth_client(t, *, client_id: str, callback_url: str,
                          display_name: str = "Label Studio") -> tuple[str, str]:
    """Ensure the OAuth client exists with the given callback; return (id, key).

    Non-destructive: creates fresh, or if it already exists, updates the
    callback in place and reads the existing client_key back (does not
    rotate it). Does NOT touch .env — see register_and_write_env.
    """
    try:
        res = t.authenticator.create_client(
            client_id=client_id, callback_url=callback_url, display_name=display_name)
        key = getattr(res, "client_key", None)
        if key:
            print(f"Created OAuth client {client_id!r}.")
            return getattr(res, "client_id", client_id), key
    except Exception as exc:
        if not _is_exists_conflict(exc):
            raise
        print(f"OAuth client {client_id!r} exists; updating callback in place.")

    try:
        t.authenticator.update_client(
            client_id=client_id, callback_url=callback_url, display_name=display_name)
    except Exception as exc:
        print(f"  warning: update_client failed ({exc}); using the existing client as-is.")

    got = t.authenticator.get_client(client_id=client_id)
    key = getattr(got, "client_key", None)
    if not key:
        raise RuntimeError(
            f"Client {client_id!r} exists but Tapis did not return its client_key. "
            f"Delete it (t.authenticator.delete_client(client_id={client_id!r})) and "
            f"re-run, or pass a different --client-id.")
    return getattr(got, "client_id", client_id), key


def register_and_write_env(t, *, client_id: str, callback_url: str,
                           display_name: str = "Label Studio",
                           env_path: Path = DEFAULT_ENV_PATH) -> tuple[str, str]:
    """Register the client and persist id/key/callback into env_path."""
    cid, key = register_oauth_client(
        t, client_id=client_id, callback_url=callback_url, display_name=display_name)
    write_env_vars(env_path, {
        CLIENT_ID_KEY: cid,
        CLIENT_SECRET_KEY: key,
        CALLBACK_KEY: callback_url,
    })
    return cid, key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the Label Studio Tapis OAuth client.")
    parser.add_argument("--base-url", default=os.environ.get("TAPIS_BASE_URL", "https://portals.tapis.io"))
    parser.add_argument("--client-id", default="label-studio")
    parser.add_argument("--callback-url",
                        default="https://label-studio.pods.portals.tapis.io/tapis/callback/",
                        help="Must exactly match TAPIS_CALLBACK_URL passed to the pod.")
    parser.add_argument("--display-name", default="Label Studio")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_PATH),
                        help="Path to the .env file to write (default: repo root .env).")
    args = parser.parse_args(argv)

    try:
        from tapipy.tapis import Tapis
    except ImportError:
        raise SystemExit("tapipy is not installed (pip install tapipy).")

    username = os.environ.get("TAPIS_USERNAME") or input("Tapis username: ")
    password = os.environ.get("TAPIS_PASSWORD") or getpass("Tapis password: ")

    t = Tapis(base_url=args.base_url.rstrip("/"), username=username, password=password)
    t.get_tokens()

    cid, _key = register_and_write_env(
        t, client_id=args.client_id, callback_url=args.callback_url,
        display_name=args.display_name, env_path=Path(args.env_file))

    print(f"\nOAuth client {cid!r} registered; wrote {CLIENT_ID_KEY}/{CLIENT_SECRET_KEY}/"
          f"{CALLBACK_KEY} to {args.env_file}")
    print(f"  callback_url = {args.callback_url}")
    print("Keep TAPIS_CLIENT_SECRET secret — .env is gitignored; do not commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
