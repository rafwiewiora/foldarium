from __future__ import annotations

import unittest
from pathlib import Path


class MigrationMirrorTests(unittest.TestCase):
    def test_supabase_cli_migrations_match_pipeline_sources(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        pairs = {
            "001_control_plane.sql": "20260808010000_create_prediction_control_plane.sql",
            "002_weekly_intake.sql": "20260808010100_add_weekly_intake.sql",
            "003_weekly_quiz.sql": "20260808010200_add_weekly_quiz.sql",
            "004_external_predictions.sql": "20260808010300_add_external_predictions.sql",
            "005_curation_decisions.sql": "20260808010400_add_curation_decisions.sql",
        }
        for source_name, deployed_name in pairs.items():
            with self.subTest(source=source_name):
                source = repository / "pipeline" / "migrations" / source_name
                deployed = repository / "supabase" / "migrations" / deployed_name
                self.assertEqual(source.read_bytes(), deployed.read_bytes())


if __name__ == "__main__":
    unittest.main()
