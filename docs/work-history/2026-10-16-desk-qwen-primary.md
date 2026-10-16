# 2026-10-16 접수 1순위를 로컬 Qwen 하네스로 전환

- 요청: "당장 리팩토링해. 무조건 접수하는 애를 걔로 바꾸자".
- 변경: runs/desk_qwen.py 신규(하네스 v1 이식), runs/desk.py(_context·_try_qwen, 실패 시 기존 Luna/Haiku 루프), config.py(desk_qwen_*), runs/desk_eval.py(engine="qwen" 평가), deploy/install-services.sh qwen(서버 launchd 항목, 실행은 하지 않음), tests/test_desk_qwen.py, ADR 0032.
- 이식 검증(실모델, 운영 모듈 decide() 직접 호출): dev3 57/60, dev2 71/77, dev 123/125로 하네스 v1 과 같다. lead 선택 dev 26문항: 위임 대상 23/23, 잘못 이어붙임 0.
- 서버가 안 떠 있으면 접수는 지금처럼 Luna/Haiku 로 동작한다. 서버 설치는 사용자가 한다.
