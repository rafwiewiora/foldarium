// Actual PostgreSQL joins and source reconstruction; no remote resources.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const {PGlite}=await import(process.argv[2]?pathToFileURL(process.argv[2]).href:'@electric-sql/pglite');
const pg=new PGlite();
const fixture=JSON.parse(fs.readFileSync(new URL('../../tests/fixtures/retrospective-source-v2-authorized.golden.json',import.meta.url)));
const round=fixture.round_id;
const proof=fixture.participants.find(p=>p.benchmark_authorization).benchmark_authorization;
const id=proof.execution_id;
await pg.exec(`
create schema private; create schema auth;
create role anon; create role authenticated; create role service_role;
create function auth.role() returns text language sql as $$select current_setting('request.jwt.claim.role',true)$$;
create table public.weekly_quiz_rounds(round_id text primary key,item_count int,environment text,status text,reveal_manifest jsonb,revealed_at timestamptz,blind_manifest_sha256 text);
create table public.weekly_quiz_votes(round_id text,user_id uuid,item_id text,choice_id text,picked_none bool);
create table public.weekly_quiz_sessions(round_id text,user_id uuid,display_name text);
create table public.weekly_quiz_vote_attempts(round_id text,user_id uuid,item_id text,choice_id text,picked_none bool,app_state jsonb,submitted_at timestamptz,vote_attempt_id uuid);
create table public.weekly_retrospective_automated_identities(user_id uuid,participant_kind text,display_name text);
create table public.weekly_selector_post_close_benchmarks_v1(execution_id uuid primary key,round_id text,environment text,provider text,requested_model_id text,config_sha256 text,display_name text,execution_sha256 text,payload_digest text,execution jsonb,payload jsonb,run_class text,supersedes_execution_id uuid);
create table private.weekly_automation_round_policies(round_id text,environment text,blind_manifest_sha256 text,expected_execution_ids uuid[]);
create table private.weekly_automation_benchmarks(execution_id uuid,round_id text,driver text,model_id text,config_sha256 text,verified_execution_sha256 text,verified_payload_digest text,artifact_sha256 text,artifact_uri text);
`);
const old=fs.readFileSync(new URL('../../supabase/migrations/20260826233000_add_retrospective_post_close_benchmarks.sql',import.meta.url),'utf8');
const start=old.indexOf('create or replace function private.foldarium_expected_weekly_retrospective_source(');
await pg.exec(old.slice(start,old.indexOf('\n$$;',start)+4));
await pg.query("insert into public.weekly_quiz_rounds values($1,1,'production','revealed','{}',clock_timestamp(),$2)",[round,proof.blind_manifest_sha256]);
for(const p of fixture.participants.filter(p=>!p.benchmark_authorization)) {
 await pg.query('insert into public.weekly_quiz_sessions values($1,$2,$3)',[round,p.participant_link,p.display_name||'LegacyAgent']);
 if(p.participant_kind==='automated') await pg.query("insert into public.weekly_retrospective_automated_identities values($1,'llm',$2)",[p.participant_link,p.automated_identity]);
 for(const v of fixture.votes.filter(v=>v.participant_link===p.participant_link)) {
  await pg.query('insert into public.weekly_quiz_votes values($1,$2,$3,$4,$5)',[round,p.participant_link,v.item_id,v.choice_id,v.picked_none]);
  await pg.query("insert into public.weekly_quiz_vote_attempts values($1,$2,$3,$4,$5,'{\"selection_kind\":\"exact\"}',clock_timestamp(),$6)",[round,p.participant_link,v.item_id,v.choice_id,v.picked_none,p.participant_link]);
 }
}
const snapshot=async()=> (await pg.query('select private.foldarium_expected_weekly_retrospective_source($1) as value',[round])).rows[0].value;
const legacyBefore=await snapshot();
await pg.exec(fs.readFileSync(new URL('../../supabase/migrations/20261004230000_authorize_frozen_benchmark_archive_identities.sql',import.meta.url),'utf8'));
assert.deepEqual(await snapshot(),legacyBefore);
const authorizations=async()=> (await pg.query('select public.get_weekly_retrospective_benchmark_authorizations_v1($1) as value',[round])).rows[0].value;
await assert.rejects(authorizations(),/service role/);
await pg.exec("set request.jwt.claim.role='service_role'");
assert.deepEqual(await authorizations(),[]);
const payload={schema_version:'foldarium.weekly-selector-submission/v2',environment:'production',round_id:round,submission_id:id,blind_manifest_sha256:proof.blind_manifest_sha256,items:[{item_id:'item-1',unclustered:{selection_kind:'exact',choice_id:'choice-a'}}]};
await pg.query("insert into public.weekly_selector_post_close_benchmarks_v1 values($1,$2,'production','anthropic-api',$3,$4,$3,$5,$6,$7,$8,'post_close_benchmark',null)",[id,round,proof.model_id,proof.config_sha256,proof.execution_sha256,proof.payload_digest,{blind_manifest_sha256:proof.blind_manifest_sha256},payload]);
// A plausible model label and a registered receipt alone never authorize it.
assert.equal(await snapshot(),null);
assert.deepEqual(await authorizations(),[]);
await pg.query("insert into private.weekly_automation_round_policies values($1,'production',$2,array[$3::uuid])",[round,proof.blind_manifest_sha256,id]);
await pg.query("insert into private.weekly_automation_benchmarks values($1,$2,'anthropic-api',$3,$4,$5,$6,$7,$8)",[id,round,proof.model_id,proof.config_sha256,proof.execution_sha256,proof.payload_digest,proof.artifact_sha256,`supabase://private/sha256/ff/${proof.artifact_sha256}`]);
assert.deepEqual(await authorizations(),[proof]);
assert.deepEqual(await snapshot(),fixture);
for(const mutation of [
 "update private.weekly_automation_benchmarks set config_sha256=repeat('9',64)",
 "update private.weekly_automation_benchmarks set verified_payload_digest=repeat('9',64)",
 "update private.weekly_automation_benchmarks set verified_execution_sha256=repeat('9',64)",
 "update private.weekly_automation_benchmarks set artifact_sha256=null",
 "update private.weekly_automation_benchmarks set artifact_uri=null",
 "update private.weekly_automation_round_policies set expected_execution_ids='{}'",
 "update private.weekly_automation_round_policies set blind_manifest_sha256=repeat('9',64)",
 "update public.weekly_selector_post_close_benchmarks_v1 set display_name='PocketFox'",
 "update public.weekly_selector_post_close_benchmarks_v1 set display_name='Claude Opus'",
 "update public.weekly_selector_post_close_benchmarks_v1 set requested_model_id='other-model'",
 "update public.weekly_selector_post_close_benchmarks_v1 set provider='other-provider'",
 "update public.weekly_selector_post_close_benchmarks_v1 set execution=jsonb_build_object('blind_manifest_sha256',repeat('9',64))",
 "update public.weekly_selector_post_close_benchmarks_v1 set payload=jsonb_set(payload,'{environment}','\"preview\"')",
]) {
 await pg.exec('begin');await pg.exec(mutation);
 assert.deepEqual(await authorizations(),[],mutation);
 assert.equal(await snapshot(),null,mutation);
 await pg.exec('rollback');
}
for(const mutation of ["update public.weekly_quiz_rounds set status='open'", "update public.weekly_quiz_rounds set environment='preview'"]) {
 await pg.exec('begin');await pg.exec(mutation);assert.deepEqual(await authorizations(),[]);await pg.exec('rollback');
}
// An authenticated participant cannot become automated merely by choosing the model label.
await pg.query('update public.weekly_quiz_sessions set display_name=$1 where user_id=$2',[proof.model_id,fixture.participants.find(p=>p.participant_kind==='human').participant_link]);
const human=(await snapshot()).participants.find(p=>p.participant_kind==='human');
assert.equal(human.automated_identity,null);assert.equal(human.benchmark_authorization,undefined);
for(const role of ['anon','authenticated']) {
 assert.equal((await pg.query("select has_function_privilege($1,'public.get_weekly_retrospective_benchmark_authorizations_v1(text)','EXECUTE') as allowed",[role])).rows[0].allowed,false);
 assert.equal((await pg.query("select has_function_privilege($1,'private.foldarium_frozen_benchmark_authorization(text,uuid)','EXECUTE') as allowed",[role])).rows[0].allowed,false);
}
await pg.close();
console.log('Archive identity PostgreSQL behavior passed: exact frozen+verified receipt joins, service-only proofs, source-v2 Python parity, legacy-v1 parity, and no human name spoofing.');
