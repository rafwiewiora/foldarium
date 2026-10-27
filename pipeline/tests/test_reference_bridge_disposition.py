"""Missing deposited bridge atoms are unavailable, never incorrect predictions."""
import json
from pathlib import Path
import tempfile
import unittest

from foldarium_pipeline.evaluation import (
    _proven_unscorable_reference, evaluate_ligand_pose, UnscorableReferenceError,
)
from foldarium_pipeline.reference_disposition import (
    REFERENCE_BRIDGE_DISPOSITION_POLICY, validate_reference_disposition,
)
from test_reference_disposition import materialized


def bridge_disposition():
    return dict(policy=REFERENCE_BRIDGE_DISPOSITION_POLICY, code='missing_reference_bridge',
                component_id='DRG', expected_heavy_atoms=17, observed_heavy_atoms=16,
                explicitly_unobserved_heavy_atoms=1, reference_coverage=16/17,
                minimum_reference_coverage=.8, missing_heavy_atom_ids=['C9'],
                observed_component_count=2, missing_component_boundary_counts=[2],
                ccd_heavy_atom_graph_sha256='a'*64)


class BridgeContractTests(unittest.TestCase):
    def item(self):
        d={**bridge_disposition(), 'reference_sha256':'b'*64}
        return dict(evaluation_status='unscorable', reference_disposition=d,
                    choices=[dict(reference_sha256='b'*64, rmsd=None, correct=None,
                                  accepted_correct=None) for _ in range(10)])

    def test_bridge_retains_all_original_choices_as_explicit_nulls(self):
        item=self.item()
        self.assertTrue(validate_reference_disposition(item))
        self.assertEqual(len(item['choices']),10)
        def evaluate(*args, **kwargs):
            raise UnscorableReferenceError(bridge_disposition())
        content,_=materialized(evaluate)
        artifact=json.loads(content)
        self.assertEqual(artifact['format_version'],'foldarium.weekly-private-evaluation/v6')
        self.assertEqual(artifact['counts']['item_count'],1)
        self.assertEqual(artifact['counts']['scorable_item_count'],0)
        self.assertEqual(artifact['counts']['excluded_item_count'],1)
        self.assertEqual(len(artifact['reveal_manifest']['items'][0]['choices']),2)
        self.assertTrue(validate_reference_disposition(artifact['reveal_manifest']['items'][0]))
        self.assertEqual(artifact['answer_overlays'],[])

    def test_bridge_public_projection_strips_both_private_digests(self):
        from test_retrospective_archive import UnscorableRetrospectiveTests
        from foldarium_pipeline.quiz import manifest_sha256
        helper=UnscorableRetrospectiveTests()
        fixture=helper.fixture()
        row,_evaluation,artifact,_snapshot=fixture
        artifact['reveal_manifest']['items'][1]['reference_disposition']={
            **bridge_disposition(), 'reference_sha256':'d'*64}
        row['reveal_manifest_sha256']=manifest_sha256(artifact['reveal_manifest'])
        public,_admin=helper.build(fixture)
        question=next(q for q in public['questions'] if q['item_id']=='item-2')
        d=question['reference_disposition']
        self.assertEqual(d['policy'],REFERENCE_BRIDGE_DISPOSITION_POLICY)
        self.assertNotIn('reference_sha256',d)
        self.assertNotIn('ccd_heavy_atom_graph_sha256',d)
        self.assertEqual(d['missing_heavy_atom_ids'],['C9'])
        self.assertEqual(public['round']['scorable_item_count'],1)
        self.assertEqual(public['round']['item_count'],2)
        golden=Path(__file__).parents[2]/'tests/fixtures/retrospective-v2-missing-bridge.golden.json'
        self.assertEqual(public,json.loads(golden.read_text()))

    def test_v2_proof_is_strict_and_cannot_impersonate_old_coverage_failure(self):
        mutations=[dict(policy='foldarium.released-reference-disposition/v1'),
                   dict(code='insufficient_reference_coverage'),
                   dict(reference_coverage=.7), dict(missing_heavy_atom_ids=[]),
                   dict(missing_heavy_atom_ids=[' C9']), dict(observed_component_count=True),
                   dict(observed_component_count=1), dict(observed_component_count=17),
                   dict(missing_component_boundary_counts=[1]),
                   dict(missing_component_boundary_counts=[3]),
                   dict(missing_component_boundary_counts=[2,2]),
                   dict(ccd_heavy_atom_graph_sha256='unverified'),dict(extra='unverified')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                item=self.item();item['reference_disposition'].update(mutation)
                with self.assertRaises(ValueError):validate_reference_disposition(item)
        for key in ('rmsd','correct','accepted_correct'):
            item=self.item();item['choices'][0][key]=False
            with self.assertRaises(ValueError):validate_reference_disposition(item)


class BridgeDepositedProofTests(unittest.TestCase):
    def setUp(self):
        try:
            import gemmi
            from rdkit import Chem
        except ImportError as exc:
            self.skipTest(f'optional scientific dependency: {exc.name}')
        self.gemmi=gemmi

    def reference(self,path,missing=('C5',), second_complete=False):
        from test_evaluation import _add_protein_chain
        g=self.gemmi
        structure=g.Structure();model=g.Model('1');_add_protein_chain(model)
        for index,residue in enumerate(model[0]):
            residue[0].pos.y=(index % 3)*.5
            residue[0].pos.z=(index % 2)*.4
        for index,omitted in enumerate([missing]+([()] if second_complete else [])):
            chain=g.Chain(chr(ord('B')+index));res=g.Residue();res.name='DRG';res.seqid=g.SeqId(4,' ')
            for n in range(1,11):
                if f'C{n}' in omitted:continue
                atom=g.Atom();atom.name=f'C{n}';atom.element=g.Element('C');atom.pos=g.Position(n*1.54,2,0);res.add_atom(atom)
            chain.add_residue(res);model.add_chain(chain)
        structure.add_model(model);block=structure.make_mmcif_document().sole_block()
        atoms=block.init_loop('_chem_comp_atom.',['comp_id','atom_id','type_symbol'])
        for n in range(1,11):atoms.add_row(['DRG',f'C{n}','C'])
        bonds=block.init_loop('_chem_comp_bond.',['comp_id','atom_id_1','atom_id_2'])
        for n in range(1,10):bonds.add_row(['DRG',f'C{n}',f'C{n+1}'])
        unobs=block.init_loop('_pdbx_unobs_or_zero_occ_atoms.',['PDB_model_num','auth_asym_id','auth_comp_id','auth_seq_id','auth_atom_id','label_alt_id','PDB_ins_code'])
        for name in missing:unobs.add_row(['1','B','DRG','4',name,'?','?'])
        path.write_text(block.as_string());return structure

    def test_above_floor_internal_missing_atom_requires_exact_deposited_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reference.cif';s=self.reference(path)
            proof=_proven_unscorable_reference(path,s[0],'DRG',10,'CCCCCCCCCC')
            self.assertEqual(proof['policy'],REFERENCE_BRIDGE_DISPOSITION_POLICY)
            self.assertEqual(proof['code'],'missing_reference_bridge')
            self.assertEqual(proof['observed_heavy_atoms'],9)
            self.assertEqual(proof['missing_heavy_atom_ids'],['C5'])
            self.assertEqual(proof['observed_component_count'],2)
            self.assertEqual(proof['missing_component_boundary_counts'],[2])
            self.assertEqual(len(proof['ccd_heavy_atom_graph_sha256']),64)
            self.assertIsNone(_proven_unscorable_reference(path,s[0],'DRG',10,'CCC(C)CCCCCC'))
            path.write_text(path.read_text().replace('1 B DRG 4 C5 ?', '1 B DRG 4 C99 ?'))
            self.assertIsNone(_proven_unscorable_reference(path,s[0],'DRG',10,'CCCCCCCCCC'))

    def test_any_complete_deposited_copy_prevents_whole_item_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reference.cif';s=self.reference(path,second_complete=True)
            self.assertIsNone(_proven_unscorable_reference(path,s[0],'DRG',10,'CCCCCCCCCC'))

    def test_missing_terminal_atom_still_scores_with_existing_partial_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);reference=root/'reference.cif';prediction=root/'prediction.cif'
            s=self.reference(reference,missing=('C10',));self.reference(prediction,missing=())
            self.assertIsNone(_proven_unscorable_reference(reference,s[0],'DRG',10,'CCCCCCCCCC'))
            result=evaluate_ligand_pose(reference,prediction,component_id='DRG',heavy_atoms=10,ligand_smiles='CCCCCCCCCC')
            self.assertAlmostEqual(result['rmsd'],0.0)

    def test_internal_missing_bridge_raises_disposition_without_relaxing_rmsd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);reference=root/'reference.cif';prediction=root/'prediction.cif'
            self.reference(reference);self.reference(prediction,missing=())
            with self.assertRaises(UnscorableReferenceError) as raised:
                evaluate_ligand_pose(reference,prediction,component_id='DRG',heavy_atoms=10,ligand_smiles='CCCCCCCCCC')
            self.assertEqual(raised.exception.disposition['code'],'missing_reference_bridge')

if __name__=='__main__':unittest.main()
