#!/usr/bin/env python3
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import main


class StartupHealth(unittest.TestCase):
    def test_health_serves_while_initial_scan_and_coverage_are_waiting(self):
        gate = threading.Event()
        created = threading.Event()
        servers = []
        original_make = main.make_server
        original_scan = main.Indexer.scan_once

        def make(*args, **kwargs):
            server = original_make(*args, **kwargs)
            servers.append(server)
            created.set()
            return server

        def scan(indexer):
            gate.wait(5)
            return original_scan(indexer)

        def wait_for_scan(state):
            gate.wait(5)
            return False

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(main, 'make_server', side_effect=make), \
                 patch.object(main.Indexer, 'scan_once', scan), \
                 patch.object(main.ServerState, 'wait_for_first_scan', wait_for_scan), \
                 patch.object(main, 'load_or_create_token', return_value='fixture-startup'):
                worker = threading.Thread(target=main.serve, kwargs={
                    'host': '127.0.0.1', 'port': 0, 'db_path': Path(folder) / 'index.db',
                    'dirs': [], 'pidfile': False,
                }, daemon=True)
                worker.start()
                try:
                    self.assertTrue(created.wait(2))
                    url = f'http://127.0.0.1:{servers[0].server_address[1]}/api/health'
                    request = urllib.request.Request(url, headers={'Authorization': 'Bearer fixture-startup'})
                    start = time.monotonic()
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(request, timeout=2) as response:
                        import json
                        data = json.load(response)
                    self.assertTrue(data['building'])
                    self.assertLess(time.monotonic() - start, 2)
                    self.assertFalse(gate.is_set())
                finally:
                    gate.set()
                    if servers:
                        servers[0].state.stop()
                        servers[0].shutdown()
                    worker.join(5)
                self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main(verbosity=2)
