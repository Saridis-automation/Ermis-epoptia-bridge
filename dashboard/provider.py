"""Independent, bounded direct reads through the shared Epoptia query code."""
import asyncio
from copy import deepcopy
from concurrent.futures import Future
from datetime import datetime, timezone
from typing import Protocol
from threading import Event, Lock, RLock, Thread
from uuid import uuid4
import epoptia_read
import epoptia_queries
from dashboard.cache import SnapshotCache
from dashboard.completed_jobs import CompletedJobsTracker
from dashboard.adapter import map_snapshot, timestamp, SNAPSHOT_FRESHNESS_SECONDS

from dashboard.station_activity import StationActivity, STATION_CAPACITY_TARGETS

WORKSTATIONS = tuple(STATION_CAPACITY_TARGETS)
READS = {"production_overview": {"dashboard": True}, "workstation_wip": {"dashboard": True}}
READ_TIMEOUT = 90
OPTIONAL_READ_TIMEOUT = 5


class WholeOrderReader(Protocol):
    """Return a complete version-2 OrderCensus result; support cancellation.

    This is a verified injected reader contract, not a new upstream endpoint.
    """
    async def __call__(self) -> dict | None: ...


from dashboard.completion import completion_count
from time import monotonic


def empty_snapshot(now):
    return dict(observed_at=now.isoformat(),
                workstations=[dict(name=name) for name in WORKSTATIONS],
                urgent_orders=None, today={},
                field_status={"completed_today": "completion_history_unverified"})


SOURCE_ERRORS = frozenset(('login_failed', 'upstream_http_error', 'upstream_timeout',
    'read_unavailable', 'invalid_response', 'invalid_pagination', 'repeated_page',
    'pagination_limit', 'incomplete_order_scan', 'incomplete_station_scan',
    'incomplete_deadline_scan', 'order_contract_mismatch', 'station_contract_mismatch', 'invalid_order_population'))


class SourceError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in SOURCE_ERRORS else 'read_unavailable'
        super().__init__('Dashboard read unavailable')


async def read_local(tool):
    if tool not in READS:
        raise ValueError("Unsupported dashboard read")
    try:
        return await _read_local(tool)
    except (SourceError, TimeoutError):
        raise
    except Exception:
        # Never propagate transport diagnostics or upstream response bodies.
        # Cancellation still propagates so the enclosing deadline stays bounded.
        raise ValueError("Dashboard read unavailable") from None


async def _read_local(tool):
    return await DirectReader()(tool)


class DirectReader:
    """One daemon worker per source; timed-out workers cannot overlap a retry.

    A cancelled scan stops before its next page. An in-flight HTTP request retains
    the existing 20-second bound. No default-executor shutdown blocks the API.
    """
    def __init__(self, on_request=None, tracker=None, clock=None):
        self._lock = Lock()
        self._busy = set()
        self._wol_lock = Lock()
        self._wol_snapshot = None
        self._wol_consumers = set()
        self._wol_at = 0
        self._snapshot_generation = 0
        self._wol_generation = -1
        self.on_request = on_request or (lambda *args: None)
        self.tracker = tracker
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def begin_snapshot(self):
        with self._lock:
            self._snapshot_generation += 1

    def _shared_wols(self, tool, settings, cancelled):
        # Each source may consume a snapshot once, within 30 seconds. Repeated
        # refreshes perform a fresh scan. No global cache or credentials retained.
        with self._wol_lock:
            if cancelled.is_set():
                raise SourceError('upstream_timeout')
            generation = self._snapshot_generation
            if (self._wol_snapshot is None or self._wol_generation != generation
                    or tool in self._wol_consumers
                    or monotonic() - self._wol_at > 30):
                snapshot = epoptia_queries.read_wol_snapshot(
                    settings['base_url'], settings['headers'],
                    routing_snapshot=lambda rows, complete: self._observe_routing(
                        rows, complete=complete, cancelled=cancelled))
                if cancelled.is_set():
                    raise SourceError('upstream_timeout')
                self._wol_snapshot = snapshot
                self._wol_generation = generation
                self._wol_consumers = set()
                self._wol_at = monotonic()
            self._wol_consumers.add(tool)
            return deepcopy(self._wol_snapshot)

    def busy(self, tool):
        with self._lock:
            return tool in self._busy

    def _observe_routing(self, rows, *, complete, cancelled):
        if self.tracker is not None and not cancelled.is_set():
            self.tracker.observe(rows, complete=complete is True, now=self.clock())

    async def __call__(self, tool):
        if tool not in READS:
            raise ValueError('Unsupported dashboard read')
        with self._lock:
            if tool in self._busy:
                raise SourceError('read_unavailable')
            self._busy.add(tool)
        future = Future()
        cancelled = Event()
        read_id = uuid4().hex

        def worker():
            value, error = None, None
            try:
                settings = epoptia_queries.application_settings()
                with epoptia_read.bounded_read(READ_TIMEOUT, cancelled,
                        lambda: self.on_request(tool, read_id)):
                    if tool == 'production_overview':
                        value = epoptia_queries.production_overview(settings['base_url'],
                            username=settings['username'], password=settings['password'],
                            headers=settings['headers'],
                            wol_snapshot=lambda: self._shared_wols(tool, settings, cancelled))
                    else:
                        value = self._shared_wols(tool, settings, cancelled)['stations']
                if not isinstance(value, dict) or value.get('ok') is not True:
                    raise SourceError(value.get('source_error') if isinstance(value, dict) else None)
            except Exception as exc:
                error = exc if isinstance(exc, SourceError) else SourceError('read_unavailable')
            finally:
                with self._lock:
                    self._busy.discard(tool)
            if error:
                future.set_exception(error)
            else:
                future.set_result(value)

        Thread(target=worker, daemon=True, name='dashboard-direct-read').start()
        try:
            # The worker owns no asyncio loop; late completion after cancellation
            # is safe, including when the scheduler uses a fresh loop per refresh.
            while not future.done():
                await asyncio.sleep(0.01)
            return future.result()
        finally:
            cancelled.set()


