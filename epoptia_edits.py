"""Edit EXISTING Epoptia records: work order lines, work orders, products (web UI forms).

Forms found 2026-10-03 on /workorderlines/{id}, /workorders/{id}, /products/{id}:
- line: POST /workorderlines/update/{id} (_method=PUT) with ONE of description / qty /
  completion_date (DD-MM-YYYY); comments by AJAX to the same URL ({comments, _method:put});
  custom fields by AJAX POST /workorderline/customfields {workorderLineId, customFields:{id: value}
  for ALL fields on the form, customElementBomValues:{}} - so unchanged values are re-sent.
- order: POST /workorder-change-completion-date {completion_date, workorder_id};
  POST /workorders/updateclient/{id} {client_id}; comments by AJAX to /workorders/update/{id}.
- product: POST /products/update/{id} (_method=PUT, name); comments by AJAX ({comments, _method:put}).

Every change: the current value must equal what the caller expects (no lost concurrent edit),
one request per field, logged, then the page is read again and the new value confirmed.
Preview unless confirm=True. Nothing here deletes or changes status.
"""
import html
import json
import re

from epoptia_write import WebWriter, WriteError, _clean_text, _redact

DATE = re.compile(r"\d{2}-\d{2}-\d{4}")


def _input_value(text, name):
    for tag in re.findall(r"<input\b[^>]*>", text):
        if re.search(r'\bname="%s"' % re.escape(name), tag):
            value = re.search(r'\bvalue="([^"]*)"', tag)
            return html.unescape(value.group(1)) if value else ""
    return None


def _textarea(text, element_id):
    match = re.search(r'<textarea[^>]*id="%s"[^>]*>(.*?)</textarea>' % re.escape(element_id), text, re.S)
    return html.unescape(match.group(1)) if match else None


def _date_after_label(text):
    match = re.search(r"Ημ\. ολοκλ\. παραγωγής\s*</li>\s*<li[^>]*>\s*(?:<div[^>]*>)?\s*(\d{2}-\d{2}-\d{4})", text)
    return match.group(1) if match else None


def _hidden_json(text, element_id):
    match = re.search(r'id="%s"[^>]*>(.*?)</' % re.escape(element_id), text, re.S)
    value = html.unescape(match.group(1)).strip() if match else ""
    return json.loads(value) if value else {}


def _form_token(text, action_path):
    for form in re.findall(r"<form\b[^>]*>.*?</form>", text, re.S):
        head = re.match(r"<form\b[^>]*>", form).group(0)
        if re.search(r'action="[^"]*%s"' % re.escape(action_path), head):
            token = re.search(r'name="_token"\s+value="([^"]*)"', form)
            if token:
                return token.group(1)
    return None


def line_state(text):
    return dict(description=_input_value(text, "description"), quantity=_input_value(text, "qty"),
                completion_date=_date_after_label(text), comments=_textarea(text, "commentCom"),
                custom_fields={str(k): v for k, v in _hidden_json(text, "customFieldValues").items()},
                field_ids=sorted(set(re.findall(r'class="form-control customField"[^>]*data-id="(\d+)"', text))
                                 | set(re.findall(r'data-id="(\d+)"[^>]*class="form-control customField"', text)),
                                 key=int))


def order_state(text):
    # the order's client is shown as a button link to /clients/{id} in the order header
    clients = set(re.findall(r'<a role="button" href="[^"]*/clients/(\d+)"', text))
    return dict(completion_date=_date_after_label(text), comments=_textarea(text, "commentCom"),
                client_id=int(next(iter(clients))) if len(clients) == 1 else None)


def product_state(text, product_id):
    name = re.search(r"#%d \((.*?)\)</div>" % int(product_id), text)
    fields = {i: html.unescape(v) for i, v in
              re.findall(r'class="form-control customField" data-id="(\d+)" value="([^"]*)"', text)}
    return dict(name=html.unescape(name.group(1)) if name else None, comments=_textarea(text, "commentCom"),
                custom_fields=fields)


