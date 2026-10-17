// Read-only readiness probe through Meta's pinned official SDK. No model call.
import {createRequire} from 'node:module';
import {dirname,join} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import { assertReady } from './contract.mjs';

const museBin = process.argv[2];
const usageOnly = process.argv[5] === "--usage-only";
const runtimeRoot=process.argv[4]??dirname(fileURLToPath(import.meta.url));
const requireRuntime=createRequire(join(runtimeRoot,'package.json'));
const {spawnMspConnection}=await import(pathToFileURL(requireRuntime.resolve('@muse-code/sdk')).href);
if (!museBin) throw new Error('Usage: node .muse-runtime/probe.mjs /absolute/path/to/muse');
const env = {...process.env, MUSE_NO_AUTO_UPDATE:'1', MUSE_LOGIN:'0'};
delete env.META_API_KEY; // Never substitute a metered environment key.
delete env.MUSE_AUTH_PATH; // Never inherit an authentication-store override.
delete env.MUSE_TRANSPORT_TRACE;
const stderr = [];
const handshake = spawnMspConnection({
  command:museBin,
  args:['serve','--contributor-mode','off','--disable-request-user-input'],
  env, onStderr:chunk => stderr.push(chunk), shutdownTimeoutMs:1000,
});
process.once('SIGTERM',()=>void handshake.close().finally(()=>process.exit(143)));
process.once('SIGINT',()=>void handshake.close().finally(()=>process.exit(130)));
const report = {mode:usageOnly ? 'usage_only' : 'readiness', scope:process.argv[3] ?? 'unspecified', model_call_executed:false,model_access_verified:false};
try {
  const host = await Promise.race([
    handshake.initialize({clientInfo:{name:'ttakkari_readiness',version:'0.1.0'},capabilities:{experimentalApi:true,userInputDialogs:false,requestedCapabilities:['sessionMcp']}}),
    new Promise((_,reject)=>setTimeout(()=>reject(new Error('Muse handshake timed out')),15000).unref()),
  ]);
  const {connection,initializeResult} = host;
  report.initialize = initializeResult;
  report.fingerprint_warning = host.fingerprintWarning ?? null;
  report.account = await connection.request('account/read', {});
  if (!usageOnly) report.catalog = await connection.request('model/list', {});
  report.usage = await connection.request('usage/read', {});
  if (!usageOnly) {
    try { assertReady(initializeResult,report.account,true); report.ready_for_validation=true; }
    catch(error) {report.ready_for_validation=false; report.blocker=error.message;}
  }
  report.read_only_lookup_succeeded = true;
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
process.exitCode=(usageOnly ? report.read_only_lookup_succeeded : report.ready_for_validation) ? 0 : 2;
