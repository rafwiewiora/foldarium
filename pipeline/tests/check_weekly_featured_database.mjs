// Run against an isolated PGlite Postgres, never a production database. Pass the
// installed module path as argv[2]. The digest wrapper uses PostgreSQL's native
// sha256(bytea), exercising the same crypto as pgcrypto digest(...,'sha256').
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(process.argv[2] ? pathToFileURL(process.argv[2]).href : '@electric-sql/pglite');
const pg = new PGlite();
const canonical = value => Array.isArray(value) ? `[${value.map(canonical).join(',')}]`
  : value && typeof value === 'object'
    ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
    : JSON.stringify(value);
const sha = value => createHash('sha256').update(value).digest('hex');
await pg.exec(`
create schema private; create schema extensions;
create role anon; create role authenticated; create role service_role;
create function extensions.digest(bytea,text) returns bytea language sql immutable as
  $$ select sha256($1) where $2 = 'sha256' $$;
create table public.weekly_quiz_rounds(
 round_id text primary key,campaign_id text,opens_at timestamptz,closes_at timestamptz,item_count integer,
 blind_manifest jsonb,reveal_manifest jsonb,status text,opened_at timestamptz,revealed_at timestamptz,
 environment text,blind_manifest_sha256 text,metadata jsonb);
create view public.public_weekly_quiz_rounds with (security_barrier=true) as
 select round_id,campaign_id,opens_at,closes_at,item_count,blind_manifest,
 case when status='revealed' then reveal_manifest else null end as reveal_manifest,
 case when status='revealed' then 'revealed' when clock_timestamp()>=closes_at then 'closed'
 when clock_timestamp()>=opens_at then 'open' else 'scheduled' end as public_status,
 opened_at,revealed_at,environment from public.weekly_quiz_rounds where status in ('open','revealed');
grant select on public.public_weekly_quiz_rounds to anon,authenticated,service_role;
create function public.get_current_weekly_quiz_round(p_environment text)
 returns setof public.public_weekly_quiz_rounds language sql stable security invoker as
 $$select * from public.public_weekly_quiz_rounds where environment=p_environment order by opens_at desc limit 1$$;
`);
await pg.exec(fs.readFileSync(new URL('../../supabase/migrations/20260909203000_add_exact_open_weekly_round_lookup.sql', import.meta.url), 'utf8'));
await pg.exec(fs.readFileSync(new URL('../../supabase/migrations/20261004010000_add_weekly_featured_questions.sql', import.meta.url), 'utf8'));
const blind = {schema_version:1,round_id:'full-round',items:Array.from({length:8},(_, i)=>({id:`item-${i}`,choices:[{id:`pose-${i}`}]}))};
const fullDigest = sha(canonical(blind));
const privateDigest = 'a'.repeat(64);
await pg.query(`insert into public.weekly_quiz_rounds values(
 'full-round','campaign',clock_timestamp()-interval '1 day',clock_timestamp()+interval '3 days',8,
 $1,null,'open',clock_timestamp(),null,'production',$2,$3)`, [blind,fullDigest,{
 private_index:{sha256:privateDigest,object_uri:`supabase://private-bucket/sha256/aa/${privateDigest}`},
 private_only:'private-only-fixture',
}]);
const original = (await pg.query('select * from public.weekly_quiz_rounds')).rows;
const audit = {
 schema_version:1,policy:'foldarium-weekly-question-draw/v1',mode:'uniform',seed:'fixture-seed',
 source_round_id:'full-round',source_blind_manifest_sha256:fullDigest,source_private_index_sha256:privateDigest,
 candidate_population_sha256:'b'.repeat(64),candidate_evidence_sha256:'c'.repeat(64),
 source_item_count:8,candidate_count:8,requested_question_count:5,selected_question_count:5,
 included_item_ids:['item-0','item-2','item-3','item-6','item-7'],
 candidates:blind.items.map(item=>({item_id:item.id,interestingness:{score:0.5}})),
};
function registration(body=audit) {
 const text=canonical(body),digest=sha(text);
 return [body.source_round_id,{
  schema_version:1,policy:body.policy,mode:body.mode,seed:body.seed,
  blind_manifest_sha256:body.source_blind_manifest_sha256,
  candidate_population_sha256:body.candidate_population_sha256,selection_sha256:digest,
  source_item_count:body.source_item_count,candidate_count:body.candidate_count,
  requested_question_count:body.requested_question_count,selected_question_count:body.selected_question_count,
  item_ids:body.included_item_ids,
 },text,{
  object_uri:`supabase://private-bucket/sha256/${digest.slice(0,2)}/${digest}`,
  sha256:digest,size_bytes:Buffer.byteLength(text),media_type:'application/json',
 }];
}
const rpc = args => pg.query('select public.register_weekly_featured_questions($1,$2,$3,$4) as value',args).then(result=>result.rows[0].value);
const base = registration();
for (const role of ['anon','authenticated']) {
 await pg.exec(`set role ${role}`);
 await assert.rejects(rpc(base),/permission denied/);
 await assert.rejects(pg.query('select * from private.weekly_featured_question_selections'),/permission denied/);
 await pg.exec('reset role');
}
const legacy = (await pg.query("select * from public.get_exact_open_weekly_quiz_round('full-round','production')")).rows[0];
assert.equal(legacy.featured_questions,null);
assert.equal(legacy.blind_manifest_sha256,fullDigest);
await pg.exec('set role service_role');
for (const [field,value] of [
 ['source_blind_manifest_sha256','0'.repeat(64)],['source_private_index_sha256','0'.repeat(64)],
 ['selected_question_count',6],['requested_question_count',6],['source_item_count',5],
 ['requested_question_count','5'],['seed',{unexpected:'object'}],
 ['candidate_count',9],['mode',null],['included_item_ids',['item-0','item-0','item-3','item-6','item-7']],
 ['included_item_ids',['unknown','item-2','item-3','item-6','item-7']],
 ['candidates',audit.candidates.map(()=>({item_id:'item-0'}))],
]) {
 await assert.rejects(rpc(registration({...audit,[field]:value})),/featured|invalid|count|item/i,field);
}
const publicLeak=registration(); publicLeak[1].private_artifact='private-only-fixture';
await assert.rejects(rpc(publicLeak),/public marker/);
const badArtifact=registration(); badArtifact[3].object_uri=badArtifact[3].object_uri.replace('private-bucket','public-bucket');
await assert.rejects(rpc(badArtifact),/private content digest/);
const wrongBytes=registration(); wrongBytes[2]+=' ';
await assert.rejects(rpc(wrongBytes),/public marker/);
assert.equal((await rpc(base)).status,'registered');
assert.equal((await rpc(base)).status,'already-registered');
await assert.rejects(rpc(registration({...audit,seed:'redraw'})),/already frozen/);
await assert.rejects(pg.query("update private.weekly_featured_question_selections set featured_questions='{}'"),/permission denied/);
await pg.exec('reset role');
await assert.rejects(pg.query("update private.weekly_featured_question_selections set featured_questions='{}'"),/immutable/);
await assert.rejects(pg.query('delete from private.weekly_featured_question_selections'),/immutable/);
assert.deepEqual((await pg.query('select * from public.weekly_quiz_rounds')).rows,original);
await pg.exec('set role anon');
for (const query of ["select * from public.public_weekly_quiz_rounds", "select * from public.get_current_weekly_quiz_round('production')", "select * from public.get_exact_open_weekly_quiz_round('full-round','production')"]) {
 const row=(await pg.query(query)).rows[0];
 assert.deepEqual(row.featured_questions,base[1]);
 assert.equal(row.item_count,8);
 assert.equal(row.blind_manifest.items.length,8);
 assert.equal(row.blind_manifest_sha256,fullDigest);
 assert.equal(row.reveal_manifest,null);
 assert.doesNotMatch(JSON.stringify(row),/private-bucket|private-only-fixture|selection_artifact|interestingness/);
}
await pg.exec('reset role');
await pg.exec("update public.weekly_quiz_rounds set status='revealed',reveal_manifest='{}'");
assert.equal((await rpc(base)).status,'already-registered');
await pg.query(`insert into public.weekly_quiz_rounds select 'revealed-without-draw',campaign_id,opens_at,closes_at,item_count,
 blind_manifest,reveal_manifest,status,opened_at,revealed_at,environment,blind_manifest_sha256,metadata
 from public.weekly_quiz_rounds`);