class LocalEpoptiaProvider:
    """Independent source publications; GET consumers only copy cached state."""
    def __init__(self, read=None, clock=None, whole_orders=None,
                 overdue_orders=None, completed_today=None, cache_dir=None, tracker=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tracker = tracker if tracker is not None else (CompletedJobsTracker() if read is None else None)
        self.read = read if read is not None else DirectReader(self._record_request, self.tracker, self.clock)
        self.whole_orders = whole_orders
        # Retained signature for callers; independent overdue counts are unsafe.
        self.completed_today = completed_today
        self.cache = SnapshotCache(cache_dir)
        self.station_activity = StationActivity(cache_dir)
        self._lock = RLock()
        self._cache_lock = Lock()
        self._running = set()
        self._good = {}
        self.sources = {}
        self.initial_snapshot = None
        stored = self.cache.load('correctness-v2')
        if stored:
            try:
                snapshot = stored['snapshot']
                map_snapshot(snapshot, self.clock())
                self._good = snapshot['source_data']
                self.sources = snapshot['sources']
                for source in self.sources.values():
                    source.update(refreshing=False, stale=True, state='cached')
                self.initial_snapshot = self.core_snapshot()
            except (ValueError, TypeError, KeyError, AttributeError):
                self._good, self.sources = {}, {}

    def _record_request(self, name, read_id):
        # Called only at the actual shared HTTP page boundary, never on GET/cache.
        with self._lock:
            meta = self.sources[name]
            meta['transport'] = 'direct_python'
            meta['http_requests'] = meta.get('http_requests', 0) + 1
            if meta.get('read_id') != read_id:
                meta['read_attempts'] = meta.get('read_attempts', 0) + 1
                meta['read_id'] = read_id

    async def _refresh(self, name, reader, validate, timeout=READ_TIMEOUT):
        with self._lock:
            if name in self._running:
                return
            self._running.add(name)
            meta = self.sources.setdefault(name, dict(attempts=0, generation=0, refresh_sequence=0,
                consecutive_failures=0, last_success=None, last_failure=None, last_failure_reason=None, failure_reason=None))
            meta.update(attempts=meta['attempts']+1, refresh_sequence=meta['refresh_sequence']+1,
                        last_attempt=self.clock().isoformat(), refreshing=True, state='refreshing')
        started = monotonic()
        reason, value = None, None
        try:
            value = await asyncio.wait_for(reader(), timeout)
            value = validate(value)
            if name == 'workstation_wip' and value is not None:
                self.station_activity.observe(value, self.clock())
                value = dict(value, workstations=self.station_activity.decorate(value['workstations']))
            if value is None:
                reason = 'unverified_or_incomplete_source'
        except asyncio.CancelledError:
            reason = 'cancelled'
            raise
        except TimeoutError:
            reason = 'timeout'
        except SourceError as exc:
            reason = exc.reason
        except Exception:
            reason = 'read_unavailable'
        finally:
            now = self.clock().isoformat()
            with self._lock:
                meta.update(duration_ms=round((monotonic()-started)*1000, 3), refreshing=False)
                if reason is None:
                    meta.update(last_success=now, generation=meta['refresh_sequence'], consecutive_failures=0,
                                failure_reason=None, state='available', stale=False)
                    if isinstance(self.read, DirectReader) and name in READS:
                        meta['successful_read_id'] = meta.get('read_id')
                        if name == 'workstation_wip':
                            meta['shared_wol_scan_id'] = value['source'].get('scan_id')
                    self._good[name] = dict(value=value, observed_at=now, generation=meta['generation'])
                else:
                    meta.update(last_failure=now, last_failure_reason=reason, failure_reason=reason,
                                consecutive_failures=meta['consecutive_failures']+1,
                                state='cached' if name in self._good else 'unavailable', stale=True)
                self._running.discard(name)
        with self._cache_lock:
            with self._lock:
                stored = dict(self.core_snapshot(), source_data=deepcopy(self._good))
            self.cache.save('correctness-v2', stored)

    @property
    def refresh_sources(self):
        return (*READS, *(('whole_orders',) if self.whole_orders else ()),
                *(('completed_today',) if self.completed_today else ()))

    async def refresh_core(self, tool):
        if isinstance(self.read, DirectReader) and self.read.busy(tool):
            return
        if tool == 'whole_orders' and self.whole_orders is not None:
            return await self._refresh(tool, self.whole_orders,
                lambda value: value if self._valid_orders(value) else None, READ_TIMEOUT)
        if tool == 'completed_today' and self.completed_today is not None:
            return await self._refresh(tool, self.completed_today,
                lambda value: completion_count(value, self.clock()), OPTIONAL_READ_TIMEOUT)
        if tool not in READS:
            raise ValueError('Unsupported dashboard read')
        def validate(value):
            if not isinstance(value, dict) or value.get('ok') is not True:
                return None
            if tool == 'production_overview':
                projection = value.get('dashboard_orders')
                if not self._valid_orders(projection):
                    source = projection.get('source', {}) if isinstance(projection, dict) else {}
                    if not isinstance(projection, dict) or projection.get('canonical_version') != 2:
                        raise SourceError('order_contract_mismatch')
                    raise SourceError(source.get('status') if source.get('status') not in (None, 'ok')
                                      else 'invalid_order_population' if source.get('complete') is True
                                      else 'incomplete_order_scan')
                return projection
            projection = value.get('dashboard_stations')
            if isinstance(projection, dict):
                if projection.get('station_version') != 1:
                    raise SourceError('station_contract_mismatch')
                source, coverage = projection.get('source', {}), projection.get('coverage', {})
                if (projection.get('complete') is not True or source.get('complete') is not True
                        or source.get('endpoint') != '/api/3.03/workorderlines'
                        or coverage.get('terminal_filtered') is not True
                        or not isinstance(projection.get('workstations'), list)):
                    raise SourceError('incomplete_station_scan')
                return projection
            # Legacy MCP WIP counts include archive/paused and lack row coverage.
            # Only the dashboard raw-row collector can attest terminal filtering.
            from dashboard.stations import collect_stations
            if isinstance(value.get('wol_rows'), list):
                result = collect_stations(value['wol_rows'], value.get('complete') is True)
                return result if result['complete'] else None
            raise SourceError('station_contract_mismatch')
        await self._refresh(tool, lambda: self.read(tool), validate, READ_TIMEOUT)

    @staticmethod
    def _valid_orders(value):
        return (isinstance(value, dict) and value.get('canonical_version') == 2
                and value.get('complete') is True and isinstance(value.get('orders'), list))

    async def refresh_optional(self):
        await asyncio.gather(*(self.refresh_core(name) for name in self.refresh_sources if name not in READS))

    def core_snapshot(self):
        now = self.clock()
        with self._lock:
            good, sources = deepcopy(self._good), deepcopy(self.sources)
        snapshot = empty_snapshot(now)
        from calendar_target_dates import result as calendar_result
        snapshot['calendar_target_dates'] = calendar_result('calendar_auth_missing')
        if self.tracker is not None:
            snapshot['completed_today'], snapshot['completion_tracker'] = self.tracker.summary(now)
        else:
            snapshot['completed_today'] = dict(total=0, breakdown_by_workstation={})
            snapshot['completion_tracker'] = dict(state='pending', reason='awaiting_verified_snapshot')
        snapshot.update(sources=sources, field_observed_at={}, partial=True,
                        loading=not sources, offline=bool(sources) and not good)
        def section(name, fields):
            item = good.get(name)
            meta = sources.get(name, {})
            stale = bool(item and (now-timestamp(item['observed_at'])).total_seconds() > SNAPSHOT_FRESHNESS_SECONDS)
            if name in sources:
                sources[name]['stale'] = stale or meta.get('state') == 'cached'
            for field in fields:
                snapshot['field_status'][field] = ('stale' if stale else meta.get('state', 'unavailable'))
                if item:
                    snapshot['field_observed_at'][field] = item['observed_at']
            return item['value'] if item else None
        order_source = 'whole_orders' if self.whole_orders is not None else 'production_overview'
        orders = section(order_source, ('active_production', 'urgent_orders', 'overdue_work', 'deadline'))
        if orders is not None:
            snapshot['calendar_target_dates'] = orders.get('calendar_target_dates', snapshot['calendar_target_dates'])
            snapshot.update(canonical_orders=orders, urgent_orders=orders['urgent_orders'],
                            order_generation=good[order_source]['generation'])
            snapshot['today']['overdue_work'] = orders['overdue_work']
            if not orders.get('deadline_coverage', {}).get('complete'):
                if snapshot['field_status']['deadline'] == 'available':
                    snapshot['field_status']['deadline'] = 'partial'
            if orders.get('undated_unfinished_orders') or orders.get('unknown_lifecycle_orders'):
                for field in ('urgent_orders', 'overdue_work'):
                    if snapshot['field_status'][field] == 'available':
                        snapshot['field_status'][field] = 'partial'
            snapshot['active_production'] = dict(active_workorders_total=orders['active_workorders_total'],
                native_active_production_progress_percent=orders['native_mean_order_progress_percent'],
                native_progress_coverage_percent=orders['native_progress_coverage_percent'],
                native_active_production_progress_source={'complete': True})
        # Legacy target_date status describes optional calendar enrichment only.
        # Authoritative order deadlines have independent API coverage above.
        snapshot['field_status']['target_date'] = snapshot['calendar_target_dates']['status']
        stations = section('workstation_wip', ('workstations',))
        if stations is not None:
            snapshot['workstations'] = stations['workstations']
        completed = section('completed_today', ('completed_today',))
        snapshot['today']['completed_today'] = completed
        if self.completed_today is None:
            snapshot['field_status']['completed_today'] = 'completion_history_unverified'
        snapshot['station_coverage'] = dict(available=stations is not None,
            reason=None if stations is not None else 'raw_wol_routing_and_complete_terminal_filtered_scan_required',
            source=stations.get('source') if stations else None,
            coverage=stations.get('coverage') if stations else None,
            diagnostics=stations.get('diagnostics') if stations else None)
        snapshot['completion_coverage'] = dict(available=completed is not None,
            reason=None if completed is not None else 'verified_history_reader_not_available_or_incomplete',
            metric='distinct_whole_orders_latest_transition_completed_today_not_reopened', timezone='Europe/Athens')
        snapshot['partial'] = orders is None or stations is None or completed is None or any(
            source.get('stale') for source in sources.values())
        # _good contains validated successful publications. Worker state and
        # optional completion coverage do not determine required-source usability.
        usable = []
        for name in (order_source, 'workstation_wip'):
            item = good.get(name)
            recent = bool(item and 0 <= (now - timestamp(item['observed_at'])).total_seconds()
                          <= SNAPSHOT_FRESHNESS_SECONDS)
            usable.append(recent)
        snapshot['data_status'] = 'online' if all(usable) else 'partial' if any(usable) else 'offline'
        if good:
            snapshot['observed_at'] = min(item['observed_at'] for item in good.values())
        return snapshot

    def optional_snapshot(self):
        # All order-derived metrics publish atomically in core_snapshot.
        return None

    async def snapshot(self):
        if isinstance(self.read, DirectReader):
            self.read.begin_snapshot()
        await asyncio.gather(*(self.refresh_core(tool) for tool in READS), self.refresh_optional())
        return self.core_snapshot()

    def __call__(self):
        return asyncio.run(self.snapshot())
