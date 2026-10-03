"""Epoptia workflows (routings): read, create, modify, delete through the web UI.

How Epoptia stores a workflow (found 2026-10-02 from /workflows/{id} and js/create_lines.js):
- The page embeds the saved graph as JSON in <… id="workflowServer">: `elements` (steps:
  workstation, job tag, strict/semi-strict/free pass, position, comment) and `links`
  (parent_element -> child_element, output_id/input_id dots).
- Saving posts the WHOLE graph: form #workflowForm, POST /workflow-update/{id}, _method=put,
  workflowData = {lineId: {source: node, target: node, input, output, id: link id or 0}},
  workflowName, workflowComments, madeChangesAtWorkflow, elementCustomFields, elementFiles,
  elementFilesFirst, deleteCustomFieldsFromWorkflow. A node is identified by a page id
  (`id`), its workstation (`se_id`) and its saved element id (`node_id`, 0 = new).
  Steps without any link are not saved. Every step needs a job tag.
- New template: the editor page /workflows/create posts name + whole graph to /workflow-create
  (the /workflows "add" modal posting to /workflows/store answers 405).

Levels (2026-10-02): products point straight at a TEMPLATE (no per-product copy). A work order
line gets its OWN workflow copy (type "workorderline", name "workorderline: <id>") when it goes
into production; edit it at /workorderlines/{wol}/workflow/{wf} (same save + workorderLine field).
Steps with progress are locked as the page locks them; a line still on a template is refused.

Safety: preview unless confirm=True; the live form is checked before every write; the
current graph must equal what the caller expects (no lost concurrent edits); after a save
the graph and the attached custom fields are read back and compared. Attached custom fields
are kept (saved per-step settings; new steps visible). Workflows with attached FILES are refused.
"""
import html
import json
import re
from urllib.parse import quote

from epoptia_write import FormChanged, FormSpec, WebWriter, WriteError, _clean_text, _redact

WORKFLOW_UPDATE = FormSpec(
    "/workflows/{id}", "workflowForm", "POST", "/workflow-update/{id}",
    (("_token", "hidden", False), ("_method", "hidden", False),
     ("deleteCustomFieldsFromWorkflow", "hidden", False), ("elementFiles", "hidden", False),
     ("elementFilesFirst", "hidden", False), ("elementCustomFields", "hidden", False),
     ("workflowData", "hidden", False), ("workflowName", "hidden", False),
     ("madeChangesAtWorkflow", "hidden", False), ("workflowComments", "hidden", False)))

# The /workflows "add" modal posts to /workflows/store, which answers 405 (dead route).
# The real editor page /workflows/create posts the whole graph to /workflow-create.
WORKFLOW_CREATE = FormSpec(
    "/workflows/create", None, "POST", "/workflow-create",
    (("_token", "hidden", False), ("elementCustomFields", "hidden", False),
     ("workflowData", "hidden", False), ("workflowName", "hidden", False),
     ("madeChangesAtWorkflow", "hidden", False), ("workflowComments", "hidden", False)))

WORKFLOW_DELETE = FormSpec(
    "/workflows", "workflowDeleteForm", "POST", "/workflows/destroy/0",
    (("_method", "hidden", False), ("_token", "hidden", False)))

CUSTOM_FIELD_FORM_FIELDS = (
    ("_token", "hidden", False), ("_method", "hidden", False), ("name", "text", True),
    ("name_second", "text", False), ("category", "select", False), ("code", "text", False),
    ("group", "select", False), ("type", "select", False), ("dropDownOptions[]", "text", False),
    ("limit_down", "number", False), ("limit_up", "number", False), ("has_equal", "checkbox", False),
    ("equationField", "text", False), ("#equation_set_value", "checkbox", False),
    ("affected_custom_field", "select", False), ("value_equal", "text", False),
    ("is_active", "hidden", False), ("connect_to_remote_workflows", "checkbox", False),
    ("edit_rule", "hidden", False))
