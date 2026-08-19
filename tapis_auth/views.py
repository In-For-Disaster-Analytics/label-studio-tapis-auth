"""Tapis OAuth2 authorization-code flow views for Label Studio.

State is kept in the user's session (not a DB model) — it never needs to
survive longer than the redirect round-trip, and this avoids adding new
migrations to a Label Studio deployment we don't otherwise need to modify.
"""

import logging
import secrets
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth import login
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.views.decorators.cache import never_cache

from tapis_auth.backend import TapisOAuth2Backend

logger = logging.getLogger(__name__)

STATE_SESSION_KEY = "tapis_oauth2_state"
REDIRECT_SESSION_KEY = "tapis_oauth2_redirect_after"


@never_cache
def login_redirect(request):
    """Entry point: redirects the browser into the Tapis authorization flow."""
    state = secrets.token_urlsafe(32)
    request.session[STATE_SESSION_KEY] = state
    request.session[REDIRECT_SESSION_KEY] = request.GET.get("next", "/")

    params = {
        "client_id": settings.TAPIS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.TAPIS_CALLBACK_URL,
        "state": state,
        "scope": "openid profile",
    }
    auth_url = f"{settings.TAPIS_BASE_URL}/v3/oauth2/authorize?{urlencode(params)}"
    return HttpResponseRedirect(auth_url)


@never_cache
def callback(request):
    """Handles the redirect back from Tapis after the user authorizes."""
    error = request.GET.get("error")
    if error:
        logger.error("Tapis OAuth2 error: %s - %s", error, request.GET.get("error_description"))
        return HttpResponseBadRequest(f"Tapis OAuth2 error: {error}")

    code = request.GET.get("code")
    state = request.GET.get("state")
    expected_state = request.session.pop(STATE_SESSION_KEY, None)
    if not code or not state or not expected_state or state != expected_state:
        return HttpResponseBadRequest("Missing or mismatched OAuth2 state")

    token_data = _exchange_code_for_token(code)
    if not token_data:
        return HttpResponseBadRequest("Failed to exchange authorization code for a token")

    access_token = _extract_access_token(token_data)
    if not access_token:
        return HttpResponseBadRequest("No usable access token in Tapis response")

    backend = TapisOAuth2Backend()
    user = backend.authenticate(request, access_token=access_token)
    if not user:
        logger.warning("Tapis token verification failed at callback")
        return HttpResponseBadRequest("Tapis token verification failed")

    login(request, user, backend="tapis_auth.backend.TapisOAuth2Backend")

    # Label Studio's InactivitySessionTimeoutMiddleWare (core/middleware.py)
    # logs the user straight back out on the very next request if
    # session['last_login'] is missing -- it defaults the "last login" time
    # to 0, so current_time - 0 always exceeds MAX_SESSION_AGE. Django's own
    # auth.login() never sets this key; only Label Studio's own login wrapper
    # (users/functions/common.py's login()) does, alongside auth.login(). Set
    # it here too so a Tapis-authenticated session survives past this request.
    request.session["last_login"] = time.time()

    redirect_to = request.session.pop(REDIRECT_SESSION_KEY, "/")
    return HttpResponseRedirect(redirect_to)


def _exchange_code_for_token(code):
    try:
        resp = requests.post(
            f"{settings.TAPIS_BASE_URL}/v3/oauth2/tokens",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.TAPIS_CLIENT_ID,
                "client_secret": settings.TAPIS_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.TAPIS_CALLBACK_URL,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Tapis-Tenant": settings.TAPIS_TENANT_ID,
                "Accept": "application/json",
            },
            timeout=10,
        )
    except requests.RequestException:
        logger.exception("Tapis token exchange request failed")
        return None

    if resp.status_code != 200:
        logger.error("Tapis token exchange failed: %s %s", resp.status_code, resp.text)
        return None

    body = resp.json()
    if body.get("status") == "success" and "result" in body:
        return body["result"]
    return body


def _extract_access_token(token_data):
    raw = token_data.get("access_token")
    if isinstance(raw, dict):
        return raw.get("access_token")
    if isinstance(raw, str):
        return raw
    return None
