"""WebWriter against synthetic pages: form checks, preview gate, logging, partial workorders."""
import html
import json
import tempfile
import unittest
from pathlib import Path

import epoptia_write as w

BASE = "https://epoptia.example"

PRODUCT_FORM = f"""
<form action="{BASE}/products/store" id="productCreateForm" method="post">
 <input type="hidden" name="_token" value="TOKEN123">
 <input type="radio" id="newProduct" name="product_type" value="Product" checked>
 <input type="radio" id="newRecipe" name="product_type" value="Recipe" disabled>
 <input type="radio" id="newService" name="product_type" value="Service" disabled>
 <input type="radio" id="newMaterial" name="product_type" value="Material" disabled>
 <input type="text" name="name" id="name" required>
 <input name="is_active" type="checkbox" id="isActive" checked>
 <input type='file' id="imageUploadMaterial" />
 <input type="text" name="infosupplier[]"><input type="number" name="infoprice[]">
 {{extra}}
</form>
<form id="productDeleteForm" action="{BASE}/products/destroy/0" method="POST">
 <input type="hidden" name="_method" value="DELETE"><input type="hidden" name="_token" value="TOKEN123">
</form>
"""


def product_list(*rows):
    body = "".join(f'<tr><td><input data-id="{i}" type="checkbox" id="check-{i}"></td>'
                   f'<td><div class="text-truncate">{n}</div></td></tr>' for i, n in rows)
    return f"<table>{body}</table>"


WORKORDER_PAGE = f"""<meta name="csrf-token" content="META456">
<form id="workorderForm"><select name="client" id="clientSelect" required></select>
 <input type="text" name="productionDate" id="productionDateSelect" required>
 <input type="text" name="workorderCode" id="workorderCode">
 <textarea name="workOrderComment" id="workOrderComment"></textarea></form>
<form id="workorderLineForm"><input type="text" id="wolCode">
 <select name="product" id="productSelect" required></select>
 <input type="text" id="workorderLineDescription" required>
 <input type="number" id="quantity" required><select name="tag" id="tagSelect"></select>
 <textarea id="workorderLineComment"></textarea><input type="checkbox">
 <input type="hidden" id="tmpWorkorderLineId"></form>
<script>{"".join(m.replace("{base}", BASE) for m in w.WORKORDER_SCRIPT_MARKERS)}</script>
"""


CLIENT_PAGE = f"""
<form action="{BASE}/clients/store" method="post" id="clientCreateForm">
 <input type="hidden" name="_token" value="TOKEN789">
 <input type="radio" id="newClient" name="tag" value="Client" checked>
 <input type="radio" id="newSupplier" name="tag" value="Supplier" disabled>
 <input type="radio" id="newContact" name="tag" value="ClientSupplier" disabled>
 <input type="text" name="name" id="name" required><input type="email" name="email">
 <input type="text" name="city"><input type="text" name="comments">
 <input type="checkbox" name="show_comments" id="showClientComments">
 <input type="text" name="phone_number"><input type="text" name="vat_number">
 <input type="number" name="vat_rate">
</form>
<form id="clientDeleteForm" action="{BASE}/clients/destroy/0" method="POST">
 <input type="hidden" name="_method" value="DELETE"><input type="hidden" name="_token" value="TOKEN789">
</form>
"""


def client_list(*rows):
    return "".join(f'<div class="deleteClientInfo d-none">{{&quot;id&quot;:{i},&quot;name&quot;:&quot;{n}&quot;}}</div>'
                   for i, n in rows)


