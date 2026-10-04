// Real PostgreSQL behavior in disposable PGlite; no production or provider calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const { PGlite } = await import(process.argv[2] ? pathToFileURL(process.argv[2]).href : '@electric-sql/pglite');
const pg = new PGlite();
const sql = name => fs.readFileSync(new URL(`../../supabase/migrations/${name}`, import.meta.url), 'utf8');
const table = (source, name) => source.slice(source.indexOf(`create table public.${name} (`), source.indexOf('\n);', source.indexOf(`create table public.${name} (`))+3);
const func = (source, name, prefix='create or replace function ') => { const start=source.indexOf(prefix+name+'('); const end=source.indexOf(prefix==='create function '?'end $$;':'\n$$;',start);return source.slice(start,end+(prefix==='create function '?7:4)); };
await pg.exec(`
create schema private; create schema auth;
create role anon; create role authenticated; create role service_role;
create function auth.role() returns text language sql as $$select current_setting('request.jwt.claim.role',true)$$;
create table public.weekly_quiz_rounds(round_id text primary key,campaign_id text,environment text,status text,opens_at timestamptz,closes_at timestamptz,opened_at timestamptz,revealed_at timestamptz,item_count integer,blind_manifest_sha256 text,blind_manifest jsonb,reveal_manifest jsonb,reveal_manifest_sha256 text,metadata jsonb);
create table private.weekly_automation_round_policies(round_id text primary key,environment text,blind_manifest_sha256 text,expected_execution_ids uuid[]);
create table private.weekly_automation_benchmarks(execution_id uuid primary key,round_id text,driver text,model_id text,config_sha256 text,artifact_sha256 text,verified_execution_sha256 text,verified_payload_digest text);
create table public.weekly_selector_post_close_benchmarks_v1(execution_id uuid,round_id text,environment text,provider text,requested_model_id text,config_sha256 text,execution jsonb,execution_sha256 text,payload_digest text);
`);
await pg.exec(table(sql('20260815020000_add_private_weekly_evaluations.sql'),'weekly_quiz_evaluations'));
await pg.exec(sql('20260825235500_upgrade_private_weekly_evaluations_v5.sql'));
await pg.exec(table(sql('20260826003000_add_weekly_retrospective_publications.sql'),'weekly_retrospective_publications'));
await pg.exec(func(sql('20260808010200_add_weekly_quiz.sql'),'public.reveal_weekly_quiz_round'));
await pg.exec(`revoke all on function public.reveal_weekly_quiz_round(text,jsonb,text) from public; grant execute on function public.reveal_weekly_quiz_round(text,jsonb,text) to service_role;
create trigger evaluation_insert before insert on public.weekly_quiz_evaluations for each row execute function private.foldarium_validate_weekly_evaluation_catalog();
create trigger evaluation_immutable before update or delete on public.weekly_quiz_evaluations for each row execute function private.foldarium_validate_weekly_evaluation_catalog();`);
await pg.exec(func(sql('20260826003000_add_weekly_retrospective_publications.sql'),'public.list_missing_weekly_retrospective_publications'));
await pg.exec('revoke all on function public.list_missing_weekly_retrospective_publications() from public; grant execute on function public.list_missing_weekly_retrospective_publications() to service_role;');
await pg.exec(sql('20261004200000_support_unscorable_reference_dispositions.sql'));
await pg.exec(`create trigger retrospective_insert before insert on public.weekly_retrospective_publications for each row execute function private.foldarium_validate_weekly_retrospective_catalog();
create trigger retrospective_immutable before update or delete on public.weekly_retrospective_publications for each row execute function private.foldarium_validate_weekly_retrospective_catalog();`);
await pg.exec(func(sql('20261004090000_add_weekly_automation_outbox.sql'),'public.reveal_weekly_automation_round_v1','create function '));
const digest=n=>n.repeat(64), blind=digest('a'), reference=digest('b'), revealSHA=digest('c'), privateSHA=digest('d');
const proof={policy:'foldarium.released-reference-disposition/v1',code:'insufficient_reference_coverage',component_id:'ABC',expected_heavy_atoms:71,observed_heavy_atoms:36,explicitly_unobserved_heavy_atoms:35,reference_coverage:36/71,minimum_reference_coverage:.8,reference_sha256:reference};
const unscorable={id:'item1',evaluation_status:'unscorable',reference_disposition:proof,choices:[{id:'choice1',rmsd:null,correct:null,accepted_correct:null,reference_sha256:reference},{id:'choice2',rmsd:null,correct:null,accepted_correct:null,reference_sha256:reference}]};
const scored={id:'item2',choices:[{id:'choice3',rmsd:1,correct:true},{id:'choice4',rmsd:3,correct:false}]};
const validate=async item=>pg.query('select private.foldarium_validate_reveal_item($1)',[item]);
await validate(unscorable);await validate(scored);
// Each malformed proof or ambiguous null must fail independently.
for(const mutate of [
 i=>delete i.reference_disposition, i=>i.reference_disposition.extra='bad',
 i=>i.reference_disposition.expected_heavy_atoms=true, i=>i.reference_disposition.observed_heavy_atoms=0,
 i=>i.reference_disposition.explicitly_unobserved_heavy_atoms=34, i=>i.reference_disposition.reference_coverage=.79,
 i=>i.reference_disposition.minimum_reference_coverage=.9, i=>i.reference_disposition.reference_sha256='bad',
 i=>i.reference_disposition.component_id=null, i=>i.reference_disposition.component_id=' \t',
 i=>{i.reference_disposition.expected_heavy_atoms=9007199254740992;i.reference_disposition.observed_heavy_atoms=4503599627370496;i.reference_disposition.explicitly_unobserved_heavy_atoms=4503599627370496;i.reference_disposition.reference_coverage=.5;},
 i=>i.evaluation_status='unknown',
 i=>i.id=1, i=>i.choices[0].id=1,
 i=>i.choices[0].correct=false, i=>i.choices[0].accepted_correct=false, i=>delete i.choices[0].rmsd,
 i=>i.choices[0].reference_sha256=digest('e'), i=>i.choices.push({...i.choices[0]}),
]) {const invalid=structuredClone(unscorable);mutate(invalid);await assert.rejects(validate(invalid));}
await assert.rejects(validate({...scored,reference_disposition:proof}), /disposition/);
await assert.rejects(validate({id:'item2',choices:[{id:'choice3',rmsd:null,correct:null}]}), /scored choices/);
const insert=async(tableName,record)=>pg.query(`insert into public.${tableName} select * from jsonb_populate_record(null::public.${tableName},$1)`,[record]);
const opens='2020-01-01T00:00:00Z',closes='2020-01-02T00:00:00Z';
const round={round_id:'fixture-round',campaign_id:'fixture-campaign',environment:'production',status:'open',opens_at:opens,closes_at:closes,opened_at:opens,revealed_at:null,item_count:2,blind_manifest_sha256:blind,blind_manifest:{schema_version:1,items:[{id:'item1',choices:[{id:'choice1'},{id:'choice2'}]},{id:'item2',choices:[{id:'choice3'},{id:'choice4'}]}]},reveal_manifest:null,reveal_manifest_sha256:null,metadata:{private_index:{sha256:privateSHA}}};
await insert('weekly_quiz_rounds',round);
const manifest={schema_version:1,round_id:round.round_id,blind_manifest_sha256:blind,items:[unscorable,scored]};
const reveal=async(m=manifest,sha=revealSHA)=>pg.query('select public.reveal_weekly_quiz_round($1,$2,$3)',[round.round_id,m,sha]);
for(const mutate of [m=>m.items[0].choices.pop(),m=>m.items[0].choices[0].id='wrong',m=>m.items.push(m.items[0]),m=>m.items.pop()]) {const bad=structuredClone(manifest);mutate(bad);await assert.rejects(reveal(bad));}
await assert.rejects(reveal(manifest,null));
await pg.exec("update public.weekly_quiz_rounds set closes_at=clock_timestamp()+interval '1 day'");
await assert.rejects(reveal(),/cannot be revealed/);
await pg.query('update public.weekly_quiz_rounds set closes_at=$1',[closes]);
const artifact=digest('e'), uri=sha=>`supabase://fixture-bucket/sha256/${sha.slice(0,2)}/${sha}`;
const evaluation={evaluation_id:'weekly_eval_'+'1'.repeat(32),round_id:round.round_id,campaign_id:round.campaign_id,environment:'production',round_opens_at:opens,round_closes_at:closes,blind_manifest_sha256:blind,private_index_sha256:privateSHA,reveal_manifest_sha256:revealSHA,reference_set_sha256:reference,prediction_set_sha256:digest('f'),format_version:'foldarium.weekly-private-evaluation/v6',evaluator_versions:['fixture'],reveal_policy_version:'foldarium-weekly-reveal/v1',acceptance_policy_version:'foldarium-weekly-cluster-any-member/v1',correct_rmsd_threshold_angstrom:1.5,item_count:2,choice_count:4,artifact_object_uri:uri(artifact),artifact_sha256:artifact,artifact_size_bytes:10,artifact_media_type:'application/json',created_at:closes};
await assert.rejects(insert('weekly_quiz_evaluations',{...evaluation,private_index_sha256:digest('f')}),/closed production round/);
await assert.rejects(insert('weekly_quiz_evaluations',{...evaluation,format_version:'foldarium.weekly-private-evaluation/v7'}),/format_version_check/);
await insert('weekly_quiz_evaluations',evaluation);
await assert.rejects(pg.exec('update public.weekly_quiz_evaluations set item_count=1'),/immutable/);
const execution='00000000-0000-4000-8000-000000000001';
await pg.query('insert into private.weekly_automation_round_policies values($1,$2,$3,array[$4::uuid])',[round.round_id,'production',blind,execution]);
const guarded=()=>pg.query('select public.reveal_weekly_automation_round_v1($1,$2,$3,$4)',[round.round_id,evaluation.evaluation_id,manifest,revealSHA]);
await assert.rejects(guarded(),/service role/);
await pg.exec("set request.jwt.claim.role='service_role'");
await assert.rejects(guarded(),/every verified benchmark receipt/);
await pg.query('insert into private.weekly_automation_benchmarks values($1,$2,$3,$4,$5,$6,$7,$8)',[execution,round.round_id,'fixture','model',digest('1'),digest('2'),digest('3'),digest('4')]);
await assert.rejects(guarded(),/every verified benchmark receipt/);
await pg.query('insert into public.weekly_selector_post_close_benchmarks_v1 values($1,$2,$3,$4,$5,$6,$7,$8,$9)',[execution,round.round_id,'production','fixture','model',digest('1'),{blind_manifest_sha256:blind},digest('3'),digest('4')]);
await guarded();await reveal();
const published=(await pg.query('select * from public.weekly_quiz_rounds')).rows[0];
assert.equal(published.status,'revealed');assert.equal(published.item_count,2);assert.equal(published.reveal_manifest.items.length,2);
assert.deepEqual(published.reveal_manifest.items[0].choices.map(c=>c.correct),[null,null]);
assert.deepEqual((await pg.query('select * from public.list_missing_weekly_retrospective_publications()')).rows,[{round_id:round.round_id}]);
const publication={publication_id:'weekly_archive_'+'2'.repeat(32),round_id:round.round_id,campaign_id:round.campaign_id,environment:'production',format_version:'foldarium.weekly-retrospective-publication/v1',evaluation_id:evaluation.evaluation_id,evaluation_format_version:evaluation.format_version,round_opens_at:opens,round_closes_at:closes,round_revealed_at:published.revealed_at,blind_manifest_sha256:blind,private_index_sha256:privateSHA,reveal_manifest_sha256:revealSHA,reference_set_sha256:reference,prediction_set_sha256:digest('f'),evaluation_artifact_sha256:artifact,item_count:2,choice_count:4,source_snapshot_object_uri:uri(digest('5')),source_snapshot_sha256:digest('5'),source_snapshot_size_bytes:10,source_snapshot_media_type:'application/json',public_artifact_object_uri:uri(digest('6')),public_artifact_sha256:digest('6'),public_artifact_size_bytes:10,public_artifact_media_type:'application/json',admin_artifact_object_uri:uri(digest('7')),admin_artifact_sha256:digest('7'),admin_artifact_size_bytes:10,admin_artifact_media_type:'application/json',created_at:published.revealed_at};
await assert.rejects(insert('weekly_retrospective_publications',{...publication,evaluation_format_version:'foldarium.weekly-private-evaluation/v5'}),/exact versioned evaluation/);
await assert.rejects(insert('weekly_retrospective_publications',{...publication,item_count:1}),/revealed production round/);
await insert('weekly_retrospective_publications',publication);
await assert.rejects(pg.exec('delete from public.weekly_retrospective_publications'),/immutable/);
assert.equal((await pg.query('select * from public.list_missing_weekly_retrospective_publications()')).rows.length,0);
// Legacy v5 and all-unscorable v6 remain complete, independently registered
// populations. Catalog item/choice counts continue to mean the full population.
for (const [suffix, version, items] of [
 ['normal','v5',[{...scored,id:'item1',choices:scored.choices.map((c,i)=>({...c,id:`choice${i+1}`}))},scored]],
 ['excluded','v6',[unscorable,{...unscorable,id:'item2',choices:unscorable.choices.map((c,i)=>({...c,id:`choice${i+3}`}))}]],
]) {
 const id=`fixture-${suffix}`;
 await insert('weekly_quiz_rounds',{...round,round_id:id});
 const source={...manifest,round_id:id,items};
 await insert('weekly_quiz_evaluations',{...evaluation,evaluation_id:'weekly_eval_'+(version==='v5'?'3':'4').repeat(32),round_id:id,format_version:`foldarium.weekly-private-evaluation/${version}`});
 await pg.query('select public.reveal_weekly_quiz_round($1,$2,$3)',[id,source,revealSHA]);
 const result=(await pg.query('select reveal_manifest from public.weekly_quiz_rounds where round_id=$1',[id])).rows[0].reveal_manifest;
 assert.equal(result.items.length,2);
 assert.equal(result.items.flatMap(i=>i.choices).length,4);
 assert.equal(result.items.filter(i=>i.evaluation_status!=='unscorable').length,version==='v5'?2:0);
 assert.equal(result.items.flatMap(i=>i.choices).filter(c=>c.correct===true).length,version==='v5'?2:0);
}
assert.equal((await pg.query('select * from public.list_missing_weekly_retrospective_publications()')).rows.length,2);
for(const role of ['anon','authenticated']) {
 assert.equal((await pg.query("select has_function_privilege($1,'public.reveal_weekly_quiz_round(text,jsonb,text)','EXECUTE') as allowed",[role])).rows[0].allowed,false);
 assert.equal((await pg.query("select has_function_privilege($1,'private.foldarium_validate_reveal_item(jsonb)','EXECUTE') as allowed",[role])).rows[0].allowed,false);
 assert.equal((await pg.query("select has_function_privilege($1,'public.list_missing_weekly_retrospective_publications()','EXECUTE') as allowed",[role])).rows[0].allowed,false);
}
await pg.close();
console.log('Unscorable database behavior passed: exact typed proofs/nulls/IDs, mixed full population, v6 catalog+publication, immutable provenance, preserved window/receipt/access guards.');
