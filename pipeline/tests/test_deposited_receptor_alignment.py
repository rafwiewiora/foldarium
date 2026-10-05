import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from foldarium_pipeline.evaluation import (
    _deposited_receptor_superposition, _polymer_chains, evaluate_ligand_pose,
    EvaluationError, EVALUATOR_VERSION, DEPOSITED_RECEPTOR_EVALUATOR_VERSION,
    DEPOSITED_RECEPTOR_ALIGNMENT_POLICY,
)
from foldarium_pipeline.wednesday_reveal import _evaluation_fields, WednesdayRevealError

try:
    import gemmi
    import numpy
    from rdkit import Chem
    HAS_SCIENCE=True
except ImportError:
    HAS_SCIENCE=False

NAMES='ALA CYS ASP GLU PHE GLY HIS ILE LYS LEU MET ASN PRO GLN ARG SER THR VAL TRP TYR'.split()
OBSERVED=(11,12,13,15,16,17)


def fixture(root, *, complete=False, chains=('A',)):
    def structure(predicted):
        s=gemmi.Structure();s.name='synthetic';model=gemmi.Model('1')
        for chain_name in (('P',) if predicted else chains):
            chain=gemmi.Chain(chain_name)
            for index,name in enumerate(NAMES,1):
                if not predicted and not complete and index not in OBSERVED:continue
                residue=gemmi.Residue();residue.name=name;residue.seqid=gemmi.SeqId(index,' ')
                residue.label_seq=index;residue.subchain=chain_name;residue.entity_type=gemmi.EntityType.Polymer
                atom=gemmi.Atom();atom.name='CA';atom.element=gemmi.Element('C')
                atom.pos=gemmi.Position(3*math.cos(index*.7)+(4 if not predicted else 0),3*math.sin(index*.7)-(3 if not predicted else 0),index*1.4+(2 if not predicted else 0))
                residue.add_atom(atom);chain.add_residue(residue)
            ligand=gemmi.Residue();ligand.name='DRG';ligand.seqid=gemmi.SeqId(101,' ');ligand.subchain=chain_name+'L';ligand.entity_type=gemmi.EntityType.NonPolymer;ligand.het_flag='H'
            for i in range(3):
                atom=gemmi.Atom();atom.name='C'+str(i+1);atom.element=gemmi.Element('C')
                atom.pos=gemmi.Position(1.5*i+(4 if not predicted else 0),-3 if not predicted else 0,17+(2 if not predicted else 0));ligand.add_atom(atom)
            chain.add_residue(ligand);model.add_chain(chain)
        s.add_model(model)
        entity=gemmi.Entity('1');entity.entity_type=gemmi.EntityType.Polymer;entity.polymer_type=gemmi.PolymerType.PeptideL;entity.full_sequence=NAMES;entity.subchains=list(('P',) if predicted else chains);s.entities.append(entity)
        drug=gemmi.Entity('2');drug.entity_type=gemmi.EntityType.NonPolymer;drug.subchains=[c+'L' for c in (('P',) if predicted else chains)];s.entities.append(drug)
        return s
    ref=structure(False);pred=structure(True)
    doc=ref.make_mmcif_document();block=doc.sole_block()
    loop=block.init_loop('_pdbx_unobs_or_zero_occ_residues.', ['id','PDB_model_num','polymer_flag','occupancy_flag','auth_asym_id','label_asym_id','label_comp_id','label_seq_id'])
    counter=0
    for chain in chains:
        for i,name in enumerate(NAMES,1):
            if not complete and i not in OBSERVED:
                counter+=1;loop.add_row([str(counter),'1','Y','1',chain,chain,name,str(i)])
    reference=root/'reference.cif';reference.write_text(doc.as_string())
    prediction=root/'prediction.cif';prediction.write_text(pred.make_mmcif_document().as_string())
    ref=gemmi.read_structure(str(reference));ref.setup_entities();pred=gemmi.read_structure(str(prediction));pred.setup_entities()
    return ref,pred,reference,prediction


