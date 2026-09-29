"""Internal read-only diagnostic. No HTTP route and no raw payload output."""
from dashboard.provider import DirectReader


async def diagnose_deadlines(workorder_ids):
    """Use normal app provisioning; return only selected parent deadline facts.

    Intended for a separately authorized local live read after activation.
    """
    ids = sorted(set(workorder_ids))
    if not ids or len(ids) > 100 or any(type(key) is not int or not 1 <= key <= 2147483647 for key in ids):
        raise ValueError('Provide 1 to 100 valid workorder IDs')
    try:
        report = (await DirectReader()('production_overview'))['dashboard_orders']
    except Exception:
        return [dict(order_id=key, canonical_progress=None, merged_deadline=None,
                     deadline_reason='read_unavailable', deadline_source_status='read_unavailable')
                for key in ids]
    rows = {row['id']: row for row in report['orders']}
    return [dict(order_id=key, canonical_progress=rows.get(key, {}).get('native_progress'),
                 merged_deadline=rows.get(key, {}).get('deadline'),
                 deadline_reason=rows[key]['deadline_reason'] if key in rows else 'order_not_found',
                 deadline_source_status=report['deadline_scan']['status']) for key in ids]
