// Real complete migration chain; synthetic rows, no network/provider execution.
import assert from 'node:assert/strict';
import {createHash,randomUUID} from 'node:crypto';
import {bootstrapAcceptanceDatabase,denyExternalNetwork} from './acceptance_database.mjs';
denyExternalNetwork();
const {pg,migrations}=await bootstrapAcceptanceDatabase(process.argv[2]);
const hash=s=>createHash('sha256').update(s).digest('hex'),digest='a'.repeat(64);
const rpc=async(name,args=[]) => (await pg.query(`select public.${name}(${args.map((_,i)=>'$'+(i+1)).join(',')}) as value`,args)).rows[0].value;
await pg.exec("set request.jwt.claim.role='service_role'");
const method={driver:'anthropic-api',model_id:'exact-model',config_sha256:'b'.repeat(64),max_cost_usd:10};
const policy={schema:'foldarium.weekly-benchmark-policy/v2',policy_id:'fixture-v2',required_methods:[method],enrollment_scope:{kind:'canonical-production-weekly',first_release_date:'2026-10-03',production_round_suffix:'beta-v2',max_weekly_cost_usd:10}};
const add=async(release,suffix='beta-v2',environment='production',campaignRelease=release)=>{
 const campaign='wwpdb-'+release,rid='weekly-'+release+'-'+suffix;
 await pg.query(`insert into public.campaigns(campaign_id,name,source,release_date,selection_policy_version) values($1,'Fixture','fixture',$2,'fixture/v1') on conflict do nothing`,[campaign,campaignRelease]);
 await pg.query(`insert into public.weekly_quiz_rounds(round_id,campaign_id,environment,status,opens_at,closes_at,opened_at,blind_manifest,blind_manifest_sha256,item_count) values($1,$2,$3,'open',clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 day',clock_timestamp()-interval '1 hour','{"items":[]}',$4,0)`,[rid,campaign,environment,digest]);
 return {rid,campaign,job:{...method,execution_id:randomUUID()}};
};
const args=(r,p=policy,jobs=[r.job],sha=digest)=>{const text=JSON.stringify(p);return [r.rid,r.campaign,sha,jobs,text,hash(text)]};
const freeze=(r,...a)=>rpc('freeze_weekly_automation_policy_v2',args(r,...a));
const current=await add('2026-10-03');
await assert.rejects(freeze(current,policy,[current.job],'c'.repeat(64)),/outside canonical/);
const tampered=args(current);tampered[5]='c'.repeat(64);
await assert.rejects(rpc('freeze_weekly_automation_policy_v2',tampered),/digest mismatch/);
await assert.rejects(freeze(current,policy,[{...current.job,max_cost_usd:11}]),/method or execution mismatch/);
const over=structuredClone(policy);over.required_methods.push({...method,model_id:'other'});
await assert.rejects(freeze(current,over,[current.job,{...current.job,model_id:'other',execution_id:randomUUID()}]),/cap exceeded/);
const empty=structuredClone(policy);empty.required_methods=[];
await assert.rejects(freeze(current,empty,[]),/invalid scoped benchmark policy/);
assert.equal((await freeze(current)).idempotent,false);
assert.equal((await freeze(current)).idempotent,true);
await pg.query(`update public.weekly_quiz_rounds set closes_at=clock_timestamp()-interval '1 minute' where round_id=$1`,[current.rid]);
assert.equal((await freeze(current)).idempotent,true); // exact lost-ACK replay after close
const changed=structuredClone(policy);changed.policy_id='other';
await assert.rejects(freeze(current,changed),/immutable/);
const sibling=await add('2026-10-03','beta-v3');
await assert.rejects(freeze(sibling),/outside canonical/);
const changedSuffix=structuredClone(policy);changedSuffix.enrollment_scope.production_round_suffix='beta-v3';
await assert.rejects(freeze(sibling,changedSuffix),/immutable/); // cannot reset weekly authority with a new scope/hash
const expired=await add('2026-10-10');
await pg.query(`update public.weekly_quiz_rounds set closes_at=clock_timestamp()-interval '1 minute' where round_id=$1`,[expired.rid]);
await assert.rejects(freeze(expired),/current open voting window/);
const preview=await add('2026-10-17','beta-v2','preview');
await assert.rejects(freeze(preview),/outside canonical/);
const old=await add('2026-09-26');await assert.rejects(freeze(old),/outside canonical/);
const badDate=await add('2026-10-24','beta-v2','production','2026-10-25');await assert.rejects(freeze(badDate),/outside canonical/);
const unstarted=await add('2026-10-31');
await pg.query(`update public.weekly_quiz_rounds set opens_at=clock_timestamp()+interval '1 hour' where round_id=$1`,[unstarted.rid]);
await assert.rejects(freeze(unstarted),/current open voting window/);
const canonical=await add('2026-11-07'),archived=await add('2026-11-07','historical','preview');
await rpc('freeze_weekly_automation_policy_v1',[archived.rid,[archived.job],null]);
await assert.rejects(freeze(canonical),/already has frozen benchmark authority/);
assert.equal((await rpc('freeze_weekly_automation_policy_v1',[archived.rid,[archived.job],null])).idempotent,true);
const future=await add('2026-11-14');assert.equal((await freeze(future)).idempotent,false);
assert.equal((await pg.query('select count(*)::int as n from private.weekly_automation_scoped_enrollments')).rows[0].n,2);
assert.equal((await pg.query('select count(*)::int as n from private.weekly_automation_round_policies')).rows[0].n,3);
await assert.rejects(pg.query('delete from private.weekly_automation_scoped_enrollments'),/immutable/);
for(const role of ['anon','authenticated']){
 await pg.exec(`set role ${role};set request.jwt.claim.role='${role}'`);
 await assert.rejects(freeze(future),/permission denied/);
 await assert.rejects(pg.query('select * from private.weekly_automation_scoped_enrollments'),/permission denied/);
 await pg.exec('reset role');
}
await pg.exec("set role service_role;set request.jwt.claim.role='service_role'");
assert.equal((await freeze(future)).idempotent,true);
await assert.rejects(pg.query('delete from private.weekly_automation_scoped_enrollments'),/permission denied/);
await pg.close();
console.log(JSON.stringify({passed:true,migrations:migrations.length,scoped_enrollments:2,preserved_legacy:1,provider_requests:0}));
