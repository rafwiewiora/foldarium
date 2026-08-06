import unittest

import gemmi
import numpy as np

import export_crystal_aligned as exporter


PDB_WITH_EQUIVALENT_LIGANDS = """\
ATOM      1 CA   ALA A   1       0.000   0.000   0.000  1.00  0.00           C
ATOM      2 CA   GLY A   2       1.000   0.000   0.000  1.00  0.00           C
ATOM      3 CA   SER A   3       2.000   0.000   0.000  1.00  0.00           C
ATOM      4 CA   LEU A   4       3.000   0.000   0.000  1.00  0.00           C
HETATM    5 C1   LIG L   1       0.000   2.000   0.000  1.00  0.00           C
HETATM    6 O1   LIG L   1       1.000   2.000   0.000  1.00  0.00           O
HETATM    7 C1   LIG M   1       0.000   4.000   0.000  1.00  0.00           C
HETATM    8 O1   LIG M   1       1.000   4.000   0.000  1.00  0.00           O
HETATM    9 MG   MG  I   1       2.000   5.000   0.000  1.00  0.00          MG
END
"""


class CrystalAlignedExportTests(unittest.TestCase):
    def setUp(self):
        self.structure = gemmi.read_pdb_string(PDB_WITH_EQUIVALENT_LIGANDS)
        self.structure.setup_entities()

    def test_kabsch_recovers_rigid_transform(self):
        predicted = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]]
        )
        expected_rotation = np.array(
            [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
        )
        expected_translation = np.array([4.0, -2.0, 7.0])
        crystal = exporter.transform_coordinates(
            predicted, expected_rotation, expected_translation
        )

        rotation, translation = exporter.kabsch(predicted, crystal)

        np.testing.assert_allclose(rotation, expected_rotation, atol=1e-10)
        np.testing.assert_allclose(translation, expected_translation, atol=1e-10)

    def test_predicted_ligand_finds_identical_nonprotein_copy(self):
        candidates = exporter.find_ligand_candidates(
            self.structure, "L", "af3", {"A"}
        )

        self.assertEqual(set(candidates), {"L", "M"})

    def test_crystal_ligand_finds_identical_nonprotein_copy(self):
        candidates = exporter.find_crystal_ligand_candidates(
            self.structure,
            "L",
            ["C", "O"],
            np.array([[0.0, 2.0, 0.0], [1.0, 2.0, 0.0]]),
        )

        self.assertEqual(set(candidates), {"L", "M"})
        self.assertNotIn("A", candidates)
        self.assertNotIn("I", candidates)

    def test_assignment_rmsd_is_invariant_to_same_element_order(self):
        rmsd = exporter.assignment_rmsd(
            ["C", "C", "O"],
            np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 0.0], [3.0, 0.0, 0.0]]),
            ["C", "O", "C"],
            np.array([[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
        )

        self.assertAlmostEqual(rmsd, 0.0)


if __name__ == "__main__":
    unittest.main()
