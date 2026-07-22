"""Regression test for the Tapis SSO session-persistence bug.

Root cause: Label Studio's own core.middleware.InactivitySessionTimeoutMiddleWare
logs a user straight back out on the request *after* login if
session['last_login'] is missing -- it defaults the "last login" time to 0, so
current_time - 0 always exceeds MAX_SESSION_AGE. Django's bare
django.contrib.auth.login() never sets that key; only Label Studio's own login
wrapper (users/functions/common.py's login()) does it alongside auth.login().
tapis_auth/views.py::callback() now sets it too -- this test would fail again
if that line were ever removed.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tapis_auth.views import STATE_SESSION_KEY


class TapisCallbackSessionPersistenceTest(TestCase):
    def _seed_state(self):
        # Drive the real login_redirect() view (rather than poking
        # self.client.session directly) so its actual Set-Cookie response is
        # what stores the state -- with SESSION_ENGINE=signed_cookies, the
        # test Client's `.session` property doesn't write back to
        # `.cookies` on its own, so a manually-saved session is never sent
        # on the next request. A real request/response round-trip is.
        login_response = self.client.get("/tapis/login/", {"next": "/"})
        self.assertEqual(login_response.status_code, 302, login_response.content)
        return self.client.session[STATE_SESSION_KEY]

    @patch("tapis_auth.views._exchange_code_for_token", return_value={"access_token": "fake-token"})
    @patch("tapis_auth.backend.TapisOAuth2Backend._verify_token")
    def test_login_survives_the_next_request(self, mock_verify_token, mock_exchange):
        mock_verify_token.return_value = {
            "_resolved_username": "testlabeler",
            "email": "",
            "given_name": "",
            "family_name": "",
        }

        state = self._seed_state()
        response = self.client.get(
            "/tapis/callback/", {"code": "fakecode", "state": state}
        )
        self.assertEqual(response.status_code, 302, response.content)

        # The actual bug: a second, independent request reusing the same
        # browser session used to come back unauthenticated even though the
        # callback's login() succeeded moments earlier.
        response2 = self.client.get("/")
        self.assertTrue(response2.wsgi_request.user.is_authenticated)
        self.assertEqual(response2.wsgi_request.user.username, "testlabeler")

        user = get_user_model().objects.get(username="testlabeler")
        self.assertIsNotNone(user.active_organization)