CUSTOM_FIELD_STORE_MARKER = re.compile(r'\$\("#customFieldForm"\)\.attr\("action", "[^"]*/customfields/store"\)')
CUSTOM_FIELD_DELETE = FormSpec(
    "/customfields", "customfieldDeleteForm", "POST", "/customfields/destroy/0",
    (("_method", "hidden", False), ("_token", "hidden", False)))

TAG_UPDATE = FormSpec(
    "/tags", "tagForm", "POST", "/tags/update/0",
    (("_method", "hidden", False), ("_token", "hidden", False), ("section[]", "checkbox", False),
     ("name", "text", True), ("section", "hidden", False)))

MODES = {"strict": (1, 0, 0), "semi": (0, 1, 0), "free": (0, 0, 1)}


def parse_workflow(text):
    """Saved graph from a /workflows/{id} page -> plain dict (no page ids)."""
    match = re.search(r'id="workflowServer"[^>]*>(.*?)</', text, re.S)
    if not match:
        raise WriteError("workflow data not found on the page")
    data = json.loads(html.unescape(match.group(1)))
    elements = data["elements"].values() if isinstance(data["elements"], dict) else data["elements"]
    links = data["links"].values() if isinstance(data["links"], dict) else data["links"]
    nodes = {}
    for e in elements:
        mode = "strict" if e.get("is_strict") else "semi" if e.get("is_semi_strict") else "free"
        nodes[e["id"]] = dict(node_id=e["id"], workstation_id=e["workstation_id"],
                              workstation=(e.get("workstation") or {}).get("name"),
                              tag=e.get("tag") or "", tag_id=e.get("tag_id") or "", mode=mode,
                              is_or=1 if e.get("is_or") else 0,
                              top=e.get("position_top") or "0px", left=e.get("position_left") or "0px",
                              comment=e.get("comments") or "", default_average=e.get("default_average") or 0)
    edges = [dict(link_id=l["id"], parent=l["parent_element"], child=l["child_element"],
                  output=l.get("output_id") or "output-right", input=l.get("input_id") or "input-left")
             for l in links]
    return dict(id=data.get("id"), name=data.get("name"), comments=data.get("comments") or "",
                template=bool(data.get("template")), nodes=nodes, links=edges,
                files=data.get("files") or [], element_files=data.get("elementsFiles") or [])


def attached_custom_fields(text):
    """{custom_field_id: {element_id: settings}} attached to the workflow (#tmpWorkflowElementsCustomFields).

    Every workflow page lists the whole custom-field catalogue with add/remove buttons, so those
    buttons say nothing; this element holds what is actually attached (e.g. 39: fields 6-12, all
    is_show=1). Empty list/dict = none attached.
    """
    match = re.search(r'id="tmpWorkflowElementsCustomFields"[^>]*>(.*?)</', text, re.S)
    if not match:
        raise WriteError("custom-field data not found on the workflow page")
    value = html.unescape(match.group(1)).strip()
    data = json.loads(value) if value else {}
    return data if isinstance(data, dict) else {}


def has_custom_field_settings(text):
    return bool(attached_custom_fields(text))


def mandatory_flags(text):
    """{custom_field_id: bool} 'mandatory check before production' of the attached fields."""
    return {m.group(1): m.group(2) not in ("", "0", "false")
            for m in re.finditer(r'<button[^>]*data-id="(\d+)"[^>]*customFieldEdit[^>]*'
                                 r'data-mandatorycheckbeforeproduction="([^"]*)"', text)}


SETTING_KEYS = (("is_show", "show"), ("is_check", "check"), ("editable", "editable"),
                ("mandatory_editable", "mandatory-editable"), ("strict_comparison", "strict_comparison"))


