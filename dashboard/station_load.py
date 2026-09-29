"""Relative pending routing-step counts, never time/capacity utilization."""


def load_percent(pending, busiest):
    """Inputs are validated counts from the complete visible station census."""
    if pending is None or busiest is None:
        return None
    return round(pending / busiest * 100) if busiest else 0