class Response:
    def __init__(self, status=200, text="", ctype="text/html", location=None, body=None):
        self.status_code = status
        self.text = text if body is None else json.dumps(body)
        self.headers = {"Content-Type": "application/json" if body is not None else ctype}
        if location:
            self.headers["Location"] = location
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class FakeSession:
    def __init__(self, pages, posts=()):
        self.pages = pages          # path -> list of responses (consumed in order)
        self.posts = list(posts)
        self.sent = []

    def get(self, url, **kwargs):
        path = url[len(BASE):]
        key = path.split("?")[0] + ("?term" if "?term=" in path else "?sortby" if "?sortby=" in path else "")
        if key not in self.pages:
            raise AssertionError(f"unexpected GET {path}")
        return self.pages[key].pop(0)

    def post(self, url, **kwargs):
        self.sent.append((url[len(BASE):], kwargs))
        result = self.posts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class WebWriterTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log_path = Path(directory.name) / "logs" / "epoptia_writes.log"
        self.db_path = Path(directory.name) / "backup.sqlite"
        original = w.backup_similar_products
        w.backup_similar_products = lambda name: original(name, self.db_path)
        self.addCleanup(setattr, w, "backup_similar_products", original)

    def writer(self, session):
        return w.WebWriter(BASE, "u", "p", session=session, login=lambda *a: True,
                           log_path=self.log_path)

    def log(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def product_session(self, *, extra="", after=(), posts=()):
        return FakeSession({
            "/product/create": [Response(text=PRODUCT_FORM.replace("{extra}", extra))],
            "/product/create?term": [Response(text=product_list(*rows)) for rows in [()] + list(after)],
            "/product/create?sortby": [Response(text=product_list((899, "Y")))],
        }, posts)

    def test_preview_sends_nothing_and_redacts_token(self):
        session = self.product_session()
        result = self.writer(session).create_product("ERMIS-TEST")
        self.assertFalse(result["sent"])
        self.assertEqual(session.sent, [])
        self.assertEqual(result["payload"], {"_token": "<csrf>", "product_type": "Product",
                                             "name": "ERMIS-TEST", "infosupplier[]": "",
                                             "infoprice[]": ""})
        self.assertTrue(result["delete"]["delete_form"])
        self.assertEqual(result["delete"]["method_override"], "DELETE")
        self.assertNotIn("TOKEN123", self.log_path.read_text())
        self.assertEqual([r["phase"] for r in self.log()], ["form_check", "preview"])

    def test_confirmed_create_posts_form_and_verifies(self):
        session = self.product_session(after=[[(900, "ERMIS-TEST")]],
                                       posts=[Response(302, location=BASE + "/product/create")])
        result = self.writer(session).create_product("ERMIS-TEST", confirm=True)
        path, kwargs = session.sent[0]
        self.assertEqual(path, "/products/store")
        self.assertEqual(kwargs["data"]["_token"], "TOKEN123")
        self.assertNotIn("is_active", kwargs["data"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(result["matches"], [(900, "ERMIS-TEST")])
        self.assertEqual([r["phase"] for r in self.log()],
                         ["form_check", "request", "response", "verified"])
        self.assertNotIn("TOKEN123", self.log_path.read_text())

    def test_inactive_product_found_by_id_page(self):
        session = self.product_session(after=[[]], posts=[Response(302, location=BASE + "/products")])
        session.pages["/product/create?sortby"] = [Response(text=product_list((1426, "X")))]
        session.pages["/products/1427"] = [Response(text='<div> / #1427 (ERMIS-TEST)</div>')]
        result = self.writer(session).create_product("ERMIS-TEST", confirm=True)
        self.assertEqual(result["matches"], [(1427, "ERMIS-TEST")])
        self.assertEqual(self.log()[-1]["phase"], "verified")

    def test_lookalike_name_in_backup_is_refused(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE records (kind, id, data, gone_since)")
            db.execute("INSERT INTO records VALUES ('product', 1371, ?, NULL)",
                       (json.dumps({"name": "Ψυγείο βιτρίνα ΒΧΜ 83 Ειδικό"}),))
        session = self.product_session()
        with self.assertRaises(w.WriteError):
            self.writer(session).create_product("Ψυγείο βιτρίνα BXM83 Ειδικό", confirm=True)
        self.assertEqual(session.sent, [])

    def test_order_plan_preview_sends_nothing(self):
        session = FakeSession({"/client/create": [Response(text=CLIENT_PAGE)],
                               "/client/create?term": [Response(text=client_list())],
                               "/product/create": [Response(text=PRODUCT_FORM.replace("{extra}", ""))],
                               "/product/create?term": [Response(text=product_list())]})
        plan = {"client": {"create": "NEW CLIENT"}, "date": "09-11-2026",
                "new_products": {"bx": {"name": "Νέο προϊόν", "workflow_id": 1477}},
                "lines": [{"product": "new:bx", "description": "Νέο προϊόν", "quantity": 1},
                          {"product": 900, "description": "Υπάρχον", "quantity": 2}]}
        report = w.run_order_plan(self.writer(session), plan)
        self.assertEqual(session.sent, [])
        self.assertEqual(report["steps"][-1]["workorder"]["lines"][0]["product"], "<new product bx>")

    def test_changed_form_stops_before_any_post(self):
        session = self.product_session(extra='<input type="text" name="code" required>')
        with self.assertRaises(w.FormChanged):
            self.writer(session).create_product("ERMIS-TEST", confirm=True)
        self.assertEqual(session.sent, [])
        self.assertFalse(self.log()[0]["ok"])

    def test_existing_name_is_refused(self):
        session = FakeSession({
            "/product/create": [Response(text=PRODUCT_FORM.replace("{extra}", ""))],
            "/product/create?term": [Response(text=product_list((5, "ERMIS-TEST")))]})
        with self.assertRaises(w.WriteError):
            self.writer(session).create_product("ERMIS-TEST", confirm=True)
        self.assertEqual(session.sent, [])

    def test_redirect_to_login_is_a_failure(self):
        session = self.product_session(posts=[Response(302, location=BASE + "/login")])
        with self.assertRaises(w.WriteError):
            self.writer(session).create_product("ERMIS-TEST", confirm=True)
        self.assertEqual(self.log()[-1]["phase"], "failed")

    def client_session(self, before=(), after=(), posts=()):
        return FakeSession({"/client/create": [Response(text=CLIENT_PAGE)],
                            "/client/create?term": [Response(text=client_list(*before)),
                                                    Response(text=client_list(*after))]}, posts)

    def test_client_preview_then_create(self):
        session = self.client_session()
        preview = self.writer(session).create_client("ERMIS-TEST")
        self.assertFalse(preview["sent"])
        self.assertEqual(preview["payload"]["tag"], "Client")
        self.assertEqual(session.sent, [])
        session = self.client_session(after=[(55, "ERMIS-TEST")],
                                      posts=[Response(302, location=BASE + "/client/create")])
        result = self.writer(session).create_client("ERMIS-TEST", confirm=True)
        self.assertEqual(session.sent[0][0], "/clients/store")
        self.assertEqual(result["matches"], [(55, "ERMIS-TEST")])
        self.assertNotIn("TOKEN789", self.log_path.read_text())

    def test_existing_client_refused(self):
        session = self.client_session(before=[(9, "ERMIS-TEST")])
        with self.assertRaises(w.WriteError):
            self.writer(session).create_client("ERMIS-TEST", confirm=True)
        self.assertEqual(session.sent, [])

    def assign_page(self, pid, *, empty=True, fields=()):
        picker = 'templatePickerEmptyWorkflow' if empty else 'templatePicker'
        link = "" if empty else f'<a href="{BASE}/product/{pid}/workflow/39">'
        cf = "".join(f'<input type="text" class="form-control customField" data-id="{i}" value="">' for i in fields)
        return Response(text=f"""<form id="assignFromTemplate" method="post" action="{BASE}/product/workflow/from-template">
 <input type="hidden" name="_token" value="TOKENWF"><input type="hidden" name="sectionId" value="{pid}" />
 <input type="hidden" name="templateId" /></form><select id="{picker}"></select>{link}{cf}""")

    def workflow_list(self):
        return Response(text='<select id="workflow_all"><option value="-1">x</option>'
                             '<option value="39">ΨΥΓΕΙΟ ΒΙΤΡΙΝΑ ΧΩΡΙΣ ΑΠΟΘΗΚΗ v.2</option></select>')

    def test_assign_workflow_preview_confirm_verify(self):
        session = FakeSession({"/product/create": [self.workflow_list()] * 2,
                               "/products/1500": [self.assign_page(1500), self.assign_page(1500),
                                                  self.assign_page(1500, empty=False, fields=(6, 12))]},
                              [Response(302, location=BASE + "/products/1500")])
        writer = self.writer(session)
        preview = writer.assign_workflow(1500, 39)
        self.assertEqual(preview["payload"], {"_token": "<csrf>", "sectionId": "1500", "templateId": "39"})
        self.assertEqual(session.sent, [])
        result = writer.assign_workflow(1500, 39, confirm=True)
        self.assertEqual(session.sent[0][0], "/product/workflow/from-template")
        self.assertTrue(result["assigned"])
        self.assertEqual(result["custom_field_ids"], [6, 12])

    def test_assign_workflow_refuses_product_with_workflow_or_unknown_template(self):
        session = FakeSession({"/product/create": [self.workflow_list()] * 2,
                               "/products/1500": [self.assign_page(1500, empty=False)]})
        with self.assertRaises(w.FormChanged):
            self.writer(session).assign_workflow(1500, 39, confirm=True)
        with self.assertRaises(w.WriteError):
            self.writer(session).assign_workflow(1500, 999, confirm=True)
        self.assertEqual(session.sent, [])

    def test_workorder_custom_fields_and_multiline_comments(self):
        args = self.workorder_args()
        args["lines"][0].update(comments="Α\n• Β", customFields={"12": "180x85x120 cm"})
        session = FakeSession({"/products/900": [self.assign_page(900, empty=False, fields=(12,))],
                               "/workorders/create": [Response(text=WORKORDER_PAGE)]})
        preview = self.writer(session).create_workorder(**args)
        line = preview["step2"]["json"]["workorderLines"][0]
        self.assertEqual(line["customFieldsValues"], {"12": "180x85x120 cm"})
        self.assertEqual(line["comments"], "Α\n• Β")
        args["lines"][0]["customFields"] = {"99": "x"}
        session = FakeSession({"/products/900": [self.assign_page(900, empty=False, fields=(12,))]})
        with self.assertRaises(w.WriteError):
            self.writer(session).create_workorder(**args)

    def wo_page(self, wol_id=3288, description="Test line"):
        info = html.escape(json.dumps({"id": wol_id, "workorder_id": 751, "description": description}))
        return Response(text=f"""<div class="deleteWorkorderlineInfo d-none">{info}</div>
<form id="workordelineDeleteForm" action="{BASE}/workorderlines/remove/0" method="POST">
 <input type="hidden" name="_method" value="DELETE"><input type="hidden" name="_token" value="TOKDEL"></form>""")

    def test_delete_line_checks_description_then_deletes(self):
        session = FakeSession({"/workorders/751": [self.wo_page(description="Other")]})
        with self.assertRaises(w.WriteError):
            self.writer(session).delete_workorderline(751, 3288, "Test line", confirm=True)
        session = FakeSession({"/workorders/751": [self.wo_page(), self.wo_page(), Response(text="")]},
                              [Response(302, location=BASE + "/workorders/751")])
        writer = self.writer(session)
        self.assertFalse(writer.delete_workorderline(751, 3288, "Test line")["sent"])
        self.assertEqual(session.sent, [])
        result = writer.delete_workorderline(751, 3288, "Test line", confirm=True)
        self.assertEqual(session.sent[0][0], "/workorderlines/remove/3288")
        self.assertEqual(session.sent[0][1]["data"]["_method"], "DELETE")
        self.assertTrue(result["verified"])

    def test_delete_product_refuses_name_mismatch(self):
        session = FakeSession({"/products/1428": [Response(text="<div> / #1428 (Something else)</div>")]})
        with self.assertRaises(w.WriteError):
            self.writer(session).delete_product(1428, "ERMIS-TEST", confirm=True)
        self.assertEqual(session.sent, [])

    def workorder_args(self):
        return dict(client_id=3, production_date="15-10-2026",
                    lines=[{"product": 900, "description": "test", "quantity": 1}])

    def test_workorder_preview_and_full_success(self):
        session = FakeSession({"/workorders/create": [Response(text=WORKORDER_PAGE)] * 2}, [
            Response(body={"id": 77}), Response(body={"workorderLines": {"1": 501}})])
        writer = self.writer(session)
        preview = writer.create_workorder(**self.workorder_args())
        self.assertFalse(preview["sent"])
        self.assertEqual(session.sent, [])
        result = writer.create_workorder(**self.workorder_args(), confirm=True)
        self.assertEqual(result["workorder_id"], 77)
        self.assertEqual(session.sent[1][1]["json"]["workorderId"], 77)
        self.assertEqual(session.sent[0][1]["headers"]["X-CSRF-TOKEN"], "META456")

    def test_failed_lines_call_raises_partial_and_logs_it(self):
        session = FakeSession({"/workorders/create": [Response(text=WORKORDER_PAGE)]}, [
            Response(body={"id": 77}), Response(500, text="Server Error")])
        with self.assertRaises(w.PartialWorkorder) as caught:
            self.writer(session).create_workorder(**self.workorder_args(), confirm=True)
        self.assertEqual(caught.exception.workorder_id, 77)
        last = self.log()[-1]
        self.assertEqual((last["phase"], last["workorder_id"]), ("partial_workorder", 77))

    def test_lines_timeout_also_partial(self):
        session = FakeSession({"/workorders/create": [Response(text=WORKORDER_PAGE)]}, [
            Response(body={"id": 78}), TimeoutError()])
        with self.assertRaises(w.PartialWorkorder):
            self.writer(session).create_workorder(**self.workorder_args(), confirm=True)
        self.assertEqual([r["phase"] for r in self.log()][-2:], ["error", "partial_workorder"])

    def test_missing_script_marker_stops(self):
        page = WORKORDER_PAGE.replace("workorderlines/store", "workorderlines/save")
        session = FakeSession({"/workorders/create": [Response(text=page)]})
        with self.assertRaises(w.FormChanged):
            self.writer(session).create_workorder(**self.workorder_args(), confirm=True)
        self.assertEqual(session.sent, [])


if __name__ == "__main__":
    unittest.main()
