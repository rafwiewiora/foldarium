from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from foldarium_pipeline.evaluation import _proven_unscorable_reference, UnscorableReferenceError, EvaluationError
from foldarium_pipeline.reference_disposition import validate_reference_disposition, REFERENCE_DISPOSITION_POLICY
from foldarium_pipeline.private_evaluation import materialize_private_preclose_evaluation, describe_private_evaluation_artifact, PrivateEvaluationError
from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.wednesday_reveal import WednesdayRevealError, rcsb_reference_url
from test_private_evaluation import round_fixture, FakeCoordinator, minimal_reference_mmcif_gz, coordinate, PRODUCTION_BETA_CATCHUP_ROUND_ID


def disposition(expected=17, observed=12):
    return dict(policy=REFERENCE_DISPOSITION_POLICY, code='insufficient_reference_coverage', component_id='DRG',
                expected_heavy_atoms=expected, observed_heavy_atoms=observed,
                explicitly_unobserved_heavy_atoms=expected-observed,
                reference_coverage=observed/expected, minimum_reference_coverage=.8)


def materialized(evaluator=None):
    # Resolve optional scientific fixture dependencies outside the production
    # resolver wrapper, which correctly translates resolver errors to NotReady.
    reference_bytes = minimal_reference_mmcif_gz()
    row, _, private = round_fixture(); coordinator=FakeCoordinator(row,private)
    def evaluate(*args, **kwargs):
        raise UnscorableReferenceError(disposition())
    with tempfile.TemporaryDirectory() as tmp:
        result=materialize_private_preclose_evaluation(PRODUCTION_BETA_CATCHUP_ROUND_ID,tmp,
            coordinator=coordinator, reference_resolver=lambda item: coordinate(reference_bytes,rcsb_reference_url(item['target_id'])),
            evaluator=evaluator or evaluate,now=datetime(2026,8,15,2,tzinfo=timezone.utc))
    return coordinator.stored_contents[0], result


class ReferenceDispositionTests(unittest.TestCase):
    def test_full_population_preserved_without_scores_or_overlay(self):
        content,result=materialized(); artifact=json.loads(content)
        self.assertEqual(artifact['format_version'],'foldarium.weekly-private-evaluation/v6')
        self.assertEqual(artifact['counts'],dict(item_count=1,choice_count=2,scorable_item_count=0,excluded_item_count=1))
        item=artifact['reveal_manifest']['items'][0]
        self.assertTrue(validate_reference_disposition(item))
        self.assertEqual(len(item['choices']),2)
        self.assertEqual(artifact['answer_overlays'],[])
        for choice in item['choices']:
            self.assertTrue(choice['prediction_sha256'])
            self.assertTrue(choice['sample_id'])
        self.assertEqual(describe_private_evaluation_artifact(content)['format_version'],artifact['format_version'])

    def test_scientific_errors_still_fail_not_dispositioned(self):
        def bad(*args,**kwargs): raise EvaluationError('ambiguous graph')
        with self.assertRaises(WednesdayRevealError): materialized(bad)

    def test_disposition_contract_rejects_fabrication(self):
        original = json.loads((Path(__file__).parents[2] / 'tests/fixtures/private-evaluation-v6-unscorable.golden.json').read_text())['reveal_manifest']['items'][0]
        mutations=[lambda x:x['reference_disposition'].update(expected_heavy_atoms=18),
                   lambda x:x['reference_disposition'].update(component_id='  '),
                   lambda x:x['reference_disposition'].update(expected_heavy_atoms=9007199254740992),
                   lambda x:x['reference_disposition'].update(extra='unverified'),
                   lambda x:x['reference_disposition'].update(reference_sha256='0'*64),
                   lambda x:x['reference_disposition'].update(reference_coverage=float('nan')),
                   lambda x:x['reference_disposition'].update(minimum_reference_coverage=.5),
                   lambda x:x['choices'][0].update(correct=False),
                   lambda x:x['choices'][0].update(rmsd=0),
                   lambda x:x.update(evaluation_status='invalid')]
        for mutate in mutations:
            item=deepcopy(original); mutate(item)
            with self.assertRaises(ValueError):validate_reference_disposition(item)

    def test_v5_cannot_smuggle_dispositions_or_misstate_scorable_counts(self):
        original = json.loads((Path(__file__).parents[2] / 'tests/fixtures/private-evaluation-v6-unscorable.golden.json').read_text())
        for field,value in [('format_version','foldarium.weekly-private-evaluation/v5'),('counts',dict(item_count=1,choice_count=2,scorable_item_count=1,excluded_item_count=0))]:
            artifact=deepcopy(original);artifact[field]=value
            with self.assertRaises(PrivateEvaluationError):describe_private_evaluation_artifact(canonical_json(artifact).encode())

    def test_actual_cif_proof_requires_exact_ccd_topology_and_missing_atom_complement(self):
        try:
            import gemmi
            import numpy
            from rdkit import Chem
        except ImportError as exc:
            self.skipTest(f'optional scientific evaluation dependency: {exc.name}')
        structure=gemmi.Structure();model=gemmi.Model('1');chain=gemmi.Chain('B');residue=gemmi.Residue();residue.name='DRG';residue.seqid=gemmi.SeqId(4,' ')
        for n in range(1,4):
            atom=gemmi.Atom();atom.name=f'C{n}';atom.element=gemmi.Element('C');atom.pos=gemmi.Position(n,0,0);residue.add_atom(atom)
        chain.add_residue(residue);model.add_chain(chain);structure.add_model(model)
        block=structure.make_mmcif_document().sole_block()
        atom_loop=block.init_loop('_chem_comp_atom.',['comp_id','atom_id','type_symbol'])
        for n in range(1,6):atom_loop.add_row(['DRG',f'C{n}','C'])
        bonds=block.init_loop('_chem_comp_bond.',['comp_id','atom_id_1','atom_id_2'])
        for n in range(1,5):bonds.add_row(['DRG',f'C{n}',f'C{n+1}'])
        missing=block.init_loop('_pdbx_unobs_or_zero_occ_atoms.',['PDB_model_num','auth_asym_id','auth_comp_id','auth_seq_id','auth_atom_id','label_alt_id','PDB_ins_code'])
        for n in (4,5):missing.add_row(['1','B','DRG','4',f'C{n}','?','?'])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reference.cif';path.write_text(block.as_string())
            good=_proven_unscorable_reference(path,structure[0],'DRG',5,'CCCCC')
            self.assertIsNotNone(good);self.assertEqual(good['observed_heavy_atoms'],3)
            self.assertIsNone(_proven_unscorable_reference(path,structure[0],'DRG',5,'CC(C)CC'))
            self.assertIsNone(_proven_unscorable_reference(path,structure[0],'DRG',6,'CCCCCC'))
            path.write_text(block.as_string().replace('1 B DRG 4 C5 ?', '1 B DRG 4 C99 ?'))
            self.assertIsNone(_proven_unscorable_reference(path,structure[0],'DRG',5,'CCCCC'))

if __name__=='__main__':unittest.main()
