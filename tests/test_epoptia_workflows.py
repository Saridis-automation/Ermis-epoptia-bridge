"""Workflow graph read/build/validate and the guarded update/create/delete flows (synthetic)."""
import html
import json
import tempfile
import unittest
from pathlib import Path

import epoptia_workflows as wf
from epoptia_write import FormChanged, WebWriter, WriteError

BASE = "https://epoptia.example"


def server_json(elements, links, files=()):
    return json.dumps(dict(id=900, type="template", name="ERMIS-TEST ροή", template=True, comments="",
                           elements={str(e["id"]): e for e in elements},
                           links={str(l["id"]): l for l in links}, files=list(files), elementsFiles=[]))


def element(eid, ws_id, ws_name, tag, tag_id, left):
    return dict(id=eid, workstation_id=ws_id, workstation={"name": ws_name}, tag=tag, tag_id=tag_id,
                is_strict=0, is_semi_strict=1, is_or=0, position_top="0px", position_left=f"{left}px",
                comments=None, default_average=0)


THREE = [element(1, 28, "ΚΟΠΗ ΨΑΛΙΔΙ", "ΚΟΠΗ ΨΑΛΙΔΙ", 6, 0), element(2, 3, "ΣΤΡΑΤΖΑ", "ΣΤΡΑΤΖΑ", 23, 120),
         element(3, 7, "ΜΟΝΤΑΖ 1", "ΣΥΝΑΡΜΟΛΟΓΗΣΗ", 21, 240)]
LINKS = [dict(id=11, parent_element=1, child_element=2, output_id="output-right", input_id="input-left"),
         dict(id=12, parent_element=2, child_element=3, output_id="output-right", input_id="input-left")]


def page(elements=THREE, links=LINKS, extra="", attached="[]", files=()):
    return f"""<div id="tmpWorkflowElementsCustomFields" class="d-none">{attached}</div>
<button data-id="6" class="btn btn-sm btn-outline-danger customFieldRemove mr-2"></button>
<button data-type="text" data-id="6" data-checkbox="" class="btn btn-sm btn-outline-secondary customFieldEdit" data-mandatorycheckbeforeproduction="1"></button>
<div id="workflowServer" class="d-none">{html.escape(server_json(elements, links, files))}</div>
<form id="workflowForm" method="POST" action="{BASE}/workflow-update/900">
<input type="hidden" name="_token" value="TOKWF"><input type="hidden" name="_method" value="put">
<input type="hidden" name="deleteCustomFieldsFromWorkflow"><input type="hidden" name="elementFiles">
<input type="hidden" name="elementFilesFirst"><input type="hidden" name="elementCustomFields">
<input type="hidden" name="workflowData"><input type="hidden" name="workflowName">
<input type="hidden" name="madeChangesAtWorkflow"><input type="hidden" name="workflowComments">
</form>{extra}"""


class Response:
    def __init__(self, text="", status=200, location=None):
        self.status_code, self.text = status, text
        self.headers = {"Content-Type": "text/html"}
        if location:
            self.headers["Location"] = location


class Session:
    def __init__(self, pages, posts=()):
        self.pages, self.posts, self.sent = pages, list(posts), []

    def get(self, url, **kw):
        path = url[len(BASE):].split("?")[0]
        return self.pages[path].pop(0)

    def post(self, url, **kw):
        self.sent.append((url[len(BASE):], kw))
        return self.posts.pop(0)


