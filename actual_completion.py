"""Conservative parent-page reader; never return page text or WOL dates."""
from datetime import datetime
from html.parser import HTMLParser
import re


ACTUAL_LABEL = 'Ημερομηνία ολοκλήρωσης παραγωγής'


class _Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []
        self.parts = []

    def text(self):
        return ''.join(p.text() if isinstance(p, _Node) else p for p in self.parts)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.current = _Node()
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.current)
        self.nodes.append(node)
        if len(self.nodes) > 50000:
            raise ValueError('Page limit')
        self.current.children.append(node)
        self.current.parts.append(node)
        if tag not in ('input', 'br', 'hr', 'img', 'meta', 'link', 'area',
                       'base', 'embed', 'param', 'source', 'track', 'wbr'):
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                break
            node = node.parent

    def handle_data(self, data):
        self.current.parts.append(data)


def completion_result(workorder_id, reason):
    return dict(date=None, source_endpoint=f'/workorders/{workorder_id}',
                verified_label=ACTUAL_LABEL, reason=reason)


_DATE_FORMATS = ((r'[0-9]{4}-[0-9]{2}-[0-9]{2}', '%Y-%m-%d'),
                 (r'[0-9]{2}-[0-9]{2}-[0-9]{4}', '%d-%m-%Y'))


def _value(node):
    return ((node.attrs.get('value') or '') if node.tag in ('input', 'select')
            else node.text()).strip()


def _date_like(value):
    return value == '-' or any(re.fullmatch(pattern, value) for pattern, _ in _DATE_FORMATS)


def _parsed(value):
    for pattern, fmt in _DATE_FORMATS:
        if re.fullmatch(pattern, value):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                return None
    return None


def _dom_diagnostics(labels, candidates):
    """Count all associations; bound lists to 20 and exclude page text.

    Values and parsed dates are filtered lists, not positional descriptor maps.
    """
    diagnostics = dict(exact_label_occurrences=len(labels), candidate_count=len(candidates),
                       candidate_values=[], parsed_date_candidates=[], candidates=[])
    tags = {'div', 'tr', 'td', 'th', 'li', 'p', 'dl', 'dt', 'dd', 'span',
            'label', 'input', 'select', 'form', 'section', 'fieldset', 'body', 'table', 'tbody'}

    def identifier(value, limit):
        return value if isinstance(value, str) and re.fullmatch(
            r'[A-Za-z_][A-Za-z0-9_-]{0,' + str(limit - 1) + '}', value) else None

    for node in candidates[:20]:
        parent = node.parent
        descriptor = dict(
            parent_tag=parent.tag if parent.tag in tags else 'other',
            parent_classes=[token for token in (parent.attrs.get('class') or '').split()[:8]
                            if identifier(token, 48)],
            sibling_or_control_type=node.tag if node.tag in tags else 'other')
        name = identifier(node.attrs.get('name'), 64) if node.tag == 'input' else None
        if name is not None:
            descriptor['input_name'] = name
        diagnostics['candidates'].append(descriptor)
        value = _value(node)
        if _date_like(value):
            diagnostics['candidate_values'].append(value)
            parsed = _parsed(value)
            if parsed is not None:
                diagnostics['parsed_date_candidates'].append(parsed)
    return diagnostics


