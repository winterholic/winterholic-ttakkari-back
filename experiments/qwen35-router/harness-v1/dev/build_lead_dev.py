"""lead_id 선택 dev 세트(신규 작성). expected = 이어갈 lead 의 id 또는 None(새 일)."""
import json
L = {
 "api": ("2b9f6a40-5c3e-4b7d-8e12-91d0a4c7e0f3", "로그인 API 리팩터링", "진행 중"),
 "doc": ("7c1e0b52-aa31-4f0e-9d25-0b7f3c1d8a11", "주간 회의록 정리", "완료"),
 "test": ("9d4e1c77-0a62-4c5b-b3f8-6e2a7d915c04", "결제 모듈 테스트 보강", "실패"),
 "ui": ("e51a8d03-7b94-4f26-a0c9-3d8b6f12e7a5", "설정 화면 다크모드", "대기"),
 "rep": ("11abc0de-1111-4222-8333-444455556666", "월간 리포트 자동화", "진행 중"),
}
def line(k): i, t, s = L[k]; return f"- lead_id={i} 「{t}」 상태: {s} · 작업 세션 1개(실행 중 0)"
cases = []
def add(msg, board_keys, expected_key, hist="(최근 대화 없음)"):
    cases.append(dict(message=msg, board="\n".join(line(k) for k in board_keys), history=hist, expected=L[expected_key][0] if expected_key else None))
add("로그인 API에 리프레시 토큰도 넣어줘", ["api", "doc"], "api")
add("토큰 만료 시간 설정값도 같이 손봐줘", ["api", "doc", "ui"], "api")
add("회의록에 액션 아이템 섹션 추가해줘", ["doc", "api"], "doc")
add("지난 회의록 요약본을 영어로도 만들어줘", ["doc", "test"], "doc")
add("실패한 환불 테스트 고쳐줘", ["test", "api"], "test")
add("결제 쪽 테스트 커버리지도 올려줘", ["test", "ui"], "test")
add("다크모드에서 아이콘 색이 안 보여 고쳐줘", ["ui", "api"], "ui")
add("설정 화면에 토글 위치 바꿔줘", ["ui", "doc", "api"], "ui")
add("월간 리포트에 이탈률 지표 추가해줘", ["rep", "ui"], "rep")
add("리포트 슬랙 발송 시간 오전 9시로 바꿔줘", ["rep", "test", "doc"], "rep")
add("방금 그거 테스트도 추가해줘", ["api", "doc"], "api", "사용자: 로그인 API 리팩터링 시작해줘\n따까리: 기획 세션 「로그인 API 리팩터링」으로 넘겼어요.")
add("그것도 영어로 번역해줘", ["doc", "ui"], "doc", "사용자: 회의록 정리해줘\n따까리: 기획 세션 「주간 회의록 정리」로 넘겼어요.")
add("세금 신고 방법 조사해줘", ["api", "doc"], None)
add("내일 오후 3시 치과 일정 추가해줘", ["ui", "test"], None)
add("Redis와 Memcached 차이 설명해줘", ["api", "rep"], None)
add("블로그 글 초안 써줘", ["doc", "ui", "test"], None)
add("사내 위키 동기화 상태 확인해줘", ["api", "rep"], None)
add("서버 비용 월별로 정리해줘", ["doc", "test"], None)
add("새 프로젝트 로고 디자인 시안 만들어줘", ["ui", "api", "doc"], None)
add("이번 주 장보기 목록 메모에 추가해줘", ["rep", "test"], None)
add("API 문서 갱신해줘", ["doc", "test"], None)   # 제목 유사(문서)지만 다른 일 -> 새 일로 보는 게 안전
add("오늘 점심 뭐 먹을지 추천해줘", ["ui"], None)
add("배포 끝났어?", ["api", "doc"], None)
add("어제 부탁한 계약서 검토 결과 나왔나요?", ["test", "rep"], None)
add("결제 모듈 테스트 이제 어디까지 됐어?", ["test", "api"], "test")
add("다크모드 작업 시작했어?", ["ui", "doc"], "ui")
open("lead_dev.jsonl", "w").write("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n")
print(len(cases), sum(c["expected"] is None for c in cases))
