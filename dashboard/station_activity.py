"""Display targets and observed station changes, never Epoptia audit timestamps."""
from datetime import datetime
import unicodedata
from dashboard.cache import SnapshotCache

# Provisional pending routing-step budgets at 100%, NOT measured utilization.
# No verified time/quantity capacity or representative daily distribution exists.
# Tune each positive integer after observing daily queues; never seed every
# station from its own current count (the old count / .7 forced ~70% everywhere).
STATION_CAPACITY_TARGETS = {
    'LASER': 100, 'ΚΟΠΗ ΨΑΛΙΔΙ': 60, 'ΣΤΡΑΝΤΖΑ': 80,
    'ΜΟΝΤΑΖ 1': 50, 'ΜΟΝΤΑΖ 2': 50, 'ΜΟΝΤΑΖ ΤΖΑΜΙΑ': 40,
    'ΨΥΚΤΙΚΑ': 30,
}


def station_name(value):
    normalized = ' '.join(''.join(c for c in unicodedata.normalize('NFD', str(value))
                           if not unicodedata.combining(c)).upper().split())
    return 'ΣΤΡΑΝΤΖΑ' if normalized == 'ΣΤΡΑΤΖΑ' else normalized


def star_state(last_change, now):
    if not last_change:
        return 'neutral'
    try:
        age = (now - datetime.fromisoformat(last_change)).total_seconds()
    except (ValueError, TypeError):
        return 'neutral'
    return 'neutral' if age < 0 else 'gold' if age < 3600 else 'neutral' if age <= 7200 else 'red'


class StationActivity:
    def __init__(self, directory=None):
        self.cache = SnapshotCache(directory)
        stored = self.cache.load('station-activity-v2')
        self.state = {}
        snapshot = (stored or {}).get('snapshot')
        for name, item in (snapshot if isinstance(snapshot, dict) else {}).items():
            if (name in STATION_CAPACITY_TARGETS and isinstance(item, dict)
                    and isinstance(item.get('fingerprint'), str)
                    and type(item.get('capacity_target')) is int and item['capacity_target'] > 0):
                changed = item.get('last_change_at')
                if changed is not None:
                    try:
                        if datetime.fromisoformat(changed).tzinfo is None:
                            continue
                    except (ValueError, TypeError):
                        continue
                self.state[name] = dict(fingerprint=item['fingerprint'],
                    last_change_at=changed, capacity_target=item['capacity_target'])

    def observe(self, projection, now):
        if projection.get('complete') is not True:
            return
        by_name = {row['name']: row for row in projection['workstations']}
        for name in STATION_CAPACITY_TARGETS:
            row = by_name.get(name, {})
            fingerprint = row.get('state_fingerprint', 'empty')
            # Old cached/legacy projections do not establish observation history.
            if row and not row.get('state_fingerprint'):
                continue
            previous = self.state.get(name)
            target = STATION_CAPACITY_TARGETS[name]
            changed = previous.get('last_change_at') if previous else None
            if previous and previous['fingerprint'] != fingerprint:
                changed = now.isoformat()
            self.state[name] = dict(fingerprint=fingerprint, last_change_at=changed,
                                    capacity_target=target)
        self.cache.save('station-activity-v2', self.state)

    def decorate(self, rows):
        by_name = {row['name']: dict(row) for row in rows}
        for name in STATION_CAPACITY_TARGETS:
            row = by_name.setdefault(name, dict(name=name, pending_steps=0, units='routing_steps', wip_steps=0,
                running_steps=0, paused_steps=0, waiting_steps=0, unknown_steps=0, distinct_wols=0))
            item = self.state.get(name, {})
            row['last_change_at'] = item.get('last_change_at')
            row['capacity_target'] = STATION_CAPACITY_TARGETS[name]
        return list(by_name.values())
