import {mkdir,readFile,readdir,symlink,writeFile} from 'node:fs/promises';
import {join,resolve} from 'node:path';

async function entries(path) {
  try {return await readdir(path);}
  catch(error) {if(error.code==='ENOENT')return [];throw error;}
}

// Muse 1.4.10's hook loader reads this pointer from settings.json, not -c.
// Overlay config only; data/cache and the CLI-owned auth store keep their paths.
export async function prepareHookSettings(sourceHome,privateRoot,hooksFile) {
  const source=resolve(sourceHome);
  let settings={schema_version:1};
  try {settings=JSON.parse(await readFile(join(source,'muse/settings.json'),'utf8'));}
  catch(error) {if(error.code!=='ENOENT')throw error;}
  if(!settings||typeof settings!=='object'||Array.isArray(settings))throw new Error('Invalid Muse settings document');
  if(settings.managed_hooks_path)throw new Error('Existing Muse managed hooks require composition; refusing to replace them');
  const configHome=join(privateRoot,'config');
  await mkdir(join(configHome,'muse'),{recursive:true,mode:0o700});
  // Relative config references and child programs retain the same files.
  // Symlink entries without reading or copying credential files.
  for(const name of await entries(source)) {
    if(name!=='muse')await symlink(join(source,name),join(configHome,name));
  }
  for(const name of await entries(join(source,'muse'))) {
    if(name!=='settings.json')await symlink(join(source,'muse',name),join(configHome,'muse',name));
  }
  await writeFile(join(configHome,'muse/settings.json'),JSON.stringify({...settings,managed_hooks_path:hooksFile}),{mode:0o600,flag:'wx'});
  return configHome;
}
