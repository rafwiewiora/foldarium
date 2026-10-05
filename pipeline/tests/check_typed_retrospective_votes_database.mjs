// Real migrated PostgreSQL vote provenance; all identities and votes are synthetic.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {createHash,createHmac} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {canonicalJson} from '../../lib/weekly-selector-contract.js';
const {PGlite}=await import(process.argv[2]?pathToFileURL(process.argv[2]).href:'@electric-sql/pglite');
const pg=new PGlite();
// Supabase auth/storage schemas are fixtures. PGlite lacks pgcrypto: native
// PostgreSQL sha256 implements digest; a deterministic fixture-only HMAC seed permits
// migration bootstrap. The HMAC adapter is checked against Node crypto; no
// production key/token security or external authentication is claimed.
await pg.exec(`create schema auth;create schema extensions;create schema storage;
create role anon;create role authenticated;create role service_role bypassrls;
create table auth.users(id uuid primary key);
create table storage.buckets(id text primary key,public boolean not null);
create function auth.uid() returns uuid language sql as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
create function auth.role() returns text language sql as $$select current_setting('request.jwt.claim.role',true)$$;
create function extensions.digest(bytea,text) returns bytea language sql immutable as $$select sha256($1) where $2='sha256'$$;
create function extensions.hmac(data bytea,key bytea,algorithm text) returns bytea language plpgsql immutable as $$
declare k bytea:=key; inner_pad bytea:=decode(repeat('00',64),'hex'); outer_pad bytea:=inner_pad; i integer;
begin
 if algorithm<>'sha256' then raise exception 'unsupported fixture HMAC'; end if;
 if octet_length(k)>64 then k:=sha256(k); end if;
 k:=k||decode(repeat('00',64-octet_length(k)),'hex');
 for i in 0..63 loop
  inner_pad:=set_byte(inner_pad,i,get_byte(k,i)#54);
  outer_pad:=set_byte(outer_pad,i,get_byte(k,i)#92);
 end loop;
 return sha256(outer_pad||sha256(inner_pad||data));
end $$;
create function extensions.gen_random_bytes(integer) returns bytea language sql as $$select decode(repeat('01',$1),'hex')$$;
`);
const migrations=fs.readdirSync('supabase/migrations').filter(f=>f.endsWith('.sql') && f<'20261005040000').sort();
for(const f of migrations){
 let s=fs.readFileSync('supabase/migrations/'+f,'utf8');
 s=s.replace('create extension if not exists pgcrypto with schema extensions;','');
 if(f==='20260808010500_add_named_quiz_research_events.sql')s=s.replace(/do \$\$[\s\S]*?\$\$;/,'');
 try{await pg.exec(s)}catch(e){console.log('FAILED',f,e.message);process.exit(1)}

}

