import json, sys, time, urllib.request
URL = sys.argv[1]; MODEL = sys.argv[2]
SYS = """사용자 메시지를 분류해 라벨 하나만 출력하세요. 다른 말은 쓰지 마세요.
CHAT: 인사, 감사, 잡담
STATUS: 이미 맡긴 작업의 진행 상태나 완료 여부를 묻는 질문
LOOKUP: 저장된 메모나 일정을 찾아 달라는 요청
WORK: 무언가를 고치거나 만들거나 조사하거나, 의견·설계·설명을 달라는 요청

예시:
안녕 -> CHAT
메모 UI 작업 끝났어? -> STATUS
다음주 회의 언제였지? -> LOOKUP
README 오타 고쳐줘 -> WORK"""
CASES = [
 ("야","CHAT"),("ㅎㅇ","CHAT"),("고마워","CHAT"),("수고했어","CHAT"),("오늘 날씨 좋다","CHAT"),("ㅋㅋㅋ 웃기네","CHAT"),
 ("지금 뭐 하고 있어?","STATUS"),("백엔드 재시작 게이트 끝났어?","STATUS"),("메모 작업 얼마나 됐어","STATUS"),("아까 시킨 거 어떻게 됐어?","STATUS"),
 ("작업자들 다 놀고 있어?","STATUS"),("배포 아직이야?","STATUS"),("멈춘 세션 있어?","STATUS"),
 ("내일 일정 뭐 있지?","LOOKUP"),("지난주에 적은 회의 메모 찾아줘","LOOKUP"),("이번달 치과 예약 언제야","LOOKUP"),("내가 저장한 API 키 관련 메모 보여줘","LOOKUP"),("금요일에 뭐 잡혀있어?","LOOKUP"),
 ("로그인 페이지 다크모드 깨지는 거 고쳐줘","WORK"),("오케스트레이터 슬롯 구조 어떻게 바꾸는 게 좋을까?","WORK"),("이 에러 왜 나는지 봐줘","WORK"),
 ("카카오 모델 중에 가벼운 거 뭐 있어?","WORK"),("MoE가 뭐야?","WORK"),("PR 리뷰해줘","WORK"),("내일 3시에 치과 일정 추가해줘","WORK"),
 ("메모 화면 좀 더 예쁘게 만들어","WORK"),("이거 커밋하고 푸시해","WORK"),("따까리 서버 재시작해줘","WORK"),("사용량 많이 쓴 계정이 뭐야? 정리해서 알려줘","WORK"),("이 문장 자연스럽게 다듬어줘","WORK"),
]
LABELS = ["CHAT","STATUS","LOOKUP","WORK"]
ok = 0; ts = []; conf = {}
for m, exp in CASES:
    t = time.time()
    req = urllib.request.Request(URL, headers={"Content-Type": "application/json"},
        data=json.dumps({"model": MODEL, "temperature": 0, "max_tokens": 6,
                         "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": m}]}).encode())
    o = json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"].strip()
    ts.append(time.time() - t)
    got = next((l for l in LABELS if l in o.upper()), "?")
    ok += got == exp
    if got != exp: print(f"  miss: {m} -> {o!r} (want {exp})")
    conf[(exp, got)] = conf.get((exp, got), 0) + 1
ts = sorted(ts[1:])
print(f"{MODEL}: {ok}/{len(CASES)}  median {ts[len(ts)//2]:.2f}s  max {ts[-1]:.2f}s")
print("  WORK로 넘긴 비작업:", sum(v for (e,g),v in conf.items() if g=="WORK" and e!="WORK"),
      "/ 작업을 놓친 것:", sum(v for (e,g),v in conf.items() if e=="WORK" and g!="WORK"))
