"""Nightly read-only copy of Epoptia data into our own SQLite database.

Backup plan in case Epoptia stops operating. Only GET requests (plus the
existing web login), every one through epoptia_throttle.call(). Runs once per
night; refuses to start during working hours unless --now is given.

Sources
- API  GET /api/3.03/workorderlines (100 per page): lines with work order,
  client, product, custom fields and routing/progress.
- Web list pages (per_page=100): every row embeds its full record as JSON in
  <div class="delete…Info d-none">. Products, clients, workstations, workflow
  templates, custom fields, tags.
- Web /workflows/{id}: raw HTML of each workflow template (gzip on disk).

Storage (outside the repo, default ~/epoptia-backup):
- epoptia.sqlite  records (current state per kind/id), history (every new or
  changed version), runs (one row per night).
- raw/YYYY-MM-DD/  gzip copies of workflow pages; kept RAW_KEEP_DAYS days.

Never stored: client "passwd", session cookies, CSRF tokens, credentials.
"""
import argparse
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import epoptia_throttle

BACKUP_DIR = Path.home() / "epoptia-backup"
RAW_KEEP_DAYS = 14
LOCAL_TZ = ZoneInfo("Europe/Athens")
NIGHT_START, NIGHT_END = 21, 6          # allowed local hours: 21:00-05:59
MAX_PAGES = 200                          # safety stop per listing
DROP_FIELDS = {"client": {"passwd"}}

