from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from foldarium_pipeline.interactions import calculate_interaction_summary, InteractionFingerprintError
from foldarium_pipeline.interaction_metric import normalize_interaction_count, UNAVAILABLE_FIELDS, HBOND_POLICY, HBOND_METRIC
from foldarium_pipeline.weekly_quiz import _choice_scoring_fields, WeeklyQuizAssemblyError
from foldarium_pipeline.quiz import build_blind_manifest, QuizManifestError
from test_quiz import source_items

MARKER={'metric':HBOND_METRIC,'value':None,'policy':HBOND_POLICY,**UNAVAILABLE_FIELDS}
UNK_PDB='ATOM      1  CA  UNK A   1       0.000   0.000   0.000  1.00 90.00           C\nEND\n'

class InteractionAvailabilityTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit is not installed")
    def test_actual_unknown_receptor_produces_no_fabricated_count_or_template(self):
        from rdkit import Chem
        from rdkit.Geometry import Point3D
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'protein.pdb';path.write_text(UNK_PDB)
            with patch('foldarium_pipeline.interactions._installed_versions',return_value={'prolif':'2.2.0','rdkit':'2026.3.4'}),patch('foldarium_pipeline.interactions._dependencies',return_value=(SimpleNamespace(),Chem,Point3D)):
                summary=calculate_interaction_summary(path,'CC',[[1,0,0],[2.5,0,0]])
            self.assertEqual(path.read_text(),UNK_PDB)
        self.assertIsNone(summary['count']);self.assertEqual(summary['status'],'unavailable')
        self.assertEqual(summary['unsupported_residues'],['UNK']);self.assertNotIn('residues',summary)
        self.assertEqual(summary['policy'],HBOND_POLICY);self.assertEqual(summary['engine_version'],'2.2.0')
    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit is not installed")
    def test_unknown_does_not_hide_bad_ligand_topology_or_missing_dependency(self):
        from rdkit import Chem
        from rdkit.Geometry import Point3D
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'protein.pdb';path.write_text(UNK_PDB)
            with patch('foldarium_pipeline.interactions._installed_versions',return_value={'prolif':'2.2.0','rdkit':'2026.3.4'}),patch('foldarium_pipeline.interactions._dependencies',return_value=(SimpleNamespace(),Chem,Point3D)):
                with self.assertRaisesRegex(InteractionFingerprintError,'heavy-atom count'):calculate_interaction_summary(path,'CCC',[[1,0,0],[2.5,0,0]])
    def test_remote_scoring_preserves_successful_smina_and_only_explicit_null(self):
        result={'pose_id':'pose1','schema_version':'foldarium.pose-score/v1','status':'succeeded','scores':{'smina_affinity_kcal_mol':-7.5},'provenance':{'mode':'score_only','scoring_function':'vina'},'interaction_summary':{'engine':'prolif','policy':HBOND_POLICY,'count':None,**UNAVAILABLE_FIELDS}}
        fields=_choice_scoring_fields(result,expected_pose_id='pose1')
        self.assertEqual(fields['smina_score']['value'],-7.5);self.assertEqual(fields['interaction_count'],MARKER)
        self.assertEqual(fields['scoring'],result)
        for mutation in [{'reason':'arbitrary-error'},{'count':0},{'unsupported_residues':['MSE']},{'status':'failed'}]:
            bad=deepcopy(result);bad['interaction_summary'].update(mutation)
            with self.assertRaises(WeeklyQuizAssemblyError):_choice_scoring_fields(bad,expected_pose_id='pose1')
    def test_blind_marker_preserves_all_choices_and_stable_pose_identity(self):
        items=source_items();before,_=build_blind_manifest('week',items)
        items[0]['choices'][0]['interaction_count']=deepcopy(MARKER)
        blind,private=build_blind_manifest('week',items)
        self.assertEqual(len(blind['items'][0]['choices']),2)
        self.assertEqual([c['id'] for c in before['items'][0]['choices']],[c['id'] for c in blind['items'][0]['choices']])
        marker=next(c for c in blind['items'][0]['choices'] if c['method']=='openfold3')['interaction_count']
        self.assertEqual(marker,MARKER);self.assertNotIn('residue_position',str(marker))
        self.assertEqual(next(c for c in private['items'][0]['choices'] if c['method']=='openfold3')['interaction_count'],MARKER)
    def test_unavailable_schema_rejects_unknown_fields_positions_and_silent_null(self):
        for mutation in [{'availability_policy':'future'},{'value':0},{'status':None},{'unsupported_residues':['UNK','ALA']},{'residue_position':123}]:
            bad={**MARKER,**mutation}
            with self.assertRaises(ValueError):normalize_interaction_count(bad)
        with self.assertRaises(ValueError):normalize_interaction_count({'metric':HBOND_METRIC,'value':None,'policy':HBOND_POLICY})
        self.assertEqual(normalize_interaction_count({'metric':HBOND_METRIC,'value':0,'policy':HBOND_POLICY})['value'],0)