class GraphTests(unittest.TestCase):
    def test_parse_and_signature(self):
        g = wf.parse_workflow(page())
        self.assertEqual(g["name"], "ERMIS-TEST ροή")
        self.assertEqual(len(g["nodes"]), 3)
        sig = wf.signature(g)
        self.assertEqual(len(sig["links"]), 2)

    def test_build_matches_editor_format(self):
        data = wf.build_workflow_data(wf.parse_workflow(page()))
        first = data["ermis-line-1"]
        self.assertEqual(first["id"], 11)
        self.assertEqual(first["source"]["se_id"], "28")
        self.assertEqual(first["source"]["node_id"], 1)
        self.assertEqual((first["source"]["strict"], first["source"]["semiStrict"], first["source"]["freePass"]), (0, 1, 0))
        self.assertEqual(first["target"]["tag_id"], 23)
        self.assertEqual((first["input"], first["output"]), ("input-left", "output-right"))

    def test_linear_graph_new_nodes(self):
        g = wf.linear_graph([(28, "ΚΟΠΗ ΨΑΛΙΔΙ", "ΚΟΠΗ ΨΑΛΙΔΙ", 6), (3, "ΣΤΡΑΤΖΑ", "ΣΤΡΑΤΖΑ", 23)])
        wf.validate_graph(g)
        data = wf.build_workflow_data(g)
        self.assertEqual(data["ermis-line-1"]["source"]["node_id"], 0)
        self.assertEqual(data["ermis-line-1"]["id"], 0)

    def test_validation_rejects_untagged_and_unlinked(self):
        g = wf.linear_graph([(28, "ΚΟΠΗ", "ΚΟΠΗ ΨΑΛΙΔΙ", 6), (3, "ΣΤΡΑΤΖΑ", "", "")])
        with self.assertRaises(WriteError):
            wf.validate_graph(g)
        g = wf.linear_graph([(28, "ΚΟΠΗ", "ΚΟΠΗ ΨΑΛΙΔΙ", 6), (3, "ΣΤΡΑΤΖΑ", "ΣΤΡΑΤΖΑ", 23)])
        g["nodes"]["lonely"] = dict(g["nodes"]["new1"])
        with self.assertRaises(WriteError):
            wf.validate_graph(g)

    def test_mandatory_flags(self):
        self.assertEqual(wf.mandatory_flags(page()), {"6": True})

    def test_custom_field_detection(self):
        self.assertFalse(wf.has_custom_field_settings(page()))           # catalogue buttons only
        attached = html.escape(json.dumps({"6": {"1": {"is_show": 1}}}))
        self.assertTrue(wf.has_custom_field_settings(page(attached=attached)))
        self.assertEqual(list(wf.attached_custom_fields(page(attached=attached))), ["6"])


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.log = Path(d.name) / "w.log"

    def writer(self, session):
        return wf.WorkflowWriter(WebWriter(BASE, "u", "p", session=session, login=lambda *a: True,
                                           log_path=self.log))

    def test_preview_then_confirmed_update_verified(self):
        base = wf.parse_workflow(page())
        new = json.loads(json.dumps(base))
        new["nodes"] = {int(k): v for k, v in new["nodes"].items()}
        new["nodes"][4] = dict(node_id=0, workstation_id=23, workstation="ΨΥΚΤΙΚΑ", tag="ΕΓΚΑΤΑΣΤΑΣΗ ΨΥΚΤΙΚΩΝ",
                               tag_id=14, mode="semi", is_or=0, top="0px", left="360px", comment="",
                               default_average=0)
        new["links"].append(dict(link_id=0, parent=3, child=4, output="output-right", input="input-left"))
        after = page(THREE + [element(4, 23, "ΨΥΚΤΙΚΑ", "ΕΓΚΑΤΑΣΤΑΣΗ ΨΥΚΤΙΚΩΝ", 14, 360)],
                     LINKS + [dict(id=13, parent_element=3, child_element=4, output_id="output-right", input_id="input-left")])
        session = Session({"/workflows/900": [Response(page()), Response(page()), Response(after)]},
                          [Response(status=302, location=BASE + "/workflows/900")])
        writer = self.writer(session)
        preview = writer.update(900, new, expected_current=wf.signature(base))
        self.assertFalse(preview["sent"])
        self.assertEqual(session.sent, [])
        result = writer.update(900, new, expected_current=wf.signature(base), confirm=True)
        path, kw = session.sent[0]
        self.assertEqual(path, "/workflow-update/900")
        self.assertEqual(kw["data"]["_method"], "put")
        self.assertEqual(len(json.loads(kw["data"]["workflowData"])), 3)
        self.assertTrue(result["verified"])
        self.assertNotIn("TOKWF", self.log.read_text())

    def test_update_keeps_attached_fields_and_shows_them_on_new_steps(self):
        attached = html.escape(json.dumps({"6": {"1": {"is_show": 1}, "2": {"is_show": 1, "is_check": 1}, "3": {"is_show": 1}}}))
        base = wf.parse_workflow(page())
        new = json.loads(json.dumps(base))
        new["nodes"] = {int(k): v for k, v in new["nodes"].items()}
        new["nodes"][4] = dict(node_id=0, workstation_id=23, workstation="ΨΥΚΤΙΚΑ", tag="ΕΓΚΑΤΑΣΤΑΣΗ ΨΥΚΤΙΚΩΝ",
                               tag_id=14, mode="semi", is_or=0, top="0px", left="360px", comment="", default_average=0)
        new["links"].append(dict(link_id=0, parent=3, child=4, output="output-right", input="input-left"))
        after_attached = html.escape(json.dumps({"6": {str(i): {"is_show": 1} for i in (1, 2, 3, 4)}}))
        after = page(THREE + [element(4, 23, "ΨΥΚΤΙΚΑ", "ΕΓΚΑΤΑΣΤΑΣΗ ΨΥΚΤΙΚΩΝ", 14, 360)],
                     LINKS + [dict(id=13, parent_element=3, child_element=4, output_id="output-right", input_id="input-left")],
                     attached=after_attached)
        session = Session({"/workflows/900": [Response(page(attached=attached)), Response(after)]},
                          [Response(status=302, location=BASE + "/workflows/900")])
        result = self.writer(session).update(900, new, expected_current=wf.signature(base), confirm=True)
        sent = json.loads(session.sent[0][1]["data"]["elementCustomFields"])
        self.assertEqual(sent["6"]["mandatorycheckbeforeproduction"], True)
        self.assertEqual(sent["6"]["elements"]["ermis-2"], {"show": True, "check": True})   # saved settings kept
        self.assertEqual(sent["6"]["elements"]["ermis-4"], {"show": True})                  # new step visible
        self.assertTrue(result["verified"])
        self.assertEqual(result["shown_steps"], {"6": 4})

    def test_refuses_stale_base_and_files(self):
        base = wf.parse_workflow(page())
        stale = dict(wf.signature(base), links=[])
        session = Session({"/workflows/900": [Response(page())]})
        with self.assertRaises(FormChanged):
            self.writer(session).update(900, base, expected_current=stale, confirm=True)
        session = Session({"/workflows/900": [Response(page(files=[{"id": 1}]))]})
        with self.assertRaises(FormChanged):
            self.writer(session).update(900, base, expected_current=wf.signature(base), confirm=True)
        self.assertEqual(session.sent, [])

    def test_create_posts_graph_to_workflow_create_and_verifies(self):
        create_page = f"""<form action="{BASE}/workflow-create" method="post">
<input type="hidden" name="_token" value="TOKC"><input type="hidden" name="elementCustomFields">
<input type="hidden" name="workflowData"><input type="hidden" name="workflowName">
<input type="hidden" name="madeChangesAtWorkflow"><input type="hidden" name="workflowComments"></form>"""
        listing = '<div class="deleteWorkflowInfo d-none">' + html.escape(json.dumps({"id": 900, "name": "ERMIS-TEST ροή"})) + '</div>'
        graph = wf.linear_graph([(28, "ΚΟΠΗ ΨΑΛΙΔΙ", "ΚΟΠΗ ΨΑΛΙΔΙ", 6), (3, "ΣΤΡΑΤΖΑ", "ΣΤΡΑΤΖΑ", 23),
                                 (7, "ΜΟΝΤΑΖ 1", "ΣΥΝΑΡΜΟΛΟΓΗΣΗ", 21)])
        session = Session({"/workflows/create": [Response(create_page)],
                           "/workflows": [Response(""), Response(listing)],
                           "/workflows/900": [Response(page())]},
                          [Response(status=302, location=BASE + "/workflows/900")])
        result = self.writer(session).create("ERMIS-TEST ροή", graph, confirm=True)
        self.assertEqual(session.sent[0][0], "/workflow-create")
        self.assertEqual(session.sent[0][1]["data"]["workflowName"], "ERMIS-TEST ροή")
        self.assertEqual(result["ids"], [900])
        self.assertTrue(result["verified"])

    def test_delete_checks_name(self):
        session = Session({"/workflows/900": [Response(page())]})
        with self.assertRaises(WriteError):
            self.writer(session).delete(900, "Λάθος όνομα", confirm=True)
        self.assertEqual(session.sent, [])


if __name__ == "__main__":
    unittest.main()
