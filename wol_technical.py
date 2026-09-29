"""Bounded, read-only projection of embedded WOL/product specifications.

Unknown fields are accepted only inside explicitly technical containers. No
additional endpoints are fetched. Paths preserve units and competing values.
"""
import math
from itertools import islice
import re
import unicodedata


def key(value):
    text = unicodedata.normalize('NFKD', str(value)).casefold()
    return ''.join(c for c in text if c.isalnum())


GROUPS = {
    'length': ('length', 'lengthmm', 'μηκος'),
    'width': ('width', 'widthmm', 'πλατος'),
    'depth': ('depth', 'depthmm', 'βαθος'),
    'height': ('height', 'heightmm', 'υψος'),
    'model': ('model', 'modelname', 'μοντελο'),
    'code': ('code', 'productcode', 'modelcode', 'sku', 'κωδικος'),
    'material': ('material', 'materials', 'υλικο'),
    'color': ('color', 'colour', 'χρωμα'),
    'finish': ('finish', 'surfacefinish'),
    'refrigeration': ('refrigeration', 'refrigerant', 'cooling', 'temperature', 'temperaturerange', 'compressor', 'ψυξη'),
    'electrical': ('electrical', 'voltage', 'power', 'wattage', 'frequency', 'current', 'phase'),
    'glass': ('glass', 'glazing', 'τζαμι'),
    'doors': ('doors', 'door', 'doorcount', 'πορτες'),
    'drawers': ('drawers', 'drawer', 'drawercount', 'συρταρια'),
    'options': ('options', 'accessories', 'extras'),
    'weight': ('weight', 'netweight', 'grossweight'),
    'capacity': ('capacity', 'volume'),
    'shelves': ('shelves', 'shelfcount'),
    'insulation': ('insulation', 'insulationthickness'),
    'dimensions': ('dimensions', 'dimension', 'διαστασεις'),
}
ALIASES = {key(alias): group for group, aliases in GROUPS.items() for alias in aliases}
TECH = {key(s) for s in ('technical_details', 'specifications', 'technicalCharacteristics',
        'technical_fields', 'attributes', 'characteristics', 'specs', 'customFields')}
PRODUCT = {'product', 'productmaster', 'productdetails', 'item', 'article'}
WRAPPERS = {'details', 'data', 'wol', 'workorderline', 'custom', 'customdetails'}
TEXT = ('description', 'comment', 'notes', 'note', 'remarks', 'remark',
        'specialconstruction', 'περιγραφ', 'παρατηρη', 'ειδικηκατασκευη')
DENY = ('auth', 'token', 'secret', 'password', 'credential', 'private', 'url',
        'uri', 'link', 'endpoint', 'host', 'email', 'phone', 'address', 'customer',
        'client', 'employee', 'operator', 'contact', 'person', 'owner', 'user',
        'apikey', 'cookie', 'header', 'payment', 'bank', 'salary')
EMBEDDED_DENY = DENY + ('passwd', 'pwd', 'session', 'connection', 'environment',
                       'credential', 'ssh', 'certificate')
UNSAFE_TEXT = re.compile(
    r'(?i)(?:[a-z][a-z0-9+.-]*://|www\.|\b(?:localhost|\d{1,3}(?:\.\d{1,3}){3})\b|'
    r'[\w.+-]+@[\w.-]+|\b(?:bearer|basic)\s+\S+|'
    r'(?:password|token|secret|api[_ -]?key|authorization|credential)\s*[:=]|'
    r'-----BEGIN|\beyJ[\w-]+\.[\w-]+|\b(?:sk|ghp|github_pat)[_-][\w-]+|'
    r'\b[\w-]+\.(?:internal|local|com|net|org)\b)')


