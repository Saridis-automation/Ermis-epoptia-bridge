"""Editing existing lines/orders/products: expected-value guard, request plan, verification."""
import html
import json
import tempfile
import unittest
from pathlib import Path

import epoptia_edits as e
from epoptia_write import WebWriter, WriteError

BASE = "https://epoptia.example"


def line_page(desc="Τραπέζι", qty="1", date="31-12-2026", comments="", cf=None):
    cf = cf if cf is not None else {"6": "0,8", "12": "100x60x86 cm"}
    return f"""<meta name="csrf-token" content="META">
<form action="{BASE}/workorderlines/update/9" method="post"><input type="hidden" name="_method" value="PUT">
<input type="hidden" name="_token" value="TOK"><input type="number" id="qty" name="qty" value="{qty}"></form>
<form action="{BASE}/workorderlines/update/9" method="post"><input type="hidden" name="_method" value="PUT">
<input type="hidden" name="_token" value="TOK"><input type="text" name="description" value="{html.escape(desc)}" required/></form>
<li class="list-item col-4">Ημ. ολοκλ. παραγωγής </li><li class="list-item col-8"><div class="flex-fill">{date}</div></li>
<textarea id="commentCom" name="comments">{html.escape(comments)}</textarea>
<div id="customFieldValues" class="d-none">{html.escape(json.dumps(cf))}</div>
<input type="text" class="form-control customField" data-id="6" value="">
<input type="text" class="form-control customField" data-id="7" value="">
<input type="text" class="form-control customField" data-id="12" value="">"""


class Response:
    def __init__(self, text="", status=200, location=None, body=None):
        self.status_code, self.text = status, text if body is None else json.dumps(body)
        self.headers = {"Content-Type": "application/json" if body is not None else "text/html"}
        if location:
            self.headers["Location"] = location
        self._body = body

    def json(self):
        return self._body


class Session:
    def __init__(self, pages, posts=()):
        self.pages, self.posts, self.sent = pages, list(posts), []

    def get(self, url, **kw):
        return self.pages[url[len(BASE):]].pop(0)

    def post(self, url, **kw):
        self.sent.append((url[len(BASE):], kw))
        return self.posts.pop(0)


class EditTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.log = Path(d.name) / "w.log"

    def editor(self, session):
        return e.Editor(WebWriter(BASE, "u", "p", session=session, login=lambda *a: True, log_path=self.log))

    def test_line_state_reader(self):
        state = e.line_state(line_page())
        self.assertEqual((state["description"], state["quantity"], state["completion_date"]), ("Τραπέζι", "1", "31-12-2026"))
        self.assertEqual(state["field_ids"], ["6", "7", "12"])

    def test_wrong_expected_value_refuses_without_sending(self):
        session = Session({"/workorderlines/9": [Response(line_page(qty="3"))]})
        with self.assertRaises(WriteError):
            self.editor(session).update_line(9, expected={"quantity": 1}, changes={"quantity": 2}, confirm=True)
        self.assertEqual(session.sent, [])

    def test_custom_field_change_resends_the_others(self):
        session = Session({"/workorderlines/9": [Response(line_page())]})
        preview = self.editor(session).update_line(9, expected={"custom_fields": {"12": "100x60x86 cm"}},
                                                   changes={"custom_fields": {"12": "120x60x86 cm"}})
        kind, path, body = preview["requests"][0]
        self.assertEqual(path, "/workorderline/customfields")
        self.assertEqual(body["customFields"], {"6": "0,8", "7": "", "12": "120x60x86 cm"})
        self.assertEqual(session.sent, [])

    def test_confirmed_line_edit_sends_and_verifies(self):
        after = line_page(desc="Τραπέζι εργασίας", qty="2")
        session = Session({"/workorderlines/9": [Response(line_page()), Response(after)]},
                          [Response(status=302, location=BASE + "/workorderlines/9"),
                           Response(status=302, location=BASE + "/workorderlines/9")])
        result = self.editor(session).update_line(9, expected={"description": "Τραπέζι", "quantity": "1"},
                                                  changes={"description": "Τραπέζι εργασίας", "quantity": 2}, confirm=True)
        self.assertEqual([p for p, _ in session.sent], ["/workorderlines/update/9", "/workorderlines/update/9"])
        self.assertEqual(session.sent[0][1]["data"]["_method"], "PUT")
        self.assertTrue(result["verified"])
        self.assertNotIn("TOK", self.log.read_text().replace("TOKEN", ""))

    def test_order_and_product_readers(self):
        order = ('<li>Ημ. ολοκλ. παραγωγής </li><li>31-12-2026 <a></a></li>'
                 '<a role="button" href="https://x/clients/387">ERMIS-TEST</a><textarea id="commentCom">σχ</textarea>')
        self.assertEqual(e.order_state(order), {"completion_date": "31-12-2026", "comments": "σχ", "client_id": 387})
        product = '<div> / #1500 (ERMIS-TEST αλλαγές)</div><textarea id="commentCom"></textarea>'
        self.assertEqual(e.product_state(product, 1500), {"name": "ERMIS-TEST αλλαγές", "comments": ""})


if __name__ == "__main__":
    unittest.main()
