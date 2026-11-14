from django.http import JsonResponse
from django.shortcuts import render

from .models import Visit


def index(request):
    who = getattr(request.user, "username", "") or ""
    Visit.objects.create(who=who)
    return render(request, "bdh_hello/index.html", {"who": who, "count": Visit.objects.count()})


def visits(request):
    return JsonResponse({"count": Visit.objects.count()})