def parse_actual_completion(html, workorder_id):
    """Accept exact label/value pairs, never nearby dates or script payloads.

    Search only the nearest field/row, including nested display and controls.
    Duplicate labels and distinct local values fail closed.
    verified_label names the required label; reason records missing evidence.
    """
    result = completion_result(workorder_id, 'label_missing')
    result['dom_diagnostics'] = _dom_diagnostics([], [])
    if not isinstance(html, str) or len(html) > 2000000:
        result['reason'] = 'invalid_page'
        return result
    try:
        page = _Page()
        page.feed(html)
        page.close()

        def visible(node):
            while node.parent is not None:
                if (node.tag in ('script', 'style', 'template', 'noscript')
                        or 'hidden' in node.attrs
                        or node.attrs.get('aria-hidden') == 'true'
                        or node.attrs.get('type', '').lower() == 'hidden'):
                    return False
                node = node.parent
            return True

        labels = [n for n in page.nodes if visible(n)
                  and n.text().strip() == ACTUAL_LABEL
                  and not any(c.text().strip() == ACTUAL_LABEL for c in n.children)]

        def candidate_nodes(label):
            anchor = label
            # Cross label-only wrappers, stopping at explicit field boundaries.
            while (anchor.parent.parent is not None
                   and anchor.parent.tag not in ('tr', 'li', 'p', 'dl')
                   and (anchor.parent.tag != 'div' or any(
                       token.startswith('col-') for token in
                       (anchor.parent.attrs.get('class') or '').split())
                       or anchor.parent.parent.tag == 'tr'
                       or {'row', 'form-group'}.intersection(
                           (anchor.parent.parent.attrs.get('class') or '').split()))
                   and not {'row', 'form-group'}.intersection(
                       (anchor.parent.attrs.get('class') or '').split())
                   and len(anchor.parent.children) == 1
                   and anchor.parent.text().strip() == ACTUAL_LABEL):
                anchor = anchor.parent
            container = anchor.parent
            roots = container.children
            if container.tag not in ('div', 'tr', 'td', 'th', 'li', 'p', 'dl', 'span') and not (
                    {'row', 'form-group'}.intersection((container.attrs.get('class') or '').split())):
                # Standalone label-for controls must be immediate neighbors;
                # never search a body/form or follow an ID across the page.
                index = roots.index(anchor)
                roots = [node for node in roots[max(0, index - 1):index + 2]
                         if anchor.tag == 'label' and anchor.attrs.get('for')
                         and node.tag in ('input', 'select')
                         and node.attrs.get('id') == anchor.attrs['for']]
            if anchor.tag == 'dt':
                index = roots.index(anchor)
                roots = roots[index + 1:index + 2]
                if not roots or roots[0].tag != 'dd':
                    return []
            local = []

            def collect(node):
                if not visible(node):
                    return
                if node is not anchor and node.text().strip() != ACTUAL_LABEL:
                    if node.tag == 'tr' or {'row', 'form-group'}.intersection(
                            (node.attrs.get('class') or '').split()):
                        return
                    if node.tag in ('label', 'th', 'dt') or 'Ημ. ολοκλ. παραγωγής' in node.text():
                        return
                    if any(c.tag == 'label' and c.text().strip() != ACTUAL_LABEL
                           for c in node.children):
                        return
                if node.tag in ('input', 'select'):
                    if _date_like(_value(node)):
                        local.append(node)
                    return
                for child in node.children:
                    collect(child)
                if not node.children and _date_like(_value(node)):
                    local.append(node)

            for node in roots:
                if node is not anchor and (node.tag in ('label', 'th', 'dt')
                        or node.text().strip() == 'Ημ. ολοκλ. παραγωγής'):
                    break
                collect(node)

            def distance(node):
                ancestors = {}
                current = label
                while current is not None:
                    ancestors[current] = len(ancestors)
                    current = current.parent
                steps = 0
                while node not in ancestors:
                    steps += 1
                    node = node.parent
                return steps + ancestors[node]

            # Includes label-for controls only when inside this same scope.
            return sorted(local, key=distance)

        candidates = [node for label in labels for node in candidate_nodes(label)]
        result['dom_diagnostics'] = _dom_diagnostics(labels, candidates)
        if not labels:
            return result
        if len(labels) != 1:
            result['reason'] = 'ambiguous_label'
            return result
        values = {_parsed(_value(node)) or _value(node) for node in candidates}
        if len(values) != 1 or (values != {'-'} and not _parsed(next(iter(values)))):
            result['reason'] = 'ambiguous_value'
        elif values == {'-'}:
            result['reason'] = 'not_completed'
        else:
            result.update(date=next(iter(values)), reason=None)
    except (ValueError, RecursionError):
        result['reason'] = 'invalid_page'
    return result
