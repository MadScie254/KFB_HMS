from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from hospital.views import ThrottledLoginView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("hospital.urls")),
]
