"""Typed selection proof outranks optional UI telemetry without rewriting legacy sources."""
from copy import deepcopy
import unittest
from foldarium_pipeline.retrospective_archive import (
    RetrospectiveArchiveError, build_retrospective_source_snapshot, encode_retrospective_source_snapshot)
from test_retrospective_archive import ROUND_ID, HUMAN_ID, source_rows

DIGEST='a'*64
FIELDS=('user_id','item_id','choice_id','picked_none','selection_kind','selection_id',
        'selection_source','selection_source_attempt_id','selection_resolution_id','submitted_at')

class TypedRetrospectiveScopeTests(unittest.TestCase):
    def fixture(self, kind='exact'):
        rows=source_rows()
        vote=rows['votes'][0]
        vote.update(selection_kind=kind, selection_id='choice-b' if kind=='exact' else 'cluster-a',
                    selection_source='submit_v2',selection_source_attempt_id=rows['vote_attempts'][0]['vote_attempt_id'],
                    selection_resolution_id=None,submitted_at='2026-08-17T19:00:00+00:00')
        if kind=='none':vote.update(picked_none=True,choice_id=None,selection_id=None)
        envelope={'schema_version':'foldarium.retrospective-vote-scopes/v1','round_id':ROUND_ID,
                  'blind_manifest_sha256':DIGEST,'votes':[{key:vote[key] for key in FIELDS}]}
        envelope['votes'][0]['submitted_at']='2026-08-17T19:00:00Z'
        return rows,envelope

    def build(self,rows,envelope):
        return build_retrospective_source_snapshot(ROUND_ID,**rows,verified_vote_scopes=envelope,
                                                  expected_blind_manifest_sha256=DIGEST)

    def test_typed_exact_cluster_and_none_ignore_absent_or_contradictory_telemetry(self):
        for kind in ('exact','cluster','none'):
            for state in (None,{}, {'selection_kind':'cluster'}, {'selection_kind':'exact'}, {'selection_kind':'nonsense'}):
                with self.subTest(kind=kind,state=state):
                    rows,envelope=self.fixture(kind)
                    rows['vote_attempts'][0]['app_state']=state
                    result=self.build(rows,envelope)
                    vote=next(v for v in result['votes'] if v['participant_link']==HUMAN_ID)
                    self.assertEqual(vote['selection_kind'],kind)
                    self.assertEqual(vote['picked_none'],kind=='none')
                    self.assertNotIn('selection_source',vote)

    def test_invalid_proof_never_falls_back_to_valid_legacy_telemetry(self):
        for edit in ('absent','missing','duplicate','round','manifest','source','choice','timestamp','none'):
            with self.subTest(edit=edit):
                rows,envelope=self.fixture()
                if edit=='absent':envelope=None
                elif edit=='missing':envelope['votes']=[]
                elif edit=='duplicate':envelope['votes']*=2
                elif edit=='round':envelope['round_id']='wrong-round'
                elif edit=='manifest':envelope['blind_manifest_sha256']='b'*64
                elif edit=='source':envelope['votes'][0]['selection_source_attempt_id']='wrong-attempt'
                elif edit=='choice':envelope['votes'][0]['choice_id']='choice-a'
                elif edit=='timestamp':envelope['votes'][0]['submitted_at']='2026-08-17T19:00:01Z'
                else:
                    rows,envelope=self.fixture('none');envelope['votes'][0]['selection_source_attempt_id']='wrong-attempt'
                with self.assertRaises(RetrospectiveArchiveError):self.build(rows,envelope)

    def test_legacy_source_bytes_and_typed_matching_scope_preserve_existing_canonical_output(self):
        legacy=build_retrospective_source_snapshot(ROUND_ID,**source_rows())
        envelope={'schema_version':'foldarium.retrospective-vote-scopes/v1','round_id':ROUND_ID,
                  'blind_manifest_sha256':DIGEST,'votes':[]}
        self.assertEqual(encode_retrospective_source_snapshot(legacy),
                         encode_retrospective_source_snapshot(self.build(source_rows(),envelope)))
        rows,envelope=self.fixture()
        self.assertEqual(encode_retrospective_source_snapshot(legacy),
                         encode_retrospective_source_snapshot(self.build(rows,envelope)))
