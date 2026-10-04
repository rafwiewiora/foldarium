"""Audited v4 explicit-H metadata compatibility without mutating source records."""
import unittest
from copy import deepcopy

from foldarium_pipeline.ligand_normalization import LIGAND_SMILES_HEAVY_ATOM_POLICY
from foldarium_pipeline.wednesday_reveal import WednesdayRevealError, _validated_round
from test_wednesday_reveal import round_fixture, fixture_ligand_eligibility, legacy_clustering_for_item


def fixture(heavy_atoms=17):
    row, private, _ = round_fixture()
    private = deepcopy(private)
    item = private['items'][0]
    smiles = '[2H]' + 'C' * heavy_atoms
    item['ligand']['heavy_atoms'] = heavy_atoms
    item['ligand_eligibility'] = fixture_ligand_eligibility(smiles=smiles, heavy_atoms=heavy_atoms+1)
    item['clustering'] = legacy_clustering_for_item(item, identity_round_id=row['round_id'], smiles=smiles)
    item['clustering']['ligand_atom_mapping'].update(
        heavy_atom_normalization_policy=LIGAND_SMILES_HEAVY_ATOM_POLICY,
        selected_ligand_metadata_heavy_atom_count=heavy_atoms+1,
        metadata_heavy_atom_count_matches_normalized=False,
        removed_explicit_hydrogen_count=1,
    )
    return row, private


class AuditedHydrogenEligibilityTests(unittest.TestCase):
    def test_exact_explicit_isotope_h_audit_preserves_both_source_counts_and_choices(self):
        row, private = fixture()
        original = deepcopy(private)
        _, _, items = _validated_round(row, private)
        self.assertEqual(items[0]['ligand']['heavy_atoms'], 17)
        self.assertEqual(items[0]['ligand_eligibility']['heavy_atoms'], 18)
        self.assertEqual(items[0]['choices'], original['items'][0]['choices'])
        self.assertEqual(private, original)

    def test_matching_recovered_source_eligibility_remains_compatible(self):
        row, private = fixture()
        item = private['items'][0]
        recovered = {item['target_id'].upper(): deepcopy(item['ligand_eligibility'])}
        self.assertEqual(len(_validated_round(row, private, recovered_ligand_eligibility=recovered)[2]), 1)
        item.pop('ligand_eligibility')
        self.assertEqual(len(_validated_round(row, private, recovered_ligand_eligibility=recovered)[2]), 1)

    def test_missing_or_tampered_normalization_provenance_is_rejected(self):
        mutations = [
            lambda item: item['clustering']['ligand_atom_mapping'].pop('heavy_atom_normalization_policy'),
            lambda item: item['clustering']['ligand_atom_mapping'].update(removed_explicit_hydrogen_count=2),
            lambda item: item['clustering']['ligand_atom_mapping'].update(selected_ligand_metadata_heavy_atom_count=19),
            lambda item: item['clustering']['ligand_atom_mapping'].update(metadata_heavy_atom_count_matches_normalized=True),
            lambda item: item['clustering']['ligand_atom_mapping'].update(source_topology_sha256='0'*64),
            lambda item: item['clustering']['ligand_atom_mapping'].update(source_smiles_sha256='0'*64),
            lambda item: item['clustering']['ligand_atom_mapping']['choices'][0].update(choice_digest='0'*64),
            lambda item: item['ligand_eligibility'].update(passed=False),
            lambda item: item['ligand_eligibility'].update(component_id='OTHER'),
            lambda item: item['ligand_eligibility'].update(smiles_sha256='0'*64),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                row, private = fixture()
                mutation(private['items'][0])
                with self.assertRaises(WednesdayRevealError):
                    _validated_round(row, private)

    def test_one_atom_disagreement_without_hydrogen_explanation_is_rejected(self):
        row, private = fixture()
        item = private['items'][0]
        item['ligand_eligibility'] = fixture_ligand_eligibility(smiles='C'*18, heavy_atoms=18)
        with self.assertRaisesRegex(WednesdayRevealError, 'normalization does not explain'):
            _validated_round(row, private)

    def test_hydrogen_cannot_rescue_ligand_below_scientific_minimum(self):
        row, private = fixture(heavy_atoms=14)
        with self.assertRaisesRegex(WednesdayRevealError, 'below the heavy-atom eligibility minimum'):
            _validated_round(row, private)

    def test_changed_count_cannot_be_hidden_behind_valid_hydrogen_audit(self):
        row, private = fixture()
        private['items'][0]['ligand']['heavy_atoms'] = 16
        with self.assertRaises(WednesdayRevealError):
            _validated_round(row, private)


if __name__ == '__main__':
    unittest.main()
