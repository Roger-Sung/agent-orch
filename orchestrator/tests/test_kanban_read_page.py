"""Isolated backlog writer and summary-first readonly evidence."""
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from orchestrator.db import connect
from orchestrator.kanban import read
from orchestrator.kanban.commands import build_request, handle_request
from orchestrator.kanban.view import project, render, detail_fragment, PANEL_SCRIPT
from orchestrator.cli import build_parser, _kanban_payload

class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.conn=connect(self.home/'orchestrator.db');self.addCleanup(self.conn.close)
        self.writer=SimpleNamespace(home=self.home,conn=self.conn)
        self.conn.execute("PRAGMA ignore_check_constraints=ON")
    def corrupt(self):
        # Synthetic corruption fixture only; production journal remains append-only.
        for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='kanban_events'").fetchall():
            self.conn.execute('DROP TRIGGER '+row['name'])
    def send(self,command,payload,operation=None):
        return handle_request(self.writer,build_request(command,payload,operation_id=operation))
    def card(self,ident,state='inbox'):
        self.conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,created_at,updated_at) VALUES (?,?,'normal',?,1,1)",(ident,ident,state))
    def create(self,ident):
        return self.send('create',{'card_id':ident,'actor':'operator','fields':{'title':ident}})
    def place(self,ident,destination='board',revision=0,operation=None):
        return self.send('place',{'card_id':ident,'expected_revision':revision,'actor':'operator','destination':destination,'user_request':'USER explicitly selects '+ident},operation)
    def report(self,ident,status='not_started',revision=0):
        return self.send('report-progress',{'card_id':ident,'expected_revision':revision,'actor':'assistant','report_status':status,'summary':'safe summary','source_refs':[]})
    def item(self,data,ident):
        return next(p for p in project(data) if p['card']['card_id']==ident)
    def test_all_main_summaries_and_independent_backlog_archive_pages(self):
        for n in range(45):self.card(f'board-{n:03}')
        for n in range(23):self.create(f'backlog-{n:03}')
        for n in range(22):self.card(f'archive-{n:03}','archived')
        main=read.page_snapshot(self.home);self.assertEqual(45,len(main['cards']));self.assertIsNone(main['page']['size']);self.assertIsNone(main['page']['next'])
        for options,count in (({'backlog':True},23),({'archived':True},22)):
            first=read.page_snapshot(self.home,at_ms=100,**options);second=read.page_snapshot(self.home,after=first['page']['next'],at_ms=100,**options)
            self.assertEqual(20,len(first['cards']));self.assertEqual(count-20,len(second['cards']));self.assertIsNone(second['page']['next'])
            self.assertEqual(first,read.page_snapshot(self.home,before=second['page']['previous'],at_ms=100,**options))
            with self.assertRaises(read.Unavailable):read.page_snapshot(self.home,detail=first['cards'][0]['_detail_token'])
        html=render(main,lazy=True);self.assertEqual(45,html.count('class="select-card"'));self.assertNotIn('<template',html)
    def test_place_CAS_replay_reversal_no_lifecycle_changes(self):
        self.card('c');before=dict(self.conn.execute("SELECT * FROM kanban_cards WHERE card_id='c'").fetchone());operation=str(uuid.uuid4())
        outcome=self.place('c','backlog',operation=operation);self.assertEqual('accepted',outcome['result']);self.assertEqual(1,outcome['revision'])
        self.assertTrue(self.place('c','backlog',operation=operation)['replayed']);self.assertEqual('idempotency_conflict',self.place('c','board',operation=operation)['reason'])
        self.assertEqual('revision_conflict',self.place('c','board')['reason']);self.assertEqual('accepted',self.place('c','board',1)['result'])
        after=dict(self.conn.execute("SELECT * FROM kanban_cards WHERE card_id='c'").fetchone())
        for key in before:
            if key not in {'revision','updated_at'}:self.assertEqual(before[key],after[key],key)
        self.assertEqual(3,self.conn.execute("SELECT count(*) FROM kanban_events").fetchone()[0]);self.assertEqual(['board'],list(read.page_snapshot(self.home)['queue_locations'].values()))
        self.assertEqual([],read.page_snapshot(self.home,backlog=True)['cards'])
    def test_new_default_backlog_and_no_auto_refill_after_report_archive(self):
        self.create('new');self.assertEqual([],read.page_snapshot(self.home)['cards']);self.report('new','needs_decision')
        backlog=read.page_snapshot(self.home,backlog=True);self.assertEqual('待決策',self.item(backlog,'new')['group']);self.assertEqual([],read.page_snapshot(self.home)['cards'])
        self.assertEqual('accepted',self.place('new','board',1)['result']);self.assertEqual('待決策',self.item(read.page_snapshot(self.home),'new')['group'])
        self.create('remaining');self.send('archive',{'card_id':'new','expected_revision':2,'actor':'operator'})
        self.assertEqual([],read.page_snapshot(self.home)['cards']);self.assertEqual(['remaining'],[c['card_id'] for c in read.page_snapshot(self.home,backlog=True)['cards']])
        self.assertEqual('封存',self.item(read.page_snapshot(self.home,archived=True),'new')['group'])
    def test_effective_pending_only_and_explicit_selection_required(self):
        for state in ('done','needs_clarification','returned','mystery','archived'):
            self.card(state,state);self.assertEqual('rejected',self.place(state,'backlog')['result'])
        self.card('running');self.report('running','in_progress');self.assertEqual('backlog_requires_effective_pending',self.place('running','backlog',1)['reason'])
        self.card('c');payload={'card_id':'c','expected_revision':0,'actor':'operator','destination':'board','user_request':' '}
        self.assertEqual('explicit_user_request_required',self.send('place',payload)['reason'])
        payload['user_request']='yes';payload['task_id']='forbidden';self.assertEqual('invalid_place_payload',self.send('place',payload)['reason'])
    def test_malformed_location_journal_main_decision_and_archive_wins(self):
        self.create('c');self.corrupt();self.conn.execute("UPDATE kanban_events SET metadata_delta=? WHERE card_id='c'",('{"queue_location":"secret"}',))
        data=read.page_snapshot(self.home);self.assertEqual('待決策',self.item(data,'c')['group']);self.assertEqual('unknown',data['queue_locations']['c']);self.assertEqual([],read.page_snapshot(self.home,backlog=True)['cards'])
        self.conn.execute("UPDATE kanban_cards SET manual_state='archived' WHERE card_id='c'");self.assertEqual('封存',self.item(read.page_snapshot(self.home,archived=True),'c')['group'])
    def test_duplicate_valid_location_revision_is_unknown(self):
        self.card('c');self.place('c','backlog')
        columns=read.COLUMNS['events'];row=dict(self.conn.execute("SELECT * FROM kanban_events WHERE card_id='c'").fetchone());row['operation_id']=str(uuid.uuid4())
        self.conn.execute('INSERT INTO kanban_events('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row[key] for key in columns))
        data=read.page_snapshot(self.home);self.assertEqual('待決策',self.item(data,'c')['group']);self.assertEqual([],read.page_snapshot(self.home,backlog=True)['cards'])

    def test_summary_preserves_scope_ABA_barrier_after_descriptive_edit(self):
        self.card('c');self.report('c','reported_done')
        for revision,fields in ((1,{'repo_path':'B'}),(2,{'repo_path':None}),(3,{'title':'description'})):
            self.assertEqual('accepted',self.send('edit',{'card_id':'c','expected_revision':revision,'actor':'operator','fields':fields})['result'])
        data=read.page_snapshot(self.home);item=self.item(data,'c');self.assertIsNone(item['progress_report']);self.assertIn('scope 已修改',item['progress_report_note']);self.assertEqual(2,len(data['events']))
        self.place('c','backlog',4);self.assertEqual([],read.page_snapshot(self.home)['cards'])
    def test_descriptive_edit_and_place_keep_report_binding(self):
        self.card('c');self.report('c');self.place('c','backlog',1);self.send('edit',{'card_id':'c','expected_revision':2,'actor':'operator','fields':{'title':'renamed'}})
        item=self.item(read.page_snapshot(self.home,backlog=True),'c');self.assertEqual('not_started',item['progress_report']['report_status'])
    def test_older_invalid_report_revision_or_hidden_malformed_scope_is_unknown(self):
        self.card('c');self.report('c');self.send('edit',{'card_id':'c','expected_revision':1,'actor':'operator','fields':{'title':'one'}})
        self.send('edit',{'card_id':'c','expected_revision':2,'actor':'operator','fields':{'title':'two'}})
        self.corrupt();self.conn.execute("UPDATE kanban_events SET metadata_delta='broken' WHERE result_revision=2")
        data=read.page_snapshot(self.home);self.assertEqual('待決策',self.item(data,'c')['group']);self.assertTrue(data['summary_flags']['c']['bad_edit'])
        self.conn.execute("UPDATE kanban_events SET result_revision=100 WHERE kind='report-progress'")
        self.assertEqual('待決策',self.item(read.page_snapshot(self.home),'c')['group'])
    def test_summary_SQL_bounded_materialization_and_single_card_details(self):
        for n in range(25):self.card(f'c{n:02}')
        for n in range(60):
            self.conn.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result) VALUES (?,'hash','fixture','c00','synthetic',?,'accepted')",(f'event{n}',n))
        queries=[];original=read.connect
        def instrument(path,**kwargs):
            self.assertEqual({'read_only':True},kwargs);conn=original(path,**kwargs)
            def authorize(action,table,column,*rest):
                if action==sqlite3.SQLITE_READ and (table=='kanban_quota_snapshots' or table=='tasks' and column not in read.COLUMNS['tasks']):return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize);conn.set_trace_callback(queries.append);return conn
        before=list(self.conn.iterdump())
        with patch.object(read,'connect',side_effect=instrument):data=read.page_snapshot(self.home)
        self.assertEqual(25,len(data['cards']));self.assertEqual([],data['events']);self.assertEqual([],data['nights']);self.assertFalse(any('LIMIT 51' in q for q in queries))
        self.assertEqual(1,queries.count('BEGIN'));self.assertEqual(1,queries.count('COMMIT'));self.assertLess(len(queries),15)
        token=data['cards'][0]['_detail_token'];queries.clear()
        with patch.object(read,'connect',side_effect=instrument):detail=read.page_snapshot(self.home,detail=token)
        self.assertEqual(50,len(detail['events']));self.assertEqual(1,len(detail['cards']));self.assertTrue(detail['history_truncated']['c00']['events']);self.assertEqual([],detail['nights'])
        bounded=[q for q in queries if 'LIMIT 51' in q];self.assertEqual(2,len(bounded));self.assertTrue(all("WHERE card_id='c00'" in q for q in bounded));self.assertEqual(before,list(self.conn.iterdump()))
        fragment=detail_fragment(detail);self.assertIn('此卡歷史已限量',fragment);self.assertNotIn('<script>',fragment);self.assertNotIn('c01',fragment)
    def test_night_history_50_and_older_unknown_phase_not_lost(self):
        self.card('c')
        for n in range(60):
            self.conn.execute("INSERT INTO kanban_nights(night_id,window_start_ms,window_end_ms,card_id,approval_generation,approval_hash,task_id,request_id,workspace_dir,base_head,candidate_fingerprint,profile_hash,input_bytes,input_hash,pool_claims,reserved_at,phase,stop_reason) VALUES (?,1,2,'c',?,'h',?,?,'PRIVATE-workspace','h','h','h',X'00','h','{}',1,?,'synthetic')",(f'night-{n}',n+1,f'night-task-{n}',f'night-request-{n}','mystery' if n==0 else 'stopped'))
        data=read.page_snapshot(self.home);self.assertEqual([],data['nights']);self.assertEqual('待決策',self.item(data,'c')['group'])
        detail=read.page_snapshot(self.home,detail=data['cards'][0]['_detail_token']);self.assertEqual(50,len(detail['nights']));self.assertTrue(detail['history_truncated']['c']['nights']);self.assertNotIn('PRIVATE-',json.dumps(detail));self.assertEqual('待決策',self.item(detail,'c')['group'])

    def test_raw_unknown_states_unverified_completion_liveness(self):
        for ident,status in [('manual','done'),('unknown','bad')]:self.card(ident,status)
        self.card('running');self.report('running','in_progress');self.card('reported');self.report('reported','reported_done')
        data=read.page_snapshot(self.home)
        for ident in ('manual','reported'):self.assertEqual('完成',self.item(data,ident)['group']);self.assertEqual('未驗證',self.item(data,ident)['completion_evidence'])
        self.assertEqual('待決策',self.item(data,'unknown')['group']);self.assertIn('不保證存活',self.item(data,'running')['liveness'])
    def test_invalid_routes_cursors_missing_and_no_migration(self):
        for options in ({'after':1},{'archived':'yes'},{'backlog':True,'archived':True},{'detail':True},{'backlog':True,'after':1<<63},{'backlog':True,'after':1,'before':2}):
            with self.assertRaises(ValueError):read.page_snapshot(self.home,**options)
        with self.assertRaises(read.Unavailable):read.page_snapshot(self.home,detail=99)
        missing=self.home/'missing';missing.mkdir()
        with self.assertRaises(read.Unavailable):read.page_snapshot(missing)
        self.assertEqual([],list(missing.iterdir()))
    def test_long_unsafe_ID_token_security_and_keyboard_script(self):
        ident='中文'*600+' </template><script>evil</script>';self.create(ident)
        data=read.page_snapshot(self.home,backlog=True);token=data['cards'][0]['_detail_token'];html=render(data,backlog=True,lazy=True)
        self.assertIn('data-detail="/detail/backlog/'+str(token)+'"',html);self.assertNotIn('<template',html);self.assertNotIn('<script>evil',html)
        fragment=detail_fragment(read.page_snapshot(self.home,backlog=True,detail=token),backlog=True);self.assertIn('&lt;script&gt;',fragment);self.assertNotIn('<script>',fragment)
        for contract in ('AbortController','generation !== requestGeneration','requestGeneration += 1','selected.focus()','event.shiftKey','narrow.addEventListener','DOMParser','cloneNode(true)',"mode: 'same-origin'","redirect: 'error'"):
            self.assertIn(contract,PANEL_SCRIPT)
        for forbidden in ('innerHTML','localStorage','sessionStorage','eval(','WebSocket'):self.assertNotIn(forbidden,PANEL_SCRIPT)
    def test_canonical_CLI_list_show_locations_and_malformed_placement(self):
        import io
        from contextlib import redirect_stdout
        from orchestrator.cli import _kanban
        self.create('c')
        def call(action):
            args=build_parser().parse_args(['kanban',action,'--json']+(['--card','c'] if action=='show' else []))
            output=io.StringIO()
            with redirect_stdout(output):self.assertEqual(0,_kanban(self.home,args))
            return json.loads(output.getvalue())
        self.assertEqual('backlog',call('list')['projection'][0]['queue_location'])
        self.assertEqual('backlog',call('show')['queue_locations']['c'])
        snapshot=read.snapshot(self.home)
        self.assertNotIn('class="select-card"',render(snapshot))
        self.assertEqual(1,render(snapshot,backlog=True).count('class="select-card"'))
        destination=Path(self.temp.name+'-offline.html');self.addCleanup(lambda:destination.unlink(missing_ok=True));args=build_parser().parse_args(['kanban','render','--output',str(destination)])
        with redirect_stdout(io.StringIO()):self.assertEqual(0,_kanban(self.home,args))
        self.assertNotIn('class="select-card"',destination.read_text())
        self.place('c');self.assertEqual('board',call('show')['projection'][0]['queue_location'])
        self.corrupt();self.conn.execute("UPDATE kanban_events SET metadata_delta='broken' WHERE kind='place'")
        result=call('list');self.assertEqual('unknown',result['queue_locations']['c']);self.assertEqual('待決策',result['projection'][0]['group'])
        self.assertEqual('backlog_requires_effective_pending',self.place('c','backlog',1)['reason'])

    def test_cli_place_payload_and_progress_allowlist(self):
        args=build_parser().parse_args(['kanban','place','--card','c','--expected-revision','2','--destination','board','--user-request','USER picks c','--actor','operator'])
        self.assertEqual({'card_id':'c','expected_revision':2,'destination':'board','user_request':'USER picks c','actor':'operator'},_kanban_payload(args))
        from orchestrator.daemon import PROGRESS_COMMANDS
        self.assertIn('place',PROGRESS_COMMANDS);self.assertNotIn('approve',PROGRESS_COMMANDS)

if __name__=='__main__':unittest.main()
