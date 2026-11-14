from django.db import models


class Visit(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    who = models.CharField(max_length=150, blank=True)
