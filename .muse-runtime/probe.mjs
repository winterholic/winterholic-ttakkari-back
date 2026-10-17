// Read-only readiness probe through Meta's pinned official SDK. No model call.
import { spawnMspConnection } from '@muse-code/sdk';
import { assertReady } from './contract.mjs';

const museBin = process.argv[2];
if (!museBin) throw new Error('Usage: node .muse-runtime/probe.mjs /absolute/path/to/muse');
const env = {...process.env, MUSE_NO_AUTO_UPDATE:'1', MUSE_LOGIN:'0'};
delete env.META_API_KEY; // Never substitute a metered environment key.
delete env.MUSE_TRANSPORT_TRACE;
const stderr = [];
const handshake = spawnMspConnection({
  command:museBin,
  args:['serve','--contributor-mode','off','--disable-request-user-input'],
  env, onStderr:chunk => stderr.push(chunk), shutdownTimeoutMs:1000,
});
const report = {scope:process.argv[3] ?? 'unspecified', model_call_executed:false,model_access_verified:false};
try {
  const host = await Promise.race([
    handshake.initialize({clientInfo:{name:'ttakkari_readiness',version:'0.1.0'},capabilities:{experimentalApi:true,userInputDialogs:false,requestedCapabilities:['sessionMcp']}}),
    new Promise((_,reject)=>setTimeout(()=>reject(new Error('Muse handshake timed out')),15000).unref()),
  ]);
  const {connection,initializeResult} = host;
  report.initialize = initializeResult;
  report.fingerprint_warning = host.fingerprintWarning ?? null;
  report.account = await connection.request('account/read', {});
  report.catalog = await connection.request('model/list', {});
  report.usage = await connection.request('usage/read', {});
  try { assertReady(initializeResult,report.account,true); report.ready_for_validation=true; }
  catch(error) {report.ready_for_validation=false; report.blocker=error.message;}
  await host.close();
  report.exit=await host.child.exit;
} catch(error) {
  report.ready_for_validation=false;
  report.error={name:error.name,message:error.message};
  report.stderr=stderr.join('').slice(-4000);
} finally {
  await handshake.close();
}
console.log(JSON.stringify(report,null,2));
process.exitCode=report.ready_for_validation ? 0 : 2;
