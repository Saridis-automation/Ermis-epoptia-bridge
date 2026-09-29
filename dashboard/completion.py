"""Verified whole-order transition history, not archive/update timestamps."""
from dashboard.adapter import timestamp
from dashboard.orders import ATHENS


def completion_count(history, now):
    """Distinct orders whose latest completed transition is today and not reopened.

    A verified reader must attest full history coverage through `now`, including
    reopen transitions. This contract is not an assertion of an upstream field.
    """
    if not isinstance(history, dict) or history.get('complete') is not True:
        return None
    if history.get('semantics') != 'whole_order_status_transitions' or not history.get('verified_source'):
        return None
    try:
        if timestamp(history['coverage_through']) < now:
            return None
        events = history['events']
        if not isinstance(events, list):
            return None
        seen, latest = {}, {}
        for row in events:
            if any(type(row[key]) not in (int, str) or not str(row[key]).strip() for key in ('event_id', 'order_id')):
                return None
            event_id, order_id = str(row['event_id']), str(row['order_id'])
            if not event_id or not order_id or row['state'] not in ('completed', 'reopened', 'cancelled', 'archived'):
                return None
            at = timestamp(row['at'])
            if at > now:
                return None
            event = (order_id, at, row['state'])
            if event_id in seen and seen[event_id] != event:
                return None
            seen[event_id] = event
            old = latest.get(order_id)
            if old and old[0] == at and old[1] != row['state']:
                return None
            if old is None or at > old[0]:
                latest[order_id] = (at, row['state'])
        return sum(state == 'completed' and at.astimezone(ATHENS).date() == now.astimezone(ATHENS).date()
                   for at, state in latest.values())
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
