# Kafka: From Event to Consumer · 이벤트에서 컨슈머까지

## Video direction

- 16초, 16:9 기술 설명. Producer → broker partition log → consumer group의 인과 관계를 한 화면의 흐름도로 보여준다.
- 한국어와 영어는 같은 의미의 짧은 자막으로 병기한다. 단계 번호와 방향성으로 시선 위계를 만든다.
- 0–5.1초 producer 카드가 좌측에서 진입하고 패킷이 partition으로 이동한다.
- 5.1–10.5초 새 로그 레코드가 나타나고 partition 행이 펄스한다.
- 10.5–16초 패킷이 consumer로 이동하고 오프셋 배지가 갱신된다.
- 모션은 한 개의 paused GSAP 타임라인에서 시간으로 결정한다. 반복 애니메이션은 유한 반복만 쓴다.
- 오디오: 제공된 로컬 음악 WAV를 낮은 배경음으로 사용한다. 음성 내레이션은 생성하지 않는다.

## Frames

| 시간 | 프레임 | 화면 문구 | 내레이션/설명 | 시각·모션 |
|---|---|---|---|---|
| 0.0–5.1s | Producer | Key selects a partition · 키가 파티션을 선택 | The producer hashes the record key to choose a partition. · 프로듀서는 레코드 키를 해시해 파티션을 선택합니다. | Producer 카드 진입, 패킷 좌→중앙 이동 |
| 5.1–10.5s | Broker | Broker appends the record · 브로커가 레코드를 기록 | The partition leader appends the record; replicas follow according to the topic policy. · 파티션 리더가 레코드를 추가하고 복제본은 토픽 정책에 따라 뒤따릅니다. | 로그 블록 pop, 해당 파티션 강조 |
| 10.5–16.0s | Consumer | Consumer advances its offset · 컨슈머가 오프셋을 전진 | A consumer in the group reads its assigned partition and commits progress. · 그룹의 컨슈머가 할당된 파티션을 읽고 진행 위치를 커밋합니다. | 패킷 우측 이동, 오프셋 배지 갱신 |
