// Disposable PostgreSQL behavior test. No provider or production calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(process.argv[2] ? pathToFileURL(process.argv[2]).href : '@electric-sql/pglite');
const pg = new PGlite();
await pg.exec(`
create schema private; create schema auth;
create role anon; create role authenticated; create role service_role;
create function auth.role() returns text language sql as $$select current_setting('request.jwt.claim.role',true)$$;
create table public.weekly_quiz_rounds(round_id text primary key,environment text,status text,closes_at timestamptz,revealed_at timestamptz,blind_manifest_sha256 text);
create table private.weekly_automation_round_policies(round_id text primary key,environment text,blind_manifest_sha256 text,expected_execution_ids uuid[]);
create table private.weekly_automation_benchmarks(execution_id uuid primary key,round_id text,driver text,model_id text,config_sha256 text,max_cost_usd numeric,artifact_uri text);
create table private.weekly_selector_kit_catalog(round_id text,blind_manifest_sha256 text,kit_sha256 text);
`);
await pg.exec(fs.readFileSync(new URL('../../supabase/migrations/20261004170000_anchor_weekly_inference_budget.sql', import.meta.url), 'utf8'));
const execution = '00000000-0000-4000-8000-000000000001';
const blind='a'.repeat(64), config='b'.repeat(64), kit='c'.repeat(64);
const claim = async (id=execution, cfg=config, sha=kit) => (await pg.query('select public.claim_weekly_automation_inference_start_v1($1,$2,$3) as value',[id,cfg,sha])).rows[0].value;
await assert.rejects(claim(), /service role/);
await pg.exec(`set request.jwt.claim.role='service_role'`);
await pg.query(`insert into public.weekly_quiz_rounds values('round1','production','open',clock_timestamp()+interval '7 days',null,$1)`,[blind]);
await pg.query(`insert into private.weekly_automation_round_policies values('round1','production',$1,array[$2::uuid])`,[blind,execution]);
await pg.query(`insert into private.weekly_automation_benchmarks values($1,'round1','anthropic-api','exact-model',$2,10,null)`,[execution,config]);
await pg.query(`insert into private.weekly_selector_kit_catalog values('round1',$1,$2)`,[blind,kit]);
await assert.rejects(claim(execution,'d'.repeat(64)),/mismatch/);
await assert.rejects(claim(execution,config,'d'.repeat(64)),/mismatch/);
assert.equal((await pg.query('select count(*) from private.weekly_automation_inference_starts')).rows[0].count,0);
// Open voting is allowed; inference is separate from closed-window ingestion.
const first=await claim();
assert.equal(first.first_claim,true);
assert.deepEqual({...first,first_claim:false},await claim());
const duplicates=await Promise.all([claim(),claim(),claim()]);
assert.equal(duplicates.filter(row=>row.first_claim).length,0);
assert.equal((await pg.query('select count(*) from private.weekly_automation_inference_starts')).rows[0].count,1);
assert.equal(first.execution_id,execution);assert.equal(first.max_cost_usd,10);assert.equal(first.kit_sha256,kit);
for(const mutation of ["update private.weekly_automation_inference_starts set max_cost_usd=20","delete from private.weekly_automation_inference_starts"]){
  await assert.rejects(pg.exec(mutation),/immutable/);
}
await pg.query('update private.weekly_selector_kit_catalog set kit_sha256=$1',['d'.repeat(64)]);
await assert.rejects(claim(execution,config,'d'.repeat(64)),/bound differently/);
await pg.query('update private.weekly_selector_kit_catalog set kit_sha256=$1',[kit]);
await pg.exec('update private.weekly_automation_benchmarks set max_cost_usd=20');
await assert.rejects(claim(),/bound differently/);
await pg.exec('update private.weekly_automation_benchmarks set max_cost_usd=10');
await pg.exec("update private.weekly_automation_benchmarks set driver='other'");
await assert.rejects(claim(),/mismatch/);
await pg.exec("update private.weekly_automation_benchmarks set driver='anthropic-api'");
await pg.exec("update private.weekly_automation_round_policies set expected_execution_ids='{}'");
await assert.rejects(claim(),/mismatch/);
await pg.query('update private.weekly_automation_round_policies set expected_execution_ids=array[$1::uuid]',[execution]);
await pg.exec("update public.weekly_quiz_rounds set status='revealed',revealed_at=clock_timestamp()");
await assert.rejects(claim(),/mismatch/);
for(const role of ['anon','authenticated','service_role']) {
  const q=await pg.query("select has_table_privilege($1,'private.weekly_automation_inference_starts','SELECT,INSERT,UPDATE,DELETE') as allowed",[role]);
  assert.equal(q.rows[0].allowed,false);
}
for(const role of ['anon','authenticated']) {
  const q=await pg.query("select has_function_privilege($1,'public.claim_weekly_automation_inference_start_v1(uuid,text,text)','EXECUTE') as allowed",[role]);
  assert.equal(q.rows[0].allowed,false);
}
assert.equal((await pg.query("select has_function_privilege('service_role','public.claim_weekly_automation_inference_start_v1(uuid,text,text)','EXECUTE') as allowed")).rows[0].allowed,true);
await pg.close();
console.log('Inference budget database behavior passed: one initialization grant, immutable external authority, exact job/kit/cap, unrevealed scope, and service-only access.');
