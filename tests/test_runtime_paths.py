import unittest
from pathlib import Path

from comms import imsg_watcher


class RuntimePathTest(unittest.TestCase):
    def test_workspace_has_registry(self):
        self.assertTrue((imsg_watcher.WORKSPACE / "imsg_commands.yaml").exists())

    def test_default_workspace_is_repo_checkout(self):
        repo_root = Path(__file__).resolve().parent.parent
        self.assertTrue((repo_root / "imsg_commands.yaml").exists())


if __name__ == "__main__":
    unittest.main()
