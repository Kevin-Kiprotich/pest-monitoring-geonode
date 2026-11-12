from django.apps import AppConfig
from django.db.models.signals import post_migrate


def ensure_managers_group(sender, **kwargs):
    """Create the group whose members may manage PestWatch layers, boundaries, styles, model runs and input data."""
    from django.contrib.auth.models import Group, Permission
    from . import conf

    group, _ = Group.objects.get_or_create(name=conf.get("MANAGERS_GROUP"))
    perms = Permission.objects.filter(
        content_type__app_label="pestwatch",
        content_type__model__in=["pestlayer", "boundarysource", "peststyle", "pest", "pipelinerun",
                                 "pipelineschedule", "inputupload"])
    group.permissions.add(*perms)


class PestwatchConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pestwatch'
    verbose_name = "PestWatch"

    def ready(self):
        post_migrate.connect(ensure_managers_group, sender=self)
