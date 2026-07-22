
# --- Tapis OAuth2 integration (appended by label-studio-tapis-auth build) ---
import os as _tapis_os

INSTALLED_APPS = list(INSTALLED_APPS) + ["tapis_auth"]
AUTHENTICATION_BACKENDS = ["tapis_auth.backend.TapisOAuth2Backend"] + list(AUTHENTICATION_BACKENDS)

TAPIS_BASE_URL = _tapis_os.environ["TAPIS_BASE_URL"]
TAPIS_TENANT_ID = _tapis_os.environ["TAPIS_TENANT_ID"]
TAPIS_CLIENT_ID = _tapis_os.environ["TAPIS_CLIENT_ID"]
TAPIS_CLIENT_SECRET = _tapis_os.environ["TAPIS_CLIENT_SECRET"]
TAPIS_CALLBACK_URL = _tapis_os.environ["TAPIS_CALLBACK_URL"]
# --- end Tapis OAuth2 integration ---
