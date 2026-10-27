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
const migration = '20261027010000_support_missing_reference_bridges.sql';
const migrations=fs.readdirSync('supabase/migrations').filter(f=>f.endsWith('.sql') && f < migration).sort();
for(const f of migrations){
 let s=fs.readFileSync('supabase/migrations/'+f,'utf8');
 s=s.replace('create extension if not exists pgcrypto with schema extensions;','');
 if(f==='20260808010500_add_named_quiz_research_events.sql')s=s.replace(/do \$\$[\s\S]*?\$\$;/,'');
 try{await pg.exec(s)}catch(e){console.log('FAILED',f,e.message);process.exit(1)}

}

// Old v1 proofs remain valid; the original validator must reject the new v2.
const fixture=JSON.parse(fs.readFileSync(new URL('../../tests/fixtures/private-evaluation-v6-unscorable.golden.json',import.meta.url)));
const legacy=structuredClone(fixture.reveal_manifest.items[0]);
const bridge=structuredClone(legacy);
Object.assign(bridge.reference_disposition,{
 policy:'foldarium.released-reference-disposition/v2',code:'missing_reference_bridge',
 expected_heavy_atoms:45,observed_heavy_atoms:38,explicitly_unobserved_heavy_atoms:7,reference_coverage:38/45,
 missing_heavy_atom_ids:['C1','C2','C3','C4','C5','C6','SI1'],observed_component_count:2,
 missing_component_boundary_counts:[2],ccd_heavy_atom_graph_sha256:'a'.repeat(64),
});
const validate=item=>pg.query('select private.foldarium_validate_reveal_item($1)',[item]);
await validate(legacy);
await assert.rejects(validate(bridge),/proof fields/);
await pg.exec(fs.readFileSync('supabase/migrations/'+migration,'utf8'));
await validate(legacy);await validate(bridge);
for(const mutate of [
 d=>d.policy='foldarium.released-reference-disposition/v1',d=>d.code='insufficient_reference_coverage',
 d=>d.missing_heavy_atom_ids[0]='C2',d=>d.missing_heavy_atom_ids.reverse(),
 d=>d.missing_heavy_atom_ids.pop(),d=>d.missing_heavy_atom_ids[0]='',
 d=>d.missing_heavy_atom_ids[0]=true,d=>d.observed_component_count=1,
 d=>d.observed_component_count=39,d=>d.observed_component_count=true,
 d=>d.missing_component_boundary_counts=[1],d=>d.missing_component_boundary_counts=[3],
 d=>d.missing_component_boundary_counts=[2,1],d=>d.missing_component_boundary_counts=[true],
 d=>d.missing_component_boundary_counts=Array(8).fill(2),
 d=>delete d.ccd_heavy_atom_graph_sha256,d=>d.ccd_heavy_atom_graph_sha256='not a hash',
 d=>{d.observed_heavy_atoms=30;d.explicitly_unobserved_heavy_atoms=15;d.reference_coverage=30/45;},
 d=>d.extra='unknown',
]){const invalid=structuredClone(bridge);mutate(invalid.reference_disposition);await assert.rejects(validate(invalid));}
for(const field of ['rmsd','correct','accepted_correct']) {
 const invalid=structuredClone(bridge);invalid.choices[0][field]=field==='rmsd'?0:false;await assert.rejects(validate(invalid));
}
// The complete round and all null outcomes survive the actual reveal RPC.
const blind=structuredClone(fixture.blind_manifest);blind.round_id='bridge-fixture';
const canonical=canonicalJson(blind),digest=createHash('sha256').update(canonical).digest('hex');
await pg.exec("set request.jwt.claim.role='service_role'; insert into public.campaigns(campaign_id,name,source,release_date,selection_policy_version) values('fixture','Fixture','fixture','2026-10-03','fixture/v1')");
await pg.query(`insert into public.weekly_quiz_rounds(round_id,campaign_id,status,environment,opens_at,closes_at,opened_at,item_count,blind_manifest,blind_manifest_sha256)
 values($1,'fixture','open','production','2020-01-01','2020-01-02','2020-01-01',1,$2,$3)`,[blind.round_id,blind,digest]);
const manifest={schema_version:1,round_id:blind.round_id,blind_manifest_sha256:digest,items:[bridge]};
const reveal=()=>pg.query('select public.reveal_weekly_quiz_round($1,$2,$3)',[blind.round_id,manifest,'c'.repeat(64)]);
await reveal();await reveal();
const stored=(await pg.query('select status,reveal_manifest,item_count from public.weekly_quiz_rounds where round_id=$1',[blind.round_id])).rows[0];
assert.equal(stored.status,'revealed');assert.equal(stored.item_count,1);
assert.deepEqual(stored.reveal_manifest.items,[bridge]);
assert.ok(stored.reveal_manifest.items[0].choices.every(c=>c.correct===null&&c.accepted_correct===null&&c.rmsd===null));
for(const role of ['anon','authenticated']){
 assert.equal((await pg.query("select has_function_privilege($1,'private.foldarium_validate_reveal_item(jsonb)','EXECUTE') as allowed",[role])).rows[0].allowed,false);
 assert.equal((await pg.query("select has_function_privilege($1,'public.publish_weekly_historical_preview_v1(text,text,text)','EXECUTE') as allowed",[role])).rows[0].allowed,false);
}
await pg.close();
console.log('Missing-reference-bridge migration passed: complete chain, strict proof/null metrics, v1 compatibility, full reveal population and private ACLs.');
