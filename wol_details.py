"""Read-only details enhancement, loaded independently of core WOL queries."""
from datetime import datetime
import re

# Preserve existing Python imports as well as the public tool response schema.
from wol_technical import safe, technical_details, summary_fields


# Finite root-only allowlist; no substring matching or nested discovery.
COMPLETION_FIELDS = tuple(
    name
    for prefix in ('', 'db', 'display', 'production')
    for suffix in ('completionDate', 'completionTimestamp', 'completionTime', 'completedAt')
    for name in (
        prefix + suffix[0].upper() + suffix[1:] if prefix else suffix,
        (prefix + '_' if prefix else '') + re.sub(r'([A-Z])', r'_\1', suffix).lower(),
    )
)


def completion_details(row):
    """Keep exact root keys and raw scalars, bounded to 32 keys/128 chars each."""
    fields = {}
    for name in COMPLETION_FIELDS:
        if name not in row:
            continue
        value = row[name]
        if value is None:
            fields[name] = None
        elif isinstance(value, str):
            if len(value) > 128:
                continue
            # Numeric date strings can trigger the generic phone-number filter.
            date_text = re.fullmatch(
                r'(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4})'
                r'(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?'
                r'(?:Z|[+-]\d{2}:?\d{2})?)?', value)
            if value == '' or date_text or safe(value) == value:
                fields[name] = value
        elif type(value) in (int, float) and safe(value) is not None:
            fields[name] = value
    return {'raw_fields': fields}


def wol_details(rows, wol_id):
    """Project one WOL using only supplied metadata and embedded technical data."""
    if type(wol_id) is not int or not 1 <= wol_id <= 9223372036854775807:
        raise ValueError('wol_id must be a positive 64-bit integer')
    row = next((row for row in rows
                if str(row.get('workorderline_id')) == str(wol_id)), None)
    if row is None:
        return {'found': False, 'workorderline_id': wol_id,
                'message': 'Work Order Line not found'}
    fields = {name: safe(row.get(name)) for name in
              ('description', 'quantity', 'target_day', 'production_status', 'state')}
    # Valid ISO dates resemble phone numbers to the generic text filter.
    target = row.get('target_day')
    effective_date = None
    if isinstance(target, str) and re.fullmatch(
            r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?', target):
        try:
            datetime.fromisoformat(target.replace('Z', '+00:00'))
        except ValueError:
            pass
        else:
            fields['target_day'] = target
            effective_date = datetime.fromisoformat(target.replace('Z', '+00:00')).date().isoformat()
    client = row.get('client')
    fields['client'] = safe(client.get('name') if isinstance(client, dict) else client)
    details = technical_details(row, include_embedded=True)
    from calendar_target_dates import wol_target
    return {'found': True, 'workorderline_id': wol_id, **fields, **wol_target(wol_id),
            # target_date remains the optional calendar value for compatibility.
            # Use the API's date component; do not infer a calendar fallback.
            'effective_target_date': effective_date,
            'effective_target_date_status': 'ok' if effective_date else 'missing_or_invalid_target_day',
            'effective_target_date_provenance': {
                'endpoint': '/api/3.03/workorderlines', 'path': 'target_day',
                'derivation': 'date_component', 'verified': effective_date is not None},
            'completion': completion_details(row),
            'technical_details': details,
            'truncated': details['truncated'] or any(
                isinstance(value, str) and len(value) > 2000 for value in
                [*(row.get(name) for name in fields if name != 'client'),
                 client.get('name') if isinstance(client, dict) else client])}
