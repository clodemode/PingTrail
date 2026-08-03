"""Template filters for the PING_TRAIL surface."""
from django import template

from trail.models import is_in_house, kind_meta

register = template.Library()


@register.filter
def kind_in_house(kind):
    """True when this rung is Kris's own equipment, on his side of the demarc."""
    return is_in_house(kind)


@register.filter
def kind_colour(kind):
    """Semantic colour for a rung kind — keeps chart and table in agreement."""
    return kind_meta(kind)["colour"]


@register.filter
def kind_icon(kind):
    return kind_meta(kind)["icon"]


@register.filter
def kind_blurb(kind):
    return kind_meta(kind)["blurb"]


@register.filter
def abs_ms(value):
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return value
