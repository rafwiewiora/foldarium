import assert from 'node:assert/strict';
import fs from 'node:fs';
const { PGlite } = await import(process.argv[2]);
const db = new PGlite();
await db.exec(`create schema private; create role anon; create role authenticated; create role service_role;
create table public.weekly_quiz_rounds(round_id text primary key,environment text,status text,revealed_at timestamptz,reveal_manifest jsonb,blind_manifest_sha256 text);
create table private.weekly_featured_question_selections(round_id text,blind_manifest_sha256 text,featured_questions jsonb,selection_artifact jsonb,registered_at timestamptz);
insert into public.weekly_quiz_rounds values ('round','production','open',null,null,repeat('a',64));
insert into private.weekly_featured_question_selections values ('round',repeat('a',64),'{"item_ids":["a"]}','{"object_uri":"private-fixture"}','2026-08-15T00:00:00Z');`);
await db.exec(fs.readFileSync(new URL('../../supabase/migrations/20261005000000_read_verified_featured_cohorts.sql', import.meta.url), 'utf8'));
const rpc = () => db.query("select public.get_weekly_featured_cohort_source_v1('round') as value").then(r => r.rows[0].value);
for (const role of ['anon', 'authenticated']) {
  await db.exec(`set role ${role}`);
  await assert.rejects(rpc(), /permission denied/);
  await assert.rejects(db.query('select * from private.weekly_featured_question_selections'), /permission denied/);
  await db.exec('reset role');
}
await db.exec('set role service_role');
assert.equal(await rpc(), null, 'unrevealed source must not be returned');
await assert.rejects(db.query('select * from private.weekly_featured_question_selections'), /permission denied/);
await db.exec("reset role; update public.weekly_quiz_rounds set status='revealed',revealed_at='2026-08-18T00:00:00Z',reveal_manifest='{}'; set role service_role");
const source = await rpc();
assert.deepEqual(Object.keys(source).sort(), ['blind_manifest_sha256', 'environment', 'featured_questions', 'registered_at', 'round_id', 'selection_artifact']);
assert.equal(source.round_id, 'round');
assert.equal(source.featured_questions.item_ids[0], 'a');
assert.equal(source.selection_artifact.object_uri, 'private-fixture');
for (const update of ["environment='preview'", "blind_manifest_sha256=repeat('b',64)", 'reveal_manifest=null', 'revealed_at=null']) {
  await db.exec(`reset role; begin; update public.weekly_quiz_rounds set ${update}; set role service_role;`);
  assert.equal(await rpc(), null, update);
  await db.exec('reset role; rollback; set role service_role');
}
assert.equal((await db.query("select public.get_weekly_featured_cohort_source_v1('missing') as value")).rows[0].value, null);
await db.close();
console.log('Featured cohort service RPC: ACL, pre-reveal boundary, production and manifest bindings passed.');
