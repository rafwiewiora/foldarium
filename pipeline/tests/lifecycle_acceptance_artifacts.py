"""Artifact half of the isolated lifecycle acceptance test; no network clients.

Native geometry is a small stored fixture and the scientific evaluator is an
explicit fixture. All manifest, kit, provider, budget, artifact and archive
contracts run unchanged. The JS driver supplies real PostgreSQL state.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import socket
import shutil
from urllib.parse import urlsplit, parse_qs

def deny_network(*args,**kwargs):
    raise RuntimeError('acceptance network access is forbidden')
socket.socket.connect=deny_network
socket.socket.connect_ex=deny_network
socket.socket.sendto=deny_network
socket.create_connection=deny_network
socket.getaddrinfo=deny_network
try:
    socket.create_connection(('api.anthropic.com',443))
except RuntimeError as error:
    assert str(error)=='acceptance network access is forbidden'
else:raise AssertionError('network guard failed')
from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.quiz import build_blind_manifest, manifest_sha256
from foldarium_pipeline.weekly_quiz import clone_weekly_quiz_manifests
from foldarium_pipeline.weekly_reconciliation import Gates, plan_reconciliation
from foldarium_pipeline.weekly_reconciliation_store import submit_expected_benchmark, reveal_evaluated_round
from foldarium_pipeline.weekly_question_selection import select_weekly_questions, public_featured_questions
from foldarium_pipeline.weekly_selector import build_selector_kit, verify_selector_kit_zip
from foldarium_pipeline.weekly_llm_unattended import run_isolated_inference, execute_benchmark_job
from foldarium_pipeline.weekly_llm_providers.anthropic_api import ApiBudgetError
from foldarium_pipeline.weekly_llm_providers.anthropic_api import config_sha256
from foldarium_pipeline.weekly_llm_contract import digest_post_close_benchmark, sha256_hex
from foldarium_pipeline.wednesday_reveal import run_private_preclose_evaluation, rcsb_reference_url
from foldarium_pipeline.private_evaluation import build_private_evaluation_artifact, describe_private_evaluation_artifact
from foldarium_pipeline.supabase import SupabaseCoordinator
from foldarium_pipeline.retrospective_archive import build_retrospective_source_snapshot, build_retrospective_artifacts, encode_retrospective_source_snapshot, _publication_descriptor
from test_private_evaluation import source_items, coordinate, minimal_reference_mmcif_gz, overlay_evaluation_score
from test_weekly_llm_score import build_fixture_assets, _fake_fixture
from test_weekly_llm_unattended import CONFIG, EXECUTION, Transport

phase, directory = sys.argv[1:]
root=Path(directory)
def put(name,value):
    (root/name).write_text(canonical_json(value))
def get(name):
    return json.loads((root/name).read_text())
def artifact(content,bucket='fixture-private'):
    digest=sha256_hex(content)
    return dict(object_uri=f'supabase://{bucket}/sha256/{digest[:2]}/{digest}',sha256=digest,size_bytes=len(content),media_type='application/json')

def bridge(message):
    print(json.dumps(message),flush=True)
    reply=json.loads(sys.stdin.readline())
    if 'error' in reply:raise RuntimeError(reply['error'])
    return reply['value']
class Coordinator:
    def _get_all_json_rows(self,uri,label):
        parsed=urlsplit(uri);query=parse_qs(parsed.query)
        return bridge({'source_rows':parsed.path.rsplit('/',1)[-1],'query':query})
    def _rpc(self,name,payload):
        payload=dict(payload)
        if 'p_reveal_manifest' in payload:
            payload['p_reveal_manifest']=canonical_json(payload['p_reveal_manifest'])
        return bridge({'rpc':name,'payload':payload})
    def store_bytes(self,content,media_type):
        descriptor=artifact(content);(root/descriptor['sha256']).write_bytes(content);return descriptor
    def download_content_object(self,uri,expected_sha256=None):
        if uri=='supabase://fixture-public/kit.zip':return (root/'kit.zip').read_bytes()
        content=(root/uri.rsplit('/',1)[-1]).read_bytes()
        assert sha256_hex(content)==expected_sha256
        return content
    def weekly_quiz_round(self,round_id):
        return bridge({'read':'round','round_id':round_id})
    def private_weekly_evaluation(self,round_id):
        return bridge({'read':'evaluation','round_id':round_id})

if phase=='prepare':
    rows=[]
    for i in range(6):
        item=deepcopy(source_items()[0]);item['id']=item['target_id']=f'9XY{i}'
        for j,choice in enumerate(item['choices']):
            choice['run_id']+=f'-{i}';choice['sample_id']+=f'-{i}'
            choice['protein_uri']=item['protein_uri'];choice['pocket_uri']='supabase://quiz/pocket.pdb'
            choice['confidence']={'metric':'ligand_plddt','value':85.25-j,'scale_min':0,'scale_max':100,'aggregation':'arithmetic-mean-selected-ligand-heavy-atoms'}
            choice['smina_score']={'metric':'smina_affinity','protocol':'score_only','scoring_function':'vina','units':'kcal/mol','value':-7.125+j}
        rows.append(item)
    preview,preview_private=build_blind_manifest('preview-acceptance',rows)
    blind,private=clone_weekly_quiz_manifests(preview,preview_private,round_id='weekly-acceptance')
    assert [item['choices'] for item in preview['items']]==[item['choices'] for item in blind['items']]
    content=canonical_json(private).encode();(root/'private.json').write_bytes(content)
    draw=select_weekly_questions(blind,private,seed='weekly-featured:weekly-acceptance')
    unsigned=canonical_json({k:v for k,v in draw.items() if k!='selection_sha256'})
    targets={item['id']:{'schema_version':'foldarium.prediction/v1','target_id':item['target_id'],'entities':[
        {'type':'protein','chain_ids':['A'],'sequence':'MKT'},
        {'type':'ligand','chain_ids':['B'],'smiles':'CCCCCCCCCCCCCCCCC'}]} for item in rows}
    zipped,descriptor=build_selector_kit(round_id=blind['round_id'],environment='production',blind_manifest=blind,
        targets_by_item_id=targets,assets_by_choice=build_fixture_assets(blind))
    (root/'kit.zip').write_bytes(zipped);kit=verify_selector_kit_zip(zipped)
    config=replace(CONFIG,max_cost_usd='10')
    job=dict(execution_id=EXECUTION,round_id=blind['round_id'],environment='production',blind_manifest_sha256=manifest_sha256(blind),
        driver='anthropic-api',model_id=config.model_id,config_sha256=config_sha256(config),max_cost_usd=10)
    put('prepared.json',dict(preview=preview,preview_canonical=canonical_json(preview),preview_sha256=manifest_sha256(preview),preview_private_artifact=artifact(canonical_json(preview_private).encode()),blind=blind,blind_canonical=canonical_json(blind),blind_sha256=manifest_sha256(blind),private_artifact=artifact(content),
        featured=public_featured_questions(draw),draw_canonical=unsigned,draw_artifact=artifact(unsigned.encode()),
        kit_descriptor=descriptor,kit=kit,job=job,config=config.__dict__))
elif phase=='infer':
    prepared=get('prepared.json');job=prepared['job'];config=replace(CONFIG,max_cost_usd='10')
    transport=Transport({k:v['response'] for k,v in _fake_fixture(prepared['kit'])['items'].items()})
    class FailureBeforeRegistration(Coordinator):
        first_registration=True
        def _rpc(self,name,payload):
            if name=='register_weekly_automation_artifact_v1' and self.first_registration:
                self.first_registration=False
                raise ConnectionError('fixture failure before registration reaches database')
            return super()._rpc(name,payload)
    coordinator=FailureBeforeRegistration()
    def launch(**kwargs):
        assert set(kwargs)=={'kit_path','job','config','state_dir'}
        assert 'DO_NOT_EXPOSE_ANSWERS' not in json.dumps(kwargs['job'])
        return run_isolated_inference(**kwargs,egress_enforced=True,durable_commit=lambda:None,transport=transport)
    def execute():
        return execute_benchmark_job(coordinator,coordinator,job,root/'state',config=config.__dict__,launch_inference=launch)
    try:execute()
    except ConnectionError:pass
    else:raise AssertionError('registration failure was not exercised')
    assert len(transport.posts)==6
    bridge({'restart_database':True})
    state=root/'state'/EXECUTION;saved=root/'saved-execution-state';state.rename(saved)
    try:execute()
    except ApiBudgetError:pass
    else:raise AssertionError('all-files loss reset an existing budget')
    assert len(transport.posts)==6
    shutil.rmtree(state);saved.rename(state) # restore exact retained local checkpoint
    (state/'benchmark.execution.json').unlink() # crash before final publication
    assert execute()['status']=='registered' and len(transport.posts)==6
    assert execute()['status']=='already_registered' and len(transport.posts)==6
    path=state/'benchmark.execution.json';execution=json.loads(path.read_text())
    put('inferred.json',dict(execution=execution,execution_sha256=digest_post_close_benchmark(execution,kit=prepared['kit']),
        payload_digest=sha256_hex(execution['payload']),artifact=artifact(path.read_bytes()),provider_calls=len(transport.posts),
        ledger=json.loads((state/'budget.json').read_text())))
elif phase=='ingest':
    prepared=get('prepared.json');inferred=get('inferred.json');coordinator=Coordinator()
    params={k:prepared['job'][k] for k in ('round_id','environment','blind_manifest_sha256','execution_id')}
    params['artifact_sha256']=inferred['artifact']['sha256']
    assert submit_expected_benchmark(coordinator,coordinator,params)['idempotent'] is False
    assert submit_expected_benchmark(coordinator,coordinator,params)['idempotent'] is True
elif phase=='reveal':
    prepared=get('prepared.json');coordinator=Coordinator()
    assert reveal_evaluated_round(coordinator,{k:prepared['job'][k] for k in ('round_id','environment','blind_manifest_sha256')})['status']=='revealed'
elif phase=='evaluate':
    row=get('round.json');private=(root/'private.json').read_bytes();reference=minimal_reference_mmcif_gz()
    result=run_private_preclose_evaluation(row,private,root/'evaluation',
        prediction_resolver=lambda choice:coordinate(f"native:{choice['run_id']}:{choice['sample_id']}".encode(),'supabase://fixture-private/native.cif'),
        reference_resolver=lambda item:coordinate(reference,rcsb_reference_url(item['target_id'])),
        evaluator=lambda *a,**kw:overlay_evaluation_score(),now=datetime.now(timezone.utc),allow_after_close=True)
    content,descriptor=build_private_evaluation_artifact(row,result);descriptor['artifact_object_uri']=artifact(content)['object_uri']
    assert describe_private_evaluation_artifact(content)==descriptor
    (root/'evaluation.json').write_bytes(content);(root/sha256_hex(content)).write_bytes(content)
    put('evaluated.json',dict(descriptor=descriptor,reveal=result['reveal_manifest'],reveal_canonical=canonical_json(result['reveal_manifest'])))
elif phase=='archive':
    row=get('round.json');evaluated=get('evaluated.json');source=get('source.json')
    rows=SupabaseCoordinator.weekly_retrospective_source_rows(Coordinator(),row['round_id'])
    actual_source=build_retrospective_source_snapshot(row['round_id'],item_count=row['item_count'],expected_blind_manifest_sha256=row['blind_manifest_sha256'],**rows)
    assert actual_source==source, 'Python coordinator/source normalization differs from PostgreSQL source'

    public,admin,summary=build_retrospective_artifacts(row,evaluated['descriptor'],(root/'evaluation.json').read_bytes(),source)
    source_bytes=encode_retrospective_source_snapshot(source)
    descriptor=_publication_descriptor(row,evaluated['descriptor'],artifact(source_bytes),artifact(public),artifact(admin))
    (root/'public-archive.json').write_bytes(public);(root/'admin-archive.json').write_bytes(admin)
    put('archived.json',dict(descriptor=descriptor,source_canonical=source_bytes.decode(),summary=summary))
elif phase=='plan':
    put('planned.json',plan_reconciliation(get('snapshot.json'),now=datetime.now(timezone.utc),gates=Gates(**{k:True for k in Gates.__dataclass_fields__}),available_drivers=('anthropic-api',),preview_version='v5',production_suffix='acceptance'))
else:raise ValueError('unknown acceptance phase')
