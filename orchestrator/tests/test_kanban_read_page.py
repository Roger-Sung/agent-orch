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

def legacy_acceptance(**changes):
    """Synthetic fields with the real T1.3 historical sentence structure."""
    return {'summary': '使用者對「必要驗證通過後即可結案封存」回覆「確認都沒問題就可以關單」'
                       '（synthetic:user-instruction）；本次條件已核對通過，記錄 T1.3 限定 Mac mini 受控驗收。'
                       '獨立審查與通知回執已核對；此驗收不涵蓋完全不可達或人已讀，未發布限制保留。',
            'decision': 'USER 依已同意的條件接受 T1.3 Mac mini 受控驗收並准予本卡結案封存；'
                        '保留完全不可達分支未驗、有限容量與未發布限制，不宣稱全失效模式或無限運行。',
            'blocker': None, 'next_step': None,
            'source_refs': ['synthetic:user-instruction','synthetic:independent-review','synthetic:receipt','synthetic:thread'],
            **changes}

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
    def worker(self,ident,observation):
        revision=self.conn.execute('SELECT revision FROM kanban_cards WHERE card_id=?',(ident,)).fetchone()[0]
        refs=[observation['source_ref']]
        if observation['handoff'] and observation['handoff']['result_source_ref']:refs.append(observation['handoff']['result_source_ref'])
        return self.send('report-progress',{'card_id':ident,'expected_revision':revision,'actor':'assistant','report_status':'in_progress','summary':'Worker observation, synthetic only','source_refs':refs,'worker_observation':observation})
    def user_accept(self,ident,**changes):
        revision=self.conn.execute('SELECT revision FROM kanban_cards WHERE card_id=?',(ident,)).fetchone()[0]
        return self.send('report-progress',{'card_id':ident,'expected_revision':revision,'actor':'USER','report_status':'reported_done','summary':'使用者驗收通過：synthetic instruction and timestamp','decision':'接受此候選範圍；正式驗證另列','source_refs':['synthetic:user-instruction'],**changes})
    def test_legacy_USER_record_survives_worker_and_over_50_reports(self):
        from orchestrator.tests.test_kanban_commands import worker_sample,worker_handoff
        self.card('c');accepted=self.user_accept('c',**legacy_acceptance());self.assertEqual('accepted',accepted['result'])
        observation=worker_sample();first=self.worker('c',observation)
        observation={**observation,'previous_observation_operation_id':first['operation_id'],'state':'completed','source_ref':'synthetic:terminal-read','handoff':worker_handoff(True)}
        self.assertEqual('accepted',self.worker('c',observation)['result'])
        for revision in range(3,64):self.assertEqual('accepted',self.report('c','blocked',revision)['result'])
        before=list(self.conn.iterdump());page=read.page_snapshot(self.home);item=self.item(page,'c');detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'])
        self.assertLessEqual(len(page['events']),3)
        for snapshot in (page,detail):
            item=self.item(snapshot,'c');record=item['user_acceptance']
            self.assertTrue(record['acceptance_recorded']);self.assertEqual('已記錄使用者驗收',record['label'])
            self.assertEqual(accepted['operation_id'],record['record']['operation_id']);self.assertEqual('未綁目前候選',record['candidate_binding'])
            self.assertEqual('completed',item['worker_observation']['state']);self.assertEqual('blocked',item['delivery_report']['record']['report_status'])
            self.assertTrue(item['formal_verification_note'].startswith('未有可核對紀錄'))
        self.assertEqual(before,list(self.conn.iterdump()))
    def test_legacy_summary_detail_backlog_archive_are_readonly_and_consistent(self):
        self.card('board');self.create('backlog')
        for ident,backlog in (('board',False),('backlog',True)):
            self.assertEqual('accepted',self.user_accept(ident,**legacy_acceptance())['result']);self.report(ident,'reported_done',1)
            for archived in (False,True):
                if archived:self.assertEqual('accepted',self.send('archive',{'card_id':ident,'expected_revision':2,'actor':'fixture-owner'})['result'])
                options={'archived':True} if archived else {'backlog':backlog}
                before=list(self.conn.iterdump());page=read.page_snapshot(self.home,**options);item=self.item(page,ident)
                detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'],**options)
                self.assertEqual(item['user_acceptance'],self.item(detail,ident)['user_acceptance'])
                for html in (render(page,**options),render(page,lazy=True,**options),detail_fragment(detail,**options)):
                    self.assertIn('已記錄使用者驗收',html);self.assertIn('未綁目前候選',html)
                    self.assertIn('使用者驗收已記錄；技術／正式驗證另列',html);self.assertNotIn('未驗收',html)
                self.assertEqual(before,list(self.conn.iterdump()))
    def test_legacy_latest_incomplete_or_malformed_USER_never_falls_back(self):
        cases=[{'source_refs':[]},{'decision':None},{'summary':'最新 USER 進度說明'},{'report_status':'in_progress'}]
        for index,changes in enumerate(cases):
            ident='incomplete-'+str(index);self.card(ident);self.user_accept(ident,**legacy_acceptance())
            latest=self.user_accept(ident,**legacy_acceptance(**changes));self.assertEqual('accepted',latest['result'])
            item=self.item(read.page_snapshot(self.home),ident);self.assertFalse(item['user_acceptance']['acceptance_recorded'])
            self.assertEqual('incomplete',item['user_acceptance']['state']);self.assertEqual(latest['operation_id'],item['user_acceptance']['record']['operation_id'])
        self.corrupt()
        for index,kind in enumerate(('payload-actor','event-actor','malformed','unknown-field','binding')):
            ident='malformed-'+str(index);self.card(ident);self.user_accept(ident,**legacy_acceptance());latest=self.user_accept(ident,**legacy_acceptance())
            event=self.conn.execute('SELECT payload FROM kanban_events WHERE operation_id=?',(latest['operation_id'],)).fetchone();payload=json.loads(event[0]);actor='USER'
            if kind=='payload-actor':payload['actor']='assistant'
            elif kind=='event-actor':actor='assistant'
            elif kind=='unknown-field':payload['unrecognized']=True
            elif kind=='binding':payload['progress_binding']=None
            raw='not JSON' if kind=='malformed' else json.dumps(payload)
            self.conn.execute('UPDATE kanban_events SET payload=?,actor=? WHERE operation_id=?',(raw,actor,latest['operation_id']))
            page=read.page_snapshot(self.home);item=self.item(page,ident);detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'])
            for snapshot in (page,detail):
                record=self.item(snapshot,ident)['user_acceptance'];self.assertEqual('unknown',record['state']);self.assertFalse(record['acceptance_recorded']);self.assertIn('不回退',record['note'])
    def test_legacy_scope_ABA_generation_and_bad_barrier_never_revive(self):
        self.card('c');self.user_accept('c',**legacy_acceptance())
        self.send('edit',{'card_id':'c','expected_revision':1,'actor':'owner','fields':{'title':'description'}})
        self.assertTrue(self.item(read.page_snapshot(self.home),'c')['user_acceptance']['acceptance_recorded'])
        for revision,path in ((2,'B'),(3,None)):self.send('edit',{'card_id':'c','expected_revision':revision,'actor':'owner','fields':{'repo_path':path}})
        page=read.page_snapshot(self.home);detail=read.page_snapshot(self.home,detail=self.item(page,'c')['card']['_detail_token'])
        for snapshot in (page,detail):
            record=self.item(snapshot,'c')['user_acceptance'];self.assertEqual('歷史驗收；不適用目前 scope',record['label']);self.assertFalse(record['acceptance_recorded'])
        self.card('generation');self.user_accept('generation',**legacy_acceptance());self.conn.execute("UPDATE kanban_cards SET approval_generation=1 WHERE card_id='generation'")
        self.assertFalse(self.item(read.page_snapshot(self.home),'generation')['user_acceptance']['acceptance_recorded'])
        self.card('barrier');self.user_accept('barrier',**legacy_acceptance());edit=self.send('edit',{'card_id':'barrier','expected_revision':1,'actor':'owner','fields':{'note':'description'}})
        self.corrupt();self.conn.execute('UPDATE kanban_events SET metadata_delta=? WHERE operation_id=?',('null',edit['operation_id']))
        self.assertEqual('unknown',self.item(read.page_snapshot(self.home),'barrier')['user_acceptance']['state'])
    def test_USER_record_survives_worker_and_over_50_reports_readonly(self):
        from orchestrator.tests.test_kanban_commands import worker_sample,worker_handoff
        self.card('c');accepted=self.user_accept('c');self.assertEqual('accepted',accepted['result'])
        o=worker_sample();first=self.worker('c',o);o={**o,'previous_observation_operation_id':first['operation_id'],'state':'completed','source_ref':'synthetic:terminal-read','handoff':worker_handoff(True)}
        self.assertEqual('accepted',self.worker('c',o)['result'])
        for revision in range(3,64):self.assertEqual('accepted',self.report('c','blocked',revision)['result'])
        before=list(self.conn.iterdump());page=read.page_snapshot(self.home);p=self.item(page,'c');detail=read.page_snapshot(self.home,detail=p['card']['_detail_token'])
        self.assertLessEqual(len(page['events']),3);self.assertTrue(detail['history_truncated']['c']['events'])
        for snapshot in (page,detail):
            item=self.item(snapshot,'c');u=item['user_acceptance'];self.assertTrue(u['acceptance_recorded']);self.assertEqual(accepted['operation_id'],u['record']['operation_id'])
            self.assertEqual('未綁目前候選',u['candidate_binding']);self.assertEqual('completed',item['worker_observation']['state'])
            self.assertEqual('blocked',item['delivery_report']['record']['report_status']);self.assertEqual('progress',item['delivery_report']['state']);self.assertEqual('工程受阻',item['display_group'])
            self.assertTrue(item['formal_verification_note'].startswith('未有可核對紀錄'))
        self.assertIn('已記錄使用者驗收',render(page,lazy=True));self.assertIn('synthetic:user-instruction',detail_fragment(detail))
        self.assertEqual(before,list(self.conn.iterdump()))
    def test_USER_acceptance_summary_detail_backlog_and_archive_consistent(self):
        self.card('board');self.create('backlog')
        for ident,backlog in (('board',False),('backlog',True)):
            accepted=self.user_accept(ident);self.assertEqual('accepted',accepted['result']);self.assertEqual('accepted',self.report(ident,'reported_done',1)['result'])
            before=list(self.conn.iterdump());page=read.page_snapshot(self.home,backlog=backlog);item=self.item(page,ident);detail=read.page_snapshot(self.home,backlog=backlog,detail=item['card']['_detail_token'])
            self.assertEqual(item['user_acceptance'],self.item(detail,ident)['user_acceptance'])
            for html in (render(page,backlog=backlog),detail_fragment(detail,backlog=backlog)):
                self.assertIn('使用者驗收已記錄；技術／正式驗證另列',html);self.assertIn('未綁目前候選',html)
            self.assertEqual(before,list(self.conn.iterdump()))
            self.assertEqual('accepted',self.send('archive',{'card_id':ident,'expected_revision':2,'actor':'fixture-owner'})['result'])
            page=read.page_snapshot(self.home,archived=True);item=self.item(page,ident);detail=read.page_snapshot(self.home,archived=True,detail=item['card']['_detail_token'])
            self.assertEqual('封存',item['display_group']);self.assertTrue(item['user_acceptance']['acceptance_recorded'])
            self.assertIn('使用者驗收已記錄；技術／正式驗證另列',detail_fragment(detail,archived=True))
    def test_latest_USER_incomplete_nonacceptance_or_malformed_never_falls_back(self):
        self.card('c');old=self.user_accept('c');latest=self.user_accept('c',source_refs=[]);self.assertEqual('accepted',latest['result'])
        self.assertEqual('accepted',self.report('c','blocked',2)['result'])
        item=self.item(read.page_snapshot(self.home),'c');u=item['user_acceptance'];self.assertEqual('incomplete',u['state']);self.assertFalse(u['acceptance_recorded']);self.assertEqual(latest['operation_id'],u['record']['operation_id'])
        self.corrupt();original=json.loads(self.conn.execute('SELECT payload FROM kanban_events WHERE operation_id=?',(latest['operation_id'],)).fetchone()[0])
        cases=[('not JSON','USER'),(json.dumps({**original,'actor':'assistant'}),'USER'),(json.dumps(original),'assistant'),(json.dumps({**original,'progress_binding':None}),'USER')]
        for raw,actor in cases:
            with self.subTest(raw=raw,actor=actor):
                self.conn.execute('UPDATE kanban_events SET payload=?,actor=? WHERE operation_id=?',(raw,actor,latest['operation_id']))
                page=read.page_snapshot(self.home);p=self.item(page,'c');detail=read.page_snapshot(self.home,detail=p['card']['_detail_token'])
                for snapshot in (page,detail):
                    u=self.item(snapshot,'c')['user_acceptance'];self.assertEqual('unknown',u['state']);self.assertFalse(u['acceptance_recorded']);self.assertIn('不回退',u['note']);self.assertEqual(raw,u['raw_payload'])
        # A later legitimate USER report is content, neither acceptance nor withdrawal.
        newer=self.user_accept('c',report_status='in_progress',summary='最新 USER 進度說明',decision='仍在討論範圍');self.assertEqual('accepted',newer['result'])
        u=self.item(read.page_snapshot(self.home),'c')['user_acceptance'];self.assertEqual('incomplete',u['state']);self.assertFalse(u['acceptance_recorded']);self.assertEqual('最新 USER 進度說明',u['record']['summary']);self.assertIn('不解讀為批准或撤回',u['note'])
    def test_USER_scope_ABA_and_generation_invalidate_but_description_does_not(self):
        self.card('c');self.user_accept('c')
        self.assertEqual('accepted',self.send('edit',{'card_id':'c','expected_revision':1,'actor':'owner','fields':{'title':'description'}})['result'])
        self.assertTrue(self.item(read.page_snapshot(self.home),'c')['user_acceptance']['acceptance_recorded'])
        for revision,fields in ((2,{'repo_path':'B'}),(3,{'repo_path':None}),(4,{'note':'later description'})):
            self.assertEqual('accepted',self.send('edit',{'card_id':'c','expected_revision':revision,'actor':'owner','fields':fields})['result'])
        self.assertEqual('accepted',self.report('c','in_progress',5)['result'])
        page=read.page_snapshot(self.home);p=self.item(page,'c');detail=read.page_snapshot(self.home,detail=p['card']['_detail_token'])
        for snapshot in (page,detail):
            u=self.item(snapshot,'c')['user_acceptance'];self.assertEqual('historical',u['state']);self.assertEqual('歷史驗收；不適用目前 scope',u['label']);self.assertFalse(u['acceptance_recorded'])
        self.assertEqual('accepted',self.user_accept('c')['result']);self.assertTrue(self.item(read.page_snapshot(self.home),'c')['user_acceptance']['acceptance_recorded'])
        self.conn.execute("UPDATE kanban_cards SET approval_generation=approval_generation+1 WHERE card_id='c'")
        self.assertEqual('historical',self.item(read.page_snapshot(self.home),'c')['user_acceptance']['state'])
    def test_USER_unknown_scope_barrier_is_not_effective_acceptance(self):
        self.card('c');self.user_accept('c');edit=self.send('edit',{'card_id':'c','expected_revision':1,'actor':'owner','fields':{'note':'descriptive edit'}})
        self.assertEqual('accepted',edit['result']);self.corrupt();self.conn.execute('UPDATE kanban_events SET metadata_delta=? WHERE operation_id=?',('null',edit['operation_id']))
        self.assertEqual('accepted',self.report('c','blocked',2)['result'])
        page=read.page_snapshot(self.home);item=self.item(page,'c');detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'])
        for snapshot in (page,detail):
            u=self.item(snapshot,'c')['user_acceptance'];self.assertFalse(u['acceptance_recorded']);self.assertEqual('unknown',u['state'])
    def test_USER_record_does_not_bind_current_candidate_or_imply_formal_PASS(self):
        self.card('c');self.conn.execute("UPDATE kanban_cards SET candidate_fingerprint=? WHERE card_id='c'",('a'*64,));self.user_accept('c')
        self.assertEqual('accepted',self.send('report-progress',{'card_id':'c','expected_revision':1,'actor':'assistant','report_status':'reported_done','summary':'PASS 部署成功 is unverified source prose','source_refs':['synthetic:claim']})['result'])
        before=list(self.conn.iterdump());item=self.item(read.page_snapshot(self.home),'c')
        self.assertEqual('delivered',item['delivery_report']['state']);self.assertTrue(item['user_acceptance']['acceptance_recorded'])
        self.assertEqual('未綁目前候選',item['user_acceptance']['candidate_binding']);self.assertNotIn('candidate_fingerprint',item['user_acceptance']['record'])
        self.assertNotEqual('已驗',item['group']);self.assertNotEqual('已驗',item['display_group']);self.assertTrue(item['formal_verification_note'].startswith('未有可核對紀錄'));self.assertIn('不表示從未執行',item['formal_verification_note'])
        self.assertEqual(before,list(self.conn.iterdump()))
    def test_worker_survives_ordinary_reports_over_history_limit(self):
        from orchestrator.tests.test_kanban_commands import worker_sample
        self.card('c');o=worker_sample();r=self.worker('c',o);self.assertEqual('accepted',r['result'])
        o={**o,'state':'running','previous_observation_operation_id':r['operation_id'],'observed_at_ms':50};r=self.worker('c',o)
        for revision in range(2,63):self.assertEqual('accepted',self.report('c','in_progress',revision)['result'])
        summary=read.page_snapshot(self.home);item=self.item(summary,'c');detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'])
        self.assertLessEqual(len(summary['events']),3)
        self.assertTrue(detail['history_truncated']['c']['events']);self.assertEqual(50,len(detail['events']))
        self.assertEqual(item['worker_observation'],self.item(detail,'c')['worker_observation'])
        self.assertEqual(r['operation_id'],item['worker_observation']['operation_id']);self.assertEqual('running',item['worker_observation']['state'])
        self.assertEqual('safe summary',item['progress_report']['summary'])
        self.assertIn('synthetic:dispatch',detail_fragment(detail));self.assertIn('觀測到 running',render(summary,lazy=True))
    def test_scope_barrier_cleanup_remains_historical_in_both_views(self):
        from orchestrator.tests.test_kanban_commands import worker_sample,worker_handoff
        self.card('c');o=worker_sample();r=self.worker('c',o)
        for revision,fields in ((1,{'repo_path':'B'}),(2,{'repo_path':None}),(3,{'title':'new title'})):
            self.assertEqual('accepted',self.send('edit',{'card_id':'c','expected_revision':revision,'actor':'owner','fields':fields})['result'])
        o={**o,'state':'completed','source_ref':'synthetic:terminal-read','handoff':worker_handoff(True),'previous_observation_operation_id':r['operation_id']};terminal=self.worker('c',o);self.assertEqual('accepted',terminal['result'])
        self.assertEqual('accepted',self.report('c','blocked',5)['result'])
        summary=read.page_snapshot(self.home);item=self.item(summary,'c');detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'])
        for snapshot in (summary,detail):
            p=self.item(snapshot,'c');self.assertIsNone(p['worker_observation']);self.assertIn('失效',p['worker_observation_note'])
            self.assertEqual('blocked',p['progress_report']['report_status'])
        new=worker_sample(supersedes_attempt_id=o['attempt_id'],previous_observation_operation_id=terminal['operation_id'])
        self.assertEqual('accepted',self.worker('c',new)['result'])
        self.assertEqual(new['attempt_id'],self.item(read.page_snapshot(self.home),'c')['worker_observation']['attempt_id'])
    def test_malformed_latest_observation_never_falls_back_after_plain_report(self):
        from orchestrator.tests.test_kanban_commands import worker_sample
        self.card('c');o=worker_sample();first=self.worker('c',o);o={**o,'state':'running','previous_observation_operation_id':first['operation_id']};latest=self.worker('c',o)
        self.corrupt();row=self.conn.execute('SELECT payload FROM kanban_events WHERE operation_id=?',(latest['operation_id'],)).fetchone();payload=json.loads(row[0]);payload['worker_observation']['schema_version']=True
        self.conn.execute('UPDATE kanban_events SET payload=? WHERE operation_id=?',(json.dumps(payload),latest['operation_id']))
        self.assertEqual('accepted',self.report('c','in_progress',2)['result'])
        summary=read.page_snapshot(self.home);item=self.item(summary,'c');detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'])
        for snapshot in (summary,detail):
            p=self.item(snapshot,'c');self.assertIsNone(p['worker_observation']);self.assertIn('不回退',p['worker_observation_note']);self.assertIsNotNone(p['progress_report'])
        # Invalid JSON and valid non-object JSON cannot disappear in summary SQL.
        for raw in ('null','[]','not JSON'):
            self.conn.execute('UPDATE kanban_events SET payload=? WHERE operation_id=?',(raw,latest['operation_id']))
            page=read.page_snapshot(self.home);p=self.item(page,'c');self.assertIsNone(p['worker_observation']);self.assertIn('不回退',p['worker_observation_note'])
            self.assertIsNotNone(p['progress_report'])
    def test_backlog_reported_done_keeps_unverified_warning_in_detail(self):
        self.create('c');self.assertEqual('accepted',self.report('c','reported_done')['result'])
        page=read.page_snapshot(self.home,backlog=True);item=self.item(page,'c');self.assertEqual('完成',item['display_group'])
        detail=read.page_snapshot(self.home,backlog=True,detail=item['card']['_detail_token'])
        for html in (render(page,backlog=True),detail_fragment(detail,backlog=True)):
            self.assertIn('未驗收',html);self.assertIn('完成證據未驗證',html)
    def test_display_holds_match_summary_detail_and_preserve_raw_report(self):
        self.card('engineering');self.task('engineering','waiting_user','dirty_tree')
        self.assertEqual('accepted',self.send('report-progress',{'card_id':'engineering','expected_revision':0,'actor':'assistant','report_status':'blocked','summary':'Candidate waiting for engineering owner','blocker':'Owner must hand over the committed baseline','decision':None,'source_refs':[]})['result'])
        self.card('question');self.assertEqual('accepted',self.send('report-progress',{'card_id':'question','expected_revision':0,'actor':'assistant','report_status':'needs_decision','summary':'Scope question','decision':'Which scope?','source_refs':[]})['result'])
        self.card('manual');self.assertEqual('accepted',self.report('manual','in_progress')['result'])
        summary=read.page_snapshot(self.home)
        for ident,expected in (('engineering','工程受阻'),('question','待使用者'),('manual','實作中')):
            with self.subTest(ident=ident):
                item=self.item(summary,ident)
                detail=read.page_snapshot(self.home,detail=item['card']['_detail_token'])
                self.assertEqual(expected,item['display_group'])
                self.assertEqual(expected,self.item(detail,ident)['display_group'])
                self.assertEqual(item['progress_report'],self.item(detail,ident)['progress_report'])
                fragment=detail_fragment(detail)
                self.assertIn('未記錄' if ident=='engineering' else 'Which scope?' if ident=='question' else 'worker 狀態未觀測',fragment)
        self.assertIsNone(self.item(summary,'manual')['workflow_reason'])
        self.assertIsNone(self.item(summary,'manual')['card']['task_id'])
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
    def test_unsafe_states_and_explicit_selection_required(self):
        for state in ('done','mystery','archived'):
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
        self.assertEqual('backlog_requires_effective_pending',self.place('c','backlog',4)['reason'])
        self.assertEqual(['c'],[c['card_id'] for c in read.page_snapshot(self.home)['cards']])
        self.assertIsNone(self.item(read.page_snapshot(self.home),'c')['progress_report'])
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
        self.assertEqual(1,queries.count('BEGIN'));self.assertEqual(1,queries.count('COMMIT'));self.assertLess(len(queries),21) # D + C add five bounded observation/attribution/barrier checks, never full histories
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

    def task(self,ident,status,stop_reason=None):
        self.conn.execute("INSERT INTO tasks(id,type,status,current_stage,profile_hash,input_hash,profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,created_at,updated_at,stop_reason) VALUES (?,'apply',?,'review','pf','ih','synthetic-profile','synthetic-input','synthetic-artifact',8,1,2,?)",('task-'+ident,status,stop_reason))
        self.conn.execute("UPDATE kanban_cards SET task_id=?,request_id=? WHERE card_id=?",('task-'+ident,'request-'+ident,ident))
    def night(self,ident,phase):
        self.conn.execute("INSERT INTO kanban_nights(night_id,window_start_ms,window_end_ms,card_id,approval_generation,approval_hash,task_id,request_id,workspace_dir,base_head,candidate_fingerprint,profile_hash,input_bytes,input_hash,pool_claims,reserved_at,phase,stop_reason) VALUES (?,1,2,?,1,'hash',?,?,'synthetic-workspace','head','fingerprint','pf',X'00','ih','{}',1,?,'synthetic')",('night-'+ident,ident,'night-task-'+ident,'night-request-'+ident,phase))
    def test_known_decision_matrix_preserves_all_nonplacement_state(self):
        cases=[('manual-clarification','needs_clarification',None,None),('manual-returned','returned',None,None),
               ('report-decision','inbox',None,'needs_decision'),('report-blocked','inbox',None,'blocked'),
               ('task-blocked','ready','blocked',None),('task-waiting','inbox','waiting_user',None),
               ('linked-report','returned','waiting_user','needs_decision')]
        for ident,manual,status,report_status in cases:
            with self.subTest(ident=ident):
                self.card(ident,manual)
                if status:self.task(ident,status)
                self.night(ident,'stopped')
                # Frozen approval/binding/profile fields are synthetic opaque metadata.
                approval='approval-'+ident
                self.conn.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result,payload) VALUES (?,'hash','fixture',?,'synthetic',1,'accepted','{}')",(approval,ident))
                self.conn.execute("UPDATE kanban_cards SET approval_generation=3,approval_hash='approval-hash',approval_actor='synthetic',approval_at=1,approval_event_id=?,repo_path='synthetic-repo',base_head='head',candidate_fingerprint='fingerprint',profile_name='synthetic-profile',profile_hash='pf',routing_digest='route',config_digest='config',note='Unanswered decision retained' WHERE card_id=?",(approval,ident))
                revision=0
                if report_status:
                    self.assertEqual('accepted',self.send('report-progress',{'card_id':ident,'expected_revision':0,'actor':'assistant','report_status':report_status,'summary':'Waiting for explicit answer','decision':'Which 18 entries? <script>question</script>','blocker':'No publication authorization','next_step':'Await answer','source_refs':['synthetic:opaque-pointer']})['result']);revision=1
                before=dict(self.conn.execute('SELECT * FROM kanban_cards WHERE card_id=?',(ident,)).fetchone())
                tables=[r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('kanban_cards','kanban_events','sqlite_sequence') ORDER BY name")]
                rows={t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}
                schema=[tuple(r) for r in self.conn.execute('SELECT * FROM sqlite_master ORDER BY name')]
                events=[tuple(r) for r in self.conn.execute('SELECT * FROM kanban_events WHERE card_id=? ORDER BY rowid',(ident,))]
                op=str(uuid.uuid4());outcome=self.place(ident,'backlog',revision,op);self.assertEqual('accepted',outcome['result'])
                self.assertTrue(self.place(ident,'backlog',revision,op)['replayed'])
                self.assertEqual('idempotency_conflict',self.place(ident,'board',revision,op)['reason'])
                self.assertEqual('revision_conflict',self.place(ident,'backlog',revision)['reason'])
                after=dict(self.conn.execute('SELECT * FROM kanban_cards WHERE card_id=?',(ident,)).fetchone())
                self.assertEqual({k:v for k,v in before.items() if k not in {'revision','updated_at'}},{k:v for k,v in after.items() if k not in {'revision','updated_at'}})
                self.assertEqual(revision+1,after['revision'])
                self.assertEqual(rows,{t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables})
                self.assertEqual(schema,[tuple(r) for r in self.conn.execute('SELECT * FROM sqlite_master ORDER BY name')])
                self.assertEqual(events,[tuple(r) for r in self.conn.execute('SELECT * FROM kanban_events WHERE card_id=? ORDER BY rowid LIMIT ?',(ident,len(events)))])
                self.assertNotIn(ident,[c['card_id'] for c in read.page_snapshot(self.home)['cards']])
                backlog=read.page_snapshot(self.home,backlog=True);item=self.item(backlog,ident);self.assertEqual('待決策',item['group']);self.assertIsNone(item['workflow_reason'])
                detail=read.page_snapshot(self.home,backlog=True,detail=item['card']['_detail_token'])
                if report_status:
                    self.assertEqual('Which 18 entries? <script>question</script>',self.item(detail,ident)['progress_report']['decision'])
                    fragment=detail_fragment(detail,backlog=True);self.assertIn('&lt;script&gt;question',fragment);self.assertNotIn('<script>',fragment)
                self.assertEqual('accepted',self.place(ident,'board',revision+1)['result'])
                self.assertEqual('待決策',self.item(read.page_snapshot(self.home),ident)['group'])
                self.assertEqual(rows,{t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables})
                reversed_card=dict(self.conn.execute('SELECT * FROM kanban_cards WHERE card_id=?',(ident,)).fetchone())
                self.assertEqual({k:v for k,v in before.items() if k not in {'revision','updated_at'}},{k:v for k,v in reversed_card.items() if k not in {'revision','updated_at'}})
                placed=self.conn.execute("SELECT metadata_delta,payload FROM kanban_events WHERE operation_id=?",(op,)).fetchone()
                self.assertEqual({'queue_location':'backlog'},json.loads(placed['metadata_delta']));self.assertIn('USER explicitly selects',json.loads(placed['payload'])['user_request'])
    def test_execution_completion_pending_and_active_nights_cannot_be_hidden(self):
        cases=[('task-'+s,'inbox',s,'needs_decision',None,None) for s in ('queued','running','paused','failed','done','UserReview','user_review','mystery')]
        cases += [('queued-no-report','inbox','queued',None,None,None),('running-no-report','inbox','running',None,None,None),
                  ('manual-done','done',None,'needs_decision',None,None),('archived','archived',None,'needs_decision',None,None),
                  ('reported-done','needs_clarification',None,'reported_done',None,None),('reported-running','returned',None,'in_progress',None,None),
                  ('card-pending','returned',None,'needs_decision','card',None),('task-pending','returned','blocked','needs_decision','task',None)]
        cases += [(phase+'-'+kind,'inbox',None,'needs_decision' if kind=='decision' else None,None,phase) for phase in ('reserved','submitted') for kind in ('decision','pending')]
        for ident,manual,status,report_status,pending,phase in cases:
            with self.subTest(ident=ident):
                self.card(ident,manual)
                if status:self.task(ident,status,'manual_pause_pending' if pending=='task' else None)
                if pending=='card':self.conn.execute("UPDATE kanban_cards SET last_reason='manual_pause_pending' WHERE card_id=?",(ident,))
                if phase:self.night(ident,phase)
                if report_status:self.assertEqual('accepted',self.report(ident,report_status)['result'])
                revision=1 if report_status else 0
                before=dict(self.conn.execute('SELECT * FROM kanban_cards WHERE card_id=?',(ident,)).fetchone())
                self.assertEqual('rejected',self.place(ident,'backlog',revision)['result'])
                self.assertEqual(before,dict(self.conn.execute('SELECT * FROM kanban_cards WHERE card_id=?',(ident,)).fetchone()))
                self.assertNotIn(ident,[c['card_id'] for c in read.page_snapshot(self.home,backlog=True)['cards']])
    def test_orphan_anomalous_and_stale_decisions_cannot_be_hidden(self):
        self.corrupt()
        for kind in ('orphan','unknown-manual','unknown-night','bad-time','bad-report','stale-generation','stale-scope','bad-location','bad-edit','bad-revision'):
            with self.subTest(kind=kind):
                self.card(kind,'needs_clarification');self.assertEqual('accepted',self.report(kind,'needs_decision')['result']);revision=1
                if kind=='orphan':
                    self.conn.execute('PRAGMA foreign_keys=OFF');self.conn.execute("UPDATE kanban_cards SET task_id='missing' WHERE card_id=?",(kind,));self.conn.execute('PRAGMA foreign_keys=ON')
                elif kind=='unknown-manual':self.conn.execute("UPDATE kanban_cards SET manual_state='mystery' WHERE card_id=?",(kind,))
                elif kind=='unknown-night':self.night(kind,'unknown')
                elif kind=='bad-time':self.conn.execute("UPDATE kanban_events SET at=-1 WHERE card_id=?",(kind,))
                elif kind=='bad-report':self.conn.execute("UPDATE kanban_events SET payload='broken' WHERE card_id=?",(kind,))
                elif kind=='stale-generation':self.conn.execute("UPDATE kanban_cards SET approval_generation=1 WHERE card_id=?",(kind,))
                elif kind=='stale-scope':self.conn.execute("UPDATE kanban_cards SET repo_path='changed' WHERE card_id=?",(kind,))
                elif kind=='bad-location':
                    self.assertEqual('accepted',self.place(kind,'board',1)['result']);revision=2
                    self.conn.execute("UPDATE kanban_events SET metadata_delta='broken' WHERE card_id=? AND kind='place'",(kind,))
                elif kind=='bad-edit':
                    self.assertEqual('accepted',self.send('edit',{'card_id':kind,'expected_revision':1,'actor':'operator','fields':{'note':'descriptive'}})['result']);revision=2
                    self.conn.execute("UPDATE kanban_events SET metadata_delta='broken' WHERE card_id=? AND kind='edit'",(kind,))
                elif kind=='bad-revision':self.conn.execute("UPDATE kanban_events SET result_revision=99 WHERE card_id=?",(kind,))
                self.assertEqual('backlog_requires_effective_pending',self.place(kind,'backlog',revision)['reason'])
                self.assertIn(kind,[c['card_id'] for c in read.page_snapshot(self.home)['cards']])
                self.assertNotIn(kind,[c['card_id'] for c in read.page_snapshot(self.home,backlog=True)['cards']])

    def test_cli_place_payload_and_progress_allowlist(self):
        args=build_parser().parse_args(['kanban','place','--card','c','--expected-revision','2','--destination','board','--user-request','USER picks c','--actor','operator'])
        self.assertEqual({'card_id':'c','expected_revision':2,'destination':'board','user_request':'USER picks c','actor':'operator'},_kanban_payload(args))
        from orchestrator.daemon import PROGRESS_COMMANDS
        self.assertIn('place',PROGRESS_COMMANDS);self.assertNotIn('approve',PROGRESS_COMMANDS)

if __name__=='__main__':unittest.main()
