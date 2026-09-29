"""Import-safe shared query composition for existing tools and dashboard reads.

No configuration or network access occurs at import time.
"""
import epoptia_read
from uuid import uuid4


def application_settings():
    """Use the same dotenv/environment provisioning as the existing MCP app."""
    import os
    from dotenv import load_dotenv
    load_dotenv()
    return dict(base_url=os.getenv('EPOPTIA_BASE_URL'),
                headers={'X-Auth-Token': os.getenv('EPOPTIA_API_KEY'), 'Accept': 'application/json'},
                username=os.getenv('EPOPTIA_USERNAME'), password=os.getenv('EPOPTIA_PASSWORD'))


def production_overview(base_url, *, username=None, password=None, result=None, headers=None, wol_snapshot=None,
                        routing_snapshot=None):
    """Enrich the legacy summary or collect the dashboard-only native census."""
    result = {'ok': True} if result is None else result
    from dashboard.orders import OrderCensus
    census = OrderCensus()
    routing_rows = []

    def consume(rows):
        census.consume(rows)
        if routing_snapshot is not None:
            routing_rows.extend(rows)

    result.update(epoptia_read.active_production_progress(
        base_url, username=username, password=password,
        consume_rows=consume))
    if routing_snapshot is not None:
        # Raw rows precede active-order filtering and contain all routing states.
        # Never publish a partial page population as a completion baseline.
        routing_snapshot(routing_rows,
                         complete=result['native_active_production_progress_source']['complete'])
    census.deadline_scan.update(result['native_active_production_progress_source'])
    if headers is not None and result['native_active_production_progress_source']['complete']:
        snapshot = wol_snapshot() if wol_snapshot is not None else read_wol_snapshot(base_url, headers)
        census.consume_deadlines(snapshot['deadlines'])
    result['dashboard_orders'] = census.result(
        result['native_active_production_progress_source']['complete'])
    from calendar_target_dates import order_targets, result as calendar_result
    calendar = result.get('calendar_target_dates', calendar_result('calendar_auth_missing'))
    result['dashboard_orders']['calendar_target_dates'] = calendar
    for order in result['dashboard_orders']['orders']:
        order.update(order_targets(order.get('order_id'), calendar))
    result['dashboard_orders']['source'] = dict(
        result['native_active_production_progress_source'],
        endpoint='/capacity-planning/workorderlines', method='POST',
        rows_read=census.rows)
    result['dashboard_orders']['sources'] = dict(
        canonical_progress=dict(result['dashboard_orders']['source']),
        parent_deadlines=dict(census.deadline_scan))
    return result


def read_wol_snapshot(base_url, headers, *, routing_snapshot=None):
    """Reuse the WOL scan for stations and linked target_day date candidates."""
    from dashboard.stations import collect_stations
    scan, deadlines, rows = {}, {}, None
    try:
        rows = epoptia_read.fetch_wols(base_url, headers, scan=scan)
    except epoptia_read.ReadError:
        station = {'ok': False, 'source_error': scan.get('status', 'read_unavailable')}
    else:
        # Completion tracking depends on the verified REST population, not on
        # successful deadline/station projection. Publish before either consumer.
        if routing_snapshot is not None and scan.get('complete') is True:
            routing_snapshot(rows, complete=True)
        from dashboard.orders import linked_wol_deadline
        for row in rows:
            key, due = linked_wol_deadline(row)
            if key is not None and due is not None:
                # Every distinct date can become the next deadline as time advances.
                deadlines.setdefault(key, set()).add(due)
        result = collect_stations(rows, scan['complete'])
        station = {'ok': True, 'dashboard_stations': result}
    scan['scan_id'] = uuid4().hex
    if station['ok']:
        station['dashboard_stations']['source'] = dict(scan)
    return dict(wol_rows=rows, complete=scan.get('complete') is True, stations=station, deadlines=dict(
        source=dict(scan, reader='epoptia_read.fetch_wols', path='target_day'),
        dates=deadlines if scan.get('complete') else {}))


def workstation_census(base_url, headers):
    return read_wol_snapshot(base_url, headers)['stations']
