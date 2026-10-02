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

Safety: preview unless confirm=True; the live form is checked before every write; the
current graph must equal what the caller expects (no lost concurrent edits); after a save
the graph is read back and compared. Custom-field and file settings per step are only
supported when the workflow has none (refused otherwise, until that path is verified).
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


def has_custom_field_settings(text):
    """True if custom fields are attached to the workflow or configured per step."""
    if "customFieldRemove" in text:
        return True
    match = re.search(r'id="tmpWorkflowElementsCustomFields"[^>]*>(.*?)<', text, re.S)
    return bool(match and html.unescape(match.group(1)).strip() not in ("", "{}", "[]"))


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


def validate_graph(graph):
    if not graph["links"]:
        raise WriteError("a workflow needs at least one link (unlinked steps are not saved)")
    linked = {l["parent"] for l in graph["links"]} | {l["child"] for l in graph["links"]}
    for key, n in graph["nodes"].items():
        if key not in linked:
            raise WriteError(f"step {key} has no link and would be dropped")
        if not str(n.get("tag")) or not n.get("tag_id"):
            raise WriteError(f"step {key} ({n.get('workstation')}) needs a job tag")
        if n.get("mode") not in MODES:
            raise WriteError(f"step {key}: mode must be strict, semi or free")
        if type(n.get("workstation_id")) is not int:
            raise WriteError(f"step {key}: workstation_id must be an integer")


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

    def create(self, name, graph, *, comments="", confirm=False):
        """New workflow template with its graph in one request (as the editor page does)."""
        action = "create_workflow"
        _clean_text(name, 150)
        validate_graph(graph)
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
        payload = {"_token": form["token"], "elementCustomFields": "",
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
            _, _, after = self.read(ids[0])
            result["verified"] = signature(after) == signature(graph)
            result["after"] = after
        else:
            result["verified"] = False
        self.w.log(action, "verified" if result["verified"] else "unverified", ids=ids)
        return result

    def update(self, workflow_id, graph, *, expected_current, name=None, comments=None, confirm=False):
        """Replace the graph of a workflow; `expected_current` = signature() the caller based it on."""
        action = "update_workflow"
        validate_graph(graph)
        parser, text, current = self.read(workflow_id)
        spec = FormSpec(WORKFLOW_UPDATE.page.format(id=workflow_id), WORKFLOW_UPDATE.form_id,
                        WORKFLOW_UPDATE.method, WORKFLOW_UPDATE.action.format(id=workflow_id),
                        WORKFLOW_UPDATE.fields)
        problems = self.w._check_form(parser, spec)
        form = parser.forms.get(spec.form_id) or {}
        if str(form.get("method_override", "")).lower() != "put":
            problems.append("update form lost _method=put")
        if signature(current) != expected_current:
            problems.append("the workflow changed since it was read; read it again")
        if current["files"] or current["element_files"] or has_custom_field_settings(text):
            problems.append("workflow has step files/custom-field settings: not supported yet")
        self.w.log(action, "form_check", page=spec.page, ok=not problems, problems=problems)
        if problems:
            raise FormChanged("; ".join(problems))
        data = build_workflow_data(graph)
        payload = {"_token": form["token"], "_method": "put", "deleteCustomFieldsFromWorkflow": "",
                   "elementFiles": "", "elementFilesFirst": "", "elementCustomFields": "",
                   "workflowData": json.dumps(data, ensure_ascii=False),
                   "workflowName": name if name is not None else current["name"],
                   "madeChangesAtWorkflow": "1",
                   "workflowComments": comments if comments is not None else current["comments"]}
        preview = {"method": "POST", "path": spec.action, "payload": _redact(dict(payload, workflowData=data))}
        if not confirm:
            self.w.log(action, "preview", **preview)
            return {"sent": False, **preview}
        response, _ = self.w._send(action, spec.action, form=payload)
        if response.status_code not in (200, 302) or self.w._same_origin_path(response.headers.get("Location")) == "/login":
            raise WriteError(f"{spec.action} answered HTTP {response.status_code}")
        _, _, after = self.read(workflow_id)
        wanted = signature(dict(graph, nodes={k: dict(v, comment=v.get("comment", ""), is_or=v.get("is_or", 0))
                                               for k, v in graph["nodes"].items()}))
        ok = signature(after) == wanted
        self.w.log(action, "verified" if ok else "unverified", workflow=workflow_id,
                   after=signature(after))
        return {"sent": True, "verified": ok, "after": after}

    def delete(self, workflow_id, expected_name, *, confirm=False):
        action = "delete_workflow"
        _, _, current = self.read(workflow_id)
        if current["name"] != expected_name:
            raise WriteError(f"workflow {workflow_id} is {current['name']!r}, not {expected_name!r}")
        parser, _ = self.w._get_page("/workflows")
        return self.w._delete(action, WORKFLOW_DELETE, parser, f"/workflows/destroy/{int(workflow_id)}",
                              {"workflow": int(workflow_id), "name": expected_name}, confirm=confirm)
