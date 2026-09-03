from __future__ import annotations

import unittest

from foldarium_pipeline.weekly_lifecycle import (
    WeeklyLifecycleError,
    delayed_retrospective_release,
)


class WeeklyLifecycleTests(unittest.TestCase):
    def test_unconfigured_round_keeps_existing_release_behavior(self) -> None:
        self.assertIsNone(delayed_retrospective_release({"metadata": {}}))
        self.assertIsNone(delayed_retrospective_release({}))

    def test_validates_opt_in_next_weekly_policy(self) -> None:
        release = {
            "policy": "next-weekly-activation",
            "original_closes_at": "2026-09-02T00:00:00+00:00",
            "safety_closes_at": "2026-09-09T00:00:00+00:00",
            "configured_at": "2026-09-01T19:00:00+00:00",
        }
        self.assertEqual(
            delayed_retrospective_release(
                {"metadata": {"retrospective_release": release}}
            ),
            release,
        )

    def test_rejects_unknown_or_incomplete_policy(self) -> None:
        for release in (
            {"policy": "wednesday"},
            {"policy": "next-weekly-activation"},
            "next-weekly-activation",
        ):
            with self.subTest(release=release), self.assertRaises(
                WeeklyLifecycleError
            ):
                delayed_retrospective_release(
                    {"metadata": {"retrospective_release": release}}
                )


if __name__ == "__main__":
    unittest.main()
