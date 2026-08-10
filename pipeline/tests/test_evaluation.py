from __future__ import annotations

import unittest

from foldarium_pipeline.evaluation import _receptor_candidate_key


class ReceptorCandidatePolicyTests(unittest.TestCase):
    def test_blind_assembly_can_prefer_one_stable_chain_pair(self) -> None:
        chain_a = {
            "sequence_similarity": 1.0,
            "receptor_rmsd": 8.0,
            "reference_chain": "A",
            "predicted_chain": "A",
        }
        chain_c = {
            "sequence_similarity": 1.0,
            "receptor_rmsd": 2.0,
            "reference_chain": "C",
            "predicted_chain": "C",
        }

        self.assertLess(
            _receptor_candidate_key(chain_c, stable_chain_pair=False),
            _receptor_candidate_key(chain_a, stable_chain_pair=False),
        )
        self.assertLess(
            _receptor_candidate_key(chain_a, stable_chain_pair=True),
            _receptor_candidate_key(chain_c, stable_chain_pair=True),
        )


if __name__ == "__main__":
    unittest.main()
