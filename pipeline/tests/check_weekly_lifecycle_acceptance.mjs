// Local composed-migration/artifact acceptance; no network or inference services.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import {execFileSync,spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import net from 'node:net';
import http from 'node:http';
import https from 'node:https';
import dns from 'node:dns';
import dgram from 'node:dgram';
import {createHash,createHmac} from 'node:crypto';
const denyNetwork=()=>{throw new Error('acceptance network access is forbidden');};
net.Socket.prototype.connect=denyNetwork;http.request=denyNetwork;https.request=denyNetwork;globalThis.fetch=denyNetwork;
dgram.Socket.prototype.send=denyNetwork;
for(const key of Object.keys(dns))if(key==='lookup'||key==='reverse'||key.startsWith('resolve'))dns[key]=denyNetwork;
for(const key of Object.keys(dns.promises))if(key==='lookup'||key==='reverse'||key.startsWith('resolve'))dns.promises[key]=denyNetwork;
assert.throws(()=>https.request('https://api.anthropic.com'),/network access is forbidden/);
assert.throws(()=>dns.lookup('api.anthropic.com'),/network access is forbidden/);
assert.throws(()=>dgram.createSocket('udp4').send('probe',53,'127.0.0.1'),/network access is forbidden/);
import {pathToFileURL} from 'node:url';
const { verifyPublicationCatalogRow,verifyEvaluationAndRound,verifySourceSnapshot,verifyPublicArtifact,buildPublicDetail,assertResponseSafe,EVALUATION_SELECT_FIELDS,ROUND_SELECT_FIELDS } = await import('../../lib/weekly-retrospectives.js');
const {buildPublishedMethodStats}=await import('../../method-performance.js');
const {verifyFeaturedSelection,buildFeaturedResults}=await import('../../lib/weekly-featured-results.js');
const {PGlite}=await import(process.argv[2]?pathToFileURL(process.argv[2]).href:'@electric-sql/pglite');
const python=process.argv[3]||'python3';
const root=fs.mkdtempSync(path.join(os.tmpdir(),'foldarium-lifecycle-acceptance-'));
const childEnv={PATH:process.env.PATH,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:'pipeline/src:pipeline/tests'};
const phase=name=>execFileSync(python,['pipeline/tests/lifecycle_acceptance_artifacts.py',name,root],{env:childEnv,stdio:['ignore','pipe','pipe'],timeout:120000});
const read=name=>JSON.parse(fs.readFileSync(path.join(root,name),'utf8'));
const write=(name,value)=>fs.writeFileSync(path.join(root,name),JSON.stringify(value));
let pg=new PGlite(path.join(root,'postgres'));
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
const rpc=async(name,args=[]) => (await pg.query(`select to_jsonb(public.${name}(${args.map((_,i)=>'$'+(i+1)).join(',')})) as value`,args)).rows[0]?.value;
const production=async()=> (await pg.query("select row_to_json(r) as value from public.weekly_quiz_rounds r where round_id='weekly-acceptance'")).rows[0].value;
const planned=async()=>{write('snapshot.json',await rpc('weekly_automation_snapshot_v1'));phase('plan');return read('planned.json');};
const closeWindow=async()=>{
 // Test-clock transition: shorten the still-open fixture window after all blind
 // work completes; no evaluation exists yet, and every frozen draw predates it.
 await pg.exec("update public.weekly_quiz_rounds set closes_at=clock_timestamp() where round_id='weekly-acceptance'");
};
phase('prepare');const p=read('prepared.json'),rid=p.blind.round_id;
await pg.exec("set request.jwt.claim.role='service_role'; insert into storage.buckets values('fixture-private',false),('fixture-public',true)");
await pg.query("insert into public.campaigns(campaign_id,name,source,release_date,selection_policy_version) values('fixture-campaign','Acceptance','fixture','2026-10-03','fixture/v1')");
const times=(await pg.query("select clock_timestamp()-interval '1 day' as opens, clock_timestamp()+interval '1 day' as closes")).rows[0];
await rpc('open_weekly_quiz_round',[p.preview.round_id,'fixture-campaign',times.opens,times.closes,p.preview_canonical,p.preview_sha256,{private_index:p.preview_private_artifact},'preview']);
const metadata={private_index:p.private_artifact,promoted_from_round_id:p.preview.round_id,promoted_from_blind_manifest_sha256:p.preview_sha256,private_canary:'DO_NOT_EXPOSE_ANSWERS'};
await rpc('open_weekly_quiz_round',[rid,'fixture-campaign',times.opens,times.closes,p.blind_canonical,p.blind_sha256,metadata,'production']);
assert.equal((await production()).item_count,6);
const d=p.kit_descriptor;
await rpc('register_weekly_selector_kit',[rid,p.kit.kit_sha256,p.blind_sha256,6,fs.statSync(path.join(root,'kit.zip')).size,'fixture-public/kit.zip',d]);
const freeze=[rid,p.featured,p.draw_canonical,p.draw_artifact];
await rpc('register_weekly_featured_questions',freeze);await rpc('register_weekly_featured_questions',freeze);
await assert.rejects(rpc('register_weekly_featured_questions',[rid,{...p.featured,item_ids:p.featured.item_ids.slice().reverse()},p.draw_canonical,p.draw_artifact]),/public marker differs from its private audit/);
await rpc('freeze_weekly_automation_policy_v1',[rid,[p.job],'a'.repeat(64)]);
await assert.rejects(rpc('freeze_weekly_automation_policy_v1',[rid,[{...p.job,model_id:'changed-model'}],'a'.repeat(64)]),/immutable/);
await pg.exec("set role anon;set request.jwt.claim.role='anon'");
const publicBefore=await rpc('get_current_weekly_quiz_round',['production']);
assert.equal(publicBefore.reveal_manifest,null);assert.equal(publicBefore.item_count,6);
assert.equal(publicBefore.featured_questions.item_ids.length,5);
assert.deepEqual(publicBefore.featured_questions,p.featured);
assert.equal(JSON.stringify(publicBefore).includes('DO_NOT_EXPOSE_ANSWERS'),false);
await assert.rejects(pg.query('select * from public.weekly_quiz_evaluations'),/permission denied/);
await assert.rejects(pg.query('select * from private.weekly_automation_benchmarks'),/permission denied/);
await pg.exec("reset role;set request.jwt.claim.role='service_role'");
const humanId='11111111-1111-4111-8111-111111111111',sessionId='22222222-2222-4222-8222-222222222222';
await pg.query('insert into auth.users(id) values($1)',[humanId]);
await pg.exec(`set role authenticated;set request.jwt.claim.role='authenticated';set request.jwt.claim.sub='${humanId}'`);
await rpc('start_named_weekly_quiz_session',[sessionId,rid,'AcceptanceHuman',{}]);
for(const [index,itemId] of p.featured.item_ids.entries()){
 const item=p.blind.items.find(item=>item.id===itemId),choice=item.choices[0].id;
 const attempt=`33333333-3333-4333-8333-${String(index+1).padStart(12,'0')}`;
 await rpc('submit_weekly_quiz_vote_attempt_v2',[attempt,sessionId,rid,itemId,p.blind.items.indexOf(item),choice,false,'exact',choice,null,null,index===0?{selection_kind:'cluster'}:null]);
 await rpc('submit_weekly_quiz_vote_attempt_v2',[attempt,sessionId,rid,itemId,p.blind.items.indexOf(item),choice,false,'exact',choice,null,null,index===0?{selection_kind:'cluster'}:null]);
}
await pg.exec("reset role;set request.jwt.claim.role='service_role';set request.jwt.claim.sub=''");
assert.equal((await pg.query('select count(*)::integer as n from public.weekly_quiz_votes')).rows[0].n,5);
assert.equal((await pg.query('select count(*)::integer as n from public.weekly_quiz_vote_attempts')).rows[0].n,5);
const budgetGrants=[];
const phaseWithDatabase=name=>new Promise((resolve,reject)=>{
 const child=spawn(python,['pipeline/tests/lifecycle_acceptance_artifacts.py',name,root],{env:childEnv,stdio:['pipe','pipe','pipe']});
 const deadline=setTimeout(()=>{child.kill('SIGKILL');reject(new Error(`${name} exceeded 120-second deadline`));},120000);
 let stderr='';child.stderr.on('data',data=>stderr+=data);child.on('error',reject);
 child.on('exit',code=>{clearTimeout(deadline);code===0?resolve():reject(new Error(stderr||`${name} helper exited ${code}`));});
 const lines=createInterface({input:child.stdout});
 lines.on('line',async line=>{
  try{
   const message=JSON.parse(line);let value;
   if(message.restart_database){await pg.close();pg=new PGlite(path.join(root,'postgres'));await pg.exec("set request.jwt.claim.role='service_role'");value=true;}
   else if(message.source_rows){
    assert(['weekly_quiz_votes','weekly_quiz_vote_attempts','weekly_quiz_sessions','weekly_retrospective_automated_identities'].includes(message.source_rows));
    const fields=message.query.select[0].split(',');assert(fields.every(k=>/^[a-z_]+$/.test(k)));
    const filter=message.query.round_id?.[0];assert(filter===undefined||filter===`eq.${rid}`);
    const ordering=message.query.order[0].split(',').map(value=>{const [field,direction]=value.split('.');assert(/^[a-z_]+$/.test(field)&&direction==='asc');return `${field} asc`;});
    value=(await pg.query(`select ${fields.join(',')} from public.${message.source_rows}${filter?' where round_id=$1':''} order by ${ordering.join(',')}`,filter?[rid]:[])).rows;
   }else if(message.read){
    assert(['round','evaluation'].includes(message.read));
    const table=message.read==='round'?'weekly_quiz_rounds':'weekly_quiz_evaluations';
    value=(await pg.query(`select row_to_json(r) as value from public.${table} r where round_id=$1`,[message.round_id])).rows[0]?.value;
   }else{
    assert(['weekly_automation_snapshot_v1','claim_weekly_automation_inference_start_v1','register_weekly_automation_artifact_v1','register_weekly_selector_benchmark_v1','verify_weekly_automation_benchmark_v1','reveal_weekly_automation_round_v1','get_weekly_selector_benchmarks_v1','get_weekly_retrospective_benchmark_authorizations_v1','get_weekly_retrospective_vote_scopes_v1'].includes(message.rpc));
    const fields=Object.keys(message.payload);assert(fields.every(k=>/^p_[a-z0-9_]+$/.test(k)));
    const result=await pg.query(`select to_jsonb(public.${message.rpc}(${fields.map((k,i)=>`${k}=>$${i+1}`).join(',')})) as value`,fields.map(k=>message.payload[k]));
    value=message.rpc==='get_weekly_selector_benchmarks_v1'?result.rows.map(row=>row.value):result.rows[0].value;
   }
   if(message.rpc==='claim_weekly_automation_inference_start_v1')budgetGrants.push(value.first_claim);
   child.stdin.write(JSON.stringify({value})+'\n');
  }catch(error){child.stdin.write(JSON.stringify({error:error.message})+'\n');}
 });
});
await phaseWithDatabase('infer');
const inference=read('inferred.json');
assert.deepEqual(budgetGrants,[true,false,false]);
assert.equal(inference.provider_calls,6);
const reg=[inference.execution,inference.execution_sha256,inference.payload_digest];
await assert.rejects(rpc('register_weekly_selector_benchmark_v1',reg),/closed and unrevealed/);
await rpc('register_weekly_automation_artifact_v1',[p.job.execution_id,inference.artifact.object_uri,inference.artifact.sha256]);
await rpc('register_weekly_automation_artifact_v1',[p.job.execution_id,inference.artifact.object_uri,inference.artifact.sha256]);
await assert.rejects(rpc('verify_weekly_automation_benchmark_v1',[p.job.execution_id,'b'.repeat(64),inference.execution_sha256,inference.payload_digest]),/artifact binding differs/);
await closeWindow();
let plan=await planned();assert(plan.actions.some(a=>a.kind==='submit_benchmark'));assert(!plan.actions.some(a=>a.kind==='reveal'&&a.identity===rid));
await phaseWithDatabase('ingest');
fs.writeFileSync(path.join(root,'round.json'),(await pg.query("select row_to_json(r)::text as value from public.weekly_quiz_rounds r where round_id='weekly-acceptance'")).rows[0].value);phase('evaluate');const evaluation=read('evaluated.json');
const fields=Object.keys(evaluation.descriptor);
await pg.query(`insert into public.weekly_quiz_evaluations(${fields.join(',')}) values(${fields.map((_,i)=>'$'+(i+1)).join(',')})`,fields.map(k=>evaluation.descriptor[k]));
plan=await planned();assert(plan.actions.some(a=>a.kind==='reveal'&&a.identity===rid));
const revealArgs=[rid,evaluation.descriptor.evaluation_id,evaluation.reveal_canonical,evaluation.descriptor.reveal_manifest_sha256];
const truncated=JSON.stringify({...evaluation.reveal,items:evaluation.reveal.items.slice(0,5)});
await assert.rejects(rpc('reveal_weekly_quiz_round',[rid,truncated,createHash('sha256').update(truncated).digest('hex')]),/item|manifest|population|identity/i);
assert.equal((await production()).status,'open');
const exactClose=(await production()).closes_at;
await pg.exec("update public.weekly_quiz_rounds set closes_at=closes_at+interval '1 millisecond' where round_id='weekly-acceptance'");
await assert.rejects(rpc('reveal_weekly_automation_round_v1',revealArgs),/source\/window/);
await pg.query("update public.weekly_quiz_rounds set closes_at=$1 where round_id='weekly-acceptance'",[exactClose]);
await phaseWithDatabase('reveal');
await assert.rejects(rpc('reveal_weekly_automation_round_v1',revealArgs),/source\/window/);
assert.equal((await production()).status,'revealed');
fs.writeFileSync(path.join(root,'round.json'),(await pg.query("select row_to_json(r)::text as value from public.weekly_quiz_rounds r where round_id='weekly-acceptance'")).rows[0].value);
write('source.json',(await pg.query("select private.foldarium_expected_weekly_retrospective_source($1) as source",[rid])).rows[0].source);
await phaseWithDatabase('archive');const archive=read('archived.json');
await rpc('register_weekly_retrospective_publication',[archive.descriptor,archive.source_canonical]);
await rpc('register_weekly_retrospective_publication',[archive.descriptor,archive.source_canonical]);
assert.equal((await pg.query('select count(*)::integer as count from public.weekly_retrospective_publications')).rows[0].count,1);
plan=await planned();assert(!plan.actions.some(a=>a.identity===rid));
const publicArchive=read('public-archive.json');
for(const forbidden of ['DO_NOT_EXPOSE_ANSWERS','execution_id','config_sha256','payload_digest','participant_link','object_uri'])assert.equal(JSON.stringify(publicArchive).includes(forbidden),false);
const publication=verifyPublicationCatalogRow((await pg.query('select row_to_json(p) as value from public.weekly_retrospective_publications p')).rows[0].value);
const pick=(row,fields)=>Object.fromEntries(fields.split(',').map(k=>[k,row[k]]));
const context=verifyEvaluationAndRound({publication,evaluationRow:pick(evaluation.descriptor,EVALUATION_SELECT_FIELDS),evaluationBytes:fs.readFileSync(path.join(root,'evaluation.json')),roundRow:pick(await production(),ROUND_SELECT_FIELDS),assetOrigin:'https://assets.example.test'});
const sourceSnapshot=verifySourceSnapshot(Buffer.from(archive.source_canonical),publication);
const artifactProjection=verifyPublicArtifact(fs.readFileSync(path.join(root,'public-archive.json')),publication,sourceSnapshot);
const detail=buildPublicDetail({publication,context,publicArtifact:artifactProjection});
assertResponseSafe(detail);assert.equal(detail.blind_manifest.items.length,6);
const featuredRow=await rpc('get_weekly_featured_cohort_source_v1',[rid]);
const selection=verifyFeaturedSelection(featuredRow,Buffer.from(p.draw_canonical),publication,context);
assert.equal(selection.itemIds.length,5);
assert.throws(()=>verifyFeaturedSelection({...featuredRow,featured_questions:{...featuredRow.featured_questions,item_ids:featuredRow.featured_questions.item_ids.slice().reverse()}},Buffer.from(p.draw_canonical),publication,context));
const scores=buildFeaturedResults({publication,context,sourceSnapshot,publicArtifact:artifactProjection,selection});
assertResponseSafe(scores);assert.equal(scores.assignment_total,5);assert.equal(scores.full_round_item_count,6);
assert.equal(scores.participants.find(row=>row.participant_kind==='llm').full_round_answered,6);
const humanScore=scores.participants.find(row=>row.participant_kind==='human');
assert.equal(humanScore.assignment_complete,true);assert.equal(humanScore.assignment_answered,5);assert.equal(humanScore.full_round_answered,5);
assert.equal(humanScore.correct,5);assert.equal(humanScore.total,5);
const methods=buildPublishedMethodStats([context]);assert.equal(methods.publication_count,1);assert.equal(methods.weeks.length,2);assert(methods.weeks.every(row=>row.targets===6));
write('method-benchmark.json',methods);
write('public-detail.json',detail);write('featured-results.json',scores);
console.log(JSON.stringify({status:'passed',migrations:migrations.length,full_items:6,featured_items:5,fake_provider_requests:inference.provider_calls,artifacts:root}));
await pg.close();