def build_custom_fields(graph, attached, flags, *, new_steps_show=True, step_settings=None):
    """elementCustomFields as the editor posts it: every attached field, settings for EVERY step.

    Existing steps keep their saved settings; new steps get show=true (SARIDIS: every station
    sees every field). Keys are the page ids used in workflowData ("ermis-<key>").
    `step_settings` = {field_id: {step_key: {"show": bool, "check": bool}}} overrides single steps
    (check = the station must tick the checkbox before it can finish the step). A field named there
    but not yet attached gets attached, shown only on the steps listed.
    """
    step_settings = {str(cf): dict(v) for cf, v in (step_settings or {}).items()}
    out = {}
    for cf_id in list(attached) + [cf for cf in step_settings if cf not in attached]:
        saved = attached.get(cf_id)
        overrides = step_settings.get(str(cf_id), {})
        elements = {}
        for key, node in graph["nodes"].items():
            old = saved.get(str(node.get("node_id"))) if saved is not None and node.get("node_id") else None
            if key in overrides:
                settings = {k: True for k in ("show", "check") if overrides[key].get(k)}
            elif old is not None:
                settings = {new: True for src, new in SETTING_KEYS if old.get(src)}
            elif node.get("node_id"):
                settings = {}   # saved step where the field is hidden: stays hidden
            else:
                settings = {"show": True} if new_steps_show and saved is not None else {}
            if settings:
                elements[f"ermis-{key}"] = settings
        out[str(cf_id)] = dict(mandatorycheckbeforeproduction=bool(flags.get(str(cf_id))), elements=elements)
    return out


def step_settings_problems(graph, step_settings):
    problems = []
    for cf, steps in (step_settings or {}).items():
        for key, settings in steps.items():
            if key not in graph["nodes"]:
                problems.append(f"field {cf}: step {key!r} is not in the workflow")
            if settings.get("check") and not settings.get("show"):
                problems.append(f"field {cf}: step {key!r} cannot check a hidden field")
    return problems


def settings_match(graph_after, attached_after, wanted, graph_sent):
    """Saved per-step show/check equal what was asked; steps matched by (station, tag)."""
    def ident(n):
        return (n["workstation_id"], str(n["tag"]))
    after_by_ident = {}
    for n in graph_after["nodes"].values():
        after_by_ident.setdefault(ident(n), []).append(str(n["node_id"]))
    for cf, steps in (wanted or {}).items():
        saved = attached_after.get(str(cf))
        if saved is None:
            return False
        for key, settings in steps.items():
            ids = after_by_ident.get(ident(graph_sent["nodes"][key]), [])
            if len(ids) != 1:
                return False
            got = saved.get(ids[0]) or {}
            if bool(got.get("is_show")) != bool(settings.get("show")) or \
                    bool(got.get("is_check")) != bool(settings.get("check")):
                return False
    return True


def signature(graph):
    """Comparable shape: steps (station, tag, mode) and links between them, ignoring ids/positions."""
    nodes = graph["nodes"]
    def key(n):
        return (n["workstation_id"], str(n["tag"]), n["mode"], n["is_or"], n["comment"])
    steps = sorted(key(n) for n in nodes.values())
    links = sorted((key(nodes[l["parent"]]), key(nodes[l["child"]])) for l in graph["links"])
    return dict(steps=steps, links=links)


def build_workflow_data(graph):
    """The exact workflowData object the editor page posts (createRequestObject)."""
    nodes = graph["nodes"]

    def node(key):
        n = nodes[key]
        strict, semi, free = MODES[n["mode"]]
        node_id = n.get("node_id") or 0
        return dict(se_id=str(n["workstation_id"]), id=f"ermis-{key}", top=n.get("top", "0px"),
                    left=n.get("left", "0px"), strict=strict, semiStrict=semi, freePass=free,
                    **{"or": n.get("is_or", 0)}, node_id=node_id if isinstance(node_id, int) else 0,
                    comment=n.get("comment", ""), default_average=n.get("default_average", 0),
                    tag=n["tag"], tag_id=n["tag_id"])
    out = {}
    for index, link in enumerate(graph["links"], 1):
        if link["parent"] not in nodes or link["child"] not in nodes:
            raise WriteError("link points to a missing step")
        out[f"ermis-line-{index}"] = dict(source=node(link["parent"]), target=node(link["child"]),
                                          input=link.get("input", "input-left"),
                                          output=link.get("output", "output-right"),
                                          id=link.get("link_id") or 0)
    return out


