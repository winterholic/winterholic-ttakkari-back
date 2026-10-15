import argparse,re
from urllib.parse import urlparse,parse_qs
from playwright.sync_api import sync_playwright
p=argparse.ArgumentParser();p.add_argument('--base',required=True);args=p.parse_args()
projects=[{'id':'pms-personal','name':'PMS 개인 프로젝트','scope':'personal'},{'id':'pms-work','name':'PMS 회사 프로젝트','scope':'work'}]
seen=[]
def api(route):
 path=route.request.url.split('/api/',1)[1].split('?',1)[0]
 if route.request.method=='OPTIONS':
  route.fulfill(status=204,headers={'Access-Control-Allow-Origin':args.base,'Access-Control-Allow-Credentials':'true','Access-Control-Allow-Headers':'authorization,content-type','Access-Control-Allow-Methods':'GET,POST,PATCH,DELETE,OPTIONS'});return
 if path=='auth/refresh':body={'access_token':'fixture','expires_in':900,'token_type':'bearer'}
 elif path=='meetings/providers':body={'providers':[{'id':'clovanote','label':'클로바노트','state':'ready','profile_configured':True,'message':'연결됨'},{'id':'local_whisper','label':'로컬 Whisper large-v3','state':'ready','profile_configured':False,'message':'준비됨'}]}
 elif path=='pms/projects':
  seen.append('pms');scope=parse_qs(urlparse(route.request.url).query).get('scope',['all'])[0];body={'projects':[p for p in projects if scope=='all' or p['scope']==scope]}
 elif path=='assistant/entities':seen.append('legacy');body={'entities':[{'id':'legacy','name':'잘못된 맥락 프로젝트','type':'project'}]}
 elif path=='tasks/active':body={'roots':[],'nodes':[]}
 elif path.startswith('tasks/'):body={'path':[],'nodes':[],'root_id':None}
 elif path=='health':body={'ok':True}
 elif path=='push/status':body={'configured':False,'enabled':False}
 elif path=='workspaces/default':body={'id':'fixture','name':'테스트','root_path':'/tmp/fixture','extra_writable_roots':[]}
 elif path=='clovanote/status':body={'state':'unconfigured','active_jobs':0,'notes':0,'unimported':0,'login':{'state':'idle'},'message':'테스트 데이터'}
 elif path=='clovanote/jobs':body={'jobs':[]}
 else:body=[]
 route.fulfill(json=body,headers={'Access-Control-Allow-Origin':args.base,'Access-Control-Allow-Credentials':'true'})
with sync_playwright() as p:
 browser=p.chromium.launch(channel='chrome',headless=True)
 for w in [390,1440]:
  page=browser.new_page(viewport={'width':w,'height':900},service_workers='block',color_scheme='dark' if w==390 else 'light')
  page.add_init_script('delete Navigator.prototype.serviceWorker')
  page.route('**/api/**',api)
  page.goto(args.base+'/meetings/new',wait_until='networkidle')
  page.get_by_role('radio',name='녹음 파일',exact=True).click()
  page.get_by_role('combobox',name=re.compile('전사 엔진')).click()
  page.get_by_role('option',name='로컬 Whisper large-v3',exact=True).click()
  page.get_by_text('이 Mac에서 전사해요',exact=True).wait_for()
  assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  page.screenshot(path=f'/Users/winterholic/.ttakkari/data/outbox/1c83384d-a413-4776-90e8-34e88a2e622a/local-stt-{w}.png',full_page=True)
  print(f'{w}px local engine selection and overflow PASS',flush=True)
  page.close()
 browser.close()
