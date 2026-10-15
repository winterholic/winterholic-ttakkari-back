/* 시안용 가상 데이터. 실제 도구 연결·검증 결과가 아니다. 앱 이름 중 PhotoCraft·FilmCraft·VectorCraft·PdfCraft·FigCraft·GridCraft 는
   사용자 요청 원문에 적힌 것이고, DesignCraft 는 PMS 노트가 인용한 공식 사이트 링크에서만 확인됐다. 나머지 6개는 이름이 확정되지 않았다(A01 감사 전). */
window.CWD = (function () {
  const PROJECTS = [
    { id: 'p1', name: '유튜브 채널', tasks: ['t1', 't2', 't3'] },
    { id: 'p2', name: '새 프로젝트 (비어 있음)', tasks: [] },
  ];
  const TASKS = {
    t1: { id: 't1', name: '유튜브 썸네일 제작', media: 'image', tool: 'photo' },
    t2: { id: 't2', name: '인트로 영상 컷 편집', media: 'video', tool: 'film' },
    t3: { id: 't3', name: '거실 평면도 수정 (CAD)', media: 'cad', tool: 'c8' },
  };

  const TOOLS = [
    { id: 'photo', name: 'PhotoCraft', use: '이미지 편집', src: '요청 원문' },
    { id: 'film', name: 'FilmCraft', use: '영상 편집', src: '요청 원문' },
    { id: 'vector', name: 'VectorCraft', use: '벡터 디자인', src: '요청 원문' },
    { id: 'pdf', name: 'PdfCraft', use: 'PDF 편집', src: '요청 원문' },
    { id: 'fig', name: 'FigCraft', use: 'UI 디자인', src: '요청 원문' },
    { id: 'grid', name: 'GridCraft', use: '스프레드시트', src: '요청 원문' },
    { id: 'design', name: 'DesignCraft', use: '용도 미확인', src: '공식 사이트 링크(PMS 노트 인용)' },
    ...[8, 9, 10, 11, 12, 13].map((n) => ({ id: 'c' + n, name: '후보 ' + n, use: '이름·용도 미확정', src: 'A01 감사 전' })),
  ];

  // 축 하나: [키, 라벨, 값, tone, 아이콘]. tone: ok | warn | bad | off | unk
  const REAL = { // 실연결 증거가 없는 지금의 실제 상태. 모르는 것은 '미설치'가 아니라 '확인 안 됨'이다.
    install: ['설치', '확인 안 됨', 'unk', 'dashed'],
    connect: ['연결', '확인 안 됨', 'unk', 'dashed'],
    support: ['지원', '미검증', 'unk', 'dashed'],
    active: ['활성', '꺼짐', 'off', 'ban'],
    verify: ['검증', '검증 기록 없음', 'unk', 'dashed'],
    summary: ['후보 · 미검증', 'unk'],
  };
  const SAMPLE = {
    photo: {
      install: ['설치', '0.9.2 설치됨', 'ok', 'circle-check'], connect: ['연결', 'Mac Studio 연결됨', 'ok', 'circle-check'],
      support: ['지원', '부분 지원 3/5', 'warn', 'warning'], active: ['활성', '켜짐', 'ok', 'circle-check'],
      verify: ['검증', '오늘 12:20 통과', 'ok', 'circle-check'], summary: ['일부 기능만 사용 가능', 'warn'],
    },
    film: {
      install: ['설치', '미설치', 'off', 'ban'], connect: ['연결', '연결할 앱 없음', 'off', 'ban'],
      support: ['지원', '미검증', 'unk', 'dashed'], active: ['활성', '켤 수 없음', 'off', 'ban'],
      verify: ['검증', '검증 기록 없음', 'unk', 'dashed'], summary: ['설치 필요', 'off'],
    },
    vector: {
      install: ['설치', '0.4.1 ≠ 검증 기준 0.5.x', 'warn', 'warning'], connect: ['연결', 'Mac Studio 연결됨', 'ok', 'circle-check'],
      support: ['지원', '버전이 달라 판정 보류', 'warn', 'warning'], active: ['활성', '꺼짐', 'off', 'ban'],
      verify: ['검증', '3일 전 (다른 버전 기준)', 'warn', 'clock'], summary: ['버전 불일치', 'warn'],
    },
    pdf: {
      install: ['설치', '1.2.0 설치됨', 'ok', 'circle-check'], connect: ['연결', '끊김 · 마지막 응답 어제 22:10', 'bad', 'wifi-off'],
      support: ['지원', '지원 (마지막 확인 기준)', 'warn', 'clock'], active: ['활성', '켜짐 · 실행 불가', 'warn', 'warning'],
      verify: ['검증', '어제 22:10 통과 (오래됨)', 'warn', 'clock'], summary: ['연결 끊김', 'bad'],
    },
    fig: {
      install: ['설치', '2.0.1 설치됨', 'ok', 'circle-check'], connect: ['연결', '연결 테스트 중…', 'unk', 'clock'],
      support: ['지원', '테스트 중', 'unk', 'clock'], active: ['활성', '꺼짐', 'off', 'ban'],
      verify: ['검증', '지금 실행 중', 'unk', 'clock'], summary: ['테스트 중', 'unk'],
    },
    grid: {
      install: ['설치', '0.7.0 설치됨', 'ok', 'circle-check'], connect: ['연결', 'Mac Studio 연결됨', 'ok', 'circle-check'],
      support: ['지원', '지원 불가 (검증 실패)', 'bad', 'circle-x'], active: ['활성', '켤 수 없음', 'off', 'ban'],
      verify: ['검증', '오늘 11:02 실패 E_CLI_EXIT_2', 'bad', 'circle-x'], summary: ['검증 실패', 'bad'],
    },
    design: {
      install: ['설치', '0.3.0 설치됨', 'ok', 'circle-check'], connect: ['연결', 'Mac Studio 연결됨', 'ok', 'circle-check'],
      support: ['지원', '지원 2/2', 'ok', 'circle-check'], active: ['활성', '켜짐', 'ok', 'circle-check'],
      verify: ['검증', '31일 전 (오래됨)', 'warn', 'clock'], summary: ['점검 필요 · 오래된 검증', 'warn'],
    },
  };
  const CAPS = { // 기능별 판정. 값이 없는 도구는 '확인된 기능 없음'.
    photo: [
      { n: '배경 밝기·색 조정', st: 'ok', note: '오늘 12:20 검증 · 입력 PNG/JPG', cons: '단일 파일만' },
      { n: '텍스트 레이어 크기·색 변경', st: 'ok', note: '오늘 12:20 검증', cons: '레이어가 있는 PSD/원본 앱 파일만' },
      { n: '이미지 내보내기 (PNG)', st: 'ok', note: '오늘 12:20 검증', cons: '' },
      { n: '배치 처리 (여러 파일)', st: 'no', note: '지원 불가', cons: '현재 CLI는 파일 하나씩만 받아요 (가상 예시)' },
      { n: '원본 앱 안에 임베드', st: 'unk', note: '미검증', cons: 'U06 PoC 전까지 판정 없음' },
    ],
    vector: [{ n: '버전 확인', st: 'ok', note: '0.4.1 응답함', cons: '' }, { n: '나머지 기능', st: 'unk', note: '검증 기준 버전이 달라 보류', cons: '' }],
    design: [{ n: '프로젝트 열기', st: 'ok', note: '31일 전 검증', cons: '' }, { n: '내보내기', st: 'ok', note: '31일 전 검증', cons: '' }],
  };
  const TESTS = { // 테스트 기록(가상)
    photo: [
      { t: '오늘 12:20', k: '기능 테스트 5개', r: 'warn', d: '3 통과 · 1 지원불가 · 1 미검증' },
      { t: '오늘 12:19', k: '연결 테스트', r: 'ok', d: '응답 84ms · 버전 0.9.2' },
    ],
    grid: [{ t: '오늘 11:02', k: '기능 테스트', r: 'bad', d: '종료 코드 2 · E_CLI_EXIT_2 · stderr 3줄' }],
    pdf: [{ t: '어제 22:10', k: '연결 테스트', r: 'ok', d: '응답 120ms' }, { t: '오늘 09:00', k: '연결 테스트', r: 'bad', d: 'Mac Studio 응답 없음 (타임아웃 10초)' }],
  };

  const LIB = [
    { id: 'l1', name: 'thumbnail.png', kind: 'image', proj: 'p1', task: '유튜브 썸네일 제작', tool: 'PhotoCraft', role: 'draft', pending: true, size: '438 KB', when: '오늘 12:41', day: '오늘', cur: 2,
      versions: [ { n: 1, role: 'draft', t: '오늘 12:31', note: '최초 생성', size: '412 KB', sha: 'a91c…07e2' }, { n: 2, role: 'draft', t: '오늘 12:41', note: '배경 어둡게 · 제목 확대', size: '438 KB', sha: '5be0…c3d9', pending: true } ],
      inputs: [{ name: '스크린샷.png', size: '1.1 MB', note: '요청에 첨부한 원본', changed: false }] },
    { id: 'l2', name: 'thumbnail.png', kind: 'image', proj: 'p1', task: '지난 영상 썸네일 재작업', tool: 'PhotoCraft', role: 'approved', size: '501 KB', when: '10-12', day: '이번 주', cur: 3,
      versions: [ { n: 1, role: 'draft', t: '10-12 09:10', note: '최초 생성', size: '390 KB', sha: '11fa…9b01' }, { n: 2, role: 'draft', t: '10-12 09:30', note: '색감 조정', size: '455 KB', sha: '77d4…aa2c' }, { n: 3, role: 'approved', t: '10-12 10:02', note: '최종 승인', size: '501 KB', sha: 'c02e…5f68' } ],
      inputs: [{ name: '촬영컷-07.jpg', size: '3.4 MB', note: '요청에 첨부한 원본', changed: false }] },
    { id: 'l3', name: '인트로-컷편집.mp4', kind: 'video', proj: 'p1', task: '인트로 영상 컷 편집', tool: 'FilmCraft', role: 'draft', size: '2.4 GB', when: '오늘 11:20', day: '오늘', state: 'large', cur: 1,
      versions: [ { n: 1, role: 'draft', t: '오늘 11:20', note: '앞 10초 컷', size: '2.4 GB', sha: '3d90…e417' } ],
      inputs: [{ name: '인트로-원본.mov', size: '3.8 GB', note: '요청에 첨부한 원본', changed: false }] },
    { id: 'l4', name: '발표-정리.pdf', kind: 'doc', proj: 'p1', task: '발표 자료 PDF 정리', tool: 'PdfCraft', role: 'approved', size: '2.1 MB', when: '10-10', day: '이번 주', cur: 2,
      versions: [ { n: 1, role: 'draft', t: '10-10 15:00', note: '3쪽 제거', size: '2.3 MB', sha: '90ab…12cd' }, { n: 2, role: 'approved', t: '10-10 15:20', note: '최종 승인', size: '2.1 MB', sha: 'be71…0f3a' } ],
      inputs: [{ name: '발표-원본.pptx', size: '8.4 MB', note: '원본이 바뀌었어요 · 이 결과는 5일 전 원본 기준', changed: true }] },
    { id: 'l5', name: '거실-평면도.dwg', kind: 'cad', proj: 'p1', task: '거실 평면도 수정 (CAD)', tool: '후보 8', role: 'source', size: '6.7 MB', when: '10-09', day: '이번 주', state: 'unsupported', cur: 1,
      versions: [ { n: 1, role: 'source', t: '10-09 20:11', note: '업로드한 원본', size: '6.7 MB', sha: 'f1c8…4b90' } ], inputs: [] },
    { id: 'l6', name: '내레이션-v3.wav', kind: 'audio', proj: 'p1', task: '내레이션 정리', tool: '후보 9', role: 'draft', size: '38 MB', when: '10-08', day: '이번 주', state: 'missing', cur: 1,
      versions: [ { n: 1, role: 'draft', t: '10-08 18:40', note: '노이즈 제거', size: '38 MB', sha: '5a27…d8e0' } ], inputs: [] },
    { id: 'l7', name: '월별-매출표.xlsx', kind: 'sheet', proj: 'p1', task: '매출표 정리', tool: 'GridCraft', role: 'draft', size: '212 KB', when: '10-07', day: '이번 주', state: 'restricted', cur: 1,
      versions: [ { n: 1, role: 'draft', t: '10-07 13:05', note: '열 정리', size: '212 KB', sha: '28be…77a1' } ], inputs: [] },
    { id: 'l8', name: '2026-10-15_유튜브-채널-신규-시리즈-썸네일-최종-진짜최종-수정본_v12_가로형_1280x720_dark-background-title-large.png', kind: 'image', proj: 'p1', task: '유튜브 썸네일 제작', tool: 'PhotoCraft', role: 'draft', size: '1.0 MB', when: '오늘 10:02', day: '오늘', retention: '3일 뒤 정리', cur: 1,
      versions: [ { n: 1, role: 'draft', t: '오늘 10:02', note: '긴 파일명 확인용', size: '1.0 MB', sha: 'ee03…91b5' } ], inputs: [] },
  ];

  const R = (id, o) => Object.assign({ id }, o);
  const RUNS = [
    R('r11', { day: '오늘', t: '12:44', task: '인트로 영상 컷 편집', attempt: 1, tool: 'FilmCraft 2.3.0', agent: 'Claude Sonnet high', state: 'running', label: '실행 중', title: '앞 10초 컷 렌더', dur: '4:12 경과', progress: 41,
      steps: [ ['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: FilmCraft', '영상 컷 편집이 필요해서(자동)', '1초'], ['running', '렌더', '41%', '4:12'], ['queued', 'v1 저장·보관함 등록', '', ''] ],
      res: { 경과: '4:12', 'CPU 평균': '312%', '최대 메모리': '5.8 GB', 디스크: '임시 3.1 GB' }, outputs: [], retry: null }),
    R('r1', { day: '오늘', t: '12:41', task: '유튜브 썸네일 제작', attempt: 1, tool: 'PhotoCraft 0.9.2', agent: 'Claude Sonnet high', state: 'succeeded', label: '완료', title: 'v2 만들기 · 배경 어둡게 · 제목 확대', dur: '48초',
      steps: [ ['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: PhotoCraft', '이미지 레이어 편집이 필요해서(자동)', '1초'], ['succeeded', '레이어 열기', 'thumbnail.psd', '6초'], ['succeeded', '배경 어둡게', 'layer: background', '9초'], ['succeeded', '제목 확대', 'layer: title (+48%)', '11초'], ['succeeded', 'v2 저장·보관함 등록', 'thumbnail.png v2', '3초'] ],
      res: { 경과: '48초', 'CPU 평균': '84%', '최대 메모리': '1.2 GB', 디스크: '+26 KB' }, outputs: [{ lib: 'l1', n: 'thumbnail.png v2' }], retry: { safe: true, note: '다시 실행하면 v3로 새로 만들어요. v2는 그대로 남아요.' },
      log: [ ['14:41:02', 'info', '요청 수신 (첨부 1개)'], ['14:41:04', 'tool', 'photocraft.open("thumbnail.psd")'], ['14:41:10', 'tool', 'photocraft.adjust(layer="background", brightness=-40)'], ['14:41:19', 'tool', 'photocraft.resize_text(layer="title", scale=1.48)'], ['14:41:48', 'ok', 'saved thumbnail.png (438 KB)'] ] }),
    R('r2', { day: '오늘', t: '12:10', task: '인트로 영상 컷 편집', attempt: 2, of: 3, tool: 'FilmCraft 2.3.0', agent: 'Claude Sonnet high', state: 'failed', label: '실패', kind: 'timeout', title: '렌더가 제한 시간을 넘겼어요', dur: '10:00 (제한 도달)',
      where: { file: '인트로-원본.mov (3.8 GB)', step: '렌더', attempt: '시도 2/3' },
      why: '렌더가 10분 제한 안에 끝나지 않아 멈췄어요. 입력이 4K·3.8 GB라 평소보다 오래 걸렸을 수 있어요.',
      steps: [ ['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: FilmCraft', '', '1초'], ['failed', '렌더', '인트로-원본.mov', '10:00', '제한 시간 600초 초과 (E_TIMEOUT)'], ['queued', 'v1 저장', '', ''] ],
      res: { 경과: '10:00', 'CPU 평균': '298%', '최대 메모리': '5.9 GB', 디스크: '임시 4.2 GB 정리됨' }, outputs: [],
      retry: { safe: true, note: '원본은 읽기 전용 복사본으로 열어서 다시 해도 안전해요. 임시 파일은 정리됐고, 다시 하면 시도 3/3으로 기록돼요.', opts: ['해상도를 낮춰 다시 시도', '제한 시간을 늘려 다시 시도'] },
      log: [ ['14:10:03', 'info', 'render start 3840x2160'], ['14:15:01', 'warn', 'frame 5120/14400 · 느림'], ['14:20:03', 'error', 'timeout 600s exceeded, killing process'], ['14:20:04', 'info', 'temp cleaned 4.2 GB'] ] }),
    R('r3', { day: '오늘', t: '11:50', task: '유튜브 썸네일 제작', attempt: 1, tool: 'PhotoCraft 0.9.2', agent: 'Claude Sonnet high', state: 'failed', label: '실패', kind: 'conflict', title: '도구가 다른 작업에서 쓰이고 있었어요', dur: '2:00 대기 후 포기',
      where: { file: 'thumbnail.psd', step: '레이어 열기', attempt: '시도 1' },
      why: 'PhotoCraft가 "집 인테리어" 프로젝트의 작업에서 열려 있어서 2분 기다렸다가 멈췄어요. 이 도구는 한 번에 하나의 작업만 받아요(가상 예시).',
      steps: [ ['succeeded', '요청 해석', '', '2초'], ['succeeded', '도구 선택: PhotoCraft', '', '1초'], ['failed', '레이어 열기', 'thumbnail.psd', '2:00', '도구 사용 중 · 대기 시간 초과 (E_TOOL_BUSY)'] ],
      res: { 경과: '2:03', 'CPU 평균': '2%', '최대 메모리': '0.1 GB', 디스크: '0' }, outputs: [],
      retry: { safe: true, note: '아직 아무것도 바뀌지 않았어요. 다른 작업이 끝난 뒤 다시 하면 돼요.', opts: ['다른 작업이 끝나면 자동으로 다시'] },
      log: [ ['13:50:01', 'info', 'acquire lock photocraft'], ['13:52:01', 'warn', 'lock held by session 7c1a (집 인테리어)'], ['13:52:01', 'error', 'E_TOOL_BUSY after 120s'] ] }),
    R('r4', { day: '오늘', t: '11:30', task: '유튜브 썸네일 제작', attempt: 1, tool: 'PhotoCraft 0.9.2', agent: 'Claude Sonnet high', state: 'blocked', label: '정책 차단', kind: 'denied', title: '허용되지 않은 위치의 파일이라 열지 않았어요', dur: '1초',
      where: { file: '~/Library/Mail/…/첨부-02.png', step: '파일 열기', attempt: '시도 1' },
      why: '파일 반출 규칙(File Broker)에 따라 허용된 위치 밖의 파일은 도구에 넘기지 않아요. 오류가 아니라 정상 동작한 보호예요.',
      steps: [ ['succeeded', '요청 해석', '', '1초'], ['failed', '파일 열기', '~/Library/Mail/…/첨부-02.png', '1초', '허용된 위치 밖 (POLICY_PATH_DENIED)'] ],
      res: { 경과: '1초', 'CPU 평균': '1%', '최대 메모리': '0.1 GB', 디스크: '0' }, outputs: [],
      retry: { safe: true, note: '파일을 작업 폴더로 옮기거나 다시 첨부하면 돼요. 허용 범위는 설정에서만 바꿀 수 있어요.', opts: ['파일 다시 첨부'] },
      log: [ ['13:30:01', 'warn', 'path outside allowed roots'], ['13:30:01', 'info', 'policy denied (no tool call made)'] ] }),
    R('r5', { day: '오늘', t: '10:40', task: '인트로 영상 컷 편집', attempt: 1, tool: 'FilmCraft 2.3.0', agent: 'GPT Luna high', state: 'failed', label: '실패', kind: 'resource', title: '메모리가 모자라 멈췄어요', dur: '1:35',
      where: { file: '인트로-원본.mov (3.8 GB)', step: '디코드', attempt: '시도 1' },
      why: '렌더에 약 6.2 GB가 필요한데 그때 쓸 수 있는 메모리는 3.1 GB였어요. 다른 작업이 메모리를 쓰고 있었어요.',
      steps: [ ['succeeded', '요청 해석', '', '2초'], ['failed', '디코드', '인트로-원본.mov', '1:35', '메모리 부족 (E_OOM) · 필요 6.2 GB / 가용 3.1 GB'] ],
      res: { 경과: '1:35', 'CPU 평균': '210%', '최대 메모리': '3.1 GB (한계)', 디스크: '0' }, outputs: [],
      retry: { safe: true, note: '다른 큰 작업이 끝난 뒤에 하거나 해상도를 낮춰요. 부분 결과는 없어요.', opts: ['해상도를 낮춰 다시 시도'] },
      log: [ ['12:40:07', 'warn', 'mem avail 3.1 GB, need ~6.2 GB'], ['12:41:42', 'error', 'E_OOM: allocation failed'] ] }),
    R('r6', { day: '오늘', t: '10:10', task: '매출표 정리', attempt: 1, tool: 'GridCraft 0.7.0', agent: 'GPT Luna high', state: 'review', label: '확인 필요', kind: 'stderr', title: '경고만 출력되고 결과 파일이 없어요', dur: '6초',
      where: { file: '월별-매출표.xlsx', step: '열 정리', attempt: '시도 1' },
      why: '도구가 종료 코드 0으로 끝났지만 표준 출력은 비어 있고 경고 3줄만 있었어요. 성공으로 보지 않고 사람이 확인해야 해요.',
      steps: [ ['succeeded', '요청 해석', '', '1초'], ['failed', '열 정리', '월별-매출표.xlsx', '6초', '결과 파일 없음 · stderr 3줄 (종료 코드 0)'] ],
      res: { 경과: '6초', 'CPU 평균': '40%', '최대 메모리': '0.4 GB', 디스크: '0' }, outputs: [],
      retry: { safe: true, note: '원본은 바뀌지 않았어요. 같은 요청을 다시 해도 안전하지만 같은 결과일 수 있어요.', opts: ['로그를 보고 요청 고치기'] },
      log: [ ['13:10:02', 'warn', 'stderr: column "합계" has merged cells, skipped'], ['13:10:03', 'warn', 'stderr: 2 sheets ignored (hidden)'], ['13:10:08', 'warn', 'stderr: nothing to write'], ['13:10:08', 'info', 'exit 0 · stdout empty · outputs 0'] ] }),
    R('r7', { day: '오늘', t: '09:50', task: '발표 자료 PDF 정리', attempt: 1, tool: 'PdfCraft 1.2.0', agent: 'GPT Luna high', state: 'review', label: '산출물 없음', kind: 'empty', title: '정상 종료했지만 만들어진 파일이 0개예요', dur: '3초',
      where: { file: '발표-원본.pptx', step: '3쪽 제거', attempt: '시도 1' },
      why: '종료 코드 0이지만 새 파일이 생기지 않았어요. 완료로 표시하지 않고, 요청한 쪽 번호가 범위 밖이었는지 확인이 필요해요.',
      steps: [ ['succeeded', '요청 해석', '', '1초'], ['succeeded', '3쪽 제거', '발표-원본.pptx', '2초'], ['failed', '저장·보관함 등록', '', '0초', '등록할 산출물 없음'] ],
      res: { 경과: '3초', 'CPU 평균': '12%', '최대 메모리': '0.2 GB', 디스크: '0' }, outputs: [],
      retry: { safe: true, note: '다시 해도 파일은 덮어쓰지 않아요.', opts: ['쪽 번호 확인하고 다시 시도'] },
      log: [ ['12:50:01', 'tool', 'pdfcraft.remove_pages([3])'], ['12:50:03', 'info', 'exit 0 · outputs 0'] ] }),
    R('r8', { day: '어제', t: '21:30', task: '촬영컷 5장 보정', attempt: 1, tool: 'PhotoCraft 0.9.2', agent: 'Claude Sonnet high', state: 'partial', label: '부분 성공', kind: 'partial', title: '5개 중 3개만 만들어졌어요', dur: '3:12',
      where: { file: '촬영컷-04.jpg, 촬영컷-05.jpg', step: '밝기 조정', attempt: '시도 1' },
      why: '촬영컷-04.jpg는 파일이 손상돼 열 수 없었고, 촬영컷-05.jpg는 용량 한도를 넘었어요. 나머지 3개는 정상이에요.',
      steps: [ ['succeeded', '촬영컷-01.jpg', '밝기 +12', '38초'], ['succeeded', '촬영컷-02.jpg', '밝기 +12', '41초'], ['succeeded', '촬영컷-03.jpg', '밝기 +12', '40초'], ['failed', '촬영컷-04.jpg', '', '3초', '파일 손상 (E_DECODE)'], ['failed', '촬영컷-05.jpg', '', '1초', '40 MB 초과 (E_TOO_LARGE)'] ],
      res: { 경과: '3:12', 'CPU 평균': '96%', '최대 메모리': '1.6 GB', 디스크: '+3.1 MB' }, outputs: [{ lib: 'l1', n: '촬영컷-01~03 (3개)' }],
      retry: { safe: true, note: '실패한 2개만 다시 해요. 이미 만든 3개는 건드리지 않아요.', opts: ['실패한 2개만 다시 시도'] },
      log: [ ['06:30:02', 'ok', '촬영컷-01.jpg done'], ['06:30:43', 'ok', '촬영컷-02.jpg done'], ['06:31:24', 'ok', '촬영컷-03.jpg done'], ['06:31:27', 'error', '촬영컷-04.jpg E_DECODE'], ['06:31:28', 'error', '촬영컷-05.jpg E_TOO_LARGE 41.8MB>40MB'] ] }),
    R('r9', { day: '어제', t: '18:20', task: '인트로 영상 컷 편집', attempt: 1, tool: 'FilmCraft 2.3.0', agent: 'Claude Sonnet high', state: 'succeeded', label: '완료', kind: 'biglog', title: '앞 10초 컷 (로그 12,481줄)', dur: '6:48',
      steps: [ ['succeeded', '요청 해석', '', '2초'], ['succeeded', '렌더', '', '6:40'], ['succeeded', '저장·보관함 등록', '인트로-컷편집.mp4', '6초'] ],
      res: { 경과: '6:48', 'CPU 평균': '305%', '최대 메모리': '5.4 GB', 디스크: '+2.4 GB' }, outputs: [{ lib: 'l3', n: '인트로-컷편집.mp4 v1' }], retry: { safe: true, note: '다시 하면 v2로 새로 만들어요.' }, logTotal: 12481,
      log: [ ['09:20:05', 'info', 'frame 1/14400'], ['09:20:05', 'debug', '… (12,470줄 접힘)'], ['09:27:00', 'ok', 'saved 2.4 GB'] ] }),
    R('r10', { day: '어제', t: '15:05', task: '외부 서비스 업로드 연결', attempt: 1, tool: 'FigCraft 2.0.1', agent: 'Claude Sonnet high', state: 'succeeded', label: '완료', kind: 'masked', title: '내보내기 (민감값 마스킹 예시)', dur: '12초',
      steps: [ ['succeeded', '요청 해석', '', '1초'], ['succeeded', '내보내기', '~/•••/Projects/•••/ui.fig', '11초'] ],
      res: { 경과: '12초', 'CPU 평균': '30%', '최대 메모리': '0.5 GB', 디스크: '+90 KB' }, outputs: [], retry: { safe: true, note: '다시 해도 안전해요.' },
      log: [ ['06:05:01', 'tool', 'figcraft.export(path="/Users/•••/Projects/•••/ui.fig")'], ['06:05:02', 'info', 'Authorization: Bearer ••••••••'], ['06:05:03', 'info', 'api_key=•••• (마스킹됨)'], ['06:05:12', 'ok', 'exported ui.png'] ] }),
  ];

  return { PROJECTS, TASKS, TOOLS, REAL, SAMPLE, CAPS, TESTS, LIB, RUNS };
})();
