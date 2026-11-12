from django import template
from django.urls import reverse

register = template.Library()


@register.simple_tag
def get_apps_menu():
    return [
        {
            "label": "Apps",
            "type": "dropdown",
            "id": "gn-apps-menu",
            "items": [
                {"type": "link", "href": reverse("pestwatch:index"), "label": "Pest Watch"},
            ],
        }
    ]
