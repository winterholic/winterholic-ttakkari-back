import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,lstat,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {prepareHookSettings} from './hook-settings.mjs';

test('private settings install the managed hook and preserve original config paths',async()=>{
  const root=await mkdtemp(join(tmpdir(),'ttakkari-hook-settings-test-'));
  try {
    const source=join(root,'source');
    const privateRoot=join(root,'private');
    await mkdir(join(source,'muse'),{recursive:true});
    await mkdir(privateRoot,{mode:0o700});
    const original=JSON.stringify({schema_version:1,tui:{terminal_background:'dark'}});
    await writeFile(join(source,'muse/settings.json'),original);
    await writeFile(join(source,'muse/user-hook.sh'),'fixture');
    await mkdir(join(source,'other-app'));
    await writeFile(join(source,'other-app/prefs'),'preserved');
    const hooksFile=join(privateRoot,'hooks.json');
    const configHome=await prepareHookSettings(source,privateRoot,hooksFile);
    const settingsPath=join(configHome,'muse/settings.json');
    const settings=JSON.parse(await readFile(settingsPath,'utf8'));
    assert.equal(settings.managed_hooks_path,hooksFile);
    assert.equal(settings.tui.terminal_background,'dark');
    assert.equal(await readFile(join(source,'muse/settings.json'),'utf8'),original);
    assert.equal(await readFile(join(configHome,'muse/user-hook.sh'),'utf8'),'fixture');
    assert.equal(await readFile(join(configHome,'other-app/prefs'),'utf8'),'preserved');
    assert.equal((await lstat(settingsPath)).mode&0o777,0o600);
    assert.equal((await lstat(configHome)).mode&0o777,0o700);
    assert.equal((await lstat(join(configHome,'muse/user-hook.sh'))).isSymbolicLink(),true);
  }finally {await rm(root,{recursive:true,force:true});}
});

test('existing managed policy is never replaced',async()=>{
  const root=await mkdtemp(join(tmpdir(),'ttakkari-hook-settings-test-'));
  try {
    const source=join(root,'source');
    await mkdir(join(source,'muse'),{recursive:true});
    await writeFile(join(source,'muse/settings.json'),JSON.stringify({schema_version:1,managed_hooks_path:'organization-hooks.json'}));
    await assert.rejects(prepareHookSettings(source,join(root,'private'),'/tmp/new-hooks'),/managed hooks/i);
  }finally {await rm(root,{recursive:true,force:true});}
});

test('absent user config starts with a valid private settings document',async()=>{
  const root=await mkdtemp(join(tmpdir(),'ttakkari-hook-settings-test-'));
  try {
    const configHome=await prepareHookSettings(join(root,'missing'),join(root,'private'),join(root,'guard.json'));
    const settings=JSON.parse(await readFile(join(configHome,'muse/settings.json'),'utf8'));
    assert.equal(settings.schema_version,1);
    assert.equal(settings.managed_hooks_path,join(root,'guard.json'));
  }finally {await rm(root,{recursive:true,force:true});}
});