class Editor:
    def __init__(self, writer: WebWriter):
        self.w = writer

    # -- plumbing --------------------------------------------------------------------
    def _meta_token(self, text):
        match = re.search(r'<meta name="csrf-token" content="([^"]*)"', text)
        if not match:
            raise WriteError("csrf-token meta missing")
        return match.group(1)

    def _ajax(self, action, path, body, token):
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": token}
        response, parsed = self.w._send(action, path, json_body=dict(body, _token=token), headers=headers)
        location = self.w._same_origin_path(response.headers.get("Location"))
        # Epoptia answers some saves with a redirect even for AJAX; the browser follows it.
        if response.status_code not in (200, 302) or location == "/login":
            raise WriteError(f"{path} answered HTTP {response.status_code}")
        return parsed

    def _form(self, action, path, form, text):
        token = _form_token(text, path)
        if not token:
            raise WriteError(f"form {path} not found on the page")
        response, _ = self.w._send(action, path, form=dict(form, _token=token))
        location = self.w._same_origin_path(response.headers.get("Location"))
        if response.status_code not in (200, 302) or location == "/login":
            raise WriteError(f"{path} answered HTTP {response.status_code}")

    @staticmethod
    def _check_expected(current, expected, changes):
        problems = []
        for field in changes:
            if field not in expected:
                problems.append(f"{field}: give the expected current value")
            elif field == "custom_fields":
                for cf, value in expected["custom_fields"].items():
                    if str(current["custom_fields"].get(str(cf), "")) != str(value):
                        problems.append(f"custom field {cf}: is {current['custom_fields'].get(str(cf))!r}, "
                                        f"expected {value!r}")
            elif str(current.get(field)) != str(expected[field]):
                problems.append(f"{field}: is {current.get(field)!r}, expected {expected[field]!r}")
        return problems

    # -- work order line ----------------------------------------------------------------
    def update_line(self, wol_id, *, expected, changes, confirm=False):
        """changes: description, quantity, completion_date (DD-MM-YYYY), comments, custom_fields {id: value}."""
        action = "edit_line"
        allowed = {"description", "quantity", "completion_date", "comments", "custom_fields"}
        if not changes or set(changes) - allowed:
            raise WriteError(f"changes must be some of {sorted(allowed)}")
        page = f"/workorderlines/{int(wol_id)}"
        _, text = self.w._get_page(page)
        current = line_state(text)
        problems = self._check_expected(current, expected, changes)
        if "description" in changes:
            _clean_text(changes["description"], 2000)
        if "comments" in changes:
            _clean_text(changes["comments"], 4000, allow_empty=True, multiline=True)
        if "quantity" in changes and not (type(changes["quantity"]) in (int, float) and changes["quantity"] > 0):
            problems.append("quantity must be a positive number")
        if "completion_date" in changes and not DATE.fullmatch(str(changes["completion_date"])):
            problems.append("completion_date must be DD-MM-YYYY")
        merged = None
        if "custom_fields" in changes:
            unknown = set(map(str, changes["custom_fields"])) - set(current["field_ids"])
            if unknown:
                problems.append(f"line has no custom fields {sorted(unknown)}")
            merged = {cf: current["custom_fields"].get(cf, "") for cf in current["field_ids"]}
            merged.update({str(k): _clean_text(str(v), 255, allow_empty=True) for k, v in changes["custom_fields"].items()})
        self.w.log(action, "check", page=page, ok=not problems, problems=problems, current=current)
        if problems:
            raise WriteError("; ".join(problems))
        plan = []
        update = f"/workorderlines/update/{int(wol_id)}"
        for field, form_name in (("description", "description"), ("quantity", "qty"), ("completion_date", "completion_date")):
            if field in changes:
                plan.append(("form", update, {"_method": "PUT", form_name: str(changes[field])}))
        if "comments" in changes:
            plan.append(("ajax", update, {"comments": changes["comments"], "_method": "put"}))
        if merged is not None:
            plan.append(("ajax", "/workorderline/customfields",
                         {"workorderLineId": str(int(wol_id)), "customFields": merged, "customElementBomValues": {}}))
        preview = {"line": int(wol_id), "current": current, "requests": [(k, p, _redact(b)) for k, p, b in plan]}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        token = self._meta_token(text)
        for kind, path, body in plan:
            if kind == "form":
                self._form(action, path, body, text)
            else:
                self._ajax(action, path, body, token)
        _, after_text = self.w._get_page(page)
        after = line_state(after_text)
        wanted = dict(changes)
        if merged is not None:
            wanted["custom_fields"] = merged
        ok = all((str(after["custom_fields"].get(cf, "")) == str(v) for cf, v in merged.items()) if f == "custom_fields"
                 else str(after.get(f)).rstrip("0").rstrip(".") == str(v).rstrip("0").rstrip(".") if f == "quantity"
                 else str(after.get(f)) == str(v) for f, v in wanted.items())
        self.w.log(action, "verified" if ok else "unverified", after=after)
        return {"sent": True, "verified": ok, "after": after}

    # -- work order ---------------------------------------------------------------------
    def update_order(self, workorder_id, *, expected, changes, confirm=False):
        """changes: completion_date (DD-MM-YYYY), comments, client_id."""
        action = "edit_order"
        allowed = {"completion_date", "comments", "client_id"}
        if not changes or set(changes) - allowed:
            raise WriteError(f"changes must be some of {sorted(allowed)}")
        page = f"/workorders/{int(workorder_id)}"
        _, text = self.w._get_page(page)
        current = order_state(text)
        problems = self._check_expected(current, expected, changes)
        if "completion_date" in changes and not DATE.fullmatch(str(changes["completion_date"])):
            problems.append("completion_date must be DD-MM-YYYY")
        if "client_id" in changes and type(changes["client_id"]) is not int:
            problems.append("client_id must be an integer")
        self.w.log(action, "check", page=page, ok=not problems, problems=problems, current=current)
        if problems:
            raise WriteError("; ".join(problems))
        plan = []
        if "completion_date" in changes:
            plan.append(("form", "/workorder-change-completion-date",
                         {"completion_date": changes["completion_date"], "workorder_id": str(int(workorder_id))}))
        if "client_id" in changes:
            plan.append(("form", f"/workorders/updateclient/{int(workorder_id)}", {"client_id": str(changes["client_id"])}))
        if "comments" in changes:
            plan.append(("ajax", f"/workorders/update/{int(workorder_id)}", {"comments": changes["comments"], "_method": "put"}))
        preview = {"order": int(workorder_id), "current": current, "requests": [(k, p, _redact(b)) for k, p, b in plan]}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        token = self._meta_token(text)
        for kind, path, body in plan:
            self._form(action, path, body, text) if kind == "form" else self._ajax(action, path, body, token)
        _, after_text = self.w._get_page(page)
        after = order_state(after_text)
        ok = all(str(after.get(f)) == str(v) for f, v in changes.items())
        self.w.log(action, "verified" if ok else "unverified", after=after)
        return {"sent": True, "verified": ok, "after": after}

    # -- product ------------------------------------------------------------------------
    def update_product(self, product_id, *, expected, changes, confirm=False):
        """changes: name, comments, custom_fields ({field_id: value}; all fields are re-sent)."""
        action = "edit_product"
        if not changes or set(changes) - {"name", "comments", "custom_fields"}:
            raise WriteError("changes must be name, comments and/or custom_fields")
        page = f"/products/{int(product_id)}"
        _, text = self.w._get_page(page)
        current = product_state(text, product_id)
        problems = self._check_expected(current, expected, changes)
        if "name" in changes:
            _clean_text(changes["name"], 150)
        self.w.log(action, "check", page=page, ok=not problems, problems=problems, current=current)
        if problems:
            raise WriteError("; ".join(problems))
        update = f"/products/update/{int(product_id)}"
        plan = []
        if "name" in changes:
            plan.append(("form", update, {"_method": "PUT", "name": changes["name"]}))
        if "comments" in changes:
            plan.append(("ajax", update, {"comments": changes["comments"], "_method": "put"}))
        if "custom_fields" in changes:
            unknown = set(map(str, changes["custom_fields"])) - set(current["custom_fields"])
            if unknown:
                raise WriteError(f"product has no custom field(s) {sorted(unknown)}")
            values = dict(current["custom_fields"], **{str(k): str(v) for k, v in changes["custom_fields"].items()})
            plan.append(("ajax", "/product/customfields/values", {"productId": int(product_id), "customFields": values}))
        preview = {"product": int(product_id), "current": current, "requests": [(k, p, _redact(b)) for k, p, b in plan]}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        token = self._meta_token(text)
        for kind, path, body in plan:
            self._form(action, path, body, text) if kind == "form" else self._ajax(action, path, body, token)
        _, after_text = self.w._get_page(page)
        after = product_state(after_text, product_id)
        ok = all(str(after.get(f)) == str(v) for f, v in changes.items() if f != "custom_fields")
        if "custom_fields" in changes:
            ok = ok and all(after["custom_fields"].get(str(k)) == str(v) for k, v in changes["custom_fields"].items())
        self.w.log(action, "verified" if ok else "unverified", after=after)
        return {"sent": True, "verified": ok, "after": after}


