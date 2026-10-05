// Local PostgreSQL bootstrap shared by composed acceptance scenarios.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import net from 'node:net';
import http from 'node:http';
import https from 'node:https';
import dns from 'node:dns';
import dgram from 'node:dgram';
import {createHmac} from 'node:crypto';
import {pathToFileURL} from 'node:url';
export function denyExternalNetwork(){
const denyNetwork=()=>{throw new Error('acceptance network access is forbidden');};
net.Socket.prototype.connect=denyNetwork;http.request=denyNetwork;https.request=denyNetwork;globalThis.fetch=denyNetwork;
dgram.Socket.prototype.send=denyNetwork;
for(const key of Object.keys(dns))if(key==='lookup'||key==='reverse'||key.startsWith('resolve'))dns[key]=denyNetwork;
for(const key of Object.keys(dns.promises))if(key==='lookup'||key==='reverse'||key.startsWith('resolve'))dns.promises[key]=denyNetwork;
assert.throws(()=>https.request('https://api.anthropic.com'),/network access is forbidden/);
assert.throws(()=>dns.lookup('api.anthropic.com'),/network access is forbidden/);
assert.throws(()=>dgram.createSocket('udp4').send('probe',53,'127.0.0.1'),/network access is forbidden/);
}
export async function bootstrapAcceptanceDatabase(modulePath,directory){
 const {PGlite}=await import(modulePath?pathToFileURL(modulePath).href:'@electric-sql/pglite');
 const pg=new PGlite(directory);
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
const migrations=fs.readdirSync('supabase/migrations').filter(f=>f.endsWith('.sql')).sort();
for(const f of migrations){
 let s=fs.readFileSync('supabase/migrations/'+f,'utf8');
 s=s.replace('create extension if not exists pgcrypto with schema extensions;','');
 if(f==='20260808010500_add_named_quiz_research_events.sql')s=s.replace(/do \$\$[\s\S]*?\$\$;/,'');
 try{await pg.exec(s)}catch(e){console.log('FAILED',f,e.message);process.exit(1)}

}

// RFC-compatible HMAC adapter must match Node crypto before testing human RPCs.
assert.equal((await pg.query("select encode(extensions.hmac(convert_to('fixture message','UTF8'),convert_to('fixture key','UTF8'),'sha256'),'hex') as value")).rows[0].value,createHmac('sha256','fixture key').update('fixture message').digest('hex'));
 return {pg,PGlite,migrations};
}
