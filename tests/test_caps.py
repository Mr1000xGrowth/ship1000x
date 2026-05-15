"""Tests for hardcoded caps : verify they don't silently truncate real data.

Two caps to monitor :
1. MAX_ACTIVE_SEC_PER_SESSION (per-event cap, default 16h)
2. wall_brut * 5 cap (per-source cap, in highlights only)

These tests do NOT check production data (which would require a populated
DB). Instead they verify :
- The constants are defined and reachable
- The default value is sane (>= 12h, <= 24h)
- The env var override mechanism works
"""

import os
import unittest


class TestMaxActiveSecPerSessionConstant(unittest.TestCase):
    """Verify MAX_ACTIVE_SEC_PER_SESSION is defined consistently across collectors."""

    def test_claude_code_constant(self):
        from ship1000x.collectors.claude_code import MAX_ACTIVE_SEC_PER_SESSION
        # Default = 16h = 57600 sec, sanity range
        self.assertGreaterEqual(MAX_ACTIVE_SEC_PER_SESSION, 12 * 3600)
        self.assertLessEqual(MAX_ACTIVE_SEC_PER_SESSION, 24 * 3600)

    def test_codex_constant(self):
        from ship1000x.collectors.codex import MAX_ACTIVE_SEC_PER_SESSION
        self.assertGreaterEqual(MAX_ACTIVE_SEC_PER_SESSION, 12 * 3600)
        self.assertLessEqual(MAX_ACTIVE_SEC_PER_SESSION, 24 * 3600)

    def test_codex_macapp_constant(self):
        from ship1000x.collectors.codex_macapp import MAX_ACTIVE_SEC_PER_SESSION
        self.assertGreaterEqual(MAX_ACTIVE_SEC_PER_SESSION, 12 * 3600)
        self.assertLessEqual(MAX_ACTIVE_SEC_PER_SESSION, 24 * 3600)

    def test_codex_desktop_constant(self):
        from ship1000x.collectors.codex_desktop import MAX_ACTIVE_SEC_PER_SESSION
        self.assertGreaterEqual(MAX_ACTIVE_SEC_PER_SESSION, 12 * 3600)
        self.assertLessEqual(MAX_ACTIVE_SEC_PER_SESSION, 24 * 3600)

    def test_default_is_16h(self):
        """Default MUST be 16h (chosen empirically to support intensive devs)."""
        # Reset env var if set, then re-import to get fresh value
        os.environ.pop("SHIP1000X_MAX_SESSION_HOURS", None)
        # Force reimport
        import importlib

        import ship1000x.collectors.claude_code as cc
        importlib.reload(cc)
        self.assertEqual(cc.MAX_ACTIVE_SEC_PER_SESSION, 16 * 3600)

    def test_env_var_override_works(self):
        """SHIP1000X_MAX_SESSION_HOURS env var must override the default."""
        os.environ["SHIP1000X_MAX_SESSION_HOURS"] = "20"
        try:
            import importlib

            import ship1000x.collectors.claude_code as cc
            importlib.reload(cc)
            self.assertEqual(cc.MAX_ACTIVE_SEC_PER_SESSION, 20 * 3600)
        finally:
            os.environ.pop("SHIP1000X_MAX_SESSION_HOURS", None)
            import importlib

            import ship1000x.collectors.claude_code as cc
            importlib.reload(cc)


class TestWallBrutCapConstant(unittest.TestCase):
    """The 5× wall_brut cap is documented in METHODOLOGY.md.

    It is currently inlined in cli.py highlights() function. This test
    verifies that the docstring of METHODOLOGY mentions it (regression
    guard against silent removal).
    """

    def test_methodology_documents_wall_brut_cap(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        meth = (repo_root / "docs" / "METHODOLOGY.md").read_text()
        self.assertIn("wall_brut", meth.lower())
        self.assertIn("5×", meth.replace("5x", "5×").replace("5 x", "5×"))
        self.assertIn("anti-inflation", meth.lower())


if __name__ == "__main__":
    unittest.main()
