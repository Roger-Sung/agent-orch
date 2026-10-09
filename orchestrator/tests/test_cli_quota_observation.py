import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from orchestrator.db import connect, connect_progress
from orchestrator.kanban.store import ensure_schema
from orchestrator.kanban.commands import build_request, handle_request, KanbanError
from orchestrator.kanban.observation import read_projection
from orchestrator.kanban.read import page_snapshot
from orchestrator.daemon import _progress_request, _progress_handle
from orchestrator.ipc import hold_daemon_lock

class QuotaTests(unittest.TestCase):
    def setUp(self):
            self.tmp = tempfile.TemporaryDirectory()
            self.home = Path(self.tmp.name)
            self.conn = connect(self.home/'orchestrator.db')
            ensure_schema(self.conn)
            self.context = SimpleNamespace(conn=self.conn, home=self.home)
            self.now = int(time.time()*1000)-1000
            self.good = {'actor':'quota-updater','source':'codex-cli','attempted_at_ms':self.now,'outcome':'ok','error':None,'observed_at_ms':self.now,'remaining_percent':77,'reset_at_ms':self.now+600000}
    def tearDown(self):
            self.conn.close();self.tmp.cleanup()
    def apply(self,payload,operation=None):
            return handle_request(self.context,build_request('quota-cli-attempt',payload,operation_id=operation))
    def test_isolation_idempotence_and_ordering(self):
            before = {t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t)] for t in ('tasks','kanban_cards','kanban_nights','kanban_quota_snapshots')}
            op=str(uuid.uuid4());self.assertEqual(self.apply(self.good,op)['result'],'accepted')
            self.assertTrue(self.apply(self.good,op)['replayed'])
            self.assertEqual(self.apply({**self.good,'remaining_percent':55},op)['reason'],'idempotency_conflict')
            failed={'actor':'quota-updater','source':'codex-cli','attempted_at_ms':self.now+10,'outcome':'failed','error':'timeout'}
            self.apply(failed)
            self.assertEqual(read_projection(self.conn)['observation']['weekly']['remaining_percent'],77)
            self.assertEqual(self.apply({**self.good,'attempted_at_ms':self.now+5})['reason'],'stale_attempt')
            self.assertEqual(self.apply({**self.good,'attempted_at_ms':self.now+20,'observed_at_ms':self.now-1})['reason'],'stale_attempt')
            self.assertEqual(self.apply({**failed,'attempted_at_ms':self.now-1})['reason'],'stale_attempt')
            self.assertEqual(self.conn.execute('SELECT count(*) FROM cli_quota_observation').fetchone()[0],1)
            for t,rows in before.items():self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM '+t)],rows)
    def test_strict_fields_no_secret_persistence(self):
            for bad in ({**self.good,'secret':'SECRET_PRIVATE'}, {**self.good,'actor':'SECRET_PRIVATE'}, {**self.good,'source':'manual'}, {**self.good,'remaining_percent':True}, {**self.good,'reset_at_ms':self.now+604800001}, {'actor':'quota-updater','source':'codex-cli','attempted_at_ms':self.now,'outcome':'failed','error':'SECRET_PRIVATE'}):
                with self.assertRaises(KanbanError):self.apply(bad)
            with self.assertRaises(KanbanError):self.apply({**self.good,'attempted_at_ms':253402300799999})
            self.assertEqual(self.conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0],0)
            self.assertNotIn('SECRET_PRIVATE', '\n'.join(self.conn.iterdump()))
            path=self.home/'bad.json';path.write_text(json.dumps({'request_id':str(uuid.uuid4()),'action':'kanban','command':'quota-cli-attempt','operation_id':str(uuid.uuid4()),'payload':{**self.good,'secret':'SECRET_PRIVATE'}}))
            self.assertEqual(_progress_request(path)[1],'not_allowed')
    def test_bootstrap_existing_and_schema_rejection(self):
            self.conn.execute('DROP TABLE cli_quota_observation')
            self.conn.close();self.conn=connect_progress(self.home/'orchestrator.db')
            self.assertIsNotNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='cli_quota_observation'").fetchone())
            self.conn.execute('CREATE TRIGGER quota_foreign AFTER UPDATE ON cli_quota_observation BEGIN SELECT 1; END')
            before=list(self.conn.iterdump())
            self.conn.close()
            with self.assertRaises(ValueError):connect_progress(self.home/'orchestrator.db')
            self.conn=sqlite3.connect(self.home/'orchestrator.db',isolation_level=None);self.conn.row_factory=sqlite3.Row
            self.assertEqual(list(self.conn.iterdump()),before)
            self.assertFalse(read_projection(self.conn)['available'])
    def test_missing_readonly_and_page_transaction(self):
            self.conn.execute('DROP TABLE cli_quota_observation')
            before=(self.home/'orchestrator.db').read_bytes()
            self.assertFalse(page_snapshot(self.home)['cli_quota']['available'])
            self.assertEqual((self.home/'orchestrator.db').read_bytes(),before)
            self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='cli_quota_observation'").fetchone())

    def test_schema_rejects_null_source_and_partial_success(self):
        for values in ((None,self.now,'failed','timeout',None,None,None),
                       ('codex-cli',self.now,'ok',None,None,None,None),
                       ('codex-cli',self.now,'failed','timeout',self.now,77,None)):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute('INSERT INTO cli_quota_observation VALUES(?,?,?,?,?,?,?)',values)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM cli_quota_observation').fetchone()[0],0)

    def test_failed_cli_fields_return_nonzero_without_nameerror(self):
        from orchestrator.cli import _kanban_payload
        from orchestrator.controller import ControllerError
        args=SimpleNamespace(actor=None,kanban_command='quota-cli-attempt',attempted_at_ms=self.now,outcome='failed',error='timeout',observed_at_ms=self.now,remaining_percent=None,reset_at_ms=None)
        with self.assertRaises(ControllerError):_kanban_payload(args)

    def test_foreign_table_shape_aborts_additive_setup_atomically(self):
        self.conn.execute('DROP TABLE cli_quota_observation')
        self.conn.execute('CREATE TABLE cli_quota_observation(source TEXT, unexpected TEXT)')
        before=list(self.conn.iterdump())
        with self.assertRaises(ValueError):ensure_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()),before)
        self.assertFalse(read_projection(self.conn)['available'])

    def _assert_incompatible_literal_schema(self, expected_schema, alter_literal):
        from orchestrator.kanban import store
        reference=sqlite3.connect(':memory:')
        try:
            reference.executescript(expected_schema)
            expected_sql=reference.execute("SELECT sql FROM sqlite_master WHERE name='cli_quota_observation'").fetchone()[0]
        finally:
            reference.close()
        self.conn.execute('DROP TABLE cli_quota_observation')
        self.conn.execute(alter_literal(expected_sql))
        before=list(self.conn.iterdump())
        with patch.object(store,'SCHEMA',expected_schema):
            with self.assertRaisesRegex(ValueError,'incompatible CLI quota schema'):
                ensure_schema(self.conn)
            self.assertEqual(list(self.conn.iterdump()),before)
            self.assertFalse(read_projection(self.conn)['available'])
            self.conn.close()
            with self.assertRaisesRegex(ValueError,'incompatible CLI quota schema'):
                connect_progress(self.home/'orchestrator.db')
            self.conn=sqlite3.connect(self.home/'orchestrator.db',isolation_level=None)
            self.conn.row_factory=sqlite3.Row
            self.assertEqual(list(self.conn.iterdump()),before)

    def test_case_sensitive_check_literal_rejected_before_migration(self):
        from orchestrator.kanban.store import SCHEMA
        self._assert_incompatible_literal_schema(SCHEMA,lambda sql:sql.replace("'codex-cli'","'CODEX-CLI'"))

    def test_whitespace_inside_check_literal_is_not_normalized(self):
        from orchestrator.kanban.store import SCHEMA
        # An isolated extended reference distinguishes one versus two spaces
        # inside literals; the former normalizer erased that semantic change.
        expected=SCHEMA.replace("CHECK(source = 'codex-cli')","CHECK(source = 'codex-cli' AND 'a b' = 'a b')")
        self._assert_incompatible_literal_schema(expected,lambda sql:sql.replace("'a b'", "'a  b'",1))

    def test_exact_expected_schema_accepts_happy_write_and_reader(self):
        from orchestrator.kanban.observation import validate_schema
        from orchestrator.kanban.store import SCHEMA
        validate_schema(self.conn,SCHEMA)
        self.conn.close();self.conn=connect_progress(self.home/'orchestrator.db')
        self.context.conn=self.conn
        self.assertEqual(self.apply(self.good)['result'],'accepted')
        self.assertEqual(read_projection(self.conn)['observation']['weekly']['remaining_percent'],77)
