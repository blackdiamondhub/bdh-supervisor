from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="hello_index"),
    path("api/visits", views.visits, name="hello_visits"),
]
