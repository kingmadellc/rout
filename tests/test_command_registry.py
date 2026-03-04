import unittest
from pathlib import Path

import yaml


class CommandRegistryTest(unittest.TestCase):
    def test_handlers_exist_for_registry(self):
        root = Path(__file__).resolve().parent.parent
        registry = yaml.safe_load((root / "imsg_commands.yaml").read_text()) or {}
        commands = registry.get("commands", {})

        self.assertIn("doctor", commands)
        self.assertIn("memory:view", commands)

        for name, cfg in commands.items():
            handler = cfg.get("handler", "")
            module_name = handler.split(".", 1)[0]
            module_path = root / "handlers" / f"{module_name}.py"
            self.assertTrue(module_path.exists(), f"Missing module for {name}: {module_path}")


if __name__ == "__main__":
    unittest.main()