def validate_graph(graph, current=None):
    """`current` (the saved graph) lets saved steps keep an empty tag when they are not changed:
    some SARIDIS templates have untagged steps; only new or changed steps must carry a tag."""
    if not graph["links"]:
        raise WriteError("a workflow needs at least one link (unlinked steps are not saved)")
    linked = {l["parent"] for l in graph["links"]} | {l["child"] for l in graph["links"]}
    for key, n in graph["nodes"].items():
        if key not in linked:
            raise WriteError(f"step {key} has no link and would be dropped")
        if not str(n.get("tag")) or not n.get("tag_id"):
            old = (current or {}).get("nodes", {}).get(key)
            unchanged = old is not None and all(old.get(f) == n.get(f) for f in
                                                ("workstation_id", "tag", "tag_id", "mode", "is_or"))
            if not unchanged:
                raise WriteError(f"step {key} ({n.get('workstation')}) needs a job tag")
        if n.get("mode") not in MODES:
            raise WriteError(f"step {key}: mode must be strict, semi or free")
        if type(n.get("workstation_id")) is not int:
            raise WriteError(f"step {key}: workstation_id must be an integer")


def replace_changed_stations(graph, current):
    """Epoptia keeps a saved step's workstation even if a new se_id is posted (verified
    2026-10-03: only the tag changed). A station change is therefore a NEW step in the same
    place, with the old step's links re-made as new links - what a user does on the page."""
    nodes, links = dict(graph["nodes"]), [dict(l) for l in graph["links"]]
    for key, node in list(nodes.items()):
        old = current["nodes"].get(key)
        if old is None or node.get("node_id") != old["node_id"] or node["workstation_id"] == old["workstation_id"]:
            continue
        new_key = f"swap{key}"
        nodes[new_key] = dict(node, node_id=0)
        del nodes[key]
        for link in links:
            if link["parent"] == key:
                link.update(parent=new_key, link_id=0)
            if link["child"] == key:
                link.update(child=new_key, link_id=0)
    return dict(graph, nodes=nodes, links=links)


def linear_graph(steps):
    """Steps [(workstation_id, workstation, tag, tag_id), ...] -> new nodes linked in order."""
    nodes, links = {}, []
    for i, (ws_id, ws_name, tag, tag_id) in enumerate(steps, 1):
        nodes[f"new{i}"] = dict(node_id=0, workstation_id=ws_id, workstation=ws_name, tag=tag,
                                tag_id=tag_id, mode="semi", is_or=0, top="0px",
                                left=f"{(i - 1) * 120}px", comment="", default_average=0)
        if i > 1:
            links.append(dict(link_id=0, parent=f"new{i - 1}", child=f"new{i}",
                              output="output-right", input="input-left"))
    return dict(nodes=nodes, links=links)


def wol_hits(text):
    match = re.search(r'id="workorderLineHits"[^>]*>(.*?)</div>', text, re.S)
    if not match:
        raise WriteError("step progress (workorderLineHits) not found on the line's workflow page")
    value = html.unescape(match.group(1)).strip()
    data = json.loads(value) if value else {}
    return data if isinstance(data, dict) else {}


def locked_steps(text, graph):
    """Saved steps the page itself would not let you move or unlink (as js/create_lines.js):
    no progress record, started at least once, done, or being worked on now."""
    hits = wol_hits(text)
    locked = set()
    for key, node in graph["nodes"].items():
        info = (hits.get(str(node["node_id"])) or {}).get("elementInfo")
        if info is None or info.get("first_start") or info.get("element_done") or info.get("working_now"):
            locked.add(key)
    return locked


def wol_problems(parser, text, current, graph, wol_id):
    """Extra checks for a work order line's own workflow."""
    problems = []
    form = parser.forms.get(WORKFLOW_UPDATE.form_id) or {}
    if str((form.get("hidden_values") or {}).get("workorderLine")) != str(int(wol_id)):
        problems.append("the page is not this work order line's workflow")
    match = re.search(r'id="workflowServer"[^>]*>(.*?)</', text, re.S)
    meta = json.loads(html.unescape(match.group(1))) if match else {}
    if meta.get("template") or meta.get("type") != "workorderline":
        problems.append("this line still uses a TEMPLATE (not started in production): saving would change "
                        "the template for every product; refused")
    locked = locked_steps(text, current)
    for key in locked:
        old, new = current["nodes"][key], graph["nodes"].get(key)
        if new is None:
            problems.append(f"step {old['workstation']} ({old['tag']}) has progress and cannot be removed")
            continue
        for field in ("workstation_id", "tag_id", "mode", "is_or"):
            if new.get(field) != old.get(field):
                problems.append(f"step {old['workstation']} ({old['tag']}) has progress: {field} cannot change")
    kept = {(l["parent"], l["child"]) for l in graph["links"]}
    for link in current["links"]:
        if link["parent"] in locked and (link["parent"], link["child"]) not in kept:
            problems.append(f"link from {current['nodes'][link['parent']]['workstation']} has progress and cannot be removed")
    return problems


