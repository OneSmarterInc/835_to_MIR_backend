"""Keep normalized 835 claim rows synchronized with stored immutable files."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from .edi835_claim_service import normalize_835_file
from .models import EDI835File


@receiver(post_save, sender=EDI835File)
def normalize_saved_835_file(sender, instance, **kwargs):
    if instance.input_file_content:
        normalize_835_file(instance)
