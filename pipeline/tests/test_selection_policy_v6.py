"""Reject unknown-containing nucleic sequences only for newly opted-in v6 intake."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import unittest

from foldarium_pipeline.intake import IntakeError, WeeklyPolicy, target_from_wwpdb, build_weekly_plan, parse_wwpdb_snapshot
from foldarium_pipeline.selection import ligand_heavy_atoms, ligand_rejection_reason
from foldarium_pipeline.sizing import count_tokens
from foldarium_pipeline.weekly_quiz import ligand_eligibility_from_target
from test_intake import SEQUENCE_TSV, NONPOLYMER_TSV

V5, V6 = 'cameo-drug-like/v5', 'cameo-drug-like/v6'
ENTRY = {'sequences': ['CGAAUXCG'], 'ligands': [{'component_id': 'C', 'smiles': 'C1=CN(C(=O)N=C1N)[C@H]2[C@@H]([C@@H]([C@H](O2)COP(=O)(O)O)O)O'}]}


class SelectionV6Tests(unittest.TestCase):
    def target(self, sequence, version):
        entry = deepcopy(ENTRY); entry['sequences'] = [sequence]
        return target_from_wwpdb('9YRD', entry, date(2026, 10, 17), WeeklyPolicy(selection_policy_version=version))

    def test_real_9yrd_canonical_sequence_does_not_masquerade_as_protein_in_v6(self):
        # RCSB polymer entity9YRD/1 is RNA; X denotes the modified T4X residue.
        old = self.target('CGAAUXCG', V5)
        self.assertEqual(old['entities'][0]['type'], 'protein')
        self.assertEqual(old['metadata']['polymer_type_policy'], 'reject-nucleic-alphabet-otherwise-protein/v1')
        before = deepcopy(ENTRY)
        with self.assertRaisesRegex(IntakeError, 'ambiguous-or-nucleic-acid-polymer'):
            self.target('CGAAUXCG', V6)
        self.assertEqual(ENTRY, before)

    def test_conservative_unknown_rejection_and_real_protein_support(self):
        for sequence in ('CGAAUXCG', 'CGCGAAXUCGCG', 'XX', 'AXCGNTUI', 'ACGTUIN'):
            with self.subTest(sequence=sequence), self.assertRaisesRegex(IntakeError, 'ambiguous-or-nucleic'):
                self.target(sequence, V6)
        for sequence in ('MELKXAAA', 'MFVWL', 'PEPTIDEX'):
            new = self.target(sequence, V6)
            self.assertEqual(new['entities'][0]['sequence'], sequence)
            self.assertEqual(new['metadata']['polymer_type_policy'], 'reject-nucleic-and-unknown-alphabet-otherwise-protein/v2')
        # X-free nucleic sequences are still rejected by the historical policy.
        with self.assertRaisesRegex(IntakeError, 'ambiguous-or-nucleic'):
            self.target('ACGUIN', V5)

    def test_v6_retains_v5_hydrogen_chemistry_sizing_and_eligibility(self):
        for smiles in ('[2H]' + 'C' * 15, '[13CH3]' + 'C' * 14):
            self.assertEqual(ligand_heavy_atoms({'smiles': smiles}, policy_version=V5),
                             ligand_heavy_atoms({'smiles': smiles}, policy_version=V6))
        for smiles in ('[H]' + 'C' * 14, '[Hg]' + 'C' * 20, 'C' * 20 + '.C'):
            ligand = {'component_id': 'DRG', 'smiles': smiles}
            self.assertEqual(ligand_rejection_reason(ligand, policy_version=V5), ligand_rejection_reason(ligand, policy_version=V6))
        old = self.target('MELKXAAA', V5); new = self.target('MELKXAAA', V6)
        self.assertEqual(count_tokens(old), count_tokens(new))
        self.assertEqual(ligand_eligibility_from_target(new)['policy'], V6)
        self.assertEqual(ligand_eligibility_from_target(old)['policy'], V5)

    def test_new_plan_records_rejection_without_rewriting_v5_plan(self):
        snapshot = parse_wwpdb_snapshot(SEQUENCE_TSV, NONPOLYMER_TSV)
        snapshot['entries']['9YRD'] = deepcopy(ENTRY)
        args = dict(release_date=date(2026, 10, 17), ww_pdb_snapshot=snapshot,
                    output_prefix='supabase://private/runs', generated_at=datetime(2026, 10, 17, tzinfo=timezone.utc))
        old = build_weekly_plan(**args, policy=WeeklyPolicy(selection_policy_version=V5))
        frozen = json.dumps(old, sort_keys=True)
        new = build_weekly_plan(**args, policy=WeeklyPolicy(selection_policy_version=V6))
        self.assertIn('9YRD', [target['target_id'] for target in old['targets']])
        self.assertNotIn('9YRD', [target['target_id'] for target in new['targets']])
        self.assertEqual(new['campaign']['selection_policy_version'], V6)
        self.assertTrue(any(row['target_id'] == '9YRD' and 'ambiguous-or-nucleic-acid-polymer' in row['reason'] for row in new['skipped']))
        self.assertEqual(json.dumps(old, sort_keys=True), frozen)
        self.assertEqual(build_weekly_plan(**args, policy=WeeklyPolicy(selection_policy_version=V5)), old)

    def test_v6_is_explicit_and_preserves_the_default_policy(self):
        self.assertEqual(WeeklyPolicy().selection_policy_version, 'cameo-drug-like/v4')
        self.assertEqual(WeeklyPolicy(selection_policy_version=V6).selection_policy_version, V6)


if __name__ == '__main__':
    unittest.main()
