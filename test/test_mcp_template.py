"""MCP connector template ships mergeable definitions without any real secrets."""
import json
import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "mcp.example.jsonc"


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


class McpTemplate(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(strip_comments(TEMPLATE.read_text()))

    def test_three_connectors_present(self):
        for name in ("gmail", "gdrive", "github"):
            self.assertIn(name, self.config["mcp"])

    def test_each_server_declares_type_and_enabled(self):
        for name, server in self.config["mcp"].items():
            self.assertIn(server["type"], ("local", "remote"), name)
            self.assertIn("enabled", server, name)

    def test_no_live_credentials_in_the_template(self):
        raw = TEMPLATE.read_text()
        self.assertNotRegex(raw, r"gh[po]_[A-Za-z0-9]{20,}")
        self.assertNotRegex(raw, r"github_pat_[A-Za-z0-9_]{20,}")

    def test_github_uses_the_documented_env_variable(self):
        github = json.dumps(self.config["mcp"]["github"])
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", github)


if __name__ == "__main__":
    unittest.main()
