// Ttakkari owns the Run; Meta's SDK owns the Muse host and MSP command transport.
import {readFile,writeFile} from 'node:fs/promises';
import {dirname,join} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {homedir} from 'node:os';
import {assertReady,sessionParams,verifySession} from './contract.mjs';
import {prepareHookSettings} from './hook-settings.mjs';

const spec=JSON.parse(await readFile(process.argv[2],'utf8'));
const requireRuntime=createRequire(join(spec.runtime_root,'package.json'));
const {spawnMspConnection}=await import(pathToFileURL(requireRuntime.resolve('@muse-code/sdk')).href);
const emit=(method,params)=>process.stdout.write(JSON.stringify({method,params})+'\n');
const shellQuote=value=>"'"+value.replaceAll("'","'\\''")+"'";
const hooksFile=join(dirname(process.argv[2]),'hooks.json');
const hookCommand=[spec.hook_python,spec.hook_script,'--policy',spec.policy_file].map(shellQuote).join(' ')
  + '; ttakkari_hook_status=$?; if [ "$ttakkari_hook_status" -ne 0 ]; then exit 2; fi';
await writeFile(hooksFile,JSON.stringify({hooks:{PreToolUse:[{hooks:[{type:'command',command:hookCommand,timeout:10}]}]}}),{mode:0o600});
const env={...process.env,MUSE_NO_AUTO_UPDATE:'1',MUSE_LOGIN:'0'};
for(const key of ['META_API_KEY','MUSE_AUTH_PATH','MUSE_TRANSPORT_TRACE'])delete env[key];
env.XDG_CONFIG_HOME=await prepareHookSettings(env.XDG_CONFIG_HOME??join(homedir(),'.config'),dirname(process.argv[2]),hooksFile);
const handshake=spawnMspConnection({command:spec.muse_bin,
  args:['serve','--provider','meta','--contributor-mode','off','--trust-workspace','--disable-request-user-input',
        '-c','run.workflow_trigger_mode="off"'],
  cwd:spec.cwd,env,onStderr:chunk=>process.stderr.write(chunk),shutdownTimeoutMs:1000});
let host,sessionId,turnId,active=false,terminalResolve;
const terminals=new Map();
const terminal=new Promise(resolve=>terminalResolve=resolve);
let stopping=false;
const cancel=async()=>{
  if(stopping)return;
  stopping=true;
  if(host&&turnId){
    await Promise.race([host.connection.command('turn/cancel',{sessionId,turnId},{maxAttempts:1}).catch(()=>{}),
      new Promise(resolve=>setTimeout(resolve,400))]);
  }
  await handshake.close();
};
process.on('SIGTERM',()=>void cancel().finally(()=>process.exit(143)));
process.on('SIGINT',()=>void cancel().finally(()=>process.exit(130)));
try{
  host=await Promise.race([
    handshake.initialize({clientInfo:{name:'ttakkari',version:'0.1.0'},
      capabilities:{experimentalApi:true,userInputDialogs:false,requestedCapabilities:['sessionMcp']}}),
    new Promise((_,reject)=>setTimeout(()=>reject(new Error('Muse initialize timeout')),15000).unref()),
  ]);
  if(host.fingerprintWarning)throw new Error('Muse MSP schema fingerprint differs from the pinned SDK');
  const account=await host.connection.request('account/read',{});
  assertReady(host.initializeResult,account,Object.keys(spec.mcp_servers).length>0);
  const catalog=await host.connection.request('model/list',{});
  // A catalog is discovery, not an entitlement oracle. Subscription-only Full
  // must be allowed to reach the documented explicit model selection surface.
  emit('muse/catalog',{source:catalog.source,modelIds:catalog.models?.map(row=>row.modelId)??[]});
  const start=sessionParams(spec);
  const opened=spec.engine_session_id
    ? await host.connection.command('session/resume',{sessionId:spec.engine_session_id,excludeItems:true,
        ...(start.config?{config:start.config}:{})},{maxAttempts:1})
    : await host.connection.command('session/start',start,{maxAttempts:1});
  sessionId=opened.session.sessionId;
  // Read back authority rather than trusting an admission acknowledgement.
  verifySession(opened.session,spec);
  if(spec.engine_session_id){
    await host.connection.command('session/setApprovalMode',{sessionId,mode:'allowAll'},{maxAttempts:1});
  }
  const hookInventory=await host.connection.request('hook/list',{sessionId});
  if(!hookInventory.hooks?.some(h=>h.enabled&&h.handlerKind==='managed'&&h.event==='preToolUse')){
    throw new Error('Muse managed PreToolUse guard was not installed');
  }
  emit('muse/sessionReady',{sessionId,modelId:opened.session.modelId});
  host.connection.onNotification(frame=>{
    // Subscription quota is account-wide: it has no sessionId. Do not filter
    // it with the session-scoped transcript frames below.
    if(frame.method==='usage/changed'){
      emit(frame.method,frame.params);
      return;
    }
    if(!active||frame.params?.sessionId!==sessionId)return;
    emit(frame.method,frame.params);
    if(frame.method==='view/gap'){
      emit('muse/error',{kind:'viewGap',message:'Muse transcript gap; refusing an incomplete execution record'});
      terminalResolve({terminal:'failed'});
    }
    if(frame.method==='turn/completed'){
      terminals.set(frame.params.turnId,frame.params);
      if(turnId===frame.params.turnId)terminalResolve(frame.params);
    }
  });
  active=true;
  const ack=await host.connection.command('turn/start',{sessionId,
    input:[{type:'text',text:spec.system_note+'\n\n[Task]\n'+spec.prompt}],
    reasoningEffort:spec.effort,workspaceRoots:start.workspaceRoots},{maxAttempts:1});
  turnId=ack.turnId;
  if(terminals.has(turnId))terminalResolve(terminals.get(turnId));
  const outcome=await Promise.race([terminal,host.exited.then(exit=>{throw new Error(`Muse host exited before terminal: ${JSON.stringify(exit)}`);})]);
  const usage=await host.connection.request('usage/read',{});
  if(usage.usage)emit('muse/quota',usage.usage);
  process.exitCode=outcome.terminal==='completed'?0:1;
}catch(error){
  emit('muse/error',{message:error.message,kind:error.kind??error.name});
  process.exitCode=1;
}finally{
  await handshake.close();
}
