"""Durable, first-observed routing-step completions from verified full scans."""
from contextlib import closing
from datetime import timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
from zoneinfo import ZoneInfo

import epoptia_read
from dashboard.orders import TERMINAL

ATHENS = ZoneInfo('Europe/Athens')
SOURCE = 'shared_routing_parser_rest_scan'
IDENTITY_VERSION = 2
ID_FIELDS = ('id', 'step_id', 'stepId', 'route_id', 'routeId',
             'routing_id', 'routingId', 'job_id', 'jobId')


def identity_hash(parts):
    return sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def routing_from(row):
    routing = row.get('erp_routing')
    lifecycle = str(row.get('production_status') or row.get('status') or '').strip().casefold()
    # Full WOL scans also include terminal parents without routing. They carry
    # no step observations; retain their prior state without inferring events.
    if routing is None and lifecycle in TERMINAL:
        return []
    if not isinstance(routing, list):
        raise ValueError('missing routing')
    return routing


def steps_from(rows, *, with_aliases=False):
    """Validate the entire population before any store writes, including terminals."""
    if not isinstance(rows, list):
        raise ValueError('invalid snapshot')
    result, wols, aliases = {}, {}, {}
    for row in rows:
        wol = row.get('workorderline_id', row.get('id'))
        if type(wol) not in (str, int) or not str(wol):
            raise ValueError('missing identity')
        wol = str(wol)
        if wol in wols:
            if wols[wol] != row:
                raise ValueError('conflicting WOL')
            continue
        wols[wol] = row
        routing = routing_from(row)
        parsed = [epoptia_read.parse_routing_step(step) for step in routing]
        if any(item is None for item in parsed):
            raise ValueError('invalid routing step')
        # Keep legacy fallback labels so persisted identities remain compatible.
        labels = [(item['workstation'], item['job'] or step.get('jobName')
                   or step.get('name') or '') for step, item in zip(routing, parsed)]
        for step, item, (station, job) in zip(routing, parsed, labels):
            if not isinstance(station, str) or not station or not isinstance(job, str):
                raise ValueError('invalid labels')
            identifiers = [(key, str(step[key])) for key in ID_FIELDS
                           if type(step.get(key)) in (str, int) and str(step[key])]
            candidates = [identity_hash([wol, 'upstream', pair]) for pair in identifiers]
            # Labels are usable only when unique within the complete parent routing.
            # Array indexes and mutable route positions are never step identities.
            if not candidates:
                if labels.count((station, job)) != 1:
                    continue
                candidates = [identity_hash([wol, 'unique_labels_v2', station, job])]
            key = candidates[0]
            aliases[key] = dict(zip([pair[0] for pair in identifiers] or ['unique_labels_v2'], candidates))
            if key in result:
                raise ValueError('ambiguous step identity')
            status = str(item['status'] or '').strip().casefold()
            if not status:
                raise ValueError('missing step status')
            result[key] = (wol, station, job, status)
    # An identifier shared by two current steps cannot safely match historical state.
    counts = {}
    for candidates in aliases.values():
        for candidate in candidates.values():
            counts[candidate] = counts.get(candidate, 0) + 1
    aliases = {key: {kind: candidate for kind, candidate in candidates.items() if counts[candidate] == 1}
               for key, candidates in aliases.items()}
    return (result, aliases) if with_aliases else result


