import unittest

from comms import imsg_watcher


class ParseCommandTest(unittest.TestCase):
    def _watcher(self):
        watcher = imsg_watcher.CommandWatcher.__new__(imsg_watcher.CommandWatcher)
        watcher.config = {
            "commands": {
                "help": {},
                "memory:view": {},
            }
        }
        return watcher

    def test_parses_bare_command(self):
        parsed = self._watcher().parse_command("help")
        self.assertEqual(("help", ""), parsed)

    def test_parses_canonical_colon_command(self):

    def test_parses_swapped_colon_order(self):

    def test_parses_bare_prefix_with_extra_text(self):
        parsed = self._watcher().parse_command("help: show commands")
        self.assertEqual(("help", "show commands"), parsed)

    def test_returns_none_for_unknown_command(self):
        parsed = self._watcher().parse_command("unknown: thing")
        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
