"""Epoptia writes.

Part 1 — fail-closed gateway contracts: no network, browser or credential loading.

A future reviewed transport must bind these relative routes to the authenticated
Epoptia origin, reject redirects, require a session and CSRF protection, and read
the current field immediately before writing. The gateway has no write transport.

Part 2 — WebWriter: reviewed writes through an Epoptia web session (CLI only,
preview unless --confirm). See the section comment below.
"""
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
import html
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote, urlsplit

import epoptia_throttle


@dataclass(frozen=True)
class Contract:
    id_field: str
    expected_field: str
    new_field: str
    route: str
    form_field: str
    limit: int


CONTRACTS = MappingProxyType({
    "update_product_name": Contract("product_id", "expected_current_name", "new_name",
                                    "/products/update/{id}", "name", 255),
    "update_wol_description": Contract("wol_id", "expected_current_description", "new_description",
                                       "/workorderlines/update/{id}", "description", 2000),
})


def validate_write(name, arguments):
    contract = CONTRACTS[name]
    if type(arguments) is not dict or set(arguments) != {
            contract.id_field, contract.expected_field, contract.new_field}:
        raise ValueError("Invalid write arguments")
    identifier = arguments[contract.id_field]
    if type(identifier) is not int or not 1 <= identifier <= 999999999999999:
        raise ValueError("Invalid target")
    for field in (contract.expected_field, contract.new_field):
        value = arguments[field]
        if (type(value) is not str or not value.strip() or len(value) > contract.limit
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("Invalid field value")
    return contract


async def execute_write(name, arguments, *, read_transport=None):
    """Return only audit-safe labels. The optional transport is read-only.

    read_transport is an internal future integration seam, never caller input:
    authenticated and csrf_ready are booleans; read_current(action, id) returns
    the exact current string. Even a valid session and matching read cannot
    enable writes: this module has no send/POST method or success path.
    """
    contract = validate_write(name, arguments)
    status = "auth_required"
    try:
        if (read_transport is not None and read_transport.authenticated is True
                and read_transport.csrf_ready is True):
            current = await read_transport.read_current(name, arguments[contract.id_field])
            if type(current) is not str:
                status = "read_unverified"
            elif current != arguments[contract.expected_field]:
                status = "conflict"
            else:
                status = "write_transport_unverified"
    except Exception:
        # Never echo transport exceptions, raw records, headers or session data.
        status = "read_unverified"
    return {"ok": False, "status": status, "action": name,
            "target_id": arguments[contract.id_field], "write_performed": False}


# ---------------------------------------------------------------------------
# Web-session write transport (Epoptia UI endpoints, see docs/epoptia_form_map.md).
#
# Separate from the read tools and from the gateway contracts above. Every
# request goes through epoptia_throttle.call(); every send is logged to
# logs/epoptia_writes.log before and after. Before each write the live form is
# re-read and compared with the documented field map: any difference stops the
# write. Nothing is sent unless the caller passes confirm=True. No retries.
# ---------------------------------------------------------------------------
WRITE_LOG = Path(__file__).resolve().parent / "logs" / "epoptia_writes.log"
PRODUCT_NAME_LIMIT = 150


@dataclass(frozen=True)
class FormSpec:
    page: str
    form_id: str
    method: str
    action: str | None                 # path on the Epoptia origin, None = no action
    fields: tuple                      # ((key, type, required), ...); key = name or "#id"
    radio_values: tuple = ()           # ((name, (values...)), ...)


PRODUCT_CREATE = FormSpec(
    "/product/create", "productCreateForm", "POST", "/products/store",
    (("_token", "hidden", False), ("product_type", "radio", False), ("name", "text", True),
     ("is_active", "checkbox", False), ("#imageUploadMaterial", "file", False),
     ("infosupplier[]", "text", False), ("infoprice[]", "number", False)),
    (("product_type", ("Product", "Recipe", "Service", "Material")),))

PRODUCT_DELETE = FormSpec(
    "/product/create", "productDeleteForm", "POST", "/products/destroy/0",
    (("_method", "hidden", False), ("_token", "hidden", False)))

PRODUCT_ASSIGN_WORKFLOW = FormSpec(
    "/products/{id}", "assignFromTemplate", "POST", "/product/workflow/from-template",
    (("_token", "hidden", False), ("sectionId", "hidden", False), ("templateId", "hidden", False)))

CLIENT_CREATE = FormSpec(
    "/client/create", "clientCreateForm", "POST", "/clients/store",
    (("_token", "hidden", False), ("tag", "radio", False), ("name", "text", True),
     ("email", "email", False), ("city", "text", False), ("comments", "text", False),
     ("show_comments", "checkbox", False), ("phone_number", "text", False),
     ("vat_number", "text", False), ("vat_rate", "number", False)),
    (("tag", ("Client", "Supplier", "ClientSupplier")),))

CLIENT_DELETE = FormSpec(
    "/client/create", "clientDeleteForm", "POST", "/clients/destroy/0",
    (("_method", "hidden", False), ("_token", "hidden", False)))

WOL_DELETE = FormSpec(
    "/workorders/{id}", "workordelineDeleteForm", "POST", "/workorderlines/remove/0",
    (("_method", "hidden", False), ("_token", "hidden", False)))

WORKORDER_HEAD = FormSpec(
    "/workorders/create", "workorderForm", "GET", None,
    (("client", "select", True), ("productionDate", "text", True),
     ("workorderCode", "text", False), ("workOrderComment", "textarea", False)))

WORKORDER_LINE = FormSpec(
    "/workorders/create", "workorderLineForm", "GET", None,
    (("#wolCode", "text", False), ("product", "select", True),
     ("#workorderLineDescription", "text", True), ("#quantity", "number", True),
     ("tag", "select", False), ("#workorderLineComment", "textarea", False),
     ("#tmpWorkorderLineId", "hidden", False)))

# The workorder page submits by JavaScript; these snippets (whitespace removed)
# pin the AJAX routes and JSON keys the UI sends. {base} = Epoptia origin.
WORKORDER_SCRIPT_MARKERS = (
    'ajax("{base}/workorders/store",{',
    'client:$("#clientSelect").val(),',
    'comments:$("#workOrderComment").val(),',
    'productionDate:$("#productionDateSelect").val(),',
    'workorderCode:$("#workorderCode").val(),',
    'ajax("{base}/workorderlines/store",{',
    'workorderId:response.id,',
    'workorderLines:workorderLinesArray,',
    'description:workorderLines[$(workorderlineRows[i]).data().id].description,',
    'product:workorderLines[$(workorderlineRows[i]).data().id].product,',
    'quantity:workorderLines[$(workorderlineRows[i]).data().id].quantity,',
    'wolCode:workorderLines[$(workorderlineRows[i]).data().id].wolCode,',
)


class WriteError(Exception):
    """A write was refused or failed; nothing further was sent."""


class FormChanged(WriteError):
    """The live form differs from docs/epoptia_form_map.md."""


class PartialWorkorder(WriteError):
    """/workorders/store succeeded but /workorderlines/store did not."""

    def __init__(self, workorder_id, reason):
        super().__init__(f"workorder {workorder_id} created without lines: {reason}")
        self.workorder_id = workorder_id
        self.reason = reason


class _PageParser(HTMLParser):
    """Collect forms (by id), their fields, product list rows and the CSRF meta."""

    def __init__(self):
        super().__init__()
        self.forms = {}
        self.all_forms = []            # every form, including those without an id
        self.csrf_meta = None
        self.rows = []
        self.info_items = []           # JSON records from <div class="delete…Info d-none">
        self._info = None
        self._form = None
        self._row = None
        self._name_div = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") == "csrf-token":
            self.csrf_meta = a.get("content")
        elif tag == "form":
            self._form = dict(method=(a.get("method") or "GET").upper(), action=a.get("action"),
                              fields={}, radios={}, token=None, method_override=None)
            self.all_forms.append(self._form)
            if a.get("id"):
                self.forms[a["id"]] = self._form
        elif tag in ("input", "select", "textarea") and self._form is not None:
            kind = (a.get("type") or "text").lower() if tag == "input" else tag
            key = a.get("name") or (("#" + a["id"]) if a.get("id") else None)
            if key is None:
                return
            if kind == "radio":
                self._form["radios"].setdefault(key, []).append(a.get("value"))
            if key == "_token" and kind == "hidden":
                self._form["token"] = a.get("value")
            if kind == "hidden":
                self._form.setdefault("hidden_values", {})[key] = a.get("value")
            if key == "_method" and kind == "hidden":
                self._form["method_override"] = a.get("value")
            self._form["fields"].setdefault(key, (kind, "required" in a))
        elif tag == "div" and re.fullmatch(r"delete\w+Info d-none", a.get("class") or ""):
            self._info = ""
        elif tag == "tr":
            self._row = {"id": None, "name": None}
        elif self._row is not None:
            if tag == "input" and (a.get("id") or "").startswith("check-") and a.get("data-id"):
                self._row["id"] = a["data-id"]
            elif tag == "div" and "text-truncate" in (a.get("class") or "") and self._row["name"] is None:
                self._name_div = True
                self._row["name"] = ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "div" and self._info is not None:
            try:
                record = json.loads(self._info)
                if isinstance(record, dict) and type(record.get("id")) is int:
                    self.info_items.append((record["id"], record.get("name")))
            except ValueError:
                pass
            self._info = None
        elif tag == "div" and self._name_div:
            self._name_div = False
        elif tag == "tr" and self._row is not None:
            if self._row["id"] and self._row["id"].isdigit():
                self.rows.append((int(self._row["id"]), " ".join((self._row["name"] or "").split())))
            self._row = None

    def handle_data(self, data):
        if self._info is not None:
            self._info += data
        if self._name_div:
            self._row["name"] += data


def _now():
    return datetime.now(timezone.utc).isoformat()


def _redact(value):
    if isinstance(value, dict):
        return {k: ("<csrf>" if k == "_token" else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _clean_text(value, limit, *, allow_empty=False, multiline=False):
    if type(value) is not str or len(value) > limit or (not allow_empty and not value.strip()):
        raise WriteError("invalid text value")
    if any((ord(c) < 32 and not (multiline and c == "\n")) or ord(c) == 127 for c in value):
        raise WriteError("control characters are not allowed")
    return value


_GREEK_LOOKALIKES = str.maketrans("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "ABEZHIKMNOPTYX")


def name_key(name):
    """Compare product names ignoring case, accents, Greek/Latin look-alikes and spacing."""
    import unicodedata
    text = "".join(c for c in unicodedata.normalize("NFD", name.upper()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[\s\-.]+", "", text.translate(_GREEK_LOOKALIKES))


def backup_similar_products(name, db_path=Path.home() / "epoptia-backup" / "epoptia.sqlite"):
    """Products in the nightly backup whose normalized name equals this one (no Epoptia request)."""
    import sqlite3
    if not Path(db_path).exists():
        return None
    key = name_key(name)
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT id, json_extract(data, '$.name') FROM records "
                          "WHERE kind='product' AND gone_since IS NULL").fetchall()
    return [(i, n) for i, n in rows if n and name_key(n) == key]


class WebWriter:
    """One authenticated Epoptia web session used only for reviewed writes."""

    def __init__(self, base_url, username, password, *, session=None, login=None,
                 log_path=WRITE_LOG):
        parts = urlsplit(base_url or "")
        if parts.scheme != "https" or not parts.netloc:
            raise WriteError("EPOPTIA_BASE_URL must be an https origin")
        self.base = f"{parts.scheme}://{parts.netloc}"
        self._username = username
        self._password = password
        self._session = session
        self._login = login
        self.log_path = Path(log_path)
        self.authenticated = False

    # -- plumbing ----------------------------------------------------------
    def log(self, action, phase, **fields):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _now(), "action": action, "phase": phase, **_redact(fields)}
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _ensure_login(self):
        if self.authenticated:
            return
        if self._session is None:
            import requests
            self._session = requests.Session()
        login = self._login
        if login is None:
            from epoptia_read import _web_login as login
        if not login(self._session, self.base, self._username, self._password):
            raise WriteError("Epoptia web login failed")
        self.authenticated = True

    def _get_page(self, path):
        self._ensure_login()
        response = epoptia_throttle.call(
            self._session.get, self.base + path, timeout=60, allow_redirects=False,
            headers={"Accept": "text/html,application/xhtml+xml"})
        if response.status_code != 200 or "html" not in response.headers.get("Content-Type", ""):
            raise WriteError(f"GET {urlsplit(path).path} answered HTTP {response.status_code}")
        parser = _PageParser()
        parser.feed(response.text)
        return parser, response.text

    def _same_origin_path(self, url):
        if url is None:
            return None
        parts = urlsplit(url)
        if parts.netloc and f"{parts.scheme}://{parts.netloc}" != self.base:
            return "<foreign-origin>"
        return parts.path

    def _check_form(self, parser, spec):
        """Return a list of differences between the live form and the spec."""
        form = parser.forms.get(spec.form_id)
        if form is None:
            return [f"form #{spec.form_id} missing"]
        problems = []
        if form["method"] != spec.method:
            problems.append(f"method {form['method']} != {spec.method}")
        if self._same_origin_path(form["action"]) != spec.action:
            problems.append(f"action {form['action']!r} != {spec.action!r}")
        live = {key: value for key, value in form["fields"].items()}
        expected = {key: (kind, required) for key, kind, required in spec.fields}
        for key in sorted(set(live) | set(expected)):
            if live.get(key) != expected.get(key):
                problems.append(f"field {key}: live={live.get(key)} documented={expected.get(key)}")
        for name, values in spec.radio_values:
            if tuple(form["radios"].get(name, ())) != values:
                problems.append(f"radio {name}: live={form['radios'].get(name)} documented={values}")
        return problems

    def verify_forms(self, action, specs, *, script_markers=()):
        """GET the page, compare every spec and marker; stop on any difference."""
        page = specs[0].page
        parser, text = self._get_page(page)
        problems = []
        for spec in specs:
            problems += self._check_form(parser, spec)
        compact = re.sub(r"\s+", "", text)
        for marker in script_markers:
            if marker.replace("{base}", self.base) not in compact:
                problems.append(f"script marker missing: {marker}")
        self.log(action, "form_check", page=page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        return parser

    def _send(self, action, path, *, form=None, json_body=None, headers=None):
        """One logged write request. Timeouts/halts are logged and re-raised, never retried."""
        self.log(action, "request", method="POST", path=path, payload=form if form is not None else json_body)
        try:
            response = epoptia_throttle.call(
                self._session.post, self.base + path, write=True, timeout=60,
                allow_redirects=False, data=form, json=json_body, headers=headers or {})
        except epoptia_throttle.EpoptiaHalted as exc:
            self.log(action, "halted", path=path, error=str(exc))
            raise
        except Exception as exc:
            self.log(action, "error", path=path, error=type(exc).__name__,
                     note="outcome unknown - check Epoptia before any further write")
            raise WriteError(f"POST {path} failed ({type(exc).__name__}); outcome unknown") from None
        body = None
        if "json" in response.headers.get("Content-Type", ""):
            try:
                body = response.json()
            except ValueError:
                body = None
        self.log(action, "response", path=path, status=response.status_code,
                 location=response.headers.get("Location"),
                 body=body if body is not None else response.text[:300])
        return response, body

    # -- products ----------------------------------------------------------
    def find_products(self, name):
        """Exact-name matches from the product list search: [(id, name), ...]."""
        parser, _ = self._get_page("/product/create?term=" + quote(name))
        return [row for row in parser.rows if row[1] == name]

    def newest_product_id(self):
        parser, _ = self._get_page("/product/create?sortby=id&type=desc")
        return max((row[0] for row in parser.rows), default=None)

    def product_page_name(self, product_id):
        """Name from the /products/{id} breadcrumb "#id (name)"; inactive products show here."""
        parser, text = self._get_page(f"/products/{int(product_id)}")
        match = re.search(r"#%d \((.*?)\)</div>" % int(product_id), text)
        return match.group(1) if match else None

    def product_delete_info(self, parser):
        form = parser.forms.get(PRODUCT_DELETE.form_id) or {}
        return {"delete_form": not self._check_form(parser, PRODUCT_DELETE),
                "route": "POST /products/destroy/{id}",
                "method_override": form.get("method_override")}

    def create_product(self, name, *, active=False, confirm=False):
        action = "create_product"
        _clean_text(name, PRODUCT_NAME_LIMIT)
        parser = self.verify_forms(action, [PRODUCT_CREATE, PRODUCT_DELETE])
        token = parser.forms[PRODUCT_CREATE.form_id]["token"]
        if not token:
            raise FormChanged("productCreateForm has no CSRF token")
        existing = self.find_products(name)
        similar = backup_similar_products(name)
        existing = existing + [row for row in (similar or []) if row[0] not in {e[0] for e in existing}]
        payload = {"_token": token, "product_type": "Product", "name": name}
        if active:
            payload["is_active"] = "on"
        payload["infosupplier[]"] = ""
        payload["infoprice[]"] = ""
        preview = {"method": "POST", "path": PRODUCT_CREATE.action,
                   "content_type": "application/x-www-form-urlencoded",
                   "payload": _redact(payload), "existing_same_name": existing,
                   "delete": self.product_delete_info(parser)}
        if existing:
            self.log(action, "refused", reason="product with this name already exists", existing=existing)
            raise WriteError(f"product named {name!r} already exists: {existing}")
        if not confirm:
            self.log(action, "preview", **preview)
            return {"sent": False, **preview}
        newest = self.newest_product_id()
        response, _ = self._send(action, PRODUCT_CREATE.action, form=payload)
        location = self._same_origin_path(response.headers.get("Location"))
        if response.status_code != 302 or location in (None, "<foreign-origin>", "/login"):
            self.log(action, "failed", status=response.status_code, location=location)
            raise WriteError(f"products/store answered HTTP {response.status_code} -> {location}")
        created = self.find_products(name)
        if not created and newest is not None:
            # The product list hides inactive products; look at the next ids directly.
            for candidate in range(newest + 1, newest + 4):
                try:
                    if self.product_page_name(candidate) == name:
                        created = [(candidate, name)]
                        break
                except WriteError:
                    continue
        self.log(action, "verified" if len(created) == 1 else "unverified", matches=created)
        return {"sent": True, "status": response.status_code, "location": location, "matches": created}

    @staticmethod
    def _custom_fields(values, index):
        if type(values) is not dict:
            raise WriteError(f"line {index}: customFields must be an object")
        out = {}
        for key, value in values.items():
            if not str(key).isdigit():
                raise WriteError(f"line {index}: custom field id {key!r} is not numeric")
            out[str(int(key))] = _clean_text(value, 255)
        return out

    def product_custom_field_ids(self, product_id):
        """Custom field ids defined on a product (from its /products/{id} page)."""
        _, text = self._get_page(f"/products/{int(product_id)}")
        return sorted({int(i) for i in re.findall(r'class="form-control customField" data-id="(\d+)"', text)})

    def workflow_templates(self):
        """{id: name} of the workflow templates offered for products."""
        _, text = self._get_page("/product/create")
        start = text.find('id="workflow_all"')
        block = text[start:text.find("</select>", start)] if start >= 0 else ""
        return {int(v): " ".join(html.unescape(n).split())
                for v, n in re.findall(r'<option[^>]*value="(\d+)"[^>]*>(.*?)</option>', block, re.S)
                if int(v) > 0}

    def assign_workflow(self, product_id, template_id, *, confirm=False, replace_from=None):
        """Give a product a workflow template. A product that already has one is refused unless
        replace_from = its current template id (the page warns that product settings are lost;
        we additionally refuse if any product-level custom-field value is set)."""
        action = "assign_workflow"
        if type(product_id) is not int or product_id < 1 or type(template_id) is not int or template_id < 1:
            raise WriteError("invalid product or template id")
        templates = self.workflow_templates()
        if template_id not in templates:
            raise WriteError(f"workflow template {template_id} not offered")
        spec = PRODUCT_ASSIGN_WORKFLOW
        page = spec.page.format(id=product_id)
        parser, text = self._get_page(page)
        problems = self._check_form(parser, spec)
        form = parser.forms.get(spec.form_id) or {}
        if str((form.get("hidden_values") or {}).get("sectionId")) != str(product_id):
            problems.append("sectionId does not match the product")
        if 'id="templatePickerEmptyWorkflow"' not in text:
            if replace_from is None:
                problems.append("product already has a workflow (would overwrite its settings)")
            else:
                if f"/product/{product_id}/workflow/{int(replace_from)}" not in text:
                    problems.append(f"product's current workflow is not {replace_from}")
                saved = [(i, v) for i, v in re.findall(r'class="form-control customField" data-id="(\d+)" value="([^"]*)"', text) if v.strip()]
                if saved:
                    problems.append(f"product has saved custom-field values that would be lost: {saved}")
        elif replace_from is not None:
            problems.append("product has no workflow; replace_from must not be given")
        self.log(action, "form_check", page=page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        payload = {"_token": form["token"], "sectionId": str(product_id), "templateId": str(template_id)}
        preview = {"method": "POST", "path": spec.action, "content_type": "application/x-www-form-urlencoded",
                   "payload": _redact(payload), "template_name": templates[template_id]}
        if not confirm:
            self.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self._send(action, spec.action, form=payload)
        location = self._same_origin_path(response.headers.get("Location"))
        if response.status_code != 302 or location in (None, "<foreign-origin>", "/login"):
            self.log(action, "failed", status=response.status_code, location=location)
            raise WriteError(f"workflow/from-template answered HTTP {response.status_code} -> {location}")
        _, text = self._get_page(page)
        assigned = (f"/product/{product_id}/workflow/{template_id}" in text
                    and 'id="templatePickerEmptyWorkflow"' not in text)
        fields = sorted({int(i) for i in re.findall(r'class="form-control customField" data-id="(\d+)"', text)})
        self.log(action, "verified" if assigned else "unverified", custom_field_ids=fields)
        return {"sent": True, "status": response.status_code, "location": location,
                "assigned": assigned, "custom_field_ids": fields}

    # -- deletes (test data clean-up) ---------------------------------------
    def _delete(self, action, spec, page_parser, route, target, *, confirm):
        """Shared delete: verified form, _method=DELETE, 302 expected; preview unless confirm."""
        problems = self._check_form(page_parser, spec)
        form = page_parser.forms.get(spec.form_id) or {}
        if form.get("method_override") != "DELETE":
            problems.append("delete form lost _method=DELETE")
        self.log(action, "form_check", page=spec.page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        payload = {"_method": "DELETE", "_token": form["token"]}
        preview = {"method": "POST", "path": route, "payload": _redact(payload), "target": target}
        if not confirm:
            self.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self._send(action, route, form=payload)
        location = self._same_origin_path(response.headers.get("Location"))
        if response.status_code != 302 or location in (None, "<foreign-origin>", "/login"):
            self.log(action, "failed", status=response.status_code, location=location)
            raise WriteError(f"{route} answered HTTP {response.status_code} -> {location}")
        return {"sent": True, "status": response.status_code, "location": location, "target": target}

    def delete_workorderline(self, workorder_id, wol_id, expected_description, *, confirm=False):
        page = WOL_DELETE.page.format(id=int(workorder_id))
        parser, text = self._get_page(page)
        lines = {}
        for blob in re.findall(r'<div class="deleteWorkorderlineInfo d-none">(.*?)</div>', text, re.S):
            record = json.loads(html.unescape(blob))
            lines[record.get("id")] = record
        line = lines.get(int(wol_id))
        if line is None or line.get("workorder_id") != int(workorder_id):
            raise WriteError(f"line {wol_id} is not on work order {workorder_id}")
        if line.get("description") != expected_description:
            raise WriteError(f"line {wol_id} is {line.get('description')!r}, not {expected_description!r}")
        spec = FormSpec(page, WOL_DELETE.form_id, WOL_DELETE.method, WOL_DELETE.action, WOL_DELETE.fields)
        result = self._delete("delete_workorderline", spec, parser, f"/workorderlines/remove/{int(wol_id)}",
                              {"workorder": int(workorder_id), "line": int(wol_id),
                               "description": expected_description}, confirm=confirm)
        if result["sent"]:
            try:
                _, after = self._get_page(page)
                result["verified"] = f'&quot;id&quot;:{int(wol_id)},' not in after and f'"id":{int(wol_id)},' not in after
            except WriteError:
                # Epoptia removes an order whose last line was deleted; its page then redirects.
                result["verified"] = True
                result["order_removed"] = True
            self.log("delete_workorderline", "verified" if result["verified"] else "unverified", line=int(wol_id))
        return result

    def set_wol_status(self, workorder_id, wol_id, status, expected_description, *, confirm=False):
        """Change a work order line's status (standby / production / archive), as the order page does."""
        action = "set_wol_status"
        if status not in ("standby", "production", "archive"):
            raise WriteError("status must be standby, production or archive")
        # Started lines have no delete info on the order page, so read the line's own page.
        _, line_text = self._get_page(f"/workorderlines/{int(wol_id)}")
        found = re.search(r'<input[^>]*name="description"[^>]*value="([^"]*)"', line_text) or \
            re.search(r'<input[^>]*value="([^"]*)"[^>]*name="description"', line_text)
        if not found or html.unescape(found.group(1)) != expected_description:
            raise WriteError(f"line {wol_id} is not {expected_description!r}")
        if f"/workorders/{int(workorder_id)}" not in line_text:
            raise WriteError(f"line {wol_id} is not on work order {workorder_id}")
        page = f"/workorders/{int(workorder_id)}"
        parser, text = self._get_page(page)
        form = parser.forms.get(f"statusForm-{int(wol_id)}") or {}
        problems = []
        if self._same_origin_path(form.get("action")) != "/workorderline/set-status" or form.get("method") != "POST":
            problems.append("status form changed")
        if set(form.get("fields", {})) != {"_token", "id", "status"}:
            problems.append(f"status form fields changed: {sorted(form.get('fields', {}))}")
        self.log(action, "form_check", page=page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        payload = {"_token": form["token"], "id": str(int(wol_id)), "status": status}
        preview = {"method": "POST", "path": "/workorderline/set-status", "payload": _redact(payload)}
        if not confirm:
            self.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self._send(action, "/workorderline/set-status", form=payload)
        if response.status_code not in (200, 302):
            raise WriteError(f"set-status answered HTTP {response.status_code}")
        return {"sent": True, "status": response.status_code}

    def finish_wol_step(self, wol_id, element_id, expected_station, *, confirm=False, mode="finish"):
        """Complete one step by hand ("Ολοκλήρωση", mode=finish) or reset its progress
        ("Επανεργασία" for the whole quantity, mode=zero) - the line page's zeroing-finish form."""
        if mode not in ("finish", "zero"):
            raise WriteError("mode must be finish or zero")
        action = "finish_wol_step" if mode == "finish" else "zero_wol_step"
        page = f"/workorderlines/{int(wol_id)}"
        parser, text = self._get_page(page)
        form = parser.forms.get("zeroFinishForm") or {}
        problems = []
        if self._same_origin_path(form.get("action")) != "/workorderline/zeroing-finish" or form.get("method") != "POST":
            problems.append("zeroing-finish form changed")
        hidden = form.get("hidden_values") or {}
        if str(hidden.get("workorderLineId")) != str(int(wol_id)) or "elementId" not in form.get("fields", {}):
            problems.append("zeroing-finish form does not belong to this line")
        if f'value="{mode}"' not in text:
            problems.append(f"{mode} button not found")
        match = re.search(r'id="workflowServer"[^>]*>(.*?)</', text, re.S)
        elements = json.loads(html.unescape(match.group(1)))["elements"] if match else {}
        element = elements.get(str(int(element_id))) if isinstance(elements, dict) else None
        if element is None or (element.get("workstation") or {}).get("name") != expected_station:
            problems.append(f"step {element_id} is not {expected_station!r} on line {wol_id}")
        self.log(action, "form_check", page=page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        payload = {"_token": form["token"], "workorderLineId": str(int(wol_id)),
                   "elementId": str(int(element_id)), "action": mode}
        preview = {"method": "POST", "path": "/workorderline/zeroing-finish", "payload": _redact(payload)}
        if not confirm:
            self.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self._send(action, "/workorderline/zeroing-finish", form=payload)
        if response.status_code not in (200, 302):
            raise WriteError(f"zeroing-finish answered HTTP {response.status_code}")
        return {"sent": True, "status": response.status_code,
                "location": self._same_origin_path(response.headers.get("Location"))}

    def delete_product(self, product_id, expected_name, *, confirm=False):
        _, text = self._get_page(f"/products/{int(product_id)}")
        match = re.search(r"#%d \((.*?)\)</div>" % int(product_id), text)
        current = match.group(1) if match else None
        if current != expected_name:
            raise WriteError(f"product {product_id} is {current!r}, not {expected_name!r}")
        if "No workorderlines with this product" not in text:
            raise WriteError(f"product {product_id} is still used by work order lines; delete those first")
        parser, _ = self._get_page(PRODUCT_DELETE.page)
        return self._delete("delete_product", PRODUCT_DELETE, parser, f"/products/destroy/{int(product_id)}",
                            {"product": int(product_id), "name": expected_name}, confirm=confirm)

    def delete_client(self, client_id, expected_name, *, confirm=False):
        if (int(client_id), expected_name) not in self.find_clients(expected_name):
            raise WriteError(f"client {client_id} named {expected_name!r} not found")
        parser, _ = self._get_page(CLIENT_DELETE.page)
        result = self._delete("delete_client", CLIENT_DELETE, parser, f"/clients/destroy/{int(client_id)}",
                              {"client": int(client_id), "name": expected_name}, confirm=confirm)
        if result["sent"]:
            # Epoptia answers 302 even when it silently refuses (e.g. a client that had orders).
            result["verified"] = (int(client_id), expected_name) not in self.find_clients(expected_name)
            self.log("delete_client", "verified" if result["verified"] else "unverified", client=int(client_id))
        return result

    # -- clients -----------------------------------------------------------
    def find_clients(self, name):
        """Exact-name matches from the contact list search: [(id, name), ...]."""
        parser, _ = self._get_page("/client/create?term=" + quote(name))
        return [item for item in parser.info_items if item[1] == name]

    def create_client(self, name, *, confirm=False):
        action = "create_client"
        _clean_text(name, 50)
        parser = self.verify_forms(action, [CLIENT_CREATE, CLIENT_DELETE])
        token = parser.forms[CLIENT_CREATE.form_id]["token"]
        if not token:
            raise FormChanged("clientCreateForm has no CSRF token")
        existing = self.find_clients(name)
        payload = {"_token": token, "tag": "Client", "name": name, "email": "", "city": "",
                   "comments": "", "phone_number": "", "vat_number": "", "vat_rate": ""}
        preview = {"method": "POST", "path": CLIENT_CREATE.action,
                   "content_type": "application/x-www-form-urlencoded",
                   "payload": _redact(payload), "existing_same_name": existing}
        if existing:
            self.log(action, "refused", reason="contact with this name already exists", existing=existing)
            raise WriteError(f"contact named {name!r} already exists: {existing}")
        if not confirm:
            self.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self._send(action, CLIENT_CREATE.action, form=payload)
        location = self._same_origin_path(response.headers.get("Location"))
        if response.status_code != 302 or location in (None, "<foreign-origin>", "/login"):
            self.log(action, "failed", status=response.status_code, location=location)
            raise WriteError(f"clients/store answered HTTP {response.status_code} -> {location}")
        created = self.find_clients(name)
        self.log(action, "verified" if len(created) == 1 else "unverified", matches=created)
        return {"sent": True, "status": response.status_code, "location": location, "matches": created}

    # -- work orders -------------------------------------------------------
    def create_workorder(self, client_id, production_date, lines, *, workorder_code="",
                         comments="", confirm=False):
        """Two AJAX calls as the UI does; a failed second call raises PartialWorkorder."""
        action = "create_workorder"
        if type(client_id) is not int or client_id < 1:
            raise WriteError("invalid client id")
        if type(production_date) is not str or not re.fullmatch(r"\d{2}-\d{2}-\d{4}", production_date):
            raise WriteError("production_date must be DD-MM-YYYY")
        _clean_text(workorder_code, 255, allow_empty=True)
        _clean_text(comments, 2000, allow_empty=True, multiline=True)
        if type(lines) is not list or not lines:
            raise WriteError("at least one line is required")
        wol = []
        for index, line in enumerate(lines, 1):
            if type(line) is not dict or not {"product", "description", "quantity"} <= set(line) \
                    or set(line) - {"product", "description", "quantity", "tag", "comments", "wolCode",
                                    "customFields"}:
                raise WriteError(f"line {index}: invalid keys")
            if type(line["product"]) is not int or line["product"] < 1:
                raise WriteError(f"line {index}: invalid product id")
            if type(line["quantity"]) not in (int, float) or not line["quantity"] > 0:
                raise WriteError(f"line {index}: invalid quantity")
            wol.append({"description": _clean_text(line["description"], 2000),
                        "product": line["product"], "quantity": line["quantity"],
                        "tag": line.get("tag"),
                        "comments": _clean_text(line.get("comments", ""), 4000, allow_empty=True,
                                                multiline=True),
                        "frontId": index,
                        "customFieldsValues": self._custom_fields(line.get("customFields", {}), index),
                        "wolCode": _clean_text(line.get("wolCode", ""), 255, allow_empty=True),
                        "bomValues": {}})
        for index, line in enumerate(wol, 1):
            if line["customFieldsValues"]:
                known = self.product_custom_field_ids(line["product"])
                unknown = sorted(set(map(int, line["customFieldsValues"])) - set(known))
                if unknown:
                    raise WriteError(f"line {index}: product {line['product']} has no custom fields {unknown}"
                                     f" (has {known})")
        parser = self.verify_forms(action, [WORKORDER_HEAD, WORKORDER_LINE],
                                   script_markers=WORKORDER_SCRIPT_MARKERS)
        token = parser.csrf_meta
        if not token:
            raise FormChanged("csrf-token meta missing")
        head = {"client": client_id, "comments": comments, "productionDate": production_date,
                "workorderCode": workorder_code, "_token": token}
        body_lines = {"workorderId": "<id from step 1>", "workorderLines": wol,
                      "productionDate": production_date, "_token": token}
        preview = {"step1": {"method": "POST", "path": "/workorders/store", "json": _redact(head)},
                   "step2": {"method": "POST", "path": "/workorderlines/store", "json": _redact(body_lines)}}
        if not confirm:
            self.log(action, "preview", **preview)
            return {"sent": False, **preview}
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                   "X-CSRF-TOKEN": token}
        response, body = self._send(action, "/workorders/store", json_body=head, headers=headers)
        workorder_id = body.get("id") if isinstance(body, dict) else None
        if response.status_code != 200 or type(workorder_id) is not int:
            self.log(action, "failed", step="workorders/store", status=response.status_code)
            raise WriteError(f"workorders/store answered HTTP {response.status_code} without an id")
        body_lines["workorderId"] = workorder_id
        try:
            response, body = self._send(action, "/workorderlines/store", json_body=body_lines,
                                        headers=headers)
            mapping = body.get("workorderLines") if isinstance(body, dict) else None
            if response.status_code != 200 or not isinstance(mapping, dict) or not mapping:
                raise WriteError(f"HTTP {response.status_code}, no workorderLines in response")
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            self.log(action, "partial_workorder", workorder_id=workorder_id, reason=reason,
                     note="workorder exists WITHOUT lines - tell the user")
            raise PartialWorkorder(workorder_id, reason) from None
        self.log(action, "done", workorder_id=workorder_id, workorder_lines=mapping)
        return {"sent": True, "workorder_id": workorder_id, "workorder_lines": mapping}


def run_order_plan(writer, plan, *, confirm=False):
    """Whole order: client -> new products (+workflow) -> one work order with all lines.

    plan = {"client": {"id": int} | {"create": name}, "date": "DD-MM-YYYY", "code": "",
            "comments": "", "new_products": {key: {"name", "workflow_id", "active"}},
            "lines": [{"product": id | "new:key", "description", "quantity", "comments",
                       "customFields"}]}
    Preview (confirm=False) sends nothing. Any failure stops before the next step.
    """
    report = {"steps": []}
    client = plan["client"]
    if "id" in client:
        client_id = client["id"]
    else:
        result = writer.create_client(client["create"], confirm=confirm)
        report["steps"].append({"client": result})
        if confirm:
            if len(result.get("matches", [])) != 1:
                raise WriteError("new client not verified; stopping before products")
            client_id = result["matches"][0][0]
        else:
            client_id = None
    product_ids = {}
    for key, spec in plan.get("new_products", {}).items():
        result = writer.create_product(spec["name"], active=spec.get("active", True), confirm=confirm)
        report["steps"].append({"product": key, "create": result})
        if not confirm:
            report["steps"].append({"product": key, "workflow": {"template_id": spec["workflow_id"],
                                                                 "sent": False}})
            continue
        if len(result.get("matches", [])) != 1:
            raise WriteError(f"new product {key} not verified; stopping before the work order")
        product_ids[key] = result["matches"][0][0]
        flow = writer.assign_workflow(product_ids[key], spec["workflow_id"], confirm=True)
        report["steps"].append({"product": key, "workflow": flow})
        if not flow.get("assigned"):
            raise WriteError(f"workflow not verified on product {product_ids[key]}; stopping")
    lines = []
    for line in plan["lines"]:
        line = dict(line)
        if isinstance(line["product"], str) and line["product"].startswith("new:"):
            key = line["product"][4:]
            if not confirm:
                line["product"] = f"<new product {key}>"
                lines.append(line)
                continue
            line["product"] = product_ids[key]
        lines.append(line)
    if not confirm:
        report["steps"].append({"workorder": {"client_id": client_id or "<new client>", "date": plan["date"],
                                              "code": plan.get("code", ""), "lines": lines, "sent": False}})
        return report
    result = writer.create_workorder(client_id, plan["date"], lines, workorder_code=plan.get("code", ""),
                                     comments=plan.get("comments", ""), confirm=True)
    report["steps"].append({"workorder": result})
    return report


def _writer_from_env():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
    return WebWriter(os.getenv("EPOPTIA_BASE_URL"), os.getenv("EPOPTIA_USERNAME"),
                     os.getenv("EPOPTIA_PASSWORD"))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Reviewed Epoptia web writes (preview by default).")
    sub = parser.add_subparsers(dest="command", required=True)
    product = sub.add_parser("create-product")
    product.add_argument("--name", required=True)
    product.add_argument("--active", action="store_true", help="default: inactive")
    product.add_argument("--confirm", action="store_true", help="actually send (else preview only)")
    client = sub.add_parser("create-client")
    client.add_argument("--name", required=True)
    client.add_argument("--confirm", action="store_true")
    flow = sub.add_parser("assign-workflow")
    flow.add_argument("--product-id", type=int, required=True)
    flow.add_argument("--template-id", type=int, required=True)
    flow.add_argument("--confirm", action="store_true")
    delete = sub.add_parser("delete", help="delete one test record (name must match)")
    delete.add_argument("--kind", choices=("workorderline", "product", "client"), required=True)
    delete.add_argument("--id", type=int, required=True)
    delete.add_argument("--expect", required=True, help="exact current name/description")
    delete.add_argument("--workorder", type=int, help="work order id (for --kind workorderline)")
    delete.add_argument("--confirm", action="store_true")
    order_plan = sub.add_parser("create-order", help="whole order from a JSON plan (see run_order_plan)")
    order_plan.add_argument("--plan", required=True)
    order_plan.add_argument("--confirm", action="store_true")
    spec = sub.add_parser("create-workorder-spec", help="work order from a JSON spec file")
    spec.add_argument("--file", required=True, help='{"client_id", "date", "code", "comments", "lines": [...]}')
    spec.add_argument("--confirm", action="store_true")
    order = sub.add_parser("create-workorder", help="one work order with one line")
    order.add_argument("--client-id", type=int, required=True)
    order.add_argument("--date", required=True, help="production date DD-MM-YYYY")
    order.add_argument("--product-id", type=int, required=True)
    order.add_argument("--description", required=True)
    order.add_argument("--quantity", type=float, required=True)
    order.add_argument("--code", default="")
    order.add_argument("--comments", default="")
    order.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    writer = _writer_from_env()
    try:
        if args.command == "create-product":
            result = writer.create_product(args.name, active=args.active, confirm=args.confirm)
        elif args.command == "assign-workflow":
            result = writer.assign_workflow(args.product_id, args.template_id, confirm=args.confirm)
        elif args.command == "delete":
            if args.kind == "workorderline":
                if not args.workorder:
                    raise WriteError("--workorder is required for a work order line")
                result = writer.delete_workorderline(args.workorder, args.id, args.expect, confirm=args.confirm)
            elif args.kind == "product":
                result = writer.delete_product(args.id, args.expect, confirm=args.confirm)
            else:
                result = writer.delete_client(args.id, args.expect, confirm=args.confirm)
        elif args.command == "create-order":
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            result = run_order_plan(writer, plan, confirm=args.confirm)
        elif args.command == "create-workorder-spec":
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            result = writer.create_workorder(
                data["client_id"], data["date"], data["lines"], workorder_code=data.get("code", ""),
                comments=data.get("comments", ""), confirm=args.confirm)
        elif args.command == "create-client":
            result = writer.create_client(args.name, confirm=args.confirm)
        else:
            quantity = int(args.quantity) if args.quantity.is_integer() else args.quantity
            result = writer.create_workorder(
                args.client_id, args.date,
                [{"product": args.product_id, "description": args.description, "quantity": quantity}],
                workorder_code=args.code, comments=args.comments, confirm=args.confirm)
    except PartialWorkorder as exc:
        print(f"ΠΡΟΣΟΧΗ: {exc}", file=sys.stderr)
        return 3
    except (WriteError, epoptia_throttle.EpoptiaHalted) as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
