# 2026-10-18 Antigravity 사용량 조회 오류 수정

실측(호스트에서 실제 `agy --print /usage`·`/credits` 실행, 계정 3개)과 DB 이력으로 확인한 내용이다.

- 주간 한도가 찬 그룹(Claude and GPT)의 5시간 버킷은 `disabled: true`, `remaining_fraction: 1`, `reset_time` 없음으로 온다. 파서가 이를 "5시간 0% 사용, 초기화 시각 없음"으로 저장했다. 이제 건너뛴다.
- `agy credits` 가 `no credits info found`(exit 1)로 끝나는 계정(내 antigravity)이 있다. 조회 실패가 아니라 해당 플랜에 정보가 없다는 뜻이라 문구를 나눴다. 크레딧 잔액과 모델 그룹 쿼터는 별개다.
- 조회 실패가 마지막 관측값을 빈 값으로 덮어써, 화면이 창을 잃고 한도가 찬 그룹의 차단(usage_limit_until)이 풀렸다. 이제 값을 유지하고 오류를 붙이며 updated_at 은 실제 관측 시각을 유지한다. 실패 후 값은 잔여량 순위에 쓰지 않는다.
- API/화면: 창별 state(current/stale/unavailable). 초기화가 지난 창은 값을 버리고 unavailable. 한 창이 지났다고 다른 창이 낡은 값이 되지 않는다.
- 남은 양이 0 초과면 사용률을 100 으로 올림하지 않는다.
- 실패가 이어지는 동안 지난 창이 30초마다 재조회를 부르지 않게 수집기 백오프를 따른다.
- 5시간 창이 없는 계정(weekly 만 있음)은 공급자가 그렇게 준 것이다(실측). 창을 만들어 내지 않는다.
- 주간 100% 소진 두 계정은 실제 런타임 오류("Individual quota reached", 초기화 시각 일치)와 맞는다.
