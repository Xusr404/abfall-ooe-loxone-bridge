import unittest
from unittest.mock import patch

from tools.release_version import resolve


class ReleaseVersionTests(unittest.TestCase):
    def test_existing_stable_and_prerelease_tags(self):
        with patch("tools.release_version.subprocess.check_output", return_value="abc123\n") as git:
            for tag in ("v1.0.0", "v0.2.3-rc.1"):
                self.assertEqual(resolve(tag), "abc123")
                self.assertEqual(git.call_args.args[0][-1], f"refs/tags/{tag}^{{commit}}")

    def test_invalid_versions_never_reach_git(self):
        with patch("tools.release_version.subprocess.check_output") as git:
            for tag in ("main", "v01.0.0", "v1.0", "v1.0.0-01", "v1.0.0;echo bad", ""):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    resolve(tag)
            git.assert_not_called()
