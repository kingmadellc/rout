import tempfile
import unittest
from pathlib import Path

from handlers import memory_handlers


class MemoryHandlersTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name) / ".openclaw"
        memory_handlers.OPENCLAW_DIR = base
        memory_handlers.MEMORY_FILE = base / "MEMORY.md"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_add_and_view_memory(self):
        add_resp = memory_handlers.memory_add_command(args="Remember my dog's name is Pixel")
        self.assertIn("Added", add_resp)

        view_resp = memory_handlers.memory_view_command(args="Pixel")
        self.assertIn("Pixel", view_resp)

    def test_clear_requires_confirm(self):
        denied = memory_handlers.memory_clear_command(args="nope")
        self.assertIn("Refusing", denied)

        accepted = memory_handlers.memory_clear_command(args="CONFIRM")
        self.assertIn("cleared", accepted.lower())


if __name__ == "__main__":
    unittest.main()
