"""Actual progress daemon/IPC in synthetic state; execution paths are traps."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from orchestrator import db
from orchestrator.kanban.commands import build_request
from orchestrator.ipc import enqueue_request, wait_for_result

DRIVER = r'''
import os,subprocess
from pathlib import Path
from orchestrator import cli,daemon,db

def forbidden(*args,**kwargs):
    Path(os.environ['ORCH_HOME'],'execution-called').write_text('forbidden')
    raise AssertionError('execution forbidden')
cli.Controller=daemon.Controller=forbidden
db.connect=forbidden
daemon.require_unattended_consent=forbidden
daemon._reconcile_startup_requests=forbidden
subprocess.Popen=forbidden
os.system=forbidden
if hasattr(os,'posix_spawn'):os.posix_spawn=forbidden
window=os.environ.get('PM_TEST_CRASH')
if window=='before_claim':
    daemon._progress_scan=lambda *args:os._exit(91)
elif window=='after_claim':
    from orchestrator.kanban import commands
    commands._accept=lambda *args,**kwargs:os._exit(91)
elif window=='after_commit':
    original=daemon.atomic_write_json
    def publish(path,payload):
        if path.name.endswith('.progress.result.json'):os._exit(91)
        return original(path,payload)
    daemon.atomic_write_json=publish
elif window=='after_publish':
    original=os.replace
    def move(source,dest):
        if Path(source).parent.name=='processing' and Path(dest).name.endswith('.progress.request.json'):os._exit(91)
        return original(source,dest)
    os.replace=move
raise SystemExit(cli.main(['daemon']))
'''


def legacy_rows(home):
    conn=db.connect(home/'orchestrator.db',read_only=True)
    try:
        tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'kanban_%' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}
    finally:conn.close()


def legacy_schema(home):
    conn=sqlite3.connect(home/'orchestrator.db')
    try:return conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE '%kanban_%' AND name <> 'sqlite_sequence' ORDER BY name").fetchall()
    finally:conn.close()


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.home=self.fixture('main')
    def fixture(self,name):
        home=self.root/name;home.mkdir()
        with patch('orchestrator.kanban.store.SCHEMA',''):
            conn=db.connect(home/'orchestrator.db')
        conn.execute("INSERT INTO tasks(id,type,status,current_stage,profile_hash,input_hash,profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,created_at,updated_at) VALUES ('old','apply','done','review','pf','ih','PRIVATE-profile','PRIVATE-input','PRIVATE-artifact',8,1,2)")
        conn.execute("INSERT INTO stage_runs(run_token,task_id,stage,cycle,attempt,owner,status,log_path,started_at) VALUES ('run','old','apply',1,1,'codex','committed','PRIVATE-log',1)")
        conn.execute("INSERT INTO transitions(task_id,seq,operation_id,to_status,at) VALUES ('old',1,'legacy-op','done',2)")
        conn.execute("INSERT INTO notifications(task_id,transition_seq,reason,message,created_at) VALUES ('old',1,'legacy','PRIVATE-notification',2)")
        conn.execute("INSERT INTO trajectory_events(trajectory_id,seq,event_id,schema_version,event_type,event_version,task_id,recorded_at_ms,canonical_json,event_hash) VALUES ('traj',1,'event',1,'legacy',1,'old',2,?,'hash')",(b'PRIVATE-trajectory',))
        conn.execute('CREATE TABLE pack_preservation_sentinel(id TEXT PRIMARY KEY,payload BLOB)')
        conn.execute('INSERT INTO pack_preservation_sentinel VALUES (?,?)',('pack',b'PRIVATE-pack'))
        conn.close()
        (home/'old-artifact.txt').write_text('PRIVATE-file-unchanged')
        (home/'fixture.toml').write_text('')
        return home
    def environment(self,home,**extra):
        return {'PATH':os.environ['PATH'],'PYTHONPATH':str(Path(__file__).resolve().parents[2]),'PYTHONDONTWRITEBYTECODE':'1','ORCH_HOME':str(home),'ORCH_CONFIG':str(home/'fixture.toml'),'ORCH_POLL_INTERVAL':'0.02','ORCH_DAEMON_MODE':'progress-management',**extra}
    def start(self,home=None,**extra):
        home=home or self.home;log=(home/'daemon-test.log').open('a')
        proc=subprocess.Popen([sys.executable,'-c',DRIVER],env=self.environment(home,**extra),stdout=log,stderr=subprocess.STDOUT)
        self.addCleanup(self.stop,proc);self.addCleanup(log.close)
        return proc
    def stop(self,proc):
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=4)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=4)
    def ready(self,proc,home=None):
        home=home or self.home;deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if proc.poll() is not None:self.fail((home/'daemon-test.log').read_text())
            if (home/'daemon.pid').exists() and (home/'daemon.pid').read_text().strip()==str(proc.pid) and f'pid={proc.pid}' in (home/'daemon-test.log').read_text():return
            time.sleep(.02)
        self.fail('daemon readiness timeout')
    def send(self,command,payload,home=None,operation=None):
        home=home or self.home
        path=enqueue_request(home,build_request(command,payload,operation_id=operation))
        return wait_for_result(home,path,4,.02)['kanban']
    def card(self,home=None):
        return self.send('create',{'actor':'assistant:synthetic','card_id':'card','fields':{'title':'中文合成卡'}},home)
    def test_mixed_queue_restart_old_state_and_zero_execution(self):
        for folder in ('inbox','processing','processed','quarantine'):(self.home/folder).mkdir()
        retained={}
        for directory in ('inbox','processing'):
            examples={'00-run.json':{'request_id':str(uuid.uuid4()),'action':'run','type':'apply','input':'PRIVATE-input'},'01-default.json':{'request_id':str(uuid.uuid4()),'type':'apply'},'02-resume.json':{'request_id':str(uuid.uuid4()),'action':'resume','task_id':'old'},'03-approve.json':{'request_id':str(uuid.uuid4()),'action':'kanban','command':'approve'},'04-bad-command.json':{'action':'kanban','command':[]},'05-done.json':{'action':'kanban','command':'done','payload':{'binding':'PRIVATE-not-read'}},'06-quota.json':{'action':'kanban','command':'quota-snapshot'}}
            for name,request in examples.items():
                path=self.home/directory/name;path.write_text(json.dumps(request));retained[path]=path.read_bytes()
            for name,content in (('07-corrupt.json','PRIVATE-invalid-json'),('.partial.tmp-old','PRIVATE-temp'),('notes.txt','PRIVATE-note')):
                path=self.home/directory/name;path.write_text(content);retained[path]=path.read_bytes()
        # A conflicting processing filename must not be replaced by inbox claim.
        collision=self.home/'inbox'/'00-run.json';request=build_request('create',{'actor':'synthetic','card_id':'collision','fields':{'title':'collision'}});collision.write_text(json.dumps(request));retained[collision]=collision.read_bytes()
        before=legacy_rows(self.home);schema=legacy_schema(self.home)
        proc=self.start();self.ready(proc);self.assertEqual('accepted',self.card()['result'])
        report={'card_id':'card','expected_revision':0,'actor':'assistant:synthetic','report_status':'reported_done','summary':'完成回報未驗證','source_refs':['PRIVATE-text-pointer']};op=str(uuid.uuid4())
        self.assertEqual('accepted',self.send('report-progress',report,operation=op)['result'])
        self.assertTrue(self.send('report-progress',report,operation=op)['replayed'])
        self.assertEqual('revision_conflict',self.send('edit',{'card_id':'card','expected_revision':0,'actor':'synthetic','fields':{'note':'stale'}})['reason'])
        self.stop(proc);proc=self.start();self.ready(proc)
        self.assertEqual('accepted',self.send('edit',{'card_id':'card','expected_revision':1,'actor':'synthetic','fields':{'note':'post-restart'}})['result'])
        self.assertEqual('accepted',self.send('archive',{'card_id':'card','expected_revision':2,'actor':'synthetic'})['result'])
        self.stop(proc)
        for path,contents in retained.items():self.assertEqual(contents,path.read_bytes(),str(path))
        self.assertEqual(before,legacy_rows(self.home));self.assertEqual(schema,legacy_schema(self.home))
        self.assertEqual('PRIVATE-file-unchanged',(self.home/'old-artifact.txt').read_text())
        self.assertEqual([],list((self.home/'quarantine').iterdir()))
        self.assertFalse((self.home/'execution-called').exists());self.assertNotIn('PRIVATE-',(self.home/'daemon-test.log').read_text())
    def test_four_real_crash_windows_recover_once(self):
        for window in ('before_claim','after_claim','after_commit','after_publish'):
            with self.subTest(window=window):
                home=self.fixture(window);before=legacy_rows(home)
                path=enqueue_request(home,build_request('create',{'actor':'synthetic','card_id':'card','fields':{'title':'crash fixture'}}))
                retained=home/'inbox'/'00-retain.json';retained.write_text('{"action":"run","PRIVATE":"preserve"}');raw=retained.read_bytes()
                proc=self.start(home,PM_TEST_CRASH=window);self.assertEqual(91,proc.wait(timeout=4))
                conn=db.connect(home/'orchestrator.db',read_only=True)
                expected=1 if window in ('after_commit','after_publish') else 0
                self.assertEqual(expected,conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0]);self.assertEqual(expected,conn.execute('SELECT count(*) FROM kanban_cards').fetchone()[0]);conn.close()
                self.assertEqual(window=='before_claim',path.exists())
                self.assertEqual(window!='before_claim',(home/'processing'/path.name).exists())
                self.assertEqual(window=='after_publish',bool(list((home/'processed').glob('*.result.json'))))
                proc=self.start(home);self.ready(proc,home)
                result=wait_for_result(home,path,4,.02);self.assertEqual('accepted',result['kanban']['result'])
                deadline=time.monotonic()+3
                while (home/'processing'/path.name).exists() and time.monotonic()<deadline:time.sleep(.02)
                self.stop(proc)
                conn=db.connect(home/'orchestrator.db',read_only=True)
                self.assertEqual(1,conn.execute('SELECT count(*) FROM kanban_cards').fetchone()[0]);self.assertEqual(1,conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0]);conn.close()
                self.assertEqual(before,legacy_rows(home));self.assertEqual(raw,retained.read_bytes());self.assertFalse((home/'execution-called').exists())
    def test_poison_integer_then_valid_request_and_restart(self):
        before=legacy_rows(self.home);schema=legacy_schema(self.home)
        proc=self.start();self.ready(proc);self.assertEqual('accepted',self.card()['result'])
        for command in ('edit','report-progress','archive','create'):
            for revision in (10**100,-(10**100)):
                with self.subTest(command=command,revision=revision):
                    payload={'actor':'synthetic','card_id':'new' if command=='create' else 'card','expected_revision':revision}
                    if command in ('edit','create'):payload['fields']={'title':'bad integer'}
                    if command=='report-progress':payload.update(report_status='in_progress',summary='bad integer')
                    operation=str(uuid.uuid4());result=self.send(command,payload,operation=operation)
                    self.assertEqual('invalid_expected_revision',result['reason'])
                    self.assertTrue(self.send(command,payload,operation=operation)['replayed'])
                    self.assertIsNone(proc.poll())
        self.assertEqual('accepted',self.send('edit',{'actor':'synthetic','card_id':'card','expected_revision':0,'fields':{'note':'valid after poison'}})['result'])
        self.stop(proc);proc=self.start();self.ready(proc)
        self.assertEqual('accepted',self.send('report-progress',{'actor':'synthetic','card_id':'card','expected_revision':1,'report_status':'in_progress','summary':'valid after restart'})['result'])
        self.stop(proc)
        conn=db.connect(self.home/'orchestrator.db',read_only=True)
        self.assertEqual(2,conn.execute("SELECT revision FROM kanban_cards WHERE card_id='card'").fetchone()[0])
        self.assertEqual(11,conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0]);conn.close()
        self.assertEqual([],list((self.home/'processing').iterdir()))
        self.assertEqual(before,legacy_rows(self.home));self.assertEqual(schema,legacy_schema(self.home))
        self.assertFalse((self.home/'execution-called').exists());self.assertNotIn('Traceback',(self.home/'daemon-test.log').read_text())
    def test_lock_exclusion_and_mode_errors(self):
        proc=self.start();self.ready(proc)
        other=self.start();self.assertEqual(2,other.wait(timeout=4));self.assertIsNone(proc.poll());self.card();self.stop(proc)
        for mode in ('','bogus'):
            proc=self.start(ORCH_DAEMON_MODE=mode);self.assertEqual(2,proc.wait(timeout=4))
        self.assertFalse((self.home/'execution-called').exists())
    def test_missing_schema_active_and_extra_schema_fail_closed(self):
        for case in ('missing','wrong-legacy','partial-kanban','extra-kanban','queued','running-stage','unknown-night'):
            with self.subTest(case=case):
                home=self.fixture(case)
                if case=='missing':(home/'orchestrator.db').unlink()
                else:
                    conn=sqlite3.connect(home/'orchestrator.db')
                    if case=='wrong-legacy':conn.execute('ALTER TABLE tasks RENAME COLUMN status TO wrong_status')
                    elif case=='partial-kanban':conn.execute('CREATE TABLE kanban_cards(card_id TEXT)')
                    elif case=='queued':conn.execute("UPDATE tasks SET status='queued'")
                    elif case=='running-stage':conn.execute("UPDATE stage_runs SET status='running'")
                    elif case in ('extra-kanban','unknown-night'):
                        conn.close();conn=db.connect(home/'orchestrator.db')
                        if case=='extra-kanban':conn.execute('CREATE INDEX kanban_extra ON kanban_cards(title)')
                        else:
                            conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,created_at,updated_at) VALUES ('c','night','normal','inbox',1,1)")
                            conn.execute("INSERT INTO kanban_nights(night_id,window_start_ms,window_end_ms,card_id,approval_generation,approval_hash,task_id,request_id,workspace_dir,base_head,candidate_fingerprint,profile_hash,input_bytes,input_hash,pool_claims,reserved_at,phase,stop_reason) VALUES ('n',1,2,'c',1,'ah','old','r','w','b','f','p',X'00','i','{}',1,'unknown','unknown')")
                    conn.commit();before=list(conn.iterdump());conn.close()
                proc=self.start(home);self.assertEqual(2,proc.wait(timeout=4));self.assertFalse((home/'daemon.pid').exists());self.assertFalse((home/'execution-called').exists())
                if case=='missing':self.assertFalse((home/'orchestrator.db').exists())
                else:
                    conn=sqlite3.connect(home/'orchestrator.db');self.assertEqual(before,list(conn.iterdump()));conn.close()
    def test_cli_disallowed_commands_do_not_enqueue(self):
        for args in (['enqueue','--type','apply','--profile','PRIVATE','--input','PRIVATE'],['kanban','approve','--card','c','--expected-revision','0'],['kanban','quota-invalidate','--snapshot','q']):
            proc=subprocess.run([sys.executable,'-m','orchestrator',*args],env=self.environment(self.home),capture_output=True,text=True,timeout=4)
            self.assertEqual(2,proc.returncode);self.assertFalse((self.home/'inbox').exists())

    def test_real_cli_render_failure_preserves_accepted_mutation(self):
        proc=self.start();self.ready(proc);self.assertEqual('accepted',self.card()['result'])
        conn=db.connect(self.home/'orchestrator.db',read_only=True);before=list(conn.iterdump());conn.close()
        missing=self.root/'missing-parent'/'board.html'
        result=subprocess.run([sys.executable,'-m','orchestrator','kanban','render','--output',str(missing)],env=self.environment(self.home),capture_output=True,text=True,timeout=4)
        self.assertEqual(2,result.returncode);self.assertIn('unavailable',result.stdout);self.assertFalse(missing.exists())
        conn=db.connect(self.home/'orchestrator.db',read_only=True);self.assertEqual(before,list(conn.iterdump()));conn.close()
        output=self.root/'board.html'
        result=subprocess.run([sys.executable,'-m','orchestrator','kanban','render','--output',str(output)],env=self.environment(self.home),capture_output=True,text=True,timeout=4)
        self.assertEqual(0,result.returncode);html=output.read_text();self.assertIn(str(self.home/'orchestrator.db'),html);self.assertNotIn('演示資料',html);self.assertNotIn('90%',html)
        self.stop(proc);self.assertFalse((self.home/'execution-called').exists())
