import hashlib
import sqlite3
import unittest
import tempfile
from pathlib import Path
from orchestrator.db import connect
from orchestrator.kanban.read import snapshot, Unavailable

def test_snapshot_read_only(tmp_path):
    path=tmp_path/'orchestrator.db'
    conn=connect(path)
    conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,created_at,updated_at) VALUES ('c','中文','normal','inbox',1,1)")
    before=list(conn.iterdump())
    first=snapshot(tmp_path,at_ms=100)
    assert first==snapshot(tmp_path,at_ms=100)
    assert first['cards'][0]['title']=='中文'
    assert list(conn.iterdump())==before
    conn.close()

def test_unavailable(tmp_path):
    with unittest.TestCase().assertRaises(Unavailable): snapshot(tmp_path)
    assert not list(tmp_path.iterdir())
    sqlite3.connect(tmp_path/'orchestrator.db').close()
    with unittest.TestCase().assertRaises(Unavailable): snapshot(tmp_path)
    conn=sqlite3.connect(tmp_path/'orchestrator.db')
    assert conn.execute("SELECT name FROM sqlite_master").fetchall()==[]
    conn.close()

class ReadTests(unittest.TestCase):
    def test_read_only(self):
        with tempfile.TemporaryDirectory() as home: test_snapshot_read_only(Path(home))
    def test_missing(self):
        with tempfile.TemporaryDirectory() as home: test_unavailable(Path(home))

from unittest.mock import patch
from orchestrator.kanban import read

class SnapshotBoundaryTests(unittest.TestCase):
    def test_permission_error(self):
        with patch.object(read, 'connect', side_effect=PermissionError('denied')):
            with self.assertRaises(Unavailable): snapshot(Path('/synthetic-only'))
    def test_schema_missing_column(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'orchestrator.db'
            conn=connect(path)
            conn.execute('ALTER TABLE kanban_cards RENAME COLUMN title TO wrong_title')
            before=list(conn.iterdump())
            with self.assertRaises(Unavailable): snapshot(Path(home))
            self.assertEqual(before,list(conn.iterdump()))
            conn.close()
    def test_one_transaction_with_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as home:
            home=Path(home); writer=connect(home/'orchestrator.db')
            writer.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,created_at,updated_at) VALUES ('c','before','normal','inbox',1,1)")
            original=read.connect
            fired=[]
            def instrument(path, **kwargs):
                reader=original(path,**kwargs)
                def trace(sql):
                    if 'FROM kanban_events' in sql and not fired:
                        fired.append(True)
                        writer.execute("UPDATE kanban_cards SET title='after' WHERE card_id='c'")
                        writer.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result) VALUES ('new','x','edit','c','synthetic',2,'accepted')")
                reader.set_trace_callback(trace)
                return reader
            with patch.object(read,'connect',side_effect=instrument): data=snapshot(home,at_ms=10)
            self.assertTrue(fired)
            self.assertEqual('before',data['cards'][0]['title'])
            self.assertEqual([],data['events'])
            writer.close()

class CardScopedReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.conn=connect(self.home/'orchestrator.db');self.addCleanup(self.conn.close)
        for ident in ('selected-task','private-task'):
            self.conn.execute("INSERT INTO tasks(id,type,status,current_stage,profile_hash,input_hash,profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,created_at,updated_at) VALUES (?, 'apply','done','review','PRIVATE-profile','PRIVATE-hash','PRIVATE-profile-path','PRIVATE-input-path','PRIVATE-artifacts',8,1,2)",(ident,))
        self.conn.execute('ALTER TABLE tasks ADD COLUMN future_private TEXT')
        self.conn.execute("UPDATE tasks SET future_private='PRIVATE-future-column'")
        for ident,task in [('selected','selected-task'),('other','private-task'),('unbound',None)]:
            self.conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,task_id,created_at,updated_at) VALUES (?,?,'normal','inbox',?,1,1)",(ident,ident,task))
            self.conn.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result,payload) VALUES (?, 'hash','edit',?,'synthetic',1,'accepted',?)",('event-'+ident,ident,'PRIVATE-unselected-event' if ident=='other' else 'selected-event'))
    def instrument(self,zero_tasks=False):
        original=read.connect;queries=[];access=[]
        def instrument(path,**kwargs):
            conn=original(path,**kwargs)
            def authorize(action,table,column,*rest):
                if action==sqlite3.SQLITE_READ and table=='tasks':
                    access.append(column)
                    if zero_tasks or column not in {'id','status','stop_reason','current_stage','updated_at'}:return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize);conn.set_trace_callback(queries.append);return conn
        return patch.object(read,'connect',side_effect=instrument),queries,access
    def test_show_filters_before_reads_and_never_loads_private_task_fields(self):
        before=list(self.conn.iterdump());instrument,queries,access=self.instrument()
        with instrument:d=snapshot(self.home,card_id='selected',at_ms=10)
        self.assertEqual(['selected'],[c['card_id'] for c in d['cards']]);self.assertEqual(['selected-task'],[t['id'] for t in d['tasks']])
        self.assertEqual(['event-selected'],[e['operation_id'] for e in d['events']]);self.assertTrue(access)
        self.assertTrue(any('FROM tasks WHERE id IN (' in q for q in queries))
        self.assertTrue(any("FROM kanban_cards WHERE card_id='selected'" in q for q in queries))
        self.assertNotIn('PRIVATE-',__import__('json').dumps(d));self.assertEqual(before,list(self.conn.iterdump()))
    def test_unbound_and_unknown_card_do_zero_task_reads(self):
        for card in ('unbound','missing'):
            instrument,queries,access=self.instrument(zero_tasks=True)
            with instrument:d=snapshot(self.home,card_id=card,at_ms=10)
            self.assertEqual([],d['tasks']);self.assertEqual([],access);self.assertFalse(any('FROM tasks' in q for q in queries))
    def test_list_only_reads_bound_task_metadata_and_orphan_remains_unknown(self):
        instrument,queries,access=self.instrument()
        with instrument:d=snapshot(self.home,at_ms=10)
        self.assertEqual({'selected-task','private-task'},{t['id'] for t in d['tasks']});self.assertNotIn('PRIVATE-',__import__('json').dumps(d['tasks']))
        self.conn.execute('PRAGMA foreign_keys=OFF');self.conn.execute("UPDATE kanban_cards SET task_id='absent' WHERE card_id='selected'")
        d=snapshot(self.home,card_id='selected',at_ms=10)
        from orchestrator.kanban.view import project
        self.assertEqual([],d['tasks']);self.assertEqual('待決策',project(d)[0]['group'])
