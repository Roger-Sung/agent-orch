import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import start
from orchestrator.tests.test_interpretation_envelope import _axis, _reply


class ResolverDiagnosticsTests(unittest.TestCase):
    def test_path_punctuation_is_consistent_and_directory_tokens_stay_detected(self):
        for text in ("`scripts/example.py`", "scripts/example.py.", "scripts/example.py,", "scripts/example.py;", "a/b/.", "a/b/.."):
            self.assertTrue(start._sources_name_any_path([("scope", text)]), text)
        self.assertFalse(start._sources_name_any_path([("scope", "read/write and/or")]))

    def test_safe_receipt_distinguishes_missing_and_extra_keys_without_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = {"task_description": "Review only", "scope": "Review only", "flags": {}, "task_type_hint": "review"}
            for label in ("missing", "extra"):
                entry = _axis("declared", [], [])
                if label == "missing": del entry["detail"]
                else: entry["sensitive-unknown-key"] = "sensitive-body-value"
                reply = _reply(semantic_change_surface=_axis("semantically_silent", ["Review only"]), task_owned_write_targets=entry)
                path = Path(tmp) / (label + '.json')
                with patch.object(start, '_invoke_resolver', return_value=reply):
                    result = start._resolve_envelope(task, receipt_path=path)
                self.assertIsNone(result.axes)
                raw = path.read_text(); receipt = json.loads(raw)
                shape = receipt['axes']['task_owned_write_targets']
                self.assertEqual(shape['missing_keys'], ['detail'] if label == 'missing' else [])
                self.assertEqual(shape['unexpected_key_count'], 1 if label == 'extra' else 0)
                self.assertNotIn('sensitive', raw)
                self.assertFalse(receipt['raw_reply_retained'])
                self.assertIsNone(receipt['provider_reported_model'])
                self.assertIsNone(receipt['provider_usage'])
                self.assertGreaterEqual(receipt['duration_ms'], 0)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_transport_failure_receipt_has_no_invented_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'error.json'
            with patch.object(start, '_invoke_resolver', side_effect=start.EnvelopeResolverError('transport failed')):
                result = start._resolve_envelope({'task_description': 'review'}, receipt_path=path)
            self.assertIsNone(result.axes)
            receipt = json.loads(path.read_text())
            self.assertFalse(receipt['reply_available'])
            self.assertNotIn('reply_sha256', receipt)

    def test_existing_receipt_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'existing.json'; path.write_text('original')
            with patch.object(start, '_invoke_resolver', return_value='invalid'):
                with self.assertRaises(FileExistsError):
                    start._resolve_envelope({'task_description': 'review'}, receipt_path=path)
            self.assertEqual(path.read_text(), 'original')