def safe(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and value.strip():
        # Reject the entire value before truncation, including trailing secrets.
        if UNSAFE_TEXT.search(value) or re.search(r'\+?\d[\d ()-]{8,}\d', value):
            return None
        return ''.join(c for c in value[:2000] if c.isprintable() or c in '\n\t')
    return None


def technical_details(row, *, include_embedded=False):
    """WOL overrides defaults for every WOL, including custom/Ειδικό rows.

    Limits: depth 8, 2,000 visited nodes, 200 leaves, 50 entries/container,
    2,000 characters/value and 160/path. Truncation is reported explicitly.
    Values withheld by safety filtering are never echoed.
    """
    sources = {s: {'specifications': {}, 'descriptions': {}, 'extra_technical_fields': {}}
               for s in ('product', 'wol')}
    count = [0, 0]
    truncated = [False]
    denied = EMBEDDED_DENY if include_embedded else DENY

    def walk(value, path=(), source='wol', technical=False, group=None, description=False, depth=0):
        count[0] += 1
        if depth > 8 or count[0] > 2000 or count[1] >= 200:
            truncated[0] = True
            return
        if isinstance(value, dict):
            if len(value) > 50:
                truncated[0] = True
            # Named attributes, e.g. {name: 'Width', value: 600, unit: 'mm'}.
            label = value.get('name', value.get('key', value.get('label')))
            if technical and isinstance(label, str) and 'value' in value:
                normalized = key(label)
                if len(label) <= 80 and safe(label) is not None and not any(s in normalized for s in denied):
                    walk(value['value'], path + (label,), source, True,
                         ALIASES.get(normalized, group), description, depth + 1)
                    if 'unit' in value:
                        walk(value['unit'], path + (label, 'unit'), source, True, None, False, depth + 1)
                return
            for name, child in islice(value.items(), 50):
                if not isinstance(name, str) or len(name) > 80 or safe(name) is None:
                    continue
                normalized = key(name)
                if any(s in normalized for s in denied):
                    continue
                next_source = 'product' if normalized in PRODUCT else source
                next_group = ALIASES.get(normalized, group)
                is_text = description or any(s in normalized for s in TEXT)
                is_tech = (technical or normalized in TECH or next_group is not None
                           or (include_embedded and normalized in PRODUCT | {'custom', 'customdetails'}))
                if is_text or is_tech or normalized in PRODUCT | WRAPPERS:
                    walk(child, path + (name,), next_source, is_tech, next_group, is_text, depth + 1)
        elif isinstance(value, list):
            if len(value) > 50:
                truncated[0] = True
            for index, child in enumerate(value[:50]):
                walk(child, path + (str(index),), source, technical, group, description, depth + 1)
        else:
            cleaned = safe(value)
            location = '.'.join(path)
            if cleaned is None or len(location) > 160:
                return
            bucket = 'descriptions' if description else 'specifications' if group else 'extra_technical_fields'
            if bucket == 'extra_technical_fields' and not technical:
                return
            target = sources[source][bucket]
            if bucket == 'specifications':
                target = target.setdefault(group, {})
            target[location] = cleaned
            count[1] += 1
            if isinstance(value, str) and len(value) > 2000:
                truncated[0] = True

    walk(row)
    effective = {}
    for source in ('product', 'wol'):
        for group, values in sources[source]['specifications'].items():
            # Unit metadata stays in the source maps, never becomes a dimension.
            candidates = [v for p, v in values.items() if key(p.split('.')[-1]) not in ('unit', 'units')]
            if candidates:
                effective[group] = {'value': candidates[0] if len(candidates) == 1 else candidates,
                                    'source': source}
    return {'specifications': effective, 'sources': sources,
            'extra_technical_fields': {s: sources[s]['extra_technical_fields'] for s in sources},
            'truncated': truncated[0]}


def summary_fields(row):
    specs = technical_details(row)['specifications']
    def compact(name):
        value = specs.get(name, {}).get('value')
        if isinstance(value, list):
            value = value[0] if value else None
        return value[:120] if isinstance(value, str) else value
    return {'dimensions': {n: compact(n) for n in ('length', 'width', 'depth', 'height') if n in specs},
            'model': compact('model'), 'code': compact('code')}