class CompletedJobsTracker:
    def __init__(self, path=None):
        self.path = (Path(path) if path is not None else Path(__file__).parent / '.cache' / 'completed_jobs.sqlite3').absolute()
        self.lock = RLock()
        self.status = dict(state='pending', reason='awaiting_verified_snapshot')
        self._load_startup_status()

    def _load_startup_status(self):
        # SQLite remains the source of truth for steps and events. Startup only
        # reads the saved baseline; it must not create a store or infer events.
        try:
            if not self.path.exists():
                return
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                db.execute('BEGIN')
                baseline = bool(db.execute('SELECT 1 FROM steps LIMIT 1').fetchone())
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='tracker_metadata'").fetchone():
                    baseline = baseline or bool(db.execute(
                        "SELECT 1 FROM tracker_metadata WHERE key='baseline_initialized' AND value='True'"
                    ).fetchone())
            if baseline:
                self.status = dict(state='persisted_awaiting_scan', reason=None)
        except (OSError, ValueError, sqlite3.Error):
            self.status = dict(state='unavailable', reason='tracker_store_unavailable')

    def observe(self, rows, *, complete, now):
        with self.lock:
            if complete is not True:
                self.status = dict(state='degraded', reason='incomplete_wol_snapshot')
                return
            try:
                steps, aliases = steps_from(rows, with_aliases=True)
                scan_steps = sum(len(routing_from(row)) for row in rows)
                if now.tzinfo is None:
                    raise ValueError('timezone required')
            except (ValueError, TypeError, AttributeError):
                self.status = dict(state='degraded', reason='invalid_routing_snapshot')
                return
            if not steps:
                self.status = dict(state='unavailable', reason='no_routing_steps')
                return
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                    db.execute('CREATE TABLE IF NOT EXISTS steps (identity TEXT PRIMARY KEY, status TEXT NOT NULL)')
                    db.execute('''CREATE TABLE IF NOT EXISTS events (
                        step_identity TEXT PRIMARY KEY, wol_id TEXT NOT NULL,
                        workstation TEXT NOT NULL, job_name TEXT NOT NULL,
                        observed_completed_at TEXT NOT NULL, local_date TEXT NOT NULL)''')
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    db.execute('CREATE TABLE IF NOT EXISTS step_aliases (alias TEXT PRIMARY KEY, identity TEXT NOT NULL)')
                    db.execute('CREATE TABLE IF NOT EXISTS identity_keys (identity TEXT NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(identity, kind))')
                    metadata = dict(db.execute('SELECT key, value FROM tracker_metadata'))
                    legacy = db.execute('SELECT COUNT(*) FROM steps').fetchone()[0] if not metadata else 0
                    new_events = recovered = 0
                    ambiguous = scan_steps != len(steps)

                    for key, (wol, station, job, status) in steps.items():
                        matches = set()
                        for candidate in aliases[key].values():
                            existing = db.execute('SELECT identity FROM step_aliases WHERE alias=?', (candidate,)).fetchone()
                            if existing:
                                matches.add(existing[0])
                            if db.execute('SELECT 1 FROM steps WHERE identity=?', (candidate,)).fetchone():
                                matches.add(candidate)
                        if len(matches) > 1:
                            # Conflicting historical baselines cannot be merged by guessing.
                            ambiguous = True
                            continue
                        canonical = next(iter(matches), key)
                        known = dict(db.execute('SELECT kind, fingerprint FROM identity_keys WHERE identity=?', (canonical,)))
                        if any(kind in known and known[kind] != candidate
                               for kind, candidate in aliases[key].items()):
                            ambiguous = True
                            continue
                        db.executemany('INSERT OR IGNORE INTO identity_keys VALUES (?, ?, ?)',
                                       [(canonical, kind, candidate) for kind, candidate in aliases[key].items()])
                        previous = db.execute('SELECT status FROM steps WHERE identity=?', (canonical,)).fetchone()
                        migrating = bool(previous and any(
                            not db.execute('SELECT 1 FROM step_aliases WHERE alias=?', (candidate,)).fetchone()
                            for candidate in aliases[key].values()))
                        for candidate in aliases[key].values():
                            db.execute('INSERT OR IGNORE INTO step_aliases VALUES (?, ?)', (candidate, canonical))
                        # Upstream may introduce states outside the station census.
                        # Keep existing identities/baselines and count only an observed
                        # non-completed -> completed transition, never disappearance.
                        if previous and previous[0] != 'completed' and status == 'completed':
                            inserted = db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?)',
                                (canonical, wol, station, job, now.astimezone(timezone.utc).isoformat(),
                                 now.astimezone(ATHENS).date().isoformat())).rowcount
                            new_events += inserted
                            recovered += inserted if migrating else 0
                        db.execute('INSERT INTO steps VALUES (?, ?) ON CONFLICT(identity) DO UPDATE SET status=excluded.status',
                                   (canonical, status))
                    metadata.update(
                        identity_version=IDENTITY_VERSION, baseline_initialized=True,
                        tracked_steps=db.execute('SELECT COUNT(*) FROM steps').fetchone()[0],
                        last_scan_steps=scan_steps, last_scan_new_events=new_events,
                        coverage_started_at=metadata.get('coverage_started_at') or (
                            'unknown_legacy' if legacy else now.astimezone(timezone.utc).isoformat()),
                        deterministic_recovery_events_count=int(metadata.get('deterministic_recovery_events_count', 0)) + recovered,
                        recovery_status='ambiguous_steps_skipped' if ambiguous else (
                            'legacy_identity_coverage_unverified' if legacy or metadata.get('recovery_status') == 'legacy_identity_coverage_unverified'
                            else 'deterministic_recovery' if recovered else 'ready'))
                    db.executemany('INSERT INTO tracker_metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                   [(key, str(value)) for key, value in metadata.items()])
                self.status = dict(state='degraded' if ambiguous else 'ready',
                                   reason='ambiguous_steps_skipped' if ambiguous else None)
            except (OSError, ValueError, sqlite3.Error):
                self.status = dict(state='unavailable', reason='tracker_store_unavailable')

    def summary(self, now):
        with self.lock:
            result = dict(total=0, breakdown_by_workstation={})
            metadata = {}
            tracked = 0
            try:
                if self.path.exists():
                    with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                        db.execute('BEGIN')
                        rows = db.execute('SELECT workstation, COUNT(*) FROM events WHERE local_date=? GROUP BY workstation',
                                          (now.astimezone(ATHENS).date().isoformat(),)).fetchall()
                        tracked = db.execute('SELECT COUNT(*) FROM steps').fetchone()[0]
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='tracker_metadata'").fetchone():
                            metadata = dict(db.execute('SELECT key, value FROM tracker_metadata'))
                    result['breakdown_by_workstation'] = dict(rows)
                    result['total'] = sum(count for _, count in rows)
            except (OSError, ValueError, sqlite3.Error):
                self.status = dict(state='unavailable', reason='tracker_store_unavailable')
            diagnostics = dict(
                source=SOURCE,
                baseline_exists=bool(metadata) or tracked > 0,
                tracked_step_count=tracked,
                last_refresh_new_events=int(metadata.get('last_scan_new_events', 0)),
                status=self.status['state'], baseline_initialized=bool(metadata) or tracked > 0,
                tracked_steps=tracked, last_scan_steps=int(metadata.get('last_scan_steps', 0)),
                last_scan_new_events=int(metadata.get('last_scan_new_events', 0)),
                identity_version=IDENTITY_VERSION, coverage_started_at=metadata.get('coverage_started_at'),
                recovery_status=metadata.get('recovery_status', 'legacy_identity_coverage_unverified' if tracked else 'awaiting_baseline'))
            if metadata and self.status['state'] == 'pending':
                diagnostics['status'] = 'persisted_awaiting_scan'
            result['tracker'] = diagnostics
            return result, dict(self.status)
