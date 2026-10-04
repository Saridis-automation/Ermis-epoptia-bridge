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

    def test_saved_untagged_step_may_stay_untagged_but_not_change(self):
        g = wf.linear_graph([(28, "ΚΟΠΗ", "ΚΟΠΗ ΨΑΛΙΔΙ", 6), (3, "ΣΤΡΑΤΖΑ", "ΣΤΡΑΤΖΑ", 23)])
        g["nodes"]["new2"].update(tag="", tag_id="", node_id=55)
        current = json.loads(json.dumps(g))
        wf.validate_graph(g, current)                                  # unchanged: allowed
        g["nodes"]["new2"]["workstation_id"] = 2
        with self.assertRaises(WriteError):
            wf.validate_graph(g, current)                              # changed without tag

    def test_station_change_becomes_new_step_with_relinked_edges(self):
        current = wf.parse_workflow(page())
        g = json.loads(json.dumps(current))
        g["nodes"] = {int(k): v for k, v in g["nodes"].items()}
        g["nodes"][2].update(workstation_id=2, workstation="LASER")
        out = wf.replace_changed_stations(g, current)
        self.assertNotIn(2, out["nodes"])
        self.assertEqual(out["nodes"]["swap2"]["node_id"], 0)
        self.assertEqual({(l["parent"], l["child"], l["link_id"]) for l in out["links"]},
                         {(1, "swap2", 0), ("swap2", 3, 0)})

    def test_custom_field_detection(self):
        self.assertFalse(wf.has_custom_field_settings(page()))           # catalogue buttons only
        attached = html.escape(json.dumps({"6": {"1": {"is_show": 1}}}))
        self.assertTrue(wf.has_custom_field_settings(page(attached=attached)))
        self.assertEqual(list(wf.attached_custom_fields(page(attached=attached))), ["6"])


def wol_page(elements=THREE, links=LINKS, hits=None, template=False, wol=3300):
    meta = json.loads(server_json(elements, links))
    meta.update(type="template" if template else "workorderline", template=template, name=f"workorderline: {wol}")
    hits = hits if hits is not None else {str(e["id"]): {"elementInfo": {"is_active": 1}} for e in elements}
    return f"""<div id="tmpWorkflowElementsCustomFields" class="d-none">[]</div>
<div id="workorderLineHits" class="d-none">{html.escape(json.dumps(hits))}</div>
<div id="workflowServer" class="d-none">{html.escape(json.dumps(meta))}</div>
<form id="workflowForm" method="POST" action="{BASE}/workflow-update/900">
<input type="hidden" name="_token" value="TOKWF"><input type="hidden" name="_method" value="put">
<input type="hidden" name="deleteCustomFieldsFromWorkflow"><input type="hidden" name="elementFiles">
<input type="hidden" name="elementFilesFirst"><input type="hidden" name="elementCustomFields">
<input type="hidden" name="workflowData"><input type="hidden" name="workflowName">
<input type="hidden" name="madeChangesAtWorkflow"><input type="hidden" name="workflowComments">
<input type="hidden" name="workorderLine" value="{wol}" />
</form>"""


class WolWorkflowTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.log = Path(d.name) / "w.log"

    def writer(self, session):
        return wf.WorkflowWriter(WebWriter(BASE, "u", "p", session=session, login=lambda *a: True, log_path=self.log))

    def extended(self, base):
        new = json.loads(json.dumps(base))
        new["nodes"] = {int(k): v for k, v in new["nodes"].items()}
        new["nodes"][4] = dict(node_id=0, workstation_id=1, workstation="ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ", tag="ADMIN", tag_id=25,
                               mode="semi", is_or=0, top="0px", left="360px", comment="", default_average=0)
        new["links"].append(dict(link_id=0, parent=3, child=4, output="output-right", input="input-left"))
        return new

    def test_line_on_template_is_refused(self):
        base = wf.parse_workflow(wol_page(template=True))
        session = Session({"/workorderlines/3300/workflow/900": [Response(wol_page(template=True))]})
        with self.assertRaises(FormChanged):
            self.writer(session).update_wol(3300, 900, self.extended(base), expected_current=wf.signature(base), confirm=True)
        self.assertEqual(session.sent, [])

    def test_step_with_progress_cannot_be_removed_or_changed(self):
        hits = {"1": {"elementInfo": {"is_active": 1, "element_done": 1, "first_start": "x"}},
                "2": {"elementInfo": {"is_active": 1}}, "3": {"elementInfo": {"is_active": 1}}}
        text = wol_page(hits=hits)
        base = wf.parse_workflow(text)
        broken = json.loads(json.dumps(base))
        broken["nodes"] = {int(k): v for k, v in broken["nodes"].items()}
        broken["nodes"][1]["tag_id"] = 99
        session = Session({"/workorderlines/3300/workflow/900": [Response(text)]})
        with self.assertRaises(FormChanged):
            self.writer(session).update_wol(3300, 900, broken, expected_current=wf.signature(base), confirm=True)
        self.assertEqual(session.sent, [])

    def test_adding_a_step_to_a_running_line_posts_workorderline(self):
        hits = {"1": {"elementInfo": {"is_active": 1, "element_done": 1, "first_start": "x"}},
                "2": {"elementInfo": {"is_active": 1}}, "3": {"elementInfo": {"is_active": 1}}}
        base = wf.parse_workflow(wol_page(hits=hits))
        after = wol_page(THREE + [element(4, 1, "ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ", "ADMIN", 25, 360)],
                         LINKS + [dict(id=13, parent_element=3, child_element=4, output_id="output-right", input_id="input-left")],
                         hits=dict(hits, **{"4": {"elementInfo": {"is_active": 1}}}))
        session = Session({"/workorderlines/3300/workflow/900": [Response(wol_page(hits=hits)), Response(after)]},
                          [Response(status=302, location=BASE + "/workorderlines/3300")])
        result = self.writer(session).update_wol(3300, 900, self.extended(base), expected_current=wf.signature(base), confirm=True)
        self.assertEqual(session.sent[0][1]["data"]["workorderLine"], "3300")
        self.assertTrue(result["verified"])


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

    def test_step_settings_attach_a_checkbox_to_one_step_and_drop_a_check(self):
        attached = html.escape(json.dumps({"26": {"1": {"is_show": 1, "is_check": 1}, "3": {"is_show": 1, "is_check": 1}}}))
        base = wf.parse_workflow(page())
        after_attached = html.escape(json.dumps({"26": {"1": {"is_show": 1, "is_check": 1}},
                                                 "40": {"3": {"is_show": 1, "is_check": 1}}}))
        session = Session({"/workflows/900": [Response(page(attached=attached)), Response(page(attached=after_attached))]},
                          [Response(status=302, location=BASE + "/workflows/900")])
        settings = {40: {3: {"show": True, "check": True}}, 26: {3: {}}}
        result = self.writer(session).update(900, base, expected_current=wf.signature(base),
                                             step_settings=settings, confirm=True)
        sent = json.loads(session.sent[0][1]["data"]["elementCustomFields"])
        self.assertEqual(sent["40"]["elements"], {"ermis-3": {"show": True, "check": True}})  # only that step
        self.assertEqual(sent["40"]["mandatorycheckbeforeproduction"], False)
        self.assertEqual(sent["26"]["elements"], {"ermis-1": {"show": True, "check": True}})  # step 3 removed
        self.assertTrue(result["verified"])

    def test_step_settings_not_saved_is_unverified(self):
        base = wf.parse_workflow(page())
        session = Session({"/workflows/900": [Response(page()), Response(page())]},
                          [Response(status=302, location=BASE + "/workflows/900")])
        result = self.writer(session).update(900, base, expected_current=wf.signature(base),
                                             step_settings={40: {3: {"show": True, "check": True}}}, confirm=True)
        self.assertFalse(result["verified"])

    def test_step_settings_reject_unknown_step_and_hidden_check(self):
        base = wf.parse_workflow(page())
        for settings in ({40: {99: {"show": True}}}, {40: {3: {"check": True}}}):
            session = Session({"/workflows/900": [Response(page())]})
            with self.assertRaises(FormChanged):
                self.writer(session).update(900, base, expected_current=wf.signature(base),
                                            step_settings=settings, confirm=True)
            self.assertEqual(session.sent, [])

    def test_create_custom_field_checks_form_and_verifies(self):
        cf_page = f"""<form id="customFieldForm" method="post">
<input type="hidden" name="_token" value="TOKCF"><input id="method" type="hidden" name="_method" value="post" />
<input type="text" name="name" required><input type="text" name="name_second">
<select name="category"><option value="characteristics"></option><option value="procedures"></option></select>
<input type="text" name="code" /><select name="group"><option value="">-</option></select>
<select name="type"><option value="text"></option><option value="checkbox" ></option></select>
<input name="dropDownOptions[]" type="text" value="" ><input name="limit_down" type="number" />
<input name="limit_up" type="number" /><input name="has_equal" type="checkbox" />
<input name="equationField" type="text"><input id="equation_set_value" type="checkbox" >
<select name="affected_custom_field"></select><input name="value_equal" type="text" value="" />
<input name="is_active" type="hidden"><input name="connect_to_remote_workflows" type="checkbox">
<input type="hidden" name="edit_rule" /></form>
<script>$("#customFieldForm").attr("action", "{BASE}/customfields/store");</script>"""
        info = {"id": 40, "name": "ΣΥΝΔΕΣΗ ΑΠΟΧΕΤΕΥΣΗΣ", "type": "checkbox", "is_active": 1}
        listing = '<div class="deleteCustomfieldInfo d-none">' + html.escape(json.dumps(info)) + '</div>'
        session = Session({"/customfields": [Response(cf_page), Response(""), Response(listing)]},
                          [Response(status=302, location=BASE + "/customfields")])
        result = self.writer(session).create_custom_field("ΣΥΝΔΕΣΗ ΑΠΟΧΕΤΕΥΣΗΣ", confirm=True)
        self.assertEqual(session.sent[0][0], "/customfields/store")
        data = session.sent[0][1]["data"]
        self.assertEqual((data["type"], data["category"], data["name"]), ("checkbox", "procedures", "ΣΥΝΔΕΣΗ ΑΠΟΧΕΤΕΥΣΗΣ"))
        self.assertEqual(result["id"], 40)
        self.assertTrue(result["verified"])
        changed = cf_page.replace('<input type="hidden" name="edit_rule" />', '<input type="text" name="rule" />')
        session = Session({"/customfields": [Response(changed)]})
        with self.assertRaises(FormChanged):
            self.writer(session).create_custom_field("Χ", confirm=True)
        self.assertEqual(session.sent, [])

    def test_rename_tag_keeps_section_and_verifies(self):
        def listing(name):
            info = {"id": 15, "name": name, "section": "workflow_elements"}
            return '<div class="deleteTagInfo d-none">' + html.escape(json.dumps(info)) + '</div>'
        form = f"""<form id="tagForm" action="{BASE}/tags/update/0" method="post">
<input type="hidden" name="_method" value="PUT"><input type="hidden" name="_token" value="TOKT">
<input type="checkbox" name="section[]" value="workstations" disabled><input type="text" name="name" required>
<input type="hidden" name="section"></form>"""
        session = Session({"/tags": [Response(listing("ΣΧΕΔΙΟ (ΘΑΝΑΣΗΣ)")), Response(form), Response(listing("ΣΧΕΔΙΟ"))]},
                          [Response(status=302, location=BASE + "/tags")])
        result = self.writer(session).rename_tag(15, "ΣΧΕΔΙΟ (ΘΑΝΑΣΗΣ)", "ΣΧΕΔΙΟ", confirm=True)
        path, kw = session.sent[0]
        self.assertEqual(path, "/tags/update/15")
        self.assertEqual((kw["data"]["name"], kw["data"]["section"], kw["data"]["_method"]),
                         ("ΣΧΕΔΙΟ", "workflow_elements", "PUT"))
        self.assertTrue(result["verified"])
        session = Session({"/tags": [Response(listing("ΣΧΕΔΙΟ (ΘΑΝΑΣΗΣ)"))]})
        with self.assertRaises(WriteError):
            self.writer(session).rename_tag(15, "Άλλο όνομα", "ΣΧΕΔΙΟ", confirm=True)
        self.assertEqual(session.sent, [])

    def test_rename_workstation_resends_current_settings(self):
        def listing(name):
            info = {"id": 1, "name": name, "max_works": 100, "folder_id": None, "cost_per_hour": None,
                    "only_tracking": 0, "grouped": 0, "not_unique_tasks": 0, "info_button_visible": None,
                    "remote_code": None, "is_active": 1, "tags": [{"id": 25}]}
            return ('<div class="workstationInfo d-none">' + html.escape(json.dumps(info)) + '</div>' + f"""
<form id="workstationForm" action="" method="post"><input type="hidden" name="_method" value="PUT">
<input type="hidden" name="_token" value="TOKW"><input type="text" name="name"><input type="text" name="workstation_code">
<select name="workstationTags[]" multiple></select><input type="number" name="max_works">
<select name="folder_id"><option value=""></option></select><input type="number" name="cost_per_hour">
<input type="checkbox" name="tracking_info"><input type="radio" name="group_by_value" value="group_by_default">
<input type="radio" name="group_by_value" value="group_by"><input name="info_button_visible" type="checkbox" value="1">
<input type="checkbox" name="edit_not_unique_tasks"></form>
<script>$("#workstationForm").attr("action", "/workstations/update/"+workstation.id);</script>""")
        session = Session({"/workstations": [Response(listing("ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ")), Response(listing("ADMIN"))]},
                          [Response(status=302, location=BASE + "/workstations")])
        result = self.writer(session).rename_workstation(1, "ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ", "ADMIN", confirm=True)
        path, kw = session.sent[0]
        self.assertEqual(path, "/workstations/update/1")
        data = kw["data"]
        self.assertEqual((data["name"], data["max_works"], data["workstationTags[]"], data["group_by_value"]),
                         ("ADMIN", "100", ["25"], "group_by_default"))
        self.assertNotIn("tracking_info", data)
        self.assertTrue(result["verified"])

    def test_create_tag_and_workstation_with_saridis_defaults(self):
        tag_form = f"""<form action="{BASE}/tags/store" id="tagsCreateForm" method="post">
<input type="hidden" name="_token" value="TOKT"><input type="checkbox" name="section[]" value="workflow_elements">
<input type="text" name="name" required></form>"""
        led = '<div class="deleteTagInfo d-none">' + html.escape(json.dumps({"id": 27, "name": "LED", "section": "workflow_elements"})) + '</div>'
        session = Session({"/tags": [Response(""), Response(tag_form), Response(led)]},
                          [Response(status=302, location=BASE + "/tags")])
        result = self.writer(session).create_tag("LED", confirm=True)
        self.assertEqual(session.sent[0][1]["data"]["section[]"], ["workflow_elements"])
        self.assertEqual((result["id"], result["verified"]), (27, True))
        ws_form = f"""<form id="addNewWorkstationForm" action="{BASE}/workstations/store" method="post">
<input type="hidden" name="_token" value="TOKW"><input type="text" name="name" required><input type="text" name="workstation_code">
<select name="workstationTags[]" multiple></select><input type="number" name="max_works">
<select name="folder_id"></select><input type="number" name="cost_per_hour"><input type="checkbox" name="tracking_info">
<input type="radio" name="group_by_value" value="group_by_default" checked><input type="radio" name="group_by_value" value="group_by">
<input type="checkbox" name="info_button_visible"><input type="checkbox" name="create_not_unique_tasks"></form>"""
        info = {"id": 32, "name": "ΗΛΕΚΤΡΟΛΟΓΟΣ", "max_works": 100, "only_tracking": 0, "grouped": 0, "is_active": 1}
        listing = '<div class="workstationInfo d-none">' + html.escape(json.dumps(info)) + '</div>'
        session = Session({"/workstations": [Response(""), Response(ws_form), Response(listing)]},
                          [Response(status=302, location=BASE + "/workstations")])
        result = self.writer(session).create_workstation("ΗΛΕΚΤΡΟΛΟΓΟΣ", confirm=True)
        data = session.sent[0][1]["data"]
        self.assertEqual((data["name"], data["max_works"], data["group_by_value"]), ("ΗΛΕΚΤΡΟΛΟΓΟΣ", "100", "group_by_default"))
        self.assertNotIn("tracking_info", data)
        self.assertEqual((result["id"], result["verified"]), (32, True))

    def test_check_rule_fixes(self):
        graph = wf.parse_workflow(page(elements=[element(1, 2, "LASER", "ΚΟΠΗ", 24, 0), element(2, 2, "LASER", "ΠΑΡΕΛΚΟΜΕΝΑ", 22, 120),
                                                  element(3, 23, "ΨΥΚΤΙΚΑ", "ΕΓΚΑΤΑΣΤΑΣΗ ΨΥΚΤΙΚΩΝ", 14, 240)]))
        attached = {"6": {"1": {"is_show": 1, "is_check": 1}, "2": {"is_show": 1}},
                    "13": {"1": {"is_show": 1, "is_check": 1}, "2": {"is_show": 1, "is_check": 1}},
                    "39": {"3": {"is_show": 1, "is_check": 1}, "2": {"is_show": 1, "is_check": 1}}}
        fixes, notes = wf.check_rule_fixes(graph, attached)
        self.assertEqual(fixes, {6: {1: {"show": True}}, 13: {2: {}}, 39: {2: {}}})
        self.assertEqual(len(notes), 3)

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
