"""
Unit tests for the parts of rc_audit_trail.py that do not need network access or credentials.
Run from the repository root:   python -m unittest discover -s tests -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rc_audit_trail as rc  # noqa: E402

# A record shaped like the Audit Trail API's output, with fictional values.
EVENT = {
    "id": "evt-1", "eventTime": "2026-01-15T10:00:00.000Z", "eventType": "Create",
    "actionId": "DELETE_USER:USER", "accountId": "1", "clientIp": "203.0.113.5",
    "details": {"parameters": [{"key": "a", "value": "b"}]},
    "initiator": {"name": "Alex Morgan", "extensionNumber": "101", "role": "Super Admin"},
    "target": {"name": "Jane Doe", "extensionNumber": "1042", "objectType": "Extension"},
}


class Flatten(unittest.TestCase):
    def test_nested_keys_become_dotted_columns(self):
        f = rc.flatten(EVENT)
        self.assertEqual(f["initiator.name"], "Alex Morgan")
        self.assertEqual(f["target.extensionNumber"], "1042")

    def test_list_of_objects_is_kept_as_json_text(self):
        self.assertEqual(json.loads(rc.flatten(EVENT)["details.parameters"]), [{"key": "a", "value": "b"}])

    def test_simple_list_is_joined_and_none_becomes_empty(self):
        f = rc.flatten({"a": ["x", "y"], "b": None})
        self.assertEqual((f["a"], f["b"]), ("x, y", ""))


class RecordId(unittest.TestCase):
    def test_uses_event_id_when_present(self):
        self.assertEqual(rc.record_id(EVENT, rc.flatten(EVENT)), "evt-1")

    def test_hash_fallback_is_stable(self):
        rec = {"x": 1}
        self.assertEqual(rc.record_id(rec, rec), rc.record_id(rec, rec))


class ScheduleValidation(unittest.TestCase):
    ok = {"enabled": True, "every_days": 30, "time": "07:00", "window_days": 35}

    def test_valid_settings_pass(self):
        self.assertEqual(rc.validate_schedule(self.ok), self.ok)

    def test_rejections(self):
        bad = [dict(self.ok, window_days=10),                    # window shorter than interval: would miss events
               dict(self.ok, window_days=181),                   # more than the API keeps
               dict(self.ok, every_days=0),
               dict(self.ok, time="7am"),
               dict(self.ok, time="07:00'; calc #"),             # must never reach PowerShell
               {"enabled": True}]                                # incomplete
        for c in bad:
            with self.subTest(c=c), self.assertRaises(ValueError):
                rc.validate_schedule(c)

    def test_powershell_quoting_doubles_single_quotes(self):
        self.assertEqual(rc.q("it's"), "'it''s'")


class Stores(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rows = {"evt-1": rc.flatten(EVENT)}

    def test_json_store_deduplicates(self):
        _, new1 = rc.save_json(self.tmp, self.rows)
        allrows, new2 = rc.save_json(self.tmp, self.rows)
        self.assertEqual((new1, new2, len(allrows)), (1, 0, 1))

    def test_sqlite_store_deduplicates_and_keeps_first_seen(self):
        db = self.tmp / "t.db"
        _, new1 = rc.save_db(db, self.rows)
        first = sqlite3.connect(db).execute("SELECT first_seen FROM audit_events").fetchone()[0]
        allrows, new2 = rc.save_db(db, self.rows)
        con = sqlite3.connect(db)
        self.assertEqual((new1, new2, len(allrows)), (1, 0, 1))
        self.assertEqual(con.execute("SELECT first_seen FROM audit_events").fetchone()[0], first)
        self.assertEqual(con.execute("SELECT action_id, target_ext FROM audit_events").fetchone(),
                         ("DELETE_USER:USER", "1042"))

    def test_unconfigured_server_databases_raise_runtime_error_not_crash(self):
        for kind in ("postgres", "mssql"):
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                rc.save_sql(kind, self.rows)

    def test_save_targets(self):
        self.assertEqual(rc.parse_targets("both"), {"json", "db", "both"})
        self.assertEqual(rc.parse_targets("none"), {"none"})


if __name__ == "__main__":
    unittest.main()