class WorkflowWriter:
    def __init__(self, writer: WebWriter):
        self.w = writer

    def read(self, workflow_id):
        parser, text = self.w._get_page(f"/workflows/{int(workflow_id)}")
        return parser, text, parse_workflow(text)

    def find(self, name):
        parser, text = self.w._get_page("/workflows?per_page=100&term=" + quote(name))
        found = []
        for blob in re.findall(r'<div class="deleteWorkflowInfo d-none">(.*?)</div>', text, re.S):
            record = json.loads(html.unescape(blob))
            if record.get("name") == name:
                found.append(record["id"])
        return found

    def create(self, name, graph, *, comments="", custom_fields=(), step_settings=None, confirm=False):
        """New workflow template with its graph in one request (as the editor page does).

        `custom_fields` are shown on every step; `step_settings` (see build_custom_fields) attaches
        further fields only where listed, e.g. a checkbox the station must tick on one step."""
        action = "create_workflow"
        _clean_text(name, 150)
        validate_graph(graph)
        problems = step_settings_problems(graph, step_settings)
        if problems:
            raise WriteError("; ".join(problems))
        parser, _ = self.w._get_page(WORKFLOW_CREATE.page)
        form = next((f for f in parser.all_forms
                     if self.w._same_origin_path(f.get("action")) == WORKFLOW_CREATE.action), None)
        if form is None:
            raise FormChanged(f"create form ({WORKFLOW_CREATE.action}) not found")
        expected = {k: (t, r) for k, t, r in WORKFLOW_CREATE.fields}
        if form["fields"] != expected or form["method"] != "POST":
            self.w.log(action, "form_check", ok=False, problems=[str(form["fields"])])
            raise FormChanged(f"create form changed: {form['fields']}")
        self.w.log(action, "form_check", page=WORKFLOW_CREATE.page, ok=True, problems=[])
        if self.find(name):
            raise WriteError(f"a workflow named {name!r} already exists")
        data = build_workflow_data(graph)
        cf = build_custom_fields(graph, {str(c): {} for c in custom_fields}, {}, step_settings=step_settings)
        payload = {"_token": form["token"], "elementCustomFields": json.dumps(cf) if cf else "",
                   "workflowData": json.dumps(data, ensure_ascii=False), "workflowName": name,
                   "madeChangesAtWorkflow": "1", "workflowComments": comments}
        preview = {"method": "POST", "path": WORKFLOW_CREATE.action,
                   "payload": _redact(dict(payload, workflowData=data))}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self.w._send(action, WORKFLOW_CREATE.action, form=payload)
        location = self.w._same_origin_path(response.headers.get("Location"))
        if response.status_code not in (200, 302) or location == "/login":
            raise WriteError(f"{WORKFLOW_CREATE.action} answered HTTP {response.status_code}")
        ids = self.find(name)
        result = {"sent": True, "ids": ids, "location": location}
        if len(ids) == 1:
            _, text_after, _ = self.read(ids[0])
            after = parse_workflow(text_after)
            attached = attached_custom_fields(text_after)
            everywhere = {str(c) for c in custom_fields}
            result["verified"] = (signature(after) == signature(graph)
                                  and set(attached) == everywhere | {str(c) for c in (step_settings or {})}
                                  and all(len(attached[c]) == len(after["nodes"])
                                          and all(e.get("is_show") for e in attached[c].values())
                                          for c in everywhere - {str(c) for c in (step_settings or {})})
                                  and settings_match(after, attached, step_settings, graph))
            result["after"], result["custom_fields"] = after, attached
        else:
            result["verified"] = False
        self.w.log(action, "verified" if result["verified"] else "unverified", ids=ids)
        return result

    def update(self, workflow_id, graph, *, expected_current, name=None, comments=None,
               custom_fields="keep", step_settings=None, confirm=False, _wol=None):
        """Replace the graph of a workflow; `expected_current` = signature() the caller based it on.

        `step_settings` (see build_custom_fields) changes show/check of single steps or attaches a
        field to chosen steps; everything else keeps its saved settings."""
        action = "update_workflow" if _wol is None else "update_wol_workflow"
        if _wol is None:
            parser, text, current = self.read(workflow_id)
            page = WORKFLOW_UPDATE.page.format(id=workflow_id)
            fields = WORKFLOW_UPDATE.fields
        else:
            page = f"/workorderlines/{int(_wol)}/workflow/{int(workflow_id)}"
            parser, text = self.w._get_page(page)
            current = parse_workflow(text)
            fields = WORKFLOW_UPDATE.fields + (("workorderLine", "hidden", False),)
        locked_now = locked_steps(text, current) if _wol is not None else set()
        swapped = [k for k in graph["nodes"] if k in current["nodes"] and k not in locked_now
                   and graph["nodes"][k]["workstation_id"] != current["nodes"][k]["workstation_id"]]
        if swapped:
            graph = replace_changed_stations(graph, current)
        validate_graph(graph, current)
        spec = FormSpec(page, WORKFLOW_UPDATE.form_id, WORKFLOW_UPDATE.method,
                        WORKFLOW_UPDATE.action.format(id=workflow_id), fields)
        problems = self.w._check_form(parser, spec)
        if _wol is not None:
            problems += wol_problems(parser, text, current, graph, _wol)
        form = parser.forms.get(spec.form_id) or {}
        if str(form.get("method_override", "")).lower() != "put":
            problems.append("update form lost _method=put")
        if signature(current) != expected_current:
            problems.append("the workflow changed since it was read; read it again")
        if current["files"] or current["element_files"]:
            problems.append("workflow has attached files: not supported yet")
        attached = attached_custom_fields(text)
        if custom_fields not in ("keep", "empty"):
            problems.append("custom_fields must be 'keep' or 'empty'")
        if step_settings and custom_fields != "keep":
            problems.append("step_settings need custom_fields='keep'")
        problems += step_settings_problems(graph, step_settings)
        self.w.log(action, "form_check", page=spec.page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        data = build_workflow_data(graph)
        cf = (build_custom_fields(graph, attached, mandatory_flags(text), step_settings=step_settings)
              if custom_fields == "keep" else {})
        payload = {"_token": form["token"], "_method": "put", "deleteCustomFieldsFromWorkflow": "",
                   "elementFiles": "", "elementFilesFirst": "",
                   "elementCustomFields": json.dumps(cf) if cf else "",
                   "workflowData": json.dumps(data, ensure_ascii=False),
                   "workflowName": name if name is not None else current["name"],
                   "madeChangesAtWorkflow": "1",
                   "workflowComments": comments if comments is not None else current["comments"]}
        if _wol is not None:
            payload["workorderLine"] = str(int(_wol))
        preview = {"method": "POST", "path": spec.action, "payload": _redact(dict(payload, workflowData=data))}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self.w._send(action, spec.action, form=payload)
        if response.status_code not in (200, 302) or self.w._same_origin_path(response.headers.get("Location")) == "/login":
            raise WriteError(f"{spec.action} answered HTTP {response.status_code}")
        if _wol is None:
            _, text_after, after = self.read(workflow_id)
        else:
            _, text_after = self.w._get_page(page)
            after = parse_workflow(text_after)
        attached_after = attached_custom_fields(text_after)
        wanted = signature(dict(graph, nodes={k: dict(v, comment=v.get("comment", ""), is_or=v.get("is_or", 0))
                                               for k, v in graph["nodes"].items()}))
        fields_kept = set(attached_after) == set(attached) | {str(c) for c in (step_settings or {})}
        shown = {cf: sum(1 for e in v.values() if e.get("is_show")) for cf, v in attached_after.items()}
        ok = signature(after) == wanted and fields_kept and settings_match(after, attached_after, step_settings, graph)
        self.w.log(action, "verified" if ok else "unverified", workflow=workflow_id,
                   after=signature(after), custom_fields_before=sorted(attached),
                   custom_fields_after=sorted(attached_after), shown_steps=shown)
        return {"sent": True, "verified": ok, "after": after, "custom_fields": attached_after,
                "fields_kept": fields_kept, "shown_steps": shown, "steps": len(after["nodes"])}

    def read_wol(self, wol_id):
        """The work order line's own workflow: (workflow_id, graph, locked step keys, meta)."""
        _, text = self.w._get_page(f"/workorderlines/{int(wol_id)}")
        ids = sorted(set(re.findall(r"/workorderlines/%d/workflow/(\d+)" % int(wol_id), text)))
        if len(ids) != 1:
            raise WriteError(f"line {wol_id}: expected one workflow link, found {ids}")
        _, page = self.w._get_page(f"/workorderlines/{int(wol_id)}/workflow/{ids[0]}")
        graph = parse_workflow(page)
        meta = json.loads(html.unescape(re.search(r'id="workflowServer"[^>]*>(.*?)</', page, re.S).group(1)))
        return int(ids[0]), graph, locked_steps(page, graph), dict(type=meta.get("type"), template=meta.get("template"),
                                                                 name=meta.get("name"))

    def update_wol(self, wol_id, workflow_id, graph, *, expected_current, custom_fields="keep", confirm=False):
        """Change the steps of ONE work order line in production (its own workflow copy only)."""
        return self.update(workflow_id, graph, expected_current=expected_current,
                           custom_fields=custom_fields, confirm=confirm, _wol=wol_id)

    def delete(self, workflow_id, expected_name, *, confirm=False):
        action = "delete_workflow"
        _, _, current = self.read(workflow_id)
        if current["name"] != expected_name:
            raise WriteError(f"workflow {workflow_id} is {current['name']!r}, not {expected_name!r}")
        parser, _ = self.w._get_page("/workflows")
        return self.w._delete(action, WORKFLOW_DELETE, parser, f"/workflows/destroy/{int(workflow_id)}",
                              {"workflow": int(workflow_id), "name": expected_name}, confirm=confirm)

    # -- custom fields ("ειδικά πεδία") -------------------------------------
    # The /customfields page: form #customFieldForm (its action is set by the page script to
    # /customfields/store for a new field). The page also has a GLOBAL "apply rule" (adds a field
    # to every line/template) - deliberately not used: fields are attached per workflow instead.
    def find_custom_fields(self, name):
        """[(id, name, type, is_active)] with exactly this name (deleted ones are not listed)."""
        _, text = self.w._get_page("/customfields?per_page=100&cf_type=all&term=" + quote(name))
        found = []
        for blob in re.findall(r'<div class="deleteCustomfieldInfo d-none">(.*?)</div>', text, re.S):
            record = json.loads(html.unescape(blob))
            if record.get("name") == name:
                found.append((record["id"], record["name"], record.get("type"), record.get("is_active")))
        return found

    def create_custom_field(self, name, *, field_type="checkbox", category="procedures", confirm=False):
        """New custom field with no global rule; attach it per workflow with step_settings."""
        action = "create_custom_field"
        _clean_text(name, 255)
        if field_type not in ("checkbox", "text"):
            raise WriteError("only checkbox/text fields are supported")
        parser, text = self.w._get_page("/customfields")
        spec = FormSpec("/customfields", "customFieldForm", "POST", None, CUSTOM_FIELD_FORM_FIELDS)
        problems = [p for p in self.w._check_form(parser, spec) if not p.startswith("action ")]
        if not CUSTOM_FIELD_STORE_MARKER.search(text):
            problems.append("page script no longer sets the store action")
        options = re.findall(r'<option value="([a-z_]+)"', text)
        if field_type not in options or category not in options:
            problems.append("type/category option missing")
        self.w.log(action, "form_check", page=spec.page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        if self.find_custom_fields(name):
            raise WriteError(f"a custom field named {name!r} already exists")
        form = parser.forms["customFieldForm"]
        payload = {"_token": form["token"], "_method": "post", "name": name, "name_second": "",
                   "category": category, "code": "", "group": "", "type": field_type,
                   "is_active": "on", "edit_rule": ""}
        preview = {"method": "POST", "path": "/customfields/store", "payload": _redact(payload)}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self.w._send(action, "/customfields/store", form=payload)
        location = self.w._same_origin_path(response.headers.get("Location"))
        if response.status_code not in (200, 302) or location == "/login":
            raise WriteError(f"/customfields/store answered HTTP {response.status_code}")
        found = self.find_custom_fields(name)
        ok = len(found) == 1 and found[0][2] == field_type and bool(found[0][3])
        self.w.log(action, "verified" if ok else "unverified", found=found)
        return {"sent": True, "verified": ok, "id": found[0][0] if len(found) == 1 else None, "found": found}

    def delete_custom_field(self, field_id, expected_name, *, confirm=False):
        if (int(field_id), expected_name) not in [(f[0], f[1]) for f in self.find_custom_fields(expected_name)]:
            raise WriteError(f"custom field {field_id} named {expected_name!r} not found")
        parser, _ = self.w._get_page("/customfields")
        result = self.w._delete("delete_custom_field", CUSTOM_FIELD_DELETE, parser,
                                f"/customfields/destroy/{int(field_id)}",
                                {"custom_field": int(field_id), "name": expected_name}, confirm=confirm)
        if result["sent"]:
            result["verified"] = not any(f[0] == int(field_id) for f in self.find_custom_fields(expected_name))
        return result

    # -- job tags ("ετικέτες") --------------------------------------------------
    # /tags page: #tagForm (PUT /tags/update/{id}; the page script swaps the id), fields
    # name + hidden section (the tag's category; the section[] checkboxes are disabled = not posted).
    # A rename changes the tag everywhere: templates, running lines and history show the new name.
    def tags(self):
        """{id: record} of active tags (all pages listed at once)."""
        _, text = self.w._get_page("/tags?per_page=100")
        found = {}
        for blob in re.findall(r'<div class="deleteTagInfo d-none">(.*?)</div>', text, re.S):
            record = json.loads(html.unescape(blob))
            found[record["id"]] = record
        return found

    def rename_tag(self, tag_id, expected_name, new_name, *, confirm=False):
        action = "rename_tag"
        _clean_text(new_name, 50)
        current = self.tags()
        tag = current.get(int(tag_id))
        if tag is None or tag["name"] != expected_name:
            raise WriteError(f"tag {tag_id} is {tag and tag['name']!r}, not {expected_name!r}")
        if any(t["name"] == new_name for t in current.values()):
            raise WriteError(f"a tag named {new_name!r} already exists")
        parser, _ = self.w._get_page("/tags")
        problems = self.w._check_form(parser, TAG_UPDATE)
        form = parser.forms.get(TAG_UPDATE.form_id) or {}
        if str(form.get("method_override", "")).upper() != "PUT":
            problems.append("tag form lost _method=PUT")
        self.w.log(action, "form_check", page=TAG_UPDATE.page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        path = f"/tags/update/{int(tag_id)}"
        payload = {"_method": "PUT", "_token": form["token"], "name": new_name, "section": tag["section"]}
        preview = {"method": "POST", "path": path, "payload": _redact(payload),
                   "target": {"tag": int(tag_id), "from": expected_name, "to": new_name}}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self.w._send(action, path, form=payload)
        if response.status_code not in (200, 302) or self.w._same_origin_path(response.headers.get("Location")) == "/login":
            raise WriteError(f"{path} answered HTTP {response.status_code}")
        after = self.tags().get(int(tag_id)) or {}
        ok = after.get("name") == new_name and after.get("section") == tag["section"]
        self.w.log(action, "verified" if ok else "unverified", tag=int(tag_id), after=after.get("name"))
        return {"sent": True, "verified": ok, "after": after}
