import unittest
import tempfile
import os
import io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
from orchestrator import cli
from orchestrator.db import connect

class CliTests(unittest.TestCase):
    def test_no_mutation(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'ORCH_HOME':home}), patch.object(cli,'Controller',side_effect=AssertionError('mutation')), patch.object(cli,'enqueue_request',side_effect=AssertionError('enqueue')), patch.object(cli,'daemon_is_running',side_effect=AssertionError('daemon')), redirect_stdout(io.StringIO()):
            path=Path(home)/'orchestrator.db'
            assert cli.main(['kanban','list','--json'])==2
            assert not path.exists()
            conn=connect(path); before=list(conn.iterdump())
            assert cli.main(['kanban','list','--json'])==0
            assert cli.main(['kanban','show','--card','missing','--json'])==2
            output=Path(home).parent/(Path(home).name+'-demo.html')
            assert cli.main(['kanban','render','--output',str(output)])==0
            assert output.is_file() and list(conn.iterdump())==before
            output.unlink()
            assert cli.main(['kanban','render','--output',str(path)])==2
            alias=Path(home).parent/(Path(home).name+'-alias.html')
            os.link(path,alias)
            try: assert cli.main(['kanban','render','--output',str(alias)])==2
            finally: alias.unlink()
            assert list(conn.iterdump())==before
            conn.close()

import json
from contextlib import redirect_stderr
from orchestrator.ipc import IPCError

class ProgressCliTests(unittest.TestCase):
    def args(self):return ['kanban','report-progress','--card','c','--expected-revision','0','--report-status','in_progress','--summary','中文回報','--actor','assistant:synthetic','--source-ref','text:pointer']
    def test_payload_and_no_controller(self):
        parsed=cli.build_parser().parse_args(self.args());payload=cli._kanban_payload(parsed)
        self.assertEqual('in_progress',payload['report_status']);self.assertEqual(['text:pointer'],payload['source_refs'])
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,{'ORCH_HOME':home}), patch.object(cli,'Controller',side_effect=AssertionError('writer forbidden')), patch.object(cli,'daemon_is_running',return_value=False), patch.object(cli,'enqueue_request') as enqueue, redirect_stderr(io.StringIO()):
            self.assertEqual(2,cli.main(self.args()));enqueue.assert_not_called();self.assertFalse((Path(home)/'orchestrator.db').exists())
    def test_result_rejection_timeout_and_unknown_response(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ,{'ORCH_HOME':home}), patch.object(cli,'daemon_is_running',return_value=True), patch.object(cli,'enqueue_request',return_value=Path(home)/'request.json') as enqueue, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for response,code in [({'kanban':{'result':'accepted'}},0),({'kanban':{'result':'rejected','reason':'revision_conflict'}},2),({},2)]:
                with patch.object(cli,'wait_for_result',return_value=response):self.assertEqual(code,cli.main(self.args()))
            self.assertEqual('report-progress',enqueue.call_args.args[1]['command'])
            with patch.object(cli,'wait_for_result',side_effect=IPCError('timed out; request remains queued')):self.assertEqual(2,cli.main(self.args()))
            self.assertFalse((Path(home)/'orchestrator.db').exists())
    def test_invalid_status_or_missing_summary(self):
        with redirect_stderr(io.StringIO()):
            for argv in [self.args()[:-4]+['--report-status','verified_done'],['kanban','report-progress','--card','c','--expected-revision','0','--report-status','in_progress']]:
                with self.assertRaises(SystemExit) as error:cli.build_parser().parse_args(argv)
                self.assertEqual(2,error.exception.code)
