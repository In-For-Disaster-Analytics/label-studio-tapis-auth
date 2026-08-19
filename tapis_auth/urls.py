from django.urls import path

from tapis_auth import views

urlpatterns = [
    path("tapis/login/", views.login_redirect, name="tapis-login"),
    path("tapis/callback/", views.callback, name="tapis-callback"),
]