@unittest.skipUnless(HAS_SCIENCE,'optional scientific dependencies')
class DepositedReceptorAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.ref,self.pred,self.reference,self.prediction=fixture(self.root)

    def fit(self,chain='A'):
        rp=dict(_polymer_chains(self.ref[0]))[chain];pp=_polymer_chains(self.pred[0])[0][1]
        return _deposited_receptor_superposition(self.ref,rp,pp,self.reference,chain,gemmi,numpy)

    def test_exact_full_entity_and_missing_ledger_align_observed_coordinates(self):
        before=(self.reference.read_bytes(),self.prediction.read_bytes())
        fit,audit=self.fit()
        self.assertLess(fit.rmsd,1e-5)
        self.assertEqual(audit['reference_receptor_model_number'],1)
        self.assertEqual(audit['reference_receptor_residues_expected'],20)
        self.assertEqual(audit['reference_receptor_residues_observed'],6)
        self.assertEqual(audit['reference_receptor_unobserved_residues'],14)
        self.assertEqual(audit['receptor_aligned_ca_count'],6)
        self.assertEqual(audit['receptor_alignment_policy'],DEPOSITED_RECEPTOR_ALIGNMENT_POLICY)
        self.assertEqual((self.reference.read_bytes(),self.prediction.read_bytes()),before)

    def test_end_to_end_fallback_version_and_provenance_survive_projection(self):
        score=evaluate_ligand_pose(self.reference,self.prediction,component_id='DRG',heavy_atoms=3)
        self.assertEqual(score['evaluator_version'],DEPOSITED_RECEPTOR_EVALUATOR_VERSION)
        self.assertLess(score['sequence_similarity'],.5)
        self.assertLess(score['rmsd'],1e-5)
        projected=_evaluation_fields(score)
        self.assertEqual(projected['receptor_alignment_policy'],DEPOSITED_RECEPTOR_ALIGNMENT_POLICY)
        self.assertEqual(projected['reference_entity_sequence_sha256'],score['reference_entity_sequence_sha256'])
        self.assertEqual(projected['receptor_aligned_ca_count'],6)

    def test_fallback_audit_rejects_stripped_or_inconsistent_projection(self):
        from copy import deepcopy
        score=evaluate_ligand_pose(self.reference,self.prediction,component_id='DRG',heavy_atoms=3)
        changes=[lambda x:x.pop('receptor_alignment_policy'),
                 lambda x:x.update(evaluator_version=EVALUATOR_VERSION),
                 lambda x:x.update(reference_receptor_unobserved_residues=1),
                 lambda x:x.update(receptor_aligned_ca_count=4),
                 lambda x:x.update(reference_receptor_model_number=2),
                 lambda x:x.update(reference_entity_sequence_sha256='broken'),
                 lambda x:x.update(sequence_similarity=.7)]
        for change in changes:
            candidate=deepcopy(score);change(candidate)
            with self.assertRaises(WednesdayRevealError):_evaluation_fields(candidate)

    def test_receptor_audit_cannot_enter_blind_item_metadata(self):
        from foldarium_pipeline.quiz import build_blind_manifest, QuizManifestError
        from test_quiz import source_items
        rows=source_items();rows[0]['metadata']={'receptor_alignment_policy':DEPOSITED_RECEPTOR_ALIGNMENT_POLICY}
        with self.assertRaises(QuizManifestError):build_blind_manifest('synthetic',rows)

    def test_wrong_prediction_or_full_entity_sequence_rejected(self):
        self.pred[0]['P'][0].name='VAL'
        with self.assertRaisesRegex(EvaluationError,'full entity'):self.fit()
        self.pred[0]['P'][0].name='ALA'
        self.ref.entities[0].full_sequence=['VAL']+NAMES[1:]
        with self.assertRaisesRegex(EvaluationError,'full entity'):self.fit()

    def test_missing_or_wrong_entity_binding_rejected(self):
        self.ref.entities[0].subchains=['OTHER']
        with self.assertRaises(EvaluationError):self.fit()

    def test_duplicate_missing_out_of_range_and_wrong_labels_rejected(self):
        rp=_polymer_chains(self.ref[0])[0][1];original=rp[0].label_seq
        for label in (None,12,999,10):
            with self.subTest(label=label):
                rp[0].label_seq=label
                with self.assertRaises(EvaluationError):self.fit()
        rp[0].label_seq=original
        self.pred[0]['P'][0].label_seq=2
        with self.assertRaisesRegex(EvaluationError,'prediction label'):self.fit()

    def test_missing_corrupt_or_wrong_chain_ledger_rejected(self):
        original=self.reference.read_text()
        for change in ('remove','wrong_name','wrong_chain','wrong_model'):
            doc=gemmi.cif.read_string(original);block=doc.sole_block();table=block.find_mmcif_category('_pdbx_unobs_or_zero_occ_residues.')
            if change=='remove':table.erase()
            else:
                col={'wrong_name':'label_comp_id','wrong_chain':'auth_asym_id','wrong_model':'PDB_model_num'}[change]
                block.find_values('_pdbx_unobs_or_zero_occ_residues.'+col)[0]={'wrong_name':'VAL','wrong_chain':'OTHER','wrong_model':'2'}[change]
            self.reference.write_text(doc.as_string())
            with self.subTest(change=change),self.assertRaises(EvaluationError):self.fit()
        self.reference.write_text(original)

    def test_full_sequence_numbering_and_entity_binding_must_match_raw_cif(self):
        original=self.reference.read_text()
        for tag,value in [('_entity_poly_seq.num','2'),('_entity_poly_seq.mon_id','VAL'),('_struct_asym.entity_id','2')]:
            doc=gemmi.cif.read_string(original);doc.sole_block().find_values(tag)[0]=value;self.reference.write_text(doc.as_string())
            with self.subTest(tag=tag),self.assertRaises(EvaluationError):self.fit()
        self.reference.write_text(original)

    def test_selected_first_model_must_match_model_one_ledger(self):
        self.ref[0].num=2
        with self.assertRaisesRegex(EvaluationError,'model one'):self.fit()
        doc=gemmi.cif.read_file(str(self.reference))
        values=doc.sole_block().find_values('_pdbx_unobs_or_zero_occ_residues.PDB_model_num')
        for index in range(len(values)):values[index]='2'
        self.reference.write_text(doc.as_string())
        with self.assertRaisesRegex(EvaluationError,'model one'):self.fit()

    def test_raw_cif_selected_model_two_cannot_use_model_one_ledger(self):
        doc=gemmi.cif.read_file(str(self.reference))
        values=doc.sole_block().find_values('_atom_site.pdbx_PDB_model_num')
        self.assertGreater(len(values),0)
        for index in range(len(values)):values[index]='2'
        self.reference.write_text(doc.as_string())
        self.ref=gemmi.read_structure(str(self.reference))
        self.assertEqual(self.ref[0].num,2)
        with self.assertRaisesRegex(EvaluationError,'model one'):self.fit()

    def test_ambiguous_ca_alternate_locations_rejected(self):
        residue=_polymer_chains(self.ref[0])[0][1][0]
        atom=residue[0].clone();atom.altloc='B';residue.add_atom(atom)
        with self.assertRaisesRegex(EvaluationError,'alternate'):self.fit()

    def test_insufficient_observed_ca_and_rank_deficient_geometry_rejected(self):
        rp=_polymer_chains(self.ref[0])[0][1]
        for i in range(2):rp[i].remove_atom('CA','\x00')
        with self.assertRaisesRegex(EvaluationError,'five observed'):self.fit()
        self.ref,self.pred,self.reference,self.prediction=fixture(self.root)
        for i,res in enumerate(_polymer_chains(self.ref[0])[0][1]):res[0].pos=gemmi.Position(i,0,0)
        with self.assertRaisesRegex(EvaluationError,'geometry'):self.fit()

    def test_multiple_chain_ledger_is_bound_independently(self):
        self.ref,self.pred,self.reference,self.prediction=fixture(self.root,chains=('A','B'))
        self.assertEqual(self.fit('A')[1]['reference_receptor_label_asym_id'],'A')
        self.assertEqual(self.fit('B')[1]['reference_receptor_label_asym_id'],'B')
        doc=gemmi.cif.read_file(str(self.reference));values=doc.sole_block().find_values('_pdbx_unobs_or_zero_occ_residues.auth_asym_id');values[14]='OTHER';self.reference.write_text(doc.as_string())
        self.fit('A')
        with self.assertRaises(EvaluationError):self.fit('B')

    def test_normal_path_stays_v4_without_new_fields(self):
        self.ref,self.pred,self.reference,self.prediction=fixture(self.root,complete=True)
        score=evaluate_ligand_pose(self.reference,self.prediction,component_id='DRG',heavy_atoms=3)
        self.assertEqual(score['evaluator_version'],EVALUATOR_VERSION)
        self.assertNotIn('receptor_alignment_policy',score)

if __name__=='__main__':unittest.main()
