// Real PostgreSQL behavior; pass the installed PGlite module path.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const {PGlite}=await import(pathToFileURL(process.argv[2]).href);
const pg=new PGlite();
await pg.exec(`create role anon;create role authenticated;create role service_role;create schema private;
create table public.weekly_quiz_rounds(round_id text primary key,campaign_id text,status text,opens_at timestamptz,closes_at timestamptz,blind_manifest jsonb,blind_manifest_sha256 text,item_count integer,opened_at timestamptz,metadata jsonb,reveal_manifest jsonb,revealed_at timestamptz);`);
for(const name of ['20260808010600_allow_weekly_pose_metrics.sql','20260808010800_allow_weekly_hbond_metric.sql','20261004240000_allow_unavailable_prolif_metric.sql']) {
 await pg.exec(fs.readFileSync(new URL('../../supabase/migrations/'+name,import.meta.url),'utf8'));
}
const marker={metric:'prolif_hbond_residue_count',value:null,policy:'prolif-implicit-hbond-unique-protein-residue/v2',status:'unavailable',availability_policy:'foldarium.prolif-availability/v1',reason:'unsupported_receptor_residue',unsupported_residues:['UNK']};
let index=0;
async function open(metric,{choiceExtra={},itemExtra={},closes='2026-10-07T00:00:00Z'}={}) {
 const round='test-'+index++;
 const blind={schema_version:1,round_id:round,items:[{id:'item',...itemExtra,choices:[{id:'choice',method:'openfold3',interaction_count:metric,...choiceExtra}]}]};
 return (await pg.query('select (public.open_weekly_quiz_round($1,$2,$3,$4,$5,$6,$7,$8)).*',[round,'campaign','2026-10-03T00:00:00Z',closes,blind,'a'.repeat(64),{},'preview'])).rows[0];
}
await pg.exec('set role service_role');
const row=await open(marker);assert.deepEqual(row.blind_manifest.items[0].choices[0].interaction_count,marker);assert.equal(row.item_count,1);
for(const metric of ['prolif_hbond_residue_count','prolif_unique_residue_interaction_type']) {
 assert.equal((await open({metric,value:0,policy:'legacy/v1'})).blind_manifest.items[0].choices[0].interaction_count.value,0);
}
for(const bad of [
 {metric:marker.metric,value:null,policy:marker.policy},
 {...marker,value:0},{...marker,value:-1},{...marker,status:'failed'},
 {...marker,availability_policy:'future'},{...marker,policy:'unknown'},
 {...marker,reason:'arbitrary'},{...marker,unsupported_residues:['MSE']},
 {...marker,residue_position:99},{...marker,unsupported_residues:['UNK','ALA']},
 {metric:marker.metric,value:true,policy:'v1'},
]) await assert.rejects(open(bad),/blind manifest contains invalid/);
await assert.rejects(open(marker,{choiceExtra:{correct:true}}),/blind manifest contains invalid/);
await assert.rejects(open(marker,{itemExtra:{reference:'private'}}),/blind manifest contains invalid/);
await assert.rejects(open(marker,{closes:'2026-10-01T00:00:00Z'}),/voting window/);
await pg.exec('reset role');
assert.equal((await pg.query('select count(*)::int as n from public.weekly_quiz_rounds')).rows[0].n,3);
await pg.exec('set role anon');await assert.rejects(open(marker),/permission denied/);
await pg.exec('reset role');
assert.equal((await pg.query("select has_function_privilege('service_role','private.weekly_interaction_count_is_valid(jsonb)','execute') as allowed")).rows[0].allowed,false);
await pg.close();
console.log('Optional ProLIF SQL: strict null disposition, legacy numeric zero, preserved answer/window guards and service-only publishing passed.');
