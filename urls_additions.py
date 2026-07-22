
# --- Tapis OAuth2 integration (appended by label-studio-tapis-auth build) ---
from django.urls import include as _tapis_include
from django.urls import path as _tapis_path

urlpatterns = [_tapis_path("", _tapis_include("tapis_auth.urls"))] + urlpatterns
# --- end Tapis OAuth2 integration ---
