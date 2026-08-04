from __future__ import annotations

import unittest

from zdecision.agent.browser_launcher import SystemDefaultBrowserLauncher


class SystemDefaultBrowserLauncherTests(unittest.TestCase):
    def test_open_delegates_the_exact_url_to_the_default_browser(self) -> None:
        calls: list[str] = []

        def opener(url: str) -> bool:
            calls.append(url)
            return True

        launcher = SystemDefaultBrowserLauncher(opener=opener)

        self.assertTrue(
            launcher.open(
                "http://127.0.0.1:8765/?repository_id=repo_2"
            )
        )
        self.assertEqual(
            ["http://127.0.0.1:8765/?repository_id=repo_2"],
            calls,
        )

    def test_open_returns_false_when_platform_rejects_request(self) -> None:
        launcher = SystemDefaultBrowserLauncher(opener=lambda _url: False)

        self.assertFalse(launcher.open("https://decisions.example.test/"))

    def test_open_contains_platform_exceptions(self) -> None:
        def failing_opener(_url: str) -> bool:
            raise OSError("browser unavailable")

        launcher = SystemDefaultBrowserLauncher(opener=failing_opener)

        self.assertFalse(launcher.open("https://decisions.example.test/"))


if __name__ == "__main__":
    unittest.main()
