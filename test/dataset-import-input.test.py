#!/usr/bin/env python3
from pathlib import Path
import errno
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.storage.sqlite_store import TraceStore  # noqa: E402
from evaluation.datasets import import_jsonl  # noqa: E402


class DatasetImportInput(unittest.TestCase):
    def test_raw_text_survives_filename_probe_failures(self):
        text = json.dumps({'type': 'example', 'inputs': {'text': 'x' * 5000}}) + '\n'
        for error in [OSError(errno.ENAMETOOLONG, 'too long'), ValueError('invalid path')]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as folder:
                store = TraceStore(Path(folder) / 'test.db')
                try:
                    with patch.object(Path, 'is_file', side_effect=error):
                        result = import_jsonl(store, text)
                    self.assertEqual(result['imported'], 1)
                    self.assertEqual(result['skipped'], 0)
                finally:
                    store.close()

    def test_file_input_still_reads_file(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as folder:
            source = Path(folder) / 'input.jsonl'
            source.write_text(json.dumps({'type': 'example', 'inputs': {'task': 'fixture'}}) + '\n')
            store = TraceStore(Path(folder) / 'test.db')
            try:
                self.assertEqual(import_jsonl(store, str(source))['imported'], 1)
                with patch.object(Path, 'read_text', side_effect=PermissionError('fixture')):
                    with self.assertRaises(PermissionError):
                        import_jsonl(store, str(source))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
