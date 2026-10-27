"""Chemistry-only presentation preference, including counterexamples to QED alone."""
from copy import deepcopy
import hashlib
import importlib.util
import unittest
from unittest.mock import patch

from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.quiz import build_blind_manifest, manifest_sha256
from foldarium_pipeline.druglike_selection import (
    assess_druglike_candidates, DRUGLIKE_PREFERENCE_POLICY, DRUGLIKE_RDKIT_VERSION,
    DruglikePreferenceError,
)
from foldarium_pipeline.weekly_question_selection import select_weekly_questions

MEDCHEM = 'Cc1ccc2c(c1)c(c(cn2)C#N)N3CCC(CC3)C(=O)N4CCC(CC4)C'
PFAS = 'O=C(O)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F'
TRP = 'N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O'
ADENOSINE = 'c1nc(c2c(n1)n(cn2)[C@H]3[C@@H]([C@@H]([C@H](O3)CO)O)O)N'
ANTAGONIST = 'c1cc(oc1)c2nc3nc(nc(n3n2)N)NCCc4ccc(cc4)O'
LIPID = 'CCCCCCCCCCCCCC(=O)OC[C@H](COP(=O)(O)O)OC(=O)CCCCCNc1ccc(c2c1non2)[N+](=O)[O-]'


def population(chemistry):
    # Real publication builder supplies the binding/choice-ID contract.
    from rdkit import Chem
    rows=[]
    for index,(component,smiles) in enumerate(chemistry):
        heavy_atoms=Chem.MolFromSmiles(smiles).GetNumHeavyAtoms()
        rows.append({'id':f'item-{index:02d}', 'ligand':{'component_id':component,'heavy_atoms':heavy_atoms},
            'ligand_eligibility': {'policy':'cameo-drug-like/v5','passed':True,
                'component_id':component, 'heavy_atoms':heavy_atoms, 'smiles':smiles,
                'smiles_sha256':hashlib.sha256(smiles.encode()).hexdigest()},
            'choices':[{'run_id':f'run-{index}-{method}','sample_id':'0','method':method,
                'pose_uri':f'supabase://public/{index}/{method}'} for method in ('boltz2','openfold3')]})
    return build_blind_manifest('weekly-chemistry-fixture',rows)


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'optional RDKit dependency')
class DruglikePreferenceTests(unittest.TestCase):
    def test_high_qed_pfas_and_endogenous_amino_acid_do_not_displace_medchem(self):
        blind,private=population([('DRG',MEDCHEM),('PFAS',PFAS),('TRP',TRP),('ADN',ADENOSINE),('LIP',LIPID)])
        original=deepcopy((blind,private))
        audit=assess_druglike_candidates(blind,private)
        self.assertEqual(audit['eligible_item_ids'],['item-00'])
        rows={r['component_id']:r for r in audit['descriptor_rows']}
        self.assertGreater(rows['PFAS']['descriptors']['qed'],.8)
        self.assertIn('no-ring-scaffold',rows['PFAS']['reasons'])
        self.assertGreater(rows['TRP']['descriptors']['qed'],.5)
        self.assertIn('known-metabolite-or-artifact-component',rows['TRP']['reasons'])
        self.assertIn('known-metabolite-or-artifact-component',rows['ADN']['reasons'])
        self.assertTrue(rows['LIP']['descriptors']['long_acyclic_aliphatic_chain'])
        self.assertEqual((blind,private),original)
        self.assertEqual(audit['rdkit_version'],DRUGLIKE_RDKIT_VERSION)
        self.assertEqual(audit['policy'],DRUGLIKE_PREFERENCE_POLICY)

    def test_nitrogen_rich_drug_scaffold_is_not_generic_nucleobase_exclusion(self):
        blind,private=population([('ZMA',ANTAGONIST),('ADN',ADENOSINE)])
        audit=assess_druglike_candidates(blind,private)
        self.assertEqual(audit['eligible_item_ids'],['item-00'])
        self.assertTrue(audit['descriptor_rows'][0]['preferred'])

    def test_preferred_count_one_to_four_never_pads_with_unpreferred_ligands(self):
        for n in range(1,5):
            with self.subTest(count=n):
                blind,private=population([('DRG',MEDCHEM)]*n+[('PFAS',PFAS)]*3)
                audit=assess_druglike_candidates(blind,private)
                selection=select_weekly_questions(blind,private,seed='test',eligible_item_ids=audit['eligible_item_ids'])
                self.assertEqual(audit['status'],'preferred')
                self.assertIsNone(audit['fallback_reason'])
                self.assertEqual(selection['selected_question_count'],n)
                self.assertEqual(len(audit['eligible_item_ids']),n)
                self.assertEqual(selection['source_item_count'],n+3)

    def test_zero_preferred_is_explicit_legacy_fallback_not_a_druglike_claim(self):
        blind,private=population([('PFAS',PFAS),('TRP',TRP),('ADN',ADENOSINE)])
        audit=assess_druglike_candidates(blind,private)
        self.assertEqual(audit['status'],'legacy-fallback-no-preferred')
        self.assertEqual(audit['fallback_reason'],'no-preferred-ligands')
        self.assertEqual(audit['preferred_count'],0)
        self.assertEqual(audit['eligible_item_ids'],[r['id'] for r in blind['items']])
        self.assertTrue(all(not r['preferred'] for r in audit['descriptor_rows']))

    def test_legacy_missing_or_invalid_chemistry_is_visible_and_cannot_fake_preference(self):
        blind,private=population([('DRG',MEDCHEM),('OTHER',MEDCHEM)])
        private['items'][0].pop('ligand_eligibility')
        private['items'][1]['ligand_eligibility']['smiles']='not SMILES'
        private['items'][1]['ligand_eligibility'].pop('smiles_sha256')
        audit=assess_druglike_candidates(blind,private)
        self.assertEqual(audit['status'],'legacy-fallback-no-preferred')
        self.assertEqual([r['status'] for r in audit['descriptor_rows']],['unavailable','unavailable'])
        self.assertIn('invalid-source-smiles',audit['descriptor_rows'][1]['reasons'])

    def test_changed_smiles_digest_or_component_identity_fails_closed(self):
        blind,private=population([('DRG',MEDCHEM)])
        private['items'][0]['ligand_eligibility']['smiles_sha256']='0'*64
        with self.assertRaisesRegex(DruglikePreferenceError,'SMILES digest differs'):
            assess_druglike_candidates(blind,private)
        private['items'][0]['ligand_eligibility'].pop('smiles_sha256')
        private['items'][0]['ligand_eligibility']['component_id']='OTHER'
        with self.assertRaisesRegex(DruglikePreferenceError,'component binding differs'):
            assess_druglike_candidates(blind,private)

    def test_preference_uses_no_pose_predictions_votes_or_experimental_answers(self):
        blind,private=population([('DRG',MEDCHEM),('PFAS',PFAS)])
        first=assess_druglike_candidates(blind,private)
        for item in blind['items']:
            for choice in item['choices']:
                choice['confidence']={'metric':'ligand_plddt','value':1,'scale_min':0,'scale_max':100,'aggregation':'mean'}
        for item in private['items']:
            item['oracle_correct']=True
            item['human_votes']=[{'correct':True}]
            for choice in item['choices']:
                choice['rmsd']=0;choice['correct']=True
        private['blind_manifest_sha256']=manifest_sha256(blind)
        second=assess_druglike_candidates(blind,private)
        self.assertEqual(first['descriptor_rows'],second['descriptor_rows'])
        self.assertEqual(first['eligible_item_ids'],second['eligible_item_ids'])
        self.assertNotEqual(first['source_private_index_sha256'],second['source_private_index_sha256'])

    def test_audit_is_deterministic_bound_to_source_and_independent_of_input_order(self):
        blind,private=population([('DRG',MEDCHEM),('PFAS',PFAS),('ZMA',ANTAGONIST)])
        a=assess_druglike_candidates(blind,private)
        self.assertEqual(a,assess_druglike_candidates(blind,private))
        unsigned={k:v for k,v in a.items() if k!='evidence_sha256'}
        self.assertEqual(a['evidence_sha256'],hashlib.sha256(canonical_json(unsigned).encode()).hexdigest())
        self.assertEqual(a['source_private_index_sha256'],hashlib.sha256(canonical_json(private).encode()).hexdigest())
        blind['items'].reverse();private['items'].reverse();private['blind_manifest_sha256']=manifest_sha256(blind)
        b=assess_druglike_candidates(blind,private)
        self.assertEqual(a['descriptor_rows'],b['descriptor_rows'])
        self.assertEqual(a['eligible_item_ids'],b['eligible_item_ids'])
        self.assertNotEqual(a['source_blind_manifest_sha256'],b['source_blind_manifest_sha256'])

    def test_wrong_rdkit_version_cannot_silently_change_preference(self):
        from rdkit import rdBase
        blind,private=population([('DRG',MEDCHEM)])
        with patch.object(rdBase,'rdkitVersion','other-version'),self.assertRaisesRegex(DruglikePreferenceError,'pinned RDKit'):
            assess_druglike_candidates(blind,private)


if __name__=='__main__':unittest.main()
