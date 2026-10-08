"""Bounded, card-page-only reads from an isolated synthetic database."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from orchestrator.db import connect
from orchestrator.kanban import read
from orchestrator.kanban.view import project, render

class PageReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.conn=connect(self.home/'orchestrator.db');self.addCleanup(self.conn.close)
        for prefix, count, state in [('active',45,'inbox'),('archive',23,'archived')]:
            for index in range(count):
                ident=f'{prefix}-{index:03d}'
                self.conn.execute("INSERT INTO tasks(id,type,status,current_stage,profile_hash,input_hash,profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,created_at,updated_at) VALUES (?,'apply','done','review','PRIVATE-profile','PRIVATE-hash','PRIVATE-profile-path','PRIVATE-input-path','PRIVATE-artifact',1,1,1)",(ident,))
                self.conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,task_id,created_at,updated_at) VALUES (?,?,'normal',?,?,1,1)",(ident,ident,state,ident))
                self.conn.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result) VALUES (?,'hash','edit',?,'synthetic',1,'accepted')",('event-'+ident,ident))

    def instrument(self):
        queries=[];original=read.connect
        def wrapped(path,**kwargs):
            self.assertEqual({'read_only':True},kwargs)
            conn=original(path,**kwargs)
            def authorize(action,table,column,*rest):
                if action==sqlite3.SQLITE_READ and (table=='kanban_quota_snapshots' or table=='tasks' and column not in read.COLUMNS['tasks']):return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize);conn.set_trace_callback(queries.append);return conn
        return patch.object(read,'connect',side_effect=wrapped),queries

    def test_active_pages_SQL_filter_related_scope_and_invariance(self):
        before=list(self.conn.iterdump());instrument,queries=self.instrument()
        with instrument:first=read.page_snapshot(self.home,at_ms=100)
        self.assertEqual(first,read.page_snapshot(self.home,at_ms=100))
        self.assertEqual([f'active-{n:03d}' for n in range(20)],[c['card_id'] for c in first['cards']])
        self.assertEqual(20,len(first['events']));self.assertEqual(20,len(first['tasks']));self.assertEqual([],first['quota'])
        self.assertEqual(1,queries.count('BEGIN'));self.assertEqual(1,queries.count('COMMIT'))
        cards_sql=[q for q in queries if q.startswith('SELECT ') and ' FROM kanban_cards' in q]
        self.assertTrue(all("manual_state <> 'archived'" in q and 'LIMIT' in q for q in cards_sql))
        history_sql=[q for q in queries if ' FROM kanban_events ' in q or ' FROM kanban_nights ' in q]
        self.assertEqual(40,len(history_sql));self.assertTrue(all('WHERE card_id=' in q and 'LIMIT 51' in q for q in history_sql))
        second=read.page_snapshot(self.home,after=first['page']['next'],at_ms=100)
        third=read.page_snapshot(self.home,after=second['page']['next'],at_ms=100)
        self.assertEqual(5,len(third['cards']));self.assertIsNone(third['page']['next'])
        self.assertEqual(first,read.page_snapshot(self.home,before=second['page']['previous'],at_ms=100))
        ids={c['card_id'] for d in (first,second,third) for c in d['cards']};self.assertEqual(45,len(ids))
        self.assertNotIn('archive-',json.dumps(first));self.assertNotIn('PRIVATE-',json.dumps(first));self.assertEqual(before,list(self.conn.iterdump()))

    def test_archive_is_independent_SQL_query_and_paged(self):
        instrument,queries=self.instrument()
        with instrument:first=read.page_snapshot(self.home,archived=True,at_ms=100)
        self.assertEqual(20,len(first['cards']));self.assertTrue(all(c['manual_state']=='archived' for c in first['cards']))
        self.assertTrue(all("manual_state = 'archived'" in q for q in queries if ' FROM kanban_cards' in q))
        second=read.page_snapshot(self.home,archived=True,after=first['page']['next'],at_ms=100)
        self.assertEqual(3,len(second['cards']));self.assertEqual(first,read.page_snapshot(self.home,archived=True,before=second['page']['previous'],at_ms=100))
        self.assertNotIn('active-',json.dumps(first))
        with self.assertRaises(read.Unavailable):read.page_snapshot(self.home,after=first['page']['next'])

    def test_histories_are_bounded_and_partial_progress_unknown(self):
        for index in range(60):
            self.conn.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result,result_revision) VALUES (?,'hash','report-progress','active-000','synthetic',?,'accepted',?)",(f'long-{index:03d}',index+2,index+1))
            self.conn.execute("INSERT INTO kanban_nights(night_id,window_start_ms,window_end_ms,card_id,approval_generation,approval_hash,task_id,request_id,workspace_dir,base_head,candidate_fingerprint,profile_hash,input_bytes,input_hash,pool_claims,reserved_at,phase,stop_reason) VALUES (?,1,2,'active-000',?,'h',?,?,'PRIVATE-workspace','h','h','h',X'00','h','{}',1,'stopped','synthetic')",(f'night-{index}',index+1,f'night-task-{index}',f'night-request-{index}'))
        data=read.page_snapshot(self.home,at_ms=100)
        self.assertEqual(50,len([e for e in data['events'] if e['card_id']=='active-000']))
        self.assertTrue(data['history_truncated']['active-000']['events'])
        self.assertEqual(50,len([n for n in data['nights'] if n['card_id']=='active-000']));self.assertTrue(data['history_truncated']['active-000']['nights']);self.assertNotIn('PRIVATE-',json.dumps(data['nights']))
        item=next(p for p in project(data) if p['card']['card_id']=='active-000')
        self.assertIsNone(item['progress_report']);self.assertIn('完整性未知',item['progress_report_note'])
        self.assertIn('此卡歷史已限量',render(data))
        # Independent missing night history must not look like a known clean state.
        data['cards'][0]['task_id']=None;data['history_truncated']['active-000']['nights']=True
        self.assertEqual('待決策',project(data)[0]['group'])

    def test_new_cards_show_without_allowlist_and_empty_is_valid(self):
        self.conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,created_at,updated_at) VALUES ('000-new','新批准正常卡片','normal','inbox',1,1)")
        self.assertEqual('000-new',read.page_snapshot(self.home)['cards'][0]['card_id'])
        last=self.conn.execute("SELECT rowid FROM kanban_cards WHERE card_id='active-044'").fetchone()[0]
        data=read.page_snapshot(self.home,after=last);self.assertEqual([],data['cards']);self.assertEqual([],data['events']);self.assertEqual([],data['tasks'])
        for kwargs in ({'after':1,'before':2},{'after':''},{'after':'x'*1001},{'archived':'yes'},{'after':True},{'after':-(1 << 63) - 1},{'after':1 << 63}):
            with self.assertRaises(ValueError):read.page_snapshot(self.home,**kwargs)
        with self.assertRaises(read.Unavailable):read.page_snapshot(self.home,after=999999)

    def test_long_unicode_quote_ID_roundtrips_fixed_size_cursor(self):
        # Places an unrestricted legal ID at the end of the first 20-card page.
        long_id='active-018z'+('中文' * 600)+'\"\' <script>'
        self.conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,created_at,updated_at) VALUES (?,?,'normal','inbox',1,1)",(long_id,'長ID卡'))
        self.conn.execute('UPDATE kanban_cards SET rowid=-7 WHERE card_id=?',(long_id,))
        first=read.page_snapshot(self.home,at_ms=100);self.assertEqual(long_id,first['cards'][-1]['card_id'])
        self.assertEqual(-7,first['page']['next']);self.assertLessEqual(len(str(first['page']['next'])),20)
        second=read.page_snapshot(self.home,after=first['page']['next'],at_ms=100)
        self.assertEqual('active-019',second['cards'][0]['card_id'])
        self.assertEqual(first,read.page_snapshot(self.home,before=second['page']['previous'],at_ms=100))
        self.assertNotIn('_page_rowid',json.dumps(first));self.assertIn('&lt;script&gt;',render(first))

    def test_one_read_transaction_with_concurrent_fixture_update(self):
        original=read.connect;fired=[]
        def instrument(path,**kwargs):
            conn=original(path,**kwargs)
            def trace(sql):
                if ' FROM kanban_events ' in sql and not fired:
                    fired.append(True);self.conn.execute("UPDATE kanban_cards SET title='after' WHERE card_id='active-000'")
                    self.conn.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result) VALUES ('concurrent','hash','edit','active-000','synthetic',2,'accepted')")
            conn.set_trace_callback(trace);return conn
        with patch.object(read,'connect',side_effect=instrument):data=read.page_snapshot(self.home,at_ms=100)
        self.assertTrue(fired);self.assertEqual('active-000',data['cards'][0]['title']);self.assertNotIn('concurrent',{e['operation_id'] for e in data['events']})

    def test_unavailable_never_creates_or_migrates(self):
        missing=self.home/'missing';missing.mkdir()
        with self.assertRaises(read.Unavailable):read.page_snapshot(missing)
        self.assertEqual([],list(missing.iterdir()))
        self.conn.execute('ALTER TABLE kanban_cards RENAME COLUMN title TO wrong_title');before=list(self.conn.iterdump())
        with self.assertRaises(read.Unavailable):read.page_snapshot(self.home)
        self.assertEqual(before,list(self.conn.iterdump()))

if __name__=='__main__':unittest.main()
