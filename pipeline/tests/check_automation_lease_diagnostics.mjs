// Full migration chain, synthetic control rows; no external network or execution.
import assert from 'node:assert/strict';
import {bootstrapAcceptanceDatabase,denyExternalNetwork} from './acceptance_database.mjs';
denyExternalNetwork();
const {pg,migrations}=await bootstrapAcceptanceDatabase(process.argv[2]);
await pg.exec("set request.jwt.claim.role='service_role'");
const rpc=async(name,args=[]) => (await pg.query(`select public.${name}(${args.map((_,i)=>'$'+(i+1)).join(',')}) value`,args)).rows[0].value;
const action={action_key:'a'.repeat(64),kind:'intake',identity:'wwpdb-2026-10-03',parameters:{secret_fixture:'never expose'},gate:'intake'};
await rpc('enqueue_weekly_automation_action_v1',[action]);
await rpc('claim_weekly_automation_action_v1',[action.action_key]);
// Durable intake succeeded, but the caller died before acknowledging completion.
await pg.query("insert into public.campaigns(campaign_id,name,source,release_date,selection_policy_version) values('wwpdb-2026-10-03','Fixture','fixture','2026-10-03','fixture/v1')");
await pg.query("insert into public.prerelease_snapshots(snapshot_id,campaign_id,release_date,plan_sha256,files) values('fixture','wwpdb-2026-10-03','2026-10-03',$1,'{}')",['b'.repeat(64)]);
await pg.query("update private.weekly_automation_actions set lease_until=clock_timestamp()-interval '1 hour',last_error='private error detail' where action_key=$1",[action.action_key]);
const dump=async()=> (await pg.query('select to_jsonb(a) value from private.weekly_automation_actions a order by action_key')).rows.map(r=>r.value);
const before=await dump();
let snapshot=await rpc('weekly_automation_snapshot_v1');
assert.deepEqual(await dump(),before); // no resets, terminal claims, or counter changes
assert.equal(snapshot.campaigns[0].campaign_id,action.identity);
assert.deepEqual(snapshot.failed_actions,[]);
assert.equal(snapshot.expired_running_actions_count,1);
assert.equal(snapshot.expired_running_actions_truncated,false);
const diagnostic=snapshot.expired_running_actions[0];
assert.deepEqual(Object.keys(diagnostic).sort(),['action_key','kind','identity','attempt_count','lease_until','diagnostic'].sort());
assert.equal(diagnostic.action_key,action.action_key);
assert.equal(diagnostic.attempt_count,1);
assert.equal(diagnostic.diagnostic,'expired-lease-unresolved');
assert.ok(!JSON.stringify(diagnostic).includes('private error'));
assert.ok(!JSON.stringify(diagnostic).includes('secret_fixture'));
const safeSnapshot=await rpc('weekly_automation_snapshot_v1');
assert.deepEqual(safeSnapshot.expired_running_actions,snapshot.expired_running_actions);
assert.deepEqual(await dump(),before);
// Active leases and terminal rows must not be reported as expired-running.
await pg.query("insert into private.weekly_automation_actions(action_key,action,status,attempt_count,lease_token,lease_until) values($1,$2,'running',1,gen_random_uuid(),clock_timestamp()+interval '1 hour')",['c'.repeat(64),action]);
await pg.query("insert into private.weekly_automation_actions(action_key,action,status,attempt_count) values($1,$2,'succeeded',1)",['d'.repeat(64),action]);
// Cap the diagnostic detail list while preserving the exact total.
await pg.query("insert into private.weekly_automation_actions(action_key,action,status,attempt_count,lease_token,lease_until) select lpad(to_hex(i),64,'0'),$1,'running',2,gen_random_uuid(),clock_timestamp()-interval '1 hour' from generate_series(1,105) i",[action]);
const boundedBefore=await dump();
snapshot=await rpc('weekly_automation_snapshot_v1');
assert.equal(snapshot.expired_running_actions_count,106);
assert.equal(snapshot.expired_running_actions.length,100);
assert.equal(snapshot.expired_running_actions_truncated,true);
assert.deepEqual(await dump(),boundedBefore);
for(const role of ['anon','authenticated']){
 await pg.exec(`set role ${role};set request.jwt.claim.role='${role}'`);
 await assert.rejects(rpc('weekly_automation_snapshot_v1'),/permission denied/);
 await assert.rejects(pg.query('select private.weekly_automation_snapshot_before_lease_diagnostics_v1()'),/permission denied/);
 await pg.exec('reset role');
}
// Independent reviewer: exact SQL text parity of all old payload fields, ACLs,
// and unchanged serialized row values across actual service-role invocation.
await pg.exec("set request.jwt.claim.role='service_role'");
const rawBefore=(await pg.query("select jsonb_agg(to_jsonb(a) order by action_key)::text value from private.weekly_automation_actions a")).rows[0].value;
const oldSnapshot=(await pg.query('select private.weekly_automation_snapshot_before_lease_diagnostics_v1() value')).rows[0].value;
const newSnapshot=await rpc('weekly_automation_snapshot_v1');
for(const k of ['expired_running_actions','expired_running_actions_count','expired_running_actions_truncated','lease_diagnostics_observed_at'])delete newSnapshot[k];
assert.deepEqual(newSnapshot,oldSnapshot);
for(const role of ['anon','authenticated','service_role']){
 const grants=(await pg.query("select has_function_privilege($1,'public.weekly_automation_snapshot_v1()','EXECUTE') public_exec, has_function_privilege($1,'private.weekly_automation_snapshot_before_lease_diagnostics_v1()','EXECUTE') private_exec",[role])).rows[0];
 assert.equal(grants.public_exec,role==='service_role');assert.equal(grants.private_exec,false);
}
await pg.exec("set role service_role;set request.jwt.claim.role='service_role'");
assert.equal((await rpc('weekly_automation_snapshot_v1')).expired_running_actions_count,106);
await assert.rejects(pg.query('select private.weekly_automation_snapshot_before_lease_diagnostics_v1()'),/permission denied/);
await pg.exec('reset role');
assert.equal((await pg.query("select jsonb_agg(to_jsonb(a) order by action_key)::text value from private.weekly_automation_actions a")).rows[0].value,rawBefore);
await pg.close();
console.log(JSON.stringify({status:'passed',migrations:migrations.length,diagnostics_read_only:true,expired_detail_limit:100,rows_preserved:108}));