LISTS = (
    # kind, path, info div class
    ("product", "/product/create", "deleteProductInfo"),
    ("client", "/client/create", "deleteClientInfo"),
    ("workstation", "/workstations", "deleteWorkstationInfo"),
    ("workflow", "/workflows", "deleteWorkflowInfo"),
    ("customfield", "/customfields", "deleteCustomfieldInfo"),
    ("tag", "/tags", "deleteTagInfo"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, id INTEGER NOT NULL, data TEXT NOT NULL, hash TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, gone_since TEXT,
    PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS history (
    kind TEXT NOT NULL, id INTEGER NOT NULL, seen_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS history_kind_id ON history (kind, id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, finished TEXT,
    status TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0, summary TEXT, error TEXT);
"""

INFO_RE_TEMPLATE = r'<div class="{cls} d-none">(.*?)</div>'


class BackupError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def extract_records(text, info_class):
    """Full JSON records embedded in a list page."""
    out = []
    for blob in re.findall(INFO_RE_TEMPLATE.format(cls=re.escape(info_class)), text, re.S):
        try:
            record = json.loads(html.unescape(blob))
        except ValueError:
            raise BackupError(f"unparseable {info_class} record")
        if not isinstance(record, dict) or type(record.get("id")) is not int:
            raise BackupError(f"{info_class} record without integer id")
        out.append(record)
    return out


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def start_run(self):
        cur = self.db.execute("INSERT INTO runs (started, status) VALUES (?, 'running')", (_now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id, status, requests_made, summary, error=None):
        self.db.execute("UPDATE runs SET finished=?, status=?, requests=?, summary=?, error=? WHERE id=?",
                        (_now(), status, requests_made, json.dumps(summary, ensure_ascii=False), error, run_id))
        self.db.commit()

    def upsert(self, kind, record, seen_at):
        """Store one record; returns 'new', 'changed' or 'same'."""
        record = {k: v for k, v in record.items() if k not in DROP_FIELDS.get(kind, ())}
        data = _canonical(record)
        digest = hashlib.sha256(data.encode()).hexdigest()
        row = self.db.execute("SELECT hash FROM records WHERE kind=? AND id=?", (kind, record["id"])).fetchone()
        if row is None:
            self.db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,NULL)",
                            (kind, record["id"], data, digest, seen_at, seen_at))
            state = "new"
        elif row[0] != digest:
            self.db.execute("UPDATE records SET data=?, hash=?, last_seen=?, gone_since=NULL WHERE kind=? AND id=?",
                            (data, digest, seen_at, kind, record["id"]))
            state = "changed"
        else:
            self.db.execute("UPDATE records SET last_seen=?, gone_since=NULL WHERE kind=? AND id=?",
                            (seen_at, kind, record["id"]))
            return "same"
        self.db.execute("INSERT INTO history VALUES (?,?,?,?)", (kind, record["id"], seen_at, data))
        return state

    def mark_gone(self, kind, seen_ids, seen_at):
        """After a COMPLETE listing: records not seen are marked gone (never deleted)."""
        rows = self.db.execute("SELECT id FROM records WHERE kind=? AND gone_since IS NULL", (kind,)).fetchall()
        gone = [r[0] for r in rows if r[0] not in seen_ids]
        self.db.executemany("UPDATE records SET gone_since=? WHERE kind=? AND id=?",
                            [(seen_at, kind, i) for i in gone])
        return len(gone)

    def commit(self):
        self.db.commit()


class Backup:
    def __init__(self, base_url, api_key, username, password, *, store, raw_dir,
                 session=None, login=None, api_get=None):
        self.base = base_url.rstrip("/")
        self.api_headers = {"X-Auth-Token": api_key, "Accept": "application/json"}
        self._username, self._password = username, password
        self.session = session if session is not None else requests.Session()
        self._login = login
        self._api_get = api_get or requests.get
        self.store = store
        self.raw_dir = Path(raw_dir)
        self.requests = 0
        self.summary = {}

    def _call(self, fn, *args, **kwargs):
        self.requests += 1
        return epoptia_throttle.call(fn, *args, **kwargs)

    def login(self):
        login = self._login
        if login is None:
            from epoptia_read import _web_login as login
        if not login(self.session, self.base, self._username, self._password):
            raise BackupError("Epoptia web login failed")

    def _page(self, path):
        response = self._call(self.session.get, self.base + path, timeout=90, allow_redirects=False,
                              headers={"Accept": "text/html,application/xhtml+xml"})
        if response.status_code != 200 or "html" not in response.headers.get("Content-Type", ""):
            raise BackupError(f"GET {path.split('?')[0]} answered HTTP {response.status_code}")
        return response.text

    def _count(self, kind, state):
        bucket = self.summary.setdefault(kind, {"seen": 0, "new": 0, "changed": 0, "gone": 0})
        bucket["seen"] += 1
        if state != "same":
            bucket[state] += 1

    def copy_list(self, kind, path, info_class, seen_at):
        """Walk every page; the last page number comes from the pagination links."""
        seen, page, last = set(), 1, 1
        self.summary.setdefault(kind, {"seen": 0, "new": 0, "changed": 0, "gone": 0})
        while page <= last:
            if page > MAX_PAGES:
                raise BackupError(f"{kind}: more than {MAX_PAGES} pages")
            text = self._page(f"{path}?per_page=100&page={page}")
            last = max([last] + [int(n) for n in re.findall(r"[?&]page=(\d+)", html.unescape(text))])
            records = extract_records(text, info_class)
            if not records and page == 1:
                raise BackupError(f"{kind}: first page has no records")
            for record in records:
                if record["id"] not in seen:
                    seen.add(record["id"])
                    self._count(kind, self.store.upsert(kind, record, seen_at))
            page += 1
        self.summary[kind]["gone"] = self.store.mark_gone(kind, seen, seen_at)
        self.store.commit()
        return seen

    def copy_workorderlines(self, seen_at):
        self.summary.setdefault("workorderline", {"seen": 0, "new": 0, "changed": 0, "gone": 0})
        seen, page, pages = set(), 1, 1
        while page <= pages:
            response = self._call(self._api_get, self.base + "/api/3.03/workorderlines",
                                  headers=self.api_headers, params={"page": page, "limit": 100}, timeout=120)
            if response.status_code != 200:
                raise BackupError(f"workorderlines page {page} answered HTTP {response.status_code}")
            body = response.json()
            pages = int(body["numberOfPages"])
            if pages > MAX_PAGES:
                raise BackupError("workorderlines: too many pages")
            for line in body["workorderLines"]:
                record = dict(line, id=line["workorderline_id"])
                seen.add(record["id"])
                self._count("workorderline", self.store.upsert("workorderline", record, seen_at))
            page += 1
        self.summary["workorderline"]["gone"] = self.store.mark_gone("workorderline", seen, seen_at)
        self.store.commit()

    def copy_workflow_pages(self, workflow_ids, day):
        target = self.raw_dir / day
        target.mkdir(parents=True, exist_ok=True)
        for workflow_id in sorted(workflow_ids):
            text = self._page(f"/workflows/{int(workflow_id)}")
            text = re.sub(r'(name="_token"\s+value=")[^"]*', r"\1", text)
            text = re.sub(r'(name="csrf-token"\s+content=")[^"]*', r"\1", text)
            with gzip.open(target / f"workflow_{int(workflow_id)}.html.gz", "wt", encoding="utf-8") as fh:
                fh.write(text)
        self.summary["workflow_pages"] = len(workflow_ids)

    def prune_raw(self, today):
        cutoff = today - timedelta(days=RAW_KEEP_DAYS)
        for child in self.raw_dir.glob("????-??-??"):
            try:
                if datetime.strptime(child.name, "%Y-%m-%d").date() < cutoff:
                    shutil.rmtree(child)
            except ValueError:
                continue

    def run(self, *, only=None):
        seen_at = _now()
        local_today = datetime.now(LOCAL_TZ).date()
        self.login()
        workflow_ids = set()
        for kind, path, info_class in LISTS:
            if only and kind not in only:
                continue
            ids = self.copy_list(kind, path, info_class, seen_at)
            if kind == "workflow":
                workflow_ids = ids
        if not only or "workorderline" in only:
            self.copy_workorderlines(seen_at)
        if workflow_ids and (not only or "workflow_pages" in only):
            self.copy_workflow_pages(workflow_ids, local_today.isoformat())
        self.prune_raw(local_today)
        return self.summary


def night_allowed(now=None):
    hour = (now or datetime.now(LOCAL_TZ)).hour
    return hour >= NIGHT_START or hour < NIGHT_END


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nightly read-only Epoptia backup.")
    parser.add_argument("--dir", default=str(BACKUP_DIR))
    parser.add_argument("--now", action="store_true", help="allow a run outside night hours")
    parser.add_argument("--only", help="comma list of kinds (product,client,...,workorderline,workflow_pages)")
    args = parser.parse_args(argv)
    if not args.now and not night_allowed():
        print("refusing: backups run only at night (21:00-06:00 Europe/Athens); use --now", file=sys.stderr)
        return 2
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    store = Store(directory / "epoptia.sqlite")
    backup = Backup(os.getenv("EPOPTIA_BASE_URL"), os.getenv("EPOPTIA_API_KEY"),
                    os.getenv("EPOPTIA_USERNAME"), os.getenv("EPOPTIA_PASSWORD"),
                    store=store, raw_dir=directory / "raw")
    run_id = store.start_run()
    only = set(args.only.split(",")) if args.only else None
    try:
        summary = backup.run(only=only)
    except (BackupError, epoptia_throttle.EpoptiaHalted, requests.RequestException, KeyError, ValueError) as exc:
        store.finish_run(run_id, "failed", backup.requests, backup.summary, f"{type(exc).__name__}: {exc}")
        print(f"backup FAILED after {backup.requests} requests: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    store.finish_run(run_id, "ok" if not only else "partial", backup.requests, summary)
    print(json.dumps({"run": run_id, "requests": backup.requests, "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
