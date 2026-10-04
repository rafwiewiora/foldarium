"""Versioned selection semantics and end-to-end frozen provenance boundaries."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from foldarium_pipeline.intake import WeeklyPolicy, build_weekly_plan, parse_wwpdb_snapshot, target_from_cameo
from foldarium_pipeline.private_evaluation import _legacy_recovered_ligand_eligibility, PrivateEvaluationError
from foldarium_pipeline.quiz import manifest_sha256
from foldarium_pipeline.selection import (
    LEGACY_SELECTION_POLICY_VERSION as V4,
    HYDROGEN_AWARE_SELECTION_POLICY_VERSION as V5,
    SelectionError, ligand_rejection_reason, select_ligand, target_selection_policy_version,
)
from foldarium_pipeline.sizing import count_smiles_heavy_atoms, count_smiles_heavy_atoms_v5, count_tokens, SizingError
from foldarium_pipeline.weekly_quiz import (
    WeeklyQuizAssemblyError, _weekly_ligand_eligibility, ligand_eligibility_from_target,
    stage_weekly_quiz, publish_staged_weekly_quiz,
)
from foldarium_pipeline.wednesday_reveal import _validated_round, WednesdayRevealError
from test_intake import SEQUENCE_TSV, NONPOLYMER_TSV, cameo_payload
from test_weekly_quiz import target, run_row, pdb_fixture, FakeCoordinator
from test_wednesday_reveal import round_fixture
from test_ligand_eligibility_normalization import fixture as hydrogen_fixture


class SelectionV5Tests(unittest.TestCase):
    def test_hydrogen_element_distinguished_from_he_hg_hf_and_attached_h(self):
        cases = [('[H]',1,0),('[2H]',1,0),('[3H+]',1,0),('[H:7]',1,0),
            ('[1H-]',1,0),('[He]',1,1),('[Hg]',1,1),('[Hf]',1,1),('[200Hg]',1,1),
            ('[NH4+]',1,1),('[13CH3]',1,1),('ClCBr',3,3),('[2H]CC[H:1]',4,2)]
        for smiles,old,new in cases:
            with self.subTest(smiles=smiles):
                self.assertEqual(count_smiles_heavy_atoms(smiles),old)
                self.assertEqual(count_smiles_heavy_atoms_v5(smiles),new)

    def test_exact_fifteen_atom_threshold_changes_only_when_explicit_h_counted(self):
        for prefix in ('[H]','[2H]','[3H]','[H:3]'):
            old={'component_id':'DRG','smiles':prefix+'C'*14}
            self.assertEqual(select_ligand([old],policy_version=V4)['heavy_atoms'],15)
            self.assertIsNone(select_ligand([old],policy_version=V5))
            self.assertEqual(ligand_rejection_reason(old,policy_version=V5),'below-heavy-atom-minimum')
            valid={**old,'smiles':prefix+'C'*15}
            self.assertEqual(select_ligand([valid],policy_version=V5)['heavy_atoms'],15)
        for isotope in ('[13CH3]','[NH3+]'):
            self.assertIsNotNone(select_ligand([{'component_id':'DRG','smiles':isotope+'C'*14}],policy_version=V5))

    def test_metal_disconnected_artifact_and_tep_rules_are_preserved(self):
        for smiles in ('[Hg]'+'C'*15,'[Hf]'+'C'*15):
            self.assertEqual(ligand_rejection_reason({'component_id':'DRG','smiles':smiles},policy_version=V5),'metal-containing-smiles')
        self.assertEqual(ligand_rejection_reason({'component_id':'DRG','smiles':'C'*15+'.[2H]'},policy_version=V5),'disconnected-smiles')
        self.assertIsNone(select_ligand([{'component_id':'PEG','smiles':'C'*20}],policy_version=V5))
        selected=select_ligand([{'component_id':'TEP','smiles':'C'*20},{'component_id':'DRG','smiles':'[2H]'+'C'*15}],policy_version=V5)
        self.assertEqual(selected['component_id'],'DRG')

    def test_unknown_policy_fails_including_empty_candidate_list(self):
        for value in ('latest','cameo-drug-like/v6',None,{}):
            with self.subTest(value=value),self.assertRaises(SelectionError):
                select_ligand([],policy_version=value)
        with self.assertRaises(SelectionError):
            WeeklyPolicy(selection_policy_version='latest').validate()

    def test_sizing_rejects_unknown_policy_instead_of_silently_using_v4(self):
        for policy in ('latest', 'cameo-drug-like/v6', None, {}):
            package = target()
            package['metadata']['selection_policy_version'] = policy
            with self.subTest(policy=policy), self.assertRaises(SizingError):
                count_tokens(package)

    def test_defaults_and_unstamped_historical_targets_keep_v4_provenance(self):
        old=target()
        old['entities'][-1]['smiles']='[2H]'+'C'*15
        old['metadata']['selected_ligand']['heavy_atoms']=16
        before=deepcopy(old)
        self.assertEqual(WeeklyPolicy().selection_policy_version,V4)
        self.assertEqual(target_selection_policy_version(old),V4)
        self.assertEqual(ligand_eligibility_from_target(old)['heavy_atoms'],16)
        self.assertEqual(ligand_eligibility_from_target(old)['policy'],V4)
        stamped=deepcopy(old);stamped['metadata']['selection_policy_version']=V4
        self.assertEqual(ligand_eligibility_from_target(old),ligand_eligibility_from_target(stamped))
        mislabeled=deepcopy(old);mislabeled['metadata']['selection_policy_version']=V5
        with self.assertRaises(WeeklyQuizAssemblyError):
            ligand_eligibility_from_target(mislabeled)
        self.assertEqual(old,before)

    def test_v5_task_target_and_campaign_are_frozen_and_v4_plan_unchanged(self):
        snapshot=parse_wwpdb_snapshot(SEQUENCE_TSV,NONPOLYMER_TSV)
        snapshot['entries']['36IQ']['ligands'][0]['smiles']='[2H]'+'C'*15
        args=dict(release_date=date(2026,6,20),ww_pdb_snapshot=snapshot,
            output_prefix='supabase://private/runs',generated_at=datetime(2026,6,20,tzinfo=timezone.utc))
        default=build_weekly_plan(**args)
        old=build_weekly_plan(**args,policy=WeeklyPolicy(selection_policy_version=V4))
        new=build_weekly_plan(**args,policy=WeeklyPolicy(selection_policy_version=V5))
        self.assertEqual(default,old)
        # Captured from unmodified public main ba825ef, including target/task IDs.
        self.assertEqual(hashlib.sha256(json.dumps(default,sort_keys=True,separators=(',', ':')).encode()).hexdigest(),
            'dc6c97562e06c715d057e214e59554495db83db391089229b17d1675a10d5686')
        self.assertEqual(new['campaign']['selection_policy_version'],V5)
        self.assertEqual(new['targets'][0]['metadata']['selection_policy_version'],V5)
        self.assertEqual(new['targets'][0]['metadata']['selected_ligand']['heavy_atoms'],15)
        self.assertEqual(old['targets'][0]['metadata']['selected_ligand']['heavy_atoms'],16)
        self.assertEqual(count_tokens(old['targets'][0]),count_tokens(new['targets'][0])+1)
        self.assertNotEqual(new['tasks'][0]['task_id'],old['tasks'][0]['task_id'])
        self.assertTrue(all(task['target']['metadata']['selection_policy_version']==V5 for task in new['tasks']))

    def test_cameo_path_uses_the_same_explicit_policy(self):
        source=cameo_payload()
        source['entities'][-1]['smiles']='[2H]'+'C'*14
        self.assertIsNotNone(target_from_cameo(source,WeeklyPolicy(selection_policy_version=V4)))
        self.assertIsNone(target_from_cameo(source,WeeklyPolicy(selection_policy_version=V5)))

    def test_legacy_recovery_preserves_v4_and_rejects_v5_wrong_count(self):
        smiles='[2H]'+'C'*15
        old=_legacy_recovered_ligand_eligibility('DRG',15,smiles)
        self.assertEqual(old['policy'],V4)
        self.assertTrue(old['passed'])
        new=_legacy_recovered_ligand_eligibility('DRG',15,smiles,policy_version=V5)
        self.assertEqual(new['policy'],V5)
        with self.assertRaises(PrivateEvaluationError):
            _legacy_recovered_ligand_eligibility('DRG',16,smiles,policy_version=V5)

    def test_v4_hydrogen_compatibility_cannot_be_relabelled_v5(self):
        row,private=hydrogen_fixture()
        self.assertEqual(len(_validated_round(row,private)[2]),1)
        private['items'][0]['ligand_eligibility']['policy']=V5
        with self.assertRaises(WednesdayRevealError):
            _validated_round(row,private)

    def test_v5_reveal_recomputes_frozen_pass_and_count(self):
        row,private,_=round_fixture();private=deepcopy(private)
        item=private['items'][0]
        item['ligand_eligibility']=_weekly_ligand_eligibility('DRG',17,'[2H]'+'C'*17,policy_version=V5)
        self.assertEqual(len(_validated_round(row,private)[2]),1)
        item['ligand_eligibility']['smiles']='[2H]'+'C'*16
        import hashlib
        item['ligand_eligibility']['smiles_sha256']=hashlib.sha256(item['ligand_eligibility']['smiles'].encode()).hexdigest()
        with self.assertRaisesRegex(WednesdayRevealError,'frozen policy'):
            _validated_round(row,private)


class SelectionV5StageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import gemmi,numpy
            from rdkit import Chem
        except ImportError:
            raise unittest.SkipTest('optional chemistry dependencies absent')

    def test_v5_lexer_matches_rdkit_actual_heavy_atoms_for_hydrogen_and_h_elements(self):
        from rdkit import Chem
        for smiles in ('[H]', '[2H]', '[H:1]', '[3H+]', '[He]', '[Hg]', '[Hf]',
                       '[200Hg]', '[NH4+]', '[13CH3]', '[2H]CC[H:1]'):
            with self.subTest(smiles=smiles):
                molecule = Chem.MolFromSmiles(smiles)
                self.assertIsNotNone(molecule)
                self.assertEqual(count_smiles_heavy_atoms_v5(smiles), molecule.GetNumHeavyAtoms())

    def test_v5_survives_assembly_publication_and_private_reveal_validation(self):
        package=target();package['metadata']['selection_policy_version']=V5
        package['entities'][-1]['smiles']='[2H]'+'C'*15
        rows=[];downloads={}
        for method,shift in [('openfold3',0.),('boltz2',20.)]:
            content=pdb_fixture(shift)
            row,uri=run_row(method,content,target_payload=package)
            rows.append(row);downloads[uri]=content
        with tempfile.TemporaryDirectory() as temporary:
            stage=stage_weekly_quiz(rows,temporary,round_id='weekly-v5-test',campaign_id='weekly-2026-08-08',downloader=lambda uri,**kwargs:downloads[uri])
            self.assertEqual(stage['ligand_eligibility_policy'],V5)
            self.assertEqual(stage['items'][0]['ligand_eligibility']['policy'],V5)
            private=FakeCoordinator('private');public=FakeCoordinator('public')
            publish_staged_weekly_quiz(temporary,private_coordinator=private,public_coordinator=public,
                opens_at='2026-08-08T03:00:00Z',closes_at='2026-08-12T00:00:00Z',open_round=True,round_environment='preview')
            self.assertEqual(private.opened['metadata']['ligand_eligibility_policy'],V5)
            index=json.loads(Path(temporary,'private-index.json').read_text())
            row={**private.opened,'blind_manifest_sha256':manifest_sha256(private.opened['blind_manifest'])}
            _,_,items=_validated_round(row,index)
            self.assertEqual(items[0]['ligand']['heavy_atoms'],15)
            self.assertEqual(items[0]['ligand_eligibility']['heavy_atoms'],15)
            self.assertEqual(items[0]['ligand_eligibility']['policy'],V5)

    def test_mixed_policy_stage_fails_before_any_artifact_download(self):
        rows=[]
        for index,policy in enumerate((V4,V5)):
            package=target('target-'+str(index));package['metadata']['selection_policy_version']=policy
            for method in ('openfold3','boltz2'):
                row,_=run_row(method,pdb_fixture(0.),target_payload=package);rows.append(row)
        def forbidden(*args,**kwargs):
            self.fail('mixed policies must fail before downloading artifacts')
        with tempfile.TemporaryDirectory() as temporary,self.assertRaisesRegex(WeeklyQuizAssemblyError,'mix frozen'):
            stage_weekly_quiz(rows,temporary,round_id='weekly-mixed',campaign_id='weekly-2026-08-08',downloader=forbidden)


if __name__=='__main__':
    unittest.main()
