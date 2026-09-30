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


def _clean_text(value, limit, *, allow_empty=False):
    if type(value) is not str or len(value) > limit or (not allow_empty and not value.strip()):
        raise WriteError("invalid text value")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise WriteError("control characters are not allowed")
    return value


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
        _clean_text(comments, 2000, allow_empty=True)
        if type(lines) is not list or not lines:
            raise WriteError("at least one line is required")
        wol = []
        for index, line in enumerate(lines, 1):
            if type(line) is not dict or not {"product", "description", "quantity"} <= set(line) \
                    or set(line) - {"product", "description", "quantity", "tag", "comments", "wolCode"}:
                raise WriteError(f"line {index}: invalid keys")
            if type(line["product"]) is not int or line["product"] < 1:
                raise WriteError(f"line {index}: invalid product id")
            if type(line["quantity"]) not in (int, float) or not line["quantity"] > 0:
                raise WriteError(f"line {index}: invalid quantity")
            wol.append({"description": _clean_text(line["description"], 2000),
                        "product": line["product"], "quantity": line["quantity"],
                        "tag": line.get("tag"),
                        "comments": _clean_text(line.get("comments", ""), 2000, allow_empty=True),
                        "frontId": index, "customFieldsValues": {},
                        "wolCode": _clean_text(line.get("wolCode", ""), 255, allow_empty=True),
                        "bomValues": {}})
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
