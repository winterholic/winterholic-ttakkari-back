"""Local protocol behavior; no DB, model, native host sampling or Seatbelt launch."""
import tempfile
import unittest
from pathlib import Path

from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.qwen_local import QwenLocalAdapter


class QwenWorkerBudgetTests(unittest.TestCase):
    def test_default_budget_preserves_the_standalone_twenty_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = QwenLocalAdapter(worker_path=root / 'bin/qwen-worker', enabled=True)
            spec = LaunchSpec(cwd=root, prompt='fixture', engine_session_id=None, model=None,
                              effort='medium', outbox=root / 'outbox', write_roots=[root], system_note='')
            try:
                argv = adapter.build_command(spec, {})
                self.assertEqual(argv[argv.index('--turns') + 1], '20')
            finally:
                adapter.cleanup(spec)


if __name__ == '__main__':
    unittest.main()
