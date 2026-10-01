from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from .models import Plan, PlanDay, PlanItem
from apps.telegram.automation import refresh_after_commit


@receiver(pre_save, sender=PlanDay)
@receiver(pre_save, sender=PlanItem)
def remember_previous_plan(sender, instance, **kwargs):
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).first()
        instance._previous_plan = (old.plan if sender is PlanDay else old.plan_day.plan) if old else None


@receiver(post_save, sender=Plan)
@receiver(post_save, sender=PlanDay)
@receiver(post_save, sender=PlanItem)
@receiver(post_delete, sender=Plan)
@receiver(post_delete, sender=PlanItem)
@receiver(post_delete, sender=PlanDay)
def refresh_published_plan(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    plan = instance if sender is Plan else instance.plan if sender is PlanDay else instance.plan_day.plan
    refresh_after_commit(plan)
    previous = getattr(instance, "_previous_plan", None)
    if previous and previous.pk != plan.pk:
        refresh_after_commit(previous)
