from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic.base import RedirectView

from hospital.views import ThrottledLoginView

urlpatterns = [
    # Browsers ask for this at the root regardless of the <link> tag. Left
    # unrouted it answers 404 with a full error page on every fresh session.
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/images/favicon.svg", permanent=True),
        name="favicon",
    ),
    path("admin/", admin.site.urls),
    path("login/", ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("hospital.urls")),
]