await assert.rejects(rpc(registration({...audit,source_round_id:'revealed-without-draw'})),/unrevealed/);
// Exercise the production Python serializer/scorer against the real SQL RPC,
// including an escaped Unicode seed and full per-choice audit provenance.
const pythonFixture=JSON.parse(execFileSync(process.env.PYTHON || 'python3',['-c',`
import hashlib,json
from foldarium_pipeline.quiz import build_blind_manifest,manifest_sha256
from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.weekly_question_selection import select_weekly_questions,public_featured_questions
blind,private=build_blind_manifest('python-round',[{'id':f'target-{i}','choices':[
 {'run_id':f'run-{i}-{m}','sample_id':'sample-0','method':m,'cluster_id':m,'is_rep':True,'pose_uri':'supabase://public/pose'}
 for m in ['boltz2','openfold3']]} for i in range(8)])
private_sha=hashlib.sha256(canonical_json(private).encode()).hexdigest()
audit=select_weekly_questions(blind,private,seed='weekly-Å-seed')
marker=public_featured_questions(audit)
text=canonical_json({k:v for k,v in audit.items() if k!='selection_sha256'})
digest=audit['selection_sha256']
artifact={'object_uri':f'supabase://private-bucket/sha256/{digest[:2]}/{digest}','sha256':digest,'size_bytes':len(text.encode()),'media_type':'application/json'}
print(json.dumps({'blind':blind,'private_sha':private_sha,'marker':marker,'canonical':text,'artifact':artifact}))
`],{encoding:'utf8',env:{...process.env,PYTHONPATH:new URL('../src',import.meta.url).pathname}}));
await pg.query(`insert into public.weekly_quiz_rounds values(
 'python-round','campaign',clock_timestamp()-interval '1 day',clock_timestamp()+interval '3 days',8,
 $1,null,'open',clock_timestamp(),null,'production',$2,$3)`,[
 pythonFixture.blind,pythonFixture.marker.blind_manifest_sha256,{private_index:{
 sha256:pythonFixture.private_sha,object_uri:`supabase://private-bucket/sha256/${pythonFixture.private_sha.slice(0,2)}/${pythonFixture.private_sha}`,
 }}]);
await pg.exec('set role service_role');
assert.equal((await rpc(['python-round',pythonFixture.marker,pythonFixture.canonical,pythonFixture.artifact])).status,'registered');
await pg.exec('reset role');
await pg.close();
console.log('Featured questions PostgreSQL behavior passed: exact lineage, immutable freeze, service-only RPC, bounded valid IDs, private artifact hashes, public projection, and existing view-return RPC compatibility.');
