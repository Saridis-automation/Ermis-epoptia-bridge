"""Nightly backup against synthetic pages: paging, history, gone marking, secrets, night guard."""
import gzip
import html
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import epoptia_backup as b

BASE = "https://epoptia.example"


def info(cls, records):
    return "".join(f'<div class="{cls} d-none">{html.escape(json.dumps(r))}</div>' for r in records)


def page_links(path, last):
    return "".join(f'<a href="{BASE}{path}?per_page=100&amp;term=&amp;page={n}">{n}</a>' for n in range(1, last + 1))


class Response:
    def __init__(self, text="", status=200, body=None):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": "application/json" if body is not None else "text/html"}
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        path = url[len(BASE):]
        if path not in self.pages:
            raise AssertionError(f"unexpected GET {path}")
        return self.pages[path]


def wol_page(lines, pages):
    return Response(body={"workorderLines": lines, "numberOfPages": pages, "total": 0})


class BackupTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = b.Store(self.dir / "db.sqlite")

    def backup(self, pages, wol_pages=()):
        api = list(wol_pages)
        self.api_calls = []

        def api_get(url, **kwargs):
            self.api_calls.append(kwargs["params"]["page"])
            return api.pop(0)

        return b.Backup(BASE, "KEY", "u", "p", store=self.store, raw_dir=self.dir / "raw",
                        session=FakeSession(pages), login=lambda *a: True, api_get=api_get)

    def rows(self, kind):
        return {r[0]: (json.loads(r[1]), r[2]) for r in self.store.db.execute(
            "SELECT id, data, gone_since FROM records WHERE kind=?", (kind,))}

    def test_products_walk_all_pages_from_links(self):
        p1 = page_links("/product/create", 2) + info("deleteProductInfo", [{"id": i, "name": f"P{i}"} for i in range(1, 101)])
        p2 = info("deleteProductInfo", [{"id": 101, "name": "P101"}])
        backup = self.backup({"/product/create?per_page=100&page=1": Response(p1),
                              "/product/create?per_page=100&page=2": Response(p2)})
        backup.copy_list("product", "/product/create", "deleteProductInfo", "t1")
        self.assertEqual(len(self.rows("product")), 101)
        self.assertEqual(backup.summary["product"]["new"], 101)

    def test_client_password_is_never_stored(self):
        page = info("deleteClientInfo", [{"id": 7, "name": "A", "passwd": "secret-hash"}])
        self.backup({"/client/create?per_page=100&page=1": Response(page)}).copy_list(
            "client", "/client/create", "deleteClientInfo", "t1")
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn("secret-hash", dump)
        self.assertNotIn("passwd", self.rows("client")[7][0])

    def test_change_history_and_gone_marking(self):
        path = "/tags?per_page=100&page=1"
        first = info("deleteTagInfo", [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
        self.backup({path: Response(first)}).copy_list("tag", "/tags", "deleteTagInfo", "t1")
        second = info("deleteTagInfo", [{"id": 1, "name": "a2"}])
        backup = self.backup({path: Response(second)})
        backup.copy_list("tag", "/tags", "deleteTagInfo", "t2")
        rows = self.rows("tag")
        self.assertEqual(rows[1][0]["name"], "a2")
        self.assertEqual(rows[2][1], "t2")          # gone, but kept
        history = self.store.db.execute("SELECT COUNT(*) FROM history WHERE kind='tag' AND id=1").fetchone()[0]
        self.assertEqual(history, 2)
        self.assertEqual(backup.summary["tag"], {"seen": 1, "new": 0, "changed": 1, "gone": 1})

    def test_empty_first_page_is_an_error_not_a_mass_gone(self):
        self.backup({"/tags?per_page=100&page=1": Response(info("deleteTagInfo", [{"id": 1}]))}).copy_list(
            "tag", "/tags", "deleteTagInfo", "t1")
        with self.assertRaises(b.BackupError):
            self.backup({"/tags?per_page=100&page=1": Response("<html>login</html>")}).copy_list(
                "tag", "/tags", "deleteTagInfo", "t2")
        self.assertIsNone(self.rows("tag")[1][1])

    def test_workorderlines_all_api_pages(self):
        backup = self.backup({}, [wol_page([{"workorderline_id": 1, "x": 1}], 2),
                                  wol_page([{"workorderline_id": 2, "x": 2}], 2)])
        backup.copy_workorderlines("t1")
        self.assertEqual(self.api_calls, [1, 2])
        self.assertEqual(sorted(self.rows("workorderline")), [1, 2])

    def test_workflow_pages_saved_without_tokens(self):
        page = '<meta name="csrf-token" content="SECRETCSRF"><input type="hidden" name="_token" value="SECRETTOK">x'
        backup = self.backup({"/workflows/39": Response(page)})
        backup.copy_workflow_pages({39}, "2026-10-01")
        saved = gzip.open(self.dir / "raw" / "2026-10-01" / "workflow_39.html.gz", "rt").read()
        self.assertNotIn("SECRETCSRF", saved)
        self.assertNotIn("SECRETTOK", saved)

    def test_prune_keeps_recent_raw_days(self):
        for day in ("2026-09-01", "2026-09-30"):
            (self.dir / "raw" / day).mkdir(parents=True)
        self.backup({}).prune_raw(datetime(2026, 10, 1).date())
        self.assertEqual(sorted(p.name for p in (self.dir / "raw").iterdir()), ["2026-09-30"])

    def test_night_guard(self):
        self.assertTrue(b.night_allowed(datetime(2026, 10, 1, 23, 30, tzinfo=b.LOCAL_TZ)))
        self.assertTrue(b.night_allowed(datetime(2026, 10, 1, 3, 0, tzinfo=b.LOCAL_TZ)))
        self.assertFalse(b.night_allowed(datetime(2026, 10, 1, 10, 0, tzinfo=b.LOCAL_TZ)))
        self.assertEqual(b.main(["--dir", str(self.dir / "x")]) if not b.night_allowed() else 2, 2)


if __name__ == "__main__":
    unittest.main()
