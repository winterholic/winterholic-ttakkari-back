"""0071 시점(KB v3 이전) 코드 경로로 운영과 같은 모양의 기존 데이터를 시드한다(백필 리허설 전용).

이 스크립트는 0071 코드 체크아웃(예: 94a4963)의 가상환경으로 돌린다. KB v3 이전 API(register_text, ingest_source,
segment_revision, KbDocumentAccess(scope만), KbObservation)만 쓰므로 새 코드의 어떤 함수도 부르지 않는다.
운영 DB 에 쓰지 않는다: TTAKKARI_DATABASE_URL 이 가리키는 임시 DB 에만 쓴다.

운영 36건의 모양(설계 §1.3): 수집기 배달 문서는 본문이 한 덩어리(passage_count=1), 헤딩은 평문 줄이다.
- web 문서(수집기 모양): 빈 줄 없이 줄바꿈만 있는 평문 → 0071 의 문단 분리가 한 패시지로 만든다.
- upload 문서(register_text): 빈 줄로 나뉜 문단 → 문단별 패시지.
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

TOPICS = [
    ("디플로이먼트", "Deployment", "파드와 레플리카셋에 대한 선언적 업데이트를 제공한다", "레플리카셋", "ReplicaSet"),
    ("레플리카셋", "ReplicaSet", "지정된 수의 파드 복제본이 항상 실행되도록 보장한다", "파드", "Pod"),
    ("파드", "Pod", "쿠버네티스에서 배포할 수 있는 가장 작은 단위이다", "컨테이너", "container"),
    ("서비스", "Service", "파드 집합에 대한 안정적인 네트워크 엔드포인트를 노출한다", "파드", "Pod"),
    ("컨피그맵", "ConfigMap", "키-값 쌍으로 비밀이 아닌 설정 데이터를 저장한다", "파드", "Pod"),
    ("퍼시스턴트 볼륨", "PersistentVolume", "클러스터 관리자가 프로비저닝한 스토리지 조각이다", "스토리지클래스", "StorageClass"),
    ("스테이트풀셋", "StatefulSet", "상태를 가진 애플리케이션의 배포와 스케일링을 관리한다", "파드", "Pod"),
    ("데몬셋", "DaemonSet", "모든 노드에서 파드 사본이 하나씩 실행되도록 보장한다", "파드", "Pod"),
    ("인그레스", "Ingress", "클러스터 외부에서 서비스로 가는 HTTP 라우팅 규칙을 정의한다", "서비스", "Service"),
    ("네임스페이스", "Namespace", "단일 클러스터 안에서 리소스 그룹을 격리하는 메커니즘이다", "파드", "Pod"),
    ("시크릿", "Secret", "비밀번호나 토큰 같은 민감한 데이터를 저장한다", "파드", "Pod"),
    ("크론잡", "CronJob", "반복 일정에 따라 잡을 생성한다", "잡", "Job"),
]

SECTIONS = [
    ("개요", "{ko}({en})는 {definition}."),
    ("동작 방식", "{ko}는 {tko}를 관리한다. 컨트롤러는 현재 상태를 원하는 상태로 계속 수렴시킨다. 이 과정에서 {en}는 {ten} 객체와 연결된다."),
    ("사용 예시", "kubectl apply -f {en_l}.yaml 명령으로 {ko}를 생성한다. kubectl get {en_l} 로 상태를 확인한다. 기본값은 변경하지 않는 것이 좋다."),
    ("제약 사항", "{ko}는 네임스페이스 범위의 리소스이다. 이름은 DNS 레이블 규칙을 따라야 한다. 삭제하면 연결된 {tko}도 함께 정리된다."),
]


def web_text(i: int, size: str) -> str:
    """수집기 모양: 헤딩이 평문 줄이고 빈 줄이 없다(0071 분리기는 이를 한 패시지로 만든다)."""
    ko, en, definition, tko, ten = TOPICS[i % len(TOPICS)]
    lines = [f"{ko} | Kubernetes 문서", f"{ko} ({en})"]
    repeats = {"small": 1, "medium": 4, "large": 12}[size]
    for r in range(repeats):
        for name, template in SECTIONS:
            lines.append(name if r == 0 else f"{name} {r + 1}")
            lines.append(
                template.format(ko=ko, en=en, definition=definition, tko=tko, ten=ten, en_l=en.lower())
                + f" (문서 {i}, 절 {r + 1})"
            )
    return "\n".join(lines)


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
            text = web_text(i, sizes[i % 3])
            url = f"https://kubernetes.io/ko/docs/concepts/{i}/"
            key = "collector:" + hashlib.sha256(f"owner\x1f{scope}\x1fseed\x1f{url}".encode()).hexdigest()
            result = await ingest_source(
                db, source_key=key, data=text.encode(), kind="web", locator=url, media_type="text/html",
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
