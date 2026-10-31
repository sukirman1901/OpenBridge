"""MCP connector template ships mergeable definitions without any real secrets."""
import json
import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "mcp.example.jsonc"


def strip_comments(text):
    return re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                  lambda match: match.group(1) or '', text, flags=re.S)


class McpTemplate(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(strip_comments(TEMPLATE.read_text()))

    def test_three_connectors_present(self):
        for name in ("gmail", "gdrive", "github"):
            self.assertIn(name, self.config["mcp"]["servers"])

    def test_each_server_declares_type_and_is_disabled_in_native_v2_shape(self):
        for name, server in self.config["mcp"]["servers"].items():
            self.assertIn(server["type"], ("local", "remote"), name)
            self.assertTrue(server['disabled'], name)
            self.assertNotIn('enabled', server)

    def test_no_live_credentials_in_the_template(self):
        raw = TEMPLATE.read_text()
        self.assertNotRegex(raw, r"gh[po]_[A-Za-z0-9]{20,}")
        self.assertNotRegex(raw, r"github_pat_[A-Za-z0-9_]{20,}")

    def test_github_uses_the_documented_env_variable(self):
        github = json.dumps(self.config["mcp"]["servers"]["github"])
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", github)


if __name__ == "__main__":
    unittest.main()
