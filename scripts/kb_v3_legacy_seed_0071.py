"""0071 시점(KB v3 이전) 코드 경로로 운영과 같은 모양의 기존 데이터를 시드한다(백필 리허설 전용).

이 스크립트는 0071 코드 체크아웃(예: 94a4963)의 가상환경으로 돌린다. KB v3 이전 API(register_text, ingest_source,
segment_revision, KbDocumentAccess(scope만), KbObservation)만 쓰므로 새 코드의 어떤 함수도 부르지 않는다.
운영 DB 에 쓰지 않는다: TTAKKARI_DATABASE_URL 이 가리키는 임시 DB 에만 쓴다.

운영 36건의 모양(설계 §1.3): 수집기 배달 문서는 본문이 한 덩어리(passage_count=1)이고 헤딩은 평문 줄이다.
- web 문서 24건(수집기 모양): 원본은 실제 HTML, 리비전 본문은 헤딩 표시를 잃은 평문(빈 줄 없음) → 0071 의 문단 분리가 한 패시지로 만든다.
  크기는 small/medium/large 로 섞고, 같은 주제를 두 문서가 다루지만 문장은 다르다(실제 문서처럼 표현이 겹치되 같지 않다).
- upload 문서 12건(register_text): 빈 줄로 나뉜 문단 → 문단별 패시지(section_path 없음).
- 일부는 work 범위, 일부는 hidden 접근 행.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.db import sessionmaker
from ttakkari.kb_library import register_text
from ttakkari.kb_store import ingest_source, segment_revision

# (한글 이름, 영문 이름, 정의 명사구(이다로 끝남), 관리 대상 한글, 관리 대상 영문)
TOPICS = [
    ("디플로이먼트", "Deployment", "파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다", "레플리카셋", "ReplicaSet"),
    ("레플리카셋", "ReplicaSet", "지정된 수의 파드 복제본이 항상 실행되도록 보장하는 컨트롤러이다", "파드", "Pod"),
    ("파드", "Pod", "쿠버네티스에서 배포할 수 있는 가장 작은 단위이다", "컨테이너", "container"),
    ("서비스", "Service", "파드 집합에 대한 안정적인 네트워크 엔드포인트를 노출하는 리소스이다", "파드", "Pod"),
    ("컨피그맵", "ConfigMap", "키-값 쌍으로 비밀이 아닌 설정 데이터를 저장하는 오브젝트이다", "파드", "Pod"),
    ("퍼시스턴트 볼륨", "PersistentVolume", "클러스터 관리자가 프로비저닝한 스토리지 조각이다", "스토리지클래스", "StorageClass"),
    ("스테이트풀셋", "StatefulSet", "상태를 가진 애플리케이션의 배포와 스케일링을 관리하는 워크로드 리소스이다", "파드", "Pod"),
    ("데몬셋", "DaemonSet", "모든 노드에서 파드 사본이 하나씩 실행되도록 보장하는 워크로드 리소스이다", "파드", "Pod"),
    ("인그레스", "Ingress", "클러스터 외부에서 서비스로 가는 HTTP 라우팅 규칙을 정의하는 리소스이다", "서비스", "Service"),
    ("네임스페이스", "Namespace", "단일 클러스터 안에서 리소스 그룹을 격리하는 메커니즘이다", "파드", "Pod"),
    ("시크릿", "Secret", "비밀번호나 토큰 같은 민감한 데이터를 저장하는 오브젝트이다", "파드", "Pod"),
    ("크론잡", "CronJob", "반복 일정에 따라 잡을 생성하는 워크로드 리소스이다", "잡", "Job"),
]

# (헤딩, 본문 템플릿). 헤딩은 문서 안에서 겹치지 않는다. 문서 크기는 앞에서부터 몇 개를 쓰는가로 정한다.
SECTION_POOL = [
    ("개요", "{ko}({en})는 {definition}."),
    ("동작 방식", "{ko}는 {tko}를 관리한다. 컨트롤러는 현재 상태를 원하는 상태로 계속 수렴시킨다."),
    ("사용 예시", "kubectl apply -f {en_l}.yaml 명령으로 {ko}를 생성한다. kubectl get {en_l} 로 상태를 확인한다."),
    ("제약 사항", "{ko}는 네임스페이스 범위의 리소스이다. 이름은 DNS 레이블 규칙을 따라야 한다."),
    ("업데이트 전략", "{ko}를 갱신할 때는 한 번에 바꾸지 않고 단계적으로 교체하는 방식을 권장한다. 문제가 생기면 이전 버전으로 되돌린다."),
    ("문제 해결", "{ko}가 기대한 상태가 되지 않으면 kubectl describe {en_l} 로 이벤트를 확인한다. 로그는 {tko}에서 읽는다."),
    ("모범 사례", "{ko}에는 의미 있는 레이블을 붙이고 리소스 요청과 제한을 함께 선언하는 것이 좋다."),
    ("보안 고려사항", "{ko}를 다룰 때는 최소 권한 원칙을 적용하고 서비스 계정을 목적별로 나눈다."),
    ("모니터링", "{ko}의 상태는 메트릭과 이벤트로 관찰한다. 알림 기준은 서비스 수준 목표에서 정한다."),
    ("확장", "{ko}는 수평 확장과 수직 확장을 모두 고려해 설계한다. 부하가 늘면 복제본 수를 조정한다."),
    ("호환성", "{ko}의 동작은 버전에 따라 다를 수 있으므로 사용하는 클러스터 버전의 문서를 확인한다."),
    ("참고 자료", "{ko}에 대한 더 자세한 내용은 공식 문서와 릴리스 노트를 참고한다."),
]
SIZES = {"small": 4, "medium": 8, "large": 12}
# 문서를 길게 만드는 세부 문장(제목이 아니라 본문이다). 크기별로 절마다 몇 문장을 덧붙인다.
DETAILS = [
    "{ko}의 설정 값은 매니페스트의 spec 항목에서 선언하며 변경 이력은 버전 관리 시스템에 남기는 것이 좋다.",
    "{ko}를 운영 클러스터에 적용하기 전에 스테이징 클러스터에서 같은 매니페스트를 먼저 검증한다.",
    "{ko}와 관련된 이벤트는 일정 시간이 지나면 사라지므로 중요한 변경은 별도 기록으로 남긴다.",
    "{ko}의 레이블과 어노테이션은 서로 용도가 다르며 선택자는 레이블만 사용한다.",
    "{ko}를 자동화 도구로 관리한다면 선언된 상태와 실제 상태의 차이를 주기적으로 비교한다.",
    "{ko}에 대한 접근 권한은 롤과 롤바인딩으로 제한하고 감사 로그로 확인한다.",
    "{ko}의 장애를 줄이려면 헬스 체크와 준비 상태 확인을 함께 구성한다.",
    "{ko}를 삭제하기 전에 의존하는 다른 리소스가 없는지 확인한다.",
]
DETAIL_COUNT = {"small": 0, "medium": 2, "large": 8}


def web_doc(i: int, size: str) -> tuple[str, str]:
    """수집기 모양: (원본 HTML, 평문 본문). 평문 본문은 헤딩 표시를 잃은 줄들이고 빈 줄이 없다."""
    ko, en, definition, tko, ten = TOPICS[i % len(TOPICS)]
    fields = {"ko": ko, "en": en, "definition": definition, "tko": tko, "ten": ten, "en_l": en.lower()}
    html = [f"<html><head><title>{ko} | Kubernetes 문서</title></head><body>", f"<h1>{ko} ({en})</h1>"]
    lines = [f"{ko} | Kubernetes 문서", f"{ko} ({en})"]
    for heading, template in SECTION_POOL[: SIZES[size]]:
        para = template.format(**fields) + "".join(" " + d.format(**fields) for d in DETAILS[: DETAIL_COUNT[size]])
        html += [f"<h2>{heading}</h2>", f"<p>{para}</p>"]
        lines += [heading, para]
    html.append("</body></html>")
    return "".join(html), "\n".join(lines)


def upload_text(i: int) -> tuple[str, str]:
    ko, en, definition, tko, ten = TOPICS[i % len(TOPICS)]
    paragraphs = [
        f"# {ko} 정리",
        f"{ko}({en})는 {definition}.",
        f"{en} manages {ten} objects. 컨트롤러가 상태를 계속 조정한다.",
        f"## 주의\n{ko}를 삭제하면 연결된 {tko}가 정리된다.",
    ]
    return f"{ko} 노트 {i}", "\n\n".join(paragraphs)


async def seed() -> dict:
    stats = {"web_personal": 0, "web_work": 0, "upload_personal": 0, "upload_work": 0, "hidden": 0}
    base = datetime(2026, 9, 1, tzinfo=UTC)
    sizes = ["small", "medium", "large"]
    async with sessionmaker()() as db:
        # 수집기 모양 web 문서 24건(개인 21, 회사 3)
        for i in range(24):
            scope = "work" if i % 8 == 7 else "personal"
            raw_html, text = web_doc(i, sizes[i % 3])
            url = f"https://kubernetes.io/ko/docs/concepts/{i}/"
            key = "collector:" + hashlib.sha256(f"owner\x1f{scope}\x1fseed\x1f{url}".encode()).hexdigest()
            result = await ingest_source(
                db, source_key=key, data=raw_html.encode(), kind="web", locator=url, media_type="text/html",
                title=TOPICS[i % len(TOPICS)][0], lang="ko", parser_version="trafilatura-2.0.0+parse-1",
                collected_at=base + timedelta(days=i), normalized_body=text,
                origin_meta={"via": "collector", "tenant_id": "owner", "scope": scope},
            )
            db.add(KbDocumentAccess(document_id=result.document_id, tenant_id="owner", scope=scope))
            await segment_revision(db, result.revision_id)
            db.add(KbObservation(
                document_id=result.document_id, revision_id=result.revision_id, envelope_id=uuid.uuid4(),
                event_time=base + timedelta(days=i), provenance={"seed": "0071"},
            ))
            stats["web_work" if scope == "work" else "web_personal"] += 1
        await db.flush()
        # register_text 문서 12건(개인 8, 회사 4)
        for i in range(12):
            scope = "work" if i % 3 == 2 else "personal"
            title, text = upload_text(i)
            await register_text(db, title=title, text=text, source_key=None, lang="ko", locator=None, scope=scope)
            stats["upload_work" if scope == "work" else "upload_personal"] += 1
        await db.commit()
        # 일부 문서를 hidden 처리(수집 정책 철회 모양)
        hidden_ids = list(await db.scalars(select(KbDocumentAccess.document_id).where(
            KbDocumentAccess.scope == "personal").order_by(KbDocumentAccess.document_id).limit(2)))
        for doc_id in hidden_ids:
            row = await db.get(KbDocumentAccess, doc_id)
            row.hidden = True
            stats["hidden"] += 1
        await db.commit()
    return stats


if __name__ == "__main__":
    print(asyncio.run(seed()))
    sys.exit(0)
