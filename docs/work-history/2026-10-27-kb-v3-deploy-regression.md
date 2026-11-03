# KB v3 최신 main의 배포 차단 회귀 재현과 통합

리드 소유 back-kb-v3-deploy-regression-20261027에서 KB candidate c07ea7cd0440363abc6cacb909b3f0cd338ef43b와 최신 main7687a9be4f9edb45b8ee8492c067ea3941bb9dbb를 병합했다. 병합commit873c7531a1047c1ef5a7162d74f3bea6e1d349a4, 충돌없음. 원본current/old final 트리는 수정하지 않았다. main push/deploy 없음.

현재 Run334209b6-1423-4154-a61d-cd023f8f9f1b의 제공DB(port57394,ttk_test,ttakkari_test)marker RunID·port를 대조했다. 다른RunDB를 사용하지 않았다.

지원ops_deploy_log(c5fb3942,summary)에서 운영배포1083a0ce가 test_quota_wait_persists_resume_after_across_restart의 FakeProfile.model 부재로 실패한 것을 확인했다. 같은 시험을 c07에서 직접 실행하여 1failed0.60s로 재현했다(review/kb-v3/deploy-regression-20261027/red.log). fetch 결과 최신main7687에는 이미 test fixture가 실제 profiles.Profile로 교정되어 있어 새 중복 패치를 만들지 않고 그commit을 정상 병합했다.

PYTHONPATH=src UV_CACHE_DIR=workspace/.cache/uv ../back-kb-v3-api/.venv/bin/python -m pytest -q tests/test_scheduled_actions.py tests/test_worker_quota_recovery.py tests/test_agent_scope_auth.py
→38passed4.81s,exit0(green.log). 예약의 resume_after 영속/복구와 agent보안·quota 인접범위이며 전체배포 검사 또는 KB E2E 통과가 아니다.

진행중: c07 KB최신통합 타공급자 독립검증80d3f58c,front000c2556 타공급자검증787a5416,별도L2기본활성·고정운영검증경로26fff9a3. 현재L2기본disabled이며 실제PDF/상충주장 E2E·운영백필/배포는 남았다. c07보고fullSHA오류와schema15 직접검사를pytest16으로표현한불일치는 리드가 확인했으나 이를실행증거로 채택하지 않는다.
