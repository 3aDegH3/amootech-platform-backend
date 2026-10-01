from django.db import models


class MutationSource(models.TextChoices):
    """Last channel to successfully mutate user data; reads do not change it."""
    WEB = "WEB", "Web"
    TELEGRAM = "TELEGRAM", "Telegram"
