"""Tapis OAuth2 authentication backend for Label Studio.

Unlike WebODM's existing app/auth/tapis_oauth2.py (which decodes the JWT
payload without verifying its signature), this backend verifies the token's
RS256 signature against the tenant's public key from the Tapis Tenants API
before trusting any claim in it. Worth backporting to WebODM's own backend.
"""

import logging
import time

import jwt
import requests
from django.conf import settings
from django.contrib.auth.backends import ModelBackend

logger = logging.getLogger(__name__)

_public_key_cache = {}  # tenant_id -> (public_key_pem, fetched_at)
_PUBLIC_KEY_CACHE_TTL = 3600  # seconds


def _get_tenant_public_key(tapis_base_url, tenant_id):
    cached = _public_key_cache.get(tenant_id)
    if cached and (time.time() - cached[1]) < _PUBLIC_KEY_CACHE_TTL:
        return cached[0]

    resp = requests.get(f"{tapis_base_url}/v3/tenants/{tenant_id}", timeout=10)
    resp.raise_for_status()
    body = resp.json()
    tenant = body.get("result", body)
    public_key = tenant["public_key"]

    _public_key_cache[tenant_id] = (public_key, time.time())
    return public_key


class TapisOAuth2Backend(ModelBackend):
    """Authenticates a Label Studio user from a verified Tapis access token."""

    def authenticate(self, request, access_token=None, **kwargs):
        if not access_token:
            return None

        tapis_base_url = getattr(settings, "TAPIS_BASE_URL", None)
        tenant_id = getattr(settings, "TAPIS_TENANT_ID", None)
        if not tapis_base_url or not tenant_id:
            logger.error("TAPIS_BASE_URL / TAPIS_TENANT_ID not configured")
            return None

        claims = self._verify_token(access_token, tapis_base_url, tenant_id)
        if not claims:
            return None

        return self._get_or_create_user(claims)

    def _verify_token(self, access_token, tapis_base_url, tenant_id):
        try:
            public_key = _get_tenant_public_key(tapis_base_url, tenant_id)
        except Exception:
            logger.exception("Failed to fetch Tapis tenant public key")
            return None

        try:
            claims = jwt.decode(
                access_token,
                key=public_key,
                algorithms=["RS256"],
                options={"verify_aud": False},  # Tapis JWTs don't set a client-specific aud
            )
        except jwt.ExpiredSignatureError:
            logger.info("Tapis access token expired")
            return None
        except jwt.InvalidTokenError:
            logger.warning("Tapis access token failed signature verification")
            return None

        username = claims.get("tapis/username") or claims.get("username") or claims.get("sub")
        if not username:
            logger.warning("Verified Tapis token has no username claim")
            return None

        claims["_resolved_username"] = username
        return claims

    def _get_or_create_user(self, claims):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        username = claims["_resolved_username"]
        email = claims.get("email") or claims.get("tapis/email") or ""
        first_name = claims.get("given_name") or claims.get("tapis/given_name") or ""
        last_name = claims.get("family_name") or claims.get("tapis/family_name") or ""

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "first_name": first_name, "last_name": last_name},
        )
        if not created:
            changed = False
            for field, value in (("email", email), ("first_name", first_name), ("last_name", last_name)):
                if value and getattr(user, field) != value:
                    setattr(user, field, value)
                    changed = True
            if changed:
                user.save()

        # Label Studio's own core/views.py main() logs the user straight back
        # out if active_organization is None -- discovered the hard way: a
        # Tapis-authenticated user landed on Label Studio's own login page
        # right after a successful SSO login, because get_or_create() above
        # makes a bare User with no organization. Mirrors the same org
        # attachment users/functions/common.py's save_user() does for a
        # normal signup: join the existing org, or create the first one.
        if user.active_organization is None:
            from organizations.models import Organization

            if Organization.objects.exists():
                org = Organization.objects.first()
                org.add_user(user)
            else:
                org = Organization.create_organization(created_by=user, title="Label Studio")
            user.active_organization = org
            user.save(update_fields=["active_organization"])
            print(f"TAPIS_DEBUG: attached user={username!r} to org={org!r} "
                  f"(pk={org.pk})", flush=True)
        else:
            print(f"TAPIS_DEBUG: user={username!r} already had "
                  f"active_organization={user.active_organization!r}", flush=True)

        logger.info("Tapis OAuth2 login: %s (%s)", username, "created" if created else "existing")
        return user

    def get_user(self, user_id):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
