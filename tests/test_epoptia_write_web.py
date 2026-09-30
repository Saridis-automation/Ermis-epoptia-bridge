"""WebWriter against synthetic pages: form checks, preview gate, logging, partial workorders."""
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