// RFC-compatible HMAC adapter must match Node crypto before testing human RPCs.
assert.equal((await pg.query("select encode(extensions.hmac(convert_to('fixture message','UTF8'),convert_to('fixture key','UTF8'),'sha256'),'hex') as value")).rows[0].value,createHmac('sha256','fixture key').update('fixture message').digest('hex'));
const rpc=async(name,args=[]) => (await pg.query(`select to_jsonb(public.${name}(${args.map((_,i)=>'$'+(i+1)).join(',')})) as value`,args)).rows[0]?.value;
const rid='weekly-typed-scope-fixture';
const fixture=JSON.parse(fs.readFileSync(new URL('../../tests/fixtures/private-evaluation-v6-unscorable.golden.json',import.meta.url)));
const blind={...fixture.blind_manifest,round_id:rid};
const canonical=canonicalJson(blind),digest=createHash('sha256').update(canonical).digest('hex');
const item=blind.items[0],choice=item.choices[0].id;
await pg.exec("set request.jwt.claim.role='service_role'; insert into public.campaigns(campaign_id,name,source,release_date,selection_policy_version) values('fixture','Fixture','fixture','2026-10-03','fixture/v1')");
const times=(await pg.query("select clock_timestamp()-interval '1 day' as opens,clock_timestamp()+interval '1 day' as closes")).rows[0];
await rpc('open_weekly_quiz_round',[rid,'fixture',times.opens,times.closes,canonical,digest,{},'production']);
const user=n=>`11111111-1111-4111-8111-${String(n).padStart(12,'0')}`;
const session=n=>`22222222-2222-4222-8222-${String(n).padStart(12,'0')}`;
const attempt=n=>`33333333-3333-4333-8333-${String(n).padStart(12,'0')}`;
async function vote(n,kind,appState){
 await pg.query('insert into auth.users values($1)',[user(n)]);
 await pg.exec(`set role authenticated;set request.jwt.claim.role='authenticated';set request.jwt.claim.sub='${user(n)}'`);
 await rpc('start_named_weekly_quiz_session',[session(n),rid,`Fixture${n}`,{}]);
 if(kind===null) {
  await rpc('submit_weekly_quiz_vote_attempt',[attempt(n),session(n),rid,item.id,0,choice,false,null,null,appState,null]);
 }else {
  const none=kind==='none';
  await rpc('submit_weekly_quiz_vote_attempt_v2',[attempt(n),session(n),rid,item.id,0,none?null:choice,none,kind,none?null:(kind==='exact'?choice:item.choices[0].cluster_id),null,null,appState]);
 }
 await pg.exec("reset role;set request.jwt.claim.role='service_role';set request.jwt.claim.sub=''");
}
const expected=async()=> (await pg.query('select private.foldarium_expected_weekly_retrospective_source($1) as value',[rid])).rows[0].value;
await vote(1,null,{selection_kind:'exact'});
const before=await expected();assert(before);
await pg.exec(fs.readFileSync(new URL('../../supabase/migrations/20261005040000_use_verified_typed_retrospective_votes.sql',import.meta.url),'utf8'));
assert.deepEqual(await expected(),before,'legacy normalized source must remain byte-equivalent');
const scopes=()=>rpc('get_weekly_retrospective_vote_scopes_v1',[rid]);
assert.deepEqual((await scopes()).votes,[]);
for(const role of ['anon','authenticated']) {
 await pg.exec(`set role ${role};set request.jwt.claim.role='${role}'`);
 await assert.rejects(scopes(),/permission denied/);
 await pg.exec("reset role;set request.jwt.claim.role='service_role'");
}
await vote(2,'exact',null);
await vote(3,'exact',{selection_kind:'cluster'});
await vote(4,'cluster',{selection_kind:'exact'});
await vote(5,'none',null);
let proof=await scopes();assert.equal(proof.blind_manifest_sha256,digest);assert.equal(proof.round_id,rid);
assert.equal(proof.votes.length,4);
const source=await expected();assert(source);
for(const [n,kind] of [[2,'exact'],[3,'exact'],[4,'cluster'],[5,'none']]) {
 assert.equal(source.votes.find(v=>v.participant_link===user(n)).selection_kind,kind);
}
// Valid typed provenance wins; optional contradictory telemetry is not authority.
// Broken typed provenance must never fall back even though app_state is valid.
const original=(await pg.query('select selection_source_metadata from public.weekly_quiz_votes where user_id=$1',[user(3)])).rows[0].selection_source_metadata;
await pg.query('update public.weekly_quiz_votes set selection_source_attempt_id=$1 where user_id=$2',[attempt(2),user(3)]);
assert.equal(await expected(),null);await assert.rejects(scopes(),/provenance is invalid/);
await pg.query('update public.weekly_quiz_votes set selection_source_attempt_id=$1 where user_id=$2',[attempt(3),user(3)]);
await pg.query("update public.weekly_quiz_votes set selection_source_metadata=jsonb_set(selection_source_metadata,'{blind_manifest_sha256}',to_jsonb($1::text)) where user_id=$2",['b'.repeat(64),user(3)]);
assert.equal(await expected(),null);await assert.rejects(scopes(),/provenance is invalid/);
await pg.query('update public.weekly_quiz_votes set selection_source_metadata=$1 where user_id=$2',[original,user(3)]);
// Existing CHECK/audit expressions have SQL three-valued edges; the full
// resolved/total partition must still reject a typed vote with a NULL source.
await pg.query('update public.weekly_quiz_votes set selection_source=null where user_id=$1',[user(3)]);
const audit=(await pg.query('select * from public.check_weekly_quiz_selection_provenance($1)',[rid])).rows[0];
assert.equal(Number(audit.resolved_votes),3);
assert.equal(await expected(),null);await assert.rejects(scopes(),/provenance is invalid/);
await pg.query("update public.weekly_quiz_votes set selection_source='submit_v2' where user_id=$1",[user(3)]);
const submitted=(await pg.query('select submitted_at from public.weekly_quiz_votes where user_id=$1',[user(3)])).rows[0].submitted_at;
await pg.query("update public.weekly_quiz_votes set submitted_at=submitted_at+interval '1 second' where user_id=$1",[user(3)]);
assert.equal(await expected(),null);await assert.rejects(scopes(),/provenance is invalid/);
await pg.query('update public.weekly_quiz_votes set submitted_at=$1 where user_id=$2',[submitted,user(3)]);
// Exercise genuine service-reviewed resolution proof, not a synthetic trusted flag.
const fp=(await pg.query('select private.weekly_quiz_vote_attempt_fingerprint($1) as value',[attempt(1)])).rows[0].value;
const revision=(await pg.query('select selection_revision from public.weekly_quiz_votes where user_id=$1',[user(1)])).rows[0].selection_revision;
const resolution='44444444-4444-4444-8444-444444444444';
await rpc('resolve_weekly_quiz_vote_selection',[resolution,attempt(1),'exact',choice,'c'.repeat(64),{},'fixture-actor','fixture-reviewer','Synthetic scope resolution',revision,fp,null]);
assert.equal((await scopes()).votes.find(v=>v.user_id===user(1)).selection_source,'resolution');
assert.deepEqual(await expected(),source,'resolved identical scope cannot reclassify canonical historical source');
const resolved=(await pg.query('select selection_source_metadata from public.weekly_quiz_votes where user_id=$1',[user(1)])).rows[0].selection_source_metadata;
await pg.query("update public.weekly_quiz_votes set selection_source_metadata=jsonb_set(selection_source_metadata,'{evidence_sha256}',to_jsonb($1::text)) where user_id=$2",['d'.repeat(64),user(1)]);
assert.equal(await expected(),null);await assert.rejects(scopes(),/provenance is invalid/);
await pg.query('update public.weekly_quiz_votes set selection_source_metadata=$1 where user_id=$2',[resolved,user(1)]);
assert.deepEqual(await expected(),source);
await pg.close();console.log('Typed retrospective scopes: real v2 votes, immutable proof guards, legacy source parity and private ACLs passed.');