def assign_workflow_keeping_values(writer, product_id, template_id, *, replace_from, confirm=False):
    """Change a product's workflow and write its product-level custom-field values back
    (Epoptia drops them on a workflow change). Refused if a field would not exist afterwards."""
    editor = Editor(writer)
    _, text = writer._get_page(f"/products/{int(product_id)}")
    before = product_state(text, product_id)
    saved = {k: v for k, v in before["custom_fields"].items() if v.strip()}
    if not confirm:
        preview = writer.assign_workflow(product_id, template_id, replace_from=replace_from, values_to_restore=saved)
        return dict(preview, restore=saved)
    result = writer.assign_workflow(product_id, template_id, replace_from=replace_from,
                                    values_to_restore=saved, confirm=True)
    if not result.get("assigned"):
        return dict(result, restored=False, restore=saved)
    if not saved:
        return dict(result, restored=True, restore={})
    _, after_text = writer._get_page(f"/products/{int(product_id)}")
    now = product_state(after_text, product_id)["custom_fields"]
    missing = sorted(set(saved) - set(now))
    if missing:
        writer.log("assign_workflow_keeping_values", "values_lost", product=int(product_id), missing=missing, values=saved)
        raise WriteError(f"fields {missing} do not exist on the new workflow; values were: {saved}")
    edit = editor.update_product(product_id, expected={"custom_fields": {k: now[k] for k in saved}},
                                 changes={"custom_fields": saved}, confirm=True)
    return dict(result, restored=edit["verified"], restore=saved)
