from django import template

register = template.Library()


@register.filter
def duration(td):
    """A timedelta as "2 h 05 min", "4 min 10 s" or "12 s"."""
    if td is None:
        return "—"
    s = int(td.total_seconds())
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {sec:02d} s"
    return f"{sec} s"
