"""한·영 시드 별칭 사전 (Bilingual Seed Aliases Dictionary).

개발(dev), 인프라(infra), AI(ai), 보안(security) 등 주요 기술 도메인의
표준 기술 용어 및 한·영·약어 별칭 체계를 제공하며, DB 테이블(kb_entities, kb_entity_aliases)과
결합하여 4단계 결정적 엔터티 해소의 기반으로 사용된다.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.common.entity_norm import norm_name
from ttakkari.kb_ontology_models import KbEntity, KbEntityAlias

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedEntity:
    """정적 시드 엔터티 및 별칭 정의."""

    canonical_name: str
    entity_kind: str  # concept, technology, system, standard, organization, api
    domain: str  # infra, dev, ai, security, general
    summary: str
    aliases: list[tuple[str, str | None, str]] = field(default_factory=list)
    # tuple: (alias, lang, alias_kind)
    # lang: 'ko', 'en', None
    # alias_kind: 'canonical', 'synonym', 'abbrev', 'translation'


# 170개 핵심 기술 시드 정의 목록
RAW_SEED_ENTITIES: list[SeedEntity] = [
    # -------------------------------------------------------------
    # 1. Kubernetes & Container Ecosystem (25개)
    # -------------------------------------------------------------
    SeedEntity(
        canonical_name="Kubernetes",
        entity_kind="technology",
        domain="infra",
        summary="컨테이너화된 워크로드와 서비스를 관리하기 위한 오픈소스 컨테이너 오케스트레이션 플랫폼.",
        aliases=[
            ("쿠버네티스", "ko", "translation"),
            ("k8s", "en", "abbrev"),
            ("컨테이너 오케스트레이션", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Pod",
        entity_kind="concept",
        domain="infra",
        summary="쿠버네티스에서 생성하고 관리할 수 있는 배포 가능한 가장 작은 컴퓨팅 단위.",
        aliases=[
            ("파드", "ko", "translation"),
            ("배포 단위", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Deployment",
        entity_kind="concept",
        domain="infra",
        summary="파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 쿠버네티스 워크로드 리소스.",
        aliases=[
            ("디플로이먼트", "ko", "translation"),
            ("배포 컨트롤러", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="ReplicaSet",
        entity_kind="concept",
        domain="infra",
        summary="지정된 수의 파드 복제본이 항상 실행되도록 보장하는 쿠버네티스 컨트롤러.",
        aliases=[
            ("레플리카셋", "ko", "translation"),
            ("복제본 셋", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="StatefulSet",
        entity_kind="concept",
        domain="infra",
        summary="상태 유지가 필요한 애플리케이션 관리를 위해 고유한 파드 식별자와 영구 스토리지를 제공하는 리소스.",
        aliases=[
            ("스테이트풀셋", "ko", "translation"),
            ("상태 유지 셋", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="DaemonSet",
        entity_kind="concept",
        domain="infra",
        summary="클러스터의 모든(또는 일부) 노드에 파드의 복제본을 하나씩 실행하도록 보장하는 리소스.",
        aliases=[
            ("데몬셋", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Job",
        entity_kind="concept",
        domain="infra",
        summary="하나 이상의 파드가 성공적으로 완료될 때까지 실행되는 배치 작업을 관리하는 리소스.",
        aliases=[
            ("잡", "ko", "translation"),
            ("배치 작업", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="CronJob",
        entity_kind="concept",
        domain="infra",
        summary="시간 기반 일정에 따라 주기적으로 반복 실행되는 쿠버네티스 작업.",
        aliases=[
            ("크론잡", "ko", "translation"),
            ("주기적 작업", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Service",
        entity_kind="concept",
        domain="infra",
        summary="파드 집합에서 실행 중인 애플리케이션을 네트워크 서비스로 노출하는 추상화 방법.",
        aliases=[
            ("서비스", "ko", "translation"),
            ("svc", "en", "abbrev"),
            ("쿠버네티스 서비스", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Ingress",
        entity_kind="concept",
        domain="infra",
        summary="클러스터 외부에서 내부 서비스로의 HTTP 및 HTTPS 라우팅을 관리하는 리소스.",
        aliases=[
            ("인그레스", "ko", "translation"),
            ("L7 라우터", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="ConfigMap",
        entity_kind="concept",
        domain="infra",
        summary="기밀이 아닌 데이터를 키-값 쌍으로 저장하여 환경설정을 파드와 분리하는 리소스.",
        aliases=[
            ("컨피그맵", "ko", "translation"),
            ("cm", "en", "abbrev"),
            ("설정 맵", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Secret",
        entity_kind="concept",
        domain="infra",
        summary="비밀번호, 토큰, 키와 같은 민감한 정보를 안전하게 보관하고 관리하는 리소스.",
        aliases=[
            ("시크릿", "ko", "translation"),
            ("비밀 데이터", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="PersistentVolume",
        entity_kind="concept",
        domain="infra",
        summary="관리자가 프로비저닝하거나 동적으로 프로비저닝된 클러스터의 네트워크 스토리지 조각.",
        aliases=[
            ("퍼시스턴트 볼륨", "ko", "translation"),
            ("영구 볼륨", "ko", "synonym"),
            ("PV", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="PersistentVolumeClaim",
        entity_kind="concept",
        domain="infra",
        summary="사용자가 스토리지 용량 및 접근 모드를 명시하여 스토리지를 요청하는 리소스.",
        aliases=[
            ("퍼시스턴트 볼륨 클레임", "ko", "translation"),
            ("영구 볼륨 요청", "ko", "synonym"),
            ("PVC", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="StorageClass",
        entity_kind="concept",
        domain="infra",
        summary="관리자가 제공하는 스토리지의 프로파일(프로비저너, 파라미터)을 기술하는 리소스.",
        aliases=[
            ("스토리지클래스", "ko", "translation"),
            ("저장소 클래스", "ko", "synonym"),
            ("sc", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Namespace",
        entity_kind="concept",
        domain="infra",
        summary="단일 클러스터 내에서 여러 사용자나 팀 간 리소스를 격리하고 범위를 나누는 가상 클러스터.",
        aliases=[
            ("네임스페이스", "ko", "translation"),
            ("이름 공간", "ko", "synonym"),
            ("ns", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="CustomResourceDefinition",
        entity_kind="concept",
        domain="infra",
        summary="쿠버네티스 API를 확장하여 커스텀 리소스를 정의할 수 있게 해주는 기능.",
        aliases=[
            ("커스텀 리소스 정의", "ko", "translation"),
            ("CRD", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Helm",
        entity_kind="technology",
        domain="infra",
        summary="쿠버네티스 애플리케이션을 패키징, 구성 및 배포하기 위한 패키지 관리자.",
        aliases=[
            ("헬름", "ko", "translation"),
            ("쿠버네티스 패키지 매니저", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Docker",
        entity_kind="technology",
        domain="infra",
        summary="애플리케이션을 가벼운 컨테이너 단위로 빌드, 공유 및 실행할 수 있는 오픈소스 플랫폼.",
        aliases=[
            ("도커", "ko", "translation"),
            ("컨테이너 엔진", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Containerd",
        entity_kind="technology",
        domain="infra",
        summary="단순성, 견고성 및 이식성을 강조하는 산업 표준 코어 컨테이너 런타임.",
        aliases=[
            ("컨테이너디", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="CRI-O",
        entity_kind="technology",
        domain="infra",
        summary="쿠버네티스 컨테이너 런타임 인터페이스(CRI) 전용 경량 런타임 구현체.",
        aliases=[
            ("크라이오", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Kubelet",
        entity_kind="system",
        domain="infra",
        summary="클러스터의 각 노드에서 실행되며 파드 내 컨테이너의 정상 동작을 책임지는 주 노드 에이전트.",
        aliases=[
            ("쿠블렛", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Kube-proxy",
        entity_kind="system",
        domain="infra",
        summary="각 노드에서 실행되며 쿠버네티스 서비스 추상화를 위한 네트워크 라우팅 및 패킷 포워딩을 수행하는 네트워크 프록시.",
        aliases=[
            ("쿠브프록시", "ko", "translation"),
            ("kube proxy", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Etcd",
        entity_kind="technology",
        domain="infra",
        summary="쿠버네티스 클러스터의 모든 상태와 메타데이터를 저장하는 분산 고가용성 키-값 저장소.",
        aliases=[
            ("엣씨디", "ko", "translation"),
            ("etcd 분산 저장소", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Istio",
        entity_kind="technology",
        domain="infra",
        summary="마이크로서비스 간 트래픽 관리, 보안, 관측성을 제공하는 오픈소스 서비스 메시 플랫폼.",
        aliases=[
            ("이스티오", "ko", "translation"),
            ("서비스 메시", "ko", "synonym"),
        ],
    ),
    # -------------------------------------------------------------
    # 2. Cloud & Infrastructure & CI/CD (25개)
    # -------------------------------------------------------------
    SeedEntity(
        canonical_name="Terraform",
        entity_kind="technology",
        domain="infra",
        summary="선언적 구성 파일을 사용하여 인프라를 안전하고 효율적으로 빌드, 변경, 버전 관리하는 IaC 도구.",
        aliases=[
            ("테라폼", "ko", "translation"),
            ("IaC", "en", "abbrev"),
            ("Infrastructure as Code", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Ansible",
        entity_kind="technology",
        domain="infra",
        summary="에이전트 없이 SSH를 통해 시스템 구성 관리와 애플리케이션 배포를 자동화하는 IT 자동화 엔진.",
        aliases=[
            ("앤서블", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="OpenTofu",
        entity_kind="technology",
        domain="infra",
        summary="Linux Foundation 산하의 테라폼 호환 오픈소스 인프라스트럭처 코드(IaC) 도구.",
        aliases=[
            ("오픈토푸", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Envoy",
        entity_kind="technology",
        domain="infra",
        summary="클라우드 네이티브 애플리케이션을 위해 설계된 고성능 L4/L7 분산 서비스 프록시.",
        aliases=[
            ("엔보이", "ko", "translation"),
            ("엔보이 프록시", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Nginx",
        entity_kind="technology",
        domain="infra",
        summary="웹 서버, 리버스 프록시, 캐싱, 로드 밸런서 기능을 제공하는 고성능 HTTP 및 역방향 프록시 서버.",
        aliases=[
            ("엔진엑스", "ko", "translation"),
            ("nginx 웹서버", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="HAProxy",
        entity_kind="technology",
        domain="infra",
        summary="고가용성, 부하 분산, TCP 및 HTTP 기반 애플리케이션 프록시를 제공하는 오픈소스 솔루션.",
        aliases=[
            ("에이치에이프록시", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="ArgoCD",
        entity_kind="technology",
        domain="infra",
        summary="쿠버네티스를 위한 선언적 GitOps 지속적 배포(CD) 도구.",
        aliases=[
            ("아르고CD", "ko", "translation"),
            ("Argo CD", "en", "synonym"),
            ("GitOps 배포기", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Flux",
        entity_kind="technology",
        domain="infra",
        summary="쿠버네티스 클러스터 상태를 Git 저장소와 자동으로 동기화하는 오픈소스 GitOps 도구 세트.",
        aliases=[
            ("플럭스", "ko", "translation"),
            ("FluxCD", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Jenkins",
        entity_kind="technology",
        domain="infra",
        summary="소프트웨어 개발의 빌드, 테스트, 배포를 자동화하는 오픈소스 지속적 통합(CI) 서버.",
        aliases=[
            ("젠킨스", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="GitHub Actions",
        entity_kind="technology",
        domain="infra",
        summary="GitHub 저장소 내에서 워크플로를 생성하여 코드 빌드, 테스트, 릴리스를 자동화하는 CI/CD 플랫폼.",
        aliases=[
            ("깃허브 액션", "ko", "translation"),
            ("GHA", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="GitLab CI",
        entity_kind="technology",
        domain="infra",
        summary="GitLab에 내장되어 커밋 시점에 자동으로 파이프라인을 실행하는 지속적 통합/배포 플랫폼.",
        aliases=[
            ("깃랩 CI", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Prometheus",
        entity_kind="technology",
        domain="infra",
        summary="시계열 데이터 모델과 강력한 PromQL 쿼리 언어를 갖춘 오픈소스 모니터링 및 경보 툴킷.",
        aliases=[
            ("프로메테우스", "ko", "translation"),
            ("시계열 모니터링", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Grafana",
        entity_kind="technology",
        domain="infra",
        summary="다양한 데이터베이스와 메트릭 소스를 시각화하고 대시보드를 구성하는 오픈소스 분석 플랫폼.",
        aliases=[
            ("그라파나", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="OpenTelemetry",
        entity_kind="technology",
        domain="infra",
        summary="원격 분석 데이터(메트릭, 로그, 트레이스)를 생성하고 수집하기 위한 오픈소스 가시성 프레임워크.",
        aliases=[
            ("오픈텔레메트리", "ko", "translation"),
            ("OTel", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Jaeger",
        entity_kind="technology",
        domain="infra",
        summary="마이크로서비스 기반 분산 시스템의 트랜잭션을 추적하고 성능 병목을 모니터링하는 분산 추적 시스템.",
        aliases=[
            ("예거", "ko", "translation"),
            ("분산 트레이싱", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Fluentd",
        entity_kind="technology",
        domain="infra",
        summary="통합 로깅 계층을 제공하여 다양한 데이터 소스에서 로그를 수집하고 전달하는 오픈소스 데이터 수집기.",
        aliases=[
            ("플루언트디", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Logstash",
        entity_kind="technology",
        domain="infra",
        summary="다양한 소스에서 동시에 데이터를 수집하여 변환한 후 지정된 저장소로 전달하는 오픈소스 서버 측 데이터 처리 파이프라인.",
        aliases=[
            ("로그스태시", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Loki",
        entity_kind="technology",
        domain="infra",
        summary="로그 내용 전체를 인덱싱하지 않고 메타데이터 레이블만 인덱싱하는 수평 확장 가능한 고효율 로그 집계 시스템.",
        aliases=[
            ("로키", "ko", "translation"),
            ("Grafana Loki", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Cloudflare",
        entity_kind="organization",
        domain="infra",
        summary="CDN, DNS, DDoS 방어, 보안 및 엣지 컴퓨팅 서비스를 제공하는 글로벌 웹 인프라 기업.",
        aliases=[
            ("클라우드플레어", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Amazon Web Services",
        entity_kind="technology",
        domain="infra",
        summary="온디맨드 클라우드 컴퓨팅 플랫폼과 API를 제공하는 아마존의 포괄적인 클라우드 플랫폼.",
        aliases=[
            ("아마존 웹 서비스", "ko", "translation"),
            ("AWS", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Google Cloud Platform",
        entity_kind="technology",
        domain="infra",
        summary="구글의 인프라스트럭처에서 실행되는 컴퓨팅, 스토리지, 네트워킹 및 데이터 분석 클라우드 서비스 세트.",
        aliases=[
            ("구글 클라우드 플랫폼", "ko", "translation"),
            ("GCP", "en", "abbrev"),
            ("Google Cloud", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Microsoft Azure",
        entity_kind="technology",
        domain="infra",
        summary="마이크로소프트가 관리하는 글로벌 데이터 센터 네트워크를 통해 제공되는 클라우드 컴퓨팅 서비스.",
        aliases=[
            ("애저", "ko", "translation"),
            ("Azure", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Virtual Private Cloud",
        entity_kind="concept",
        domain="infra",
        summary="퍼블릭 클라우드 내에서 논리적으로 격리된 가상 사설 네트워크 공간.",
        aliases=[
            ("가상 사설 클라우드", "ko", "translation"),
            ("VPC", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Load Balancer",
        entity_kind="concept",
        domain="infra",
        summary="네트워크 트래픽을 여러 백엔드 서버 풀에 효율적으로 분산하여 가용성과 확장성을 높이는 장치/서비스.",
        aliases=[
            ("로드 밸런서", "ko", "translation"),
            ("부하 분산기", "ko", "synonym"),
            ("LB", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Domain Name System",
        entity_kind="standard",
        domain="infra",
        summary="호스트 이름을 네트워크 장치가 인식할 수 있는 IP 주소로 변환하는 계층적 분산 네이밍 시스템.",
        aliases=[
            ("도메인 네임 시스템", "ko", "translation"),
            ("DNS", "en", "abbrev"),
        ],
    ),
    # -------------------------------------------------------------
    # 3. Databases, Caching & Message Queues (25개)
    # -------------------------------------------------------------
    SeedEntity(
        canonical_name="PostgreSQL",
        entity_kind="technology",
        domain="dev",
        summary="신뢰성, 기능 견고성, 성능으로 명성이 높은 강력한 오픈소스 객체-관계형 데이터베이스 시스템.",
        aliases=[
            ("포스트그레SQL", "ko", "translation"),
            ("Postgres", "en", "synonym"),
            ("포스트그레스", "ko", "translation"),
            ("pgsql", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="MySQL",
        entity_kind="technology",
        domain="dev",
        summary="세계에서 가장 널리 사용되는 오픈소스 관계형 데이터베이스 관리 시스템(RDBMS).",
        aliases=[
            ("마이에스큐엘", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="MariaDB",
        entity_kind="technology",
        domain="dev",
        summary="MySQL의 오픈소스 포크로 높은 호환성과 향상된 성능을 제공하는 관계형 데이터베이스.",
        aliases=[
            ("마리아DB", "ko", "translation"),
            ("마리아디비", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="SQLite",
        entity_kind="technology",
        domain="dev",
        summary="별도의 서버 프로세스 없이 애플리케이션에 직접 임베디드되는 C 언어 라이브러리 기반 파일형 SQL 데이터베이스.",
        aliases=[
            ("에스큐엘라이트", "ko", "translation"),
            ("스큐라이트", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="MongoDB",
        entity_kind="technology",
        domain="dev",
        summary="JSON 형태의 유연한 문서(document) 모델을 사용하는 선도적인 NoSQL 데이터베이스.",
        aliases=[
            ("몽고DB", "ko", "translation"),
            ("몽고디비", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Redis",
        entity_kind="technology",
        domain="infra",
        summary="데이터베이스, 캐시, 메시징 브로커, 스트리밍 엔진으로 사용되는 초고속 인메모리 데이터 구조 저장소.",
        aliases=[
            ("레디스", "ko", "translation"),
            ("인메모리 캐시", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Memcached",
        entity_kind="technology",
        domain="infra",
        summary="동적 웹 애플리케이션의 데이터베이스 부하를 줄이기 위한 고성능 분산 메모리 객체 캐싱 시스템.",
        aliases=[
            ("멤캐시드", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Apache Kafka",
        entity_kind="technology",
        domain="infra",
        summary="고성능 데이터 파이프라인, 스트리밍 분석 및 데이터 통합을 위한 분산 이벤트 스트리밍 플랫폼.",
        aliases=[
            ("카프카", "ko", "translation"),
            ("Kafka", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="RabbitMQ",
        entity_kind="technology",
        domain="infra",
        summary="AMQP 등 여러 메시징 프로토콜을 지원하는 널리 배포된 오픈소스 메시지 브로커.",
        aliases=[
            ("래빗MQ", "ko", "translation"),
            ("래빗엠큐", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Apache Pulsar",
        entity_kind="technology",
        domain="infra",
        summary="컴퓨팅과 스토리지가 분리된 아키텍처를 가진 분산 분할 게시-구독 메시징 및 스트리밍 시스템.",
        aliases=[
            ("펄사", "ko", "translation"),
            ("Pulsar", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="ClickHouse",
        entity_kind="technology",
        domain="dev",
        summary="실시간 분석 보고서 생성을 지원하는 초고속 오픈소스 컬럼 지향 OLAP DBMS.",
        aliases=[
            ("클릭하우스", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Apache Cassandra",
        entity_kind="technology",
        domain="dev",
        summary="단일 장애점 없이 대규모 데이터를 처리하도록 설계된 오픈소스 분산 광역 컬럼 NoSQL 데이터베이스.",
        aliases=[
            ("카산드라", "ko", "translation"),
            ("Cassandra", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Amazon DynamoDB",
        entity_kind="technology",
        domain="dev",
        summary="어떤 규모에서도 한 자릿수 밀리초 성능을 제공하는 완전관리형 NoSQL 데이터베이스 서비스.",
        aliases=[
            ("다이나모DB", "ko", "translation"),
            ("DynamoDB", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Elasticsearch",
        entity_kind="technology",
        domain="dev",
        summary="Lucene 기반으로 JSON 문서를 실시간 색인하고 분산 검색·분석을 제공하는 검색 엔진.",
        aliases=[
            ("엘라스틱서치", "ko", "translation"),
            ("ES", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="OpenSearch",
        entity_kind="technology",
        domain="dev",
        summary="Elasticsearch에서 분기된 커뮤니티 주도 오픈소스 분산 검색 및 분석 제품군.",
        aliases=[
            ("오픈서치", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Neo4j",
        entity_kind="technology",
        domain="dev",
        summary="노드, 관계, 속성으로 이루어진 그래프 구조를 효율적으로 쿼리할 수 있는 오픈소스 그래프 데이터베이스.",
        aliases=[
            ("네오포제이", "ko", "translation"),
            ("그래프DB", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Apache Spark",
        entity_kind="technology",
        domain="dev",
        summary="대규모 데이터 처리를 위한 통합 분석 엔진으로 인메모리 연산을 지원한다.",
        aliases=[
            ("스파크", "ko", "translation"),
            ("Spark", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Snowflake",
        entity_kind="technology",
        domain="dev",
        summary="클라우드 전용으로 구축된 완전관리형 데이터 웨어하우스 및 데이터 레이크 플랫폼.",
        aliases=[
            ("스노우플레이크", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="BigQuery",
        entity_kind="technology",
        domain="dev",
        summary="페타바이트급 데이터에 대해 초고속 SQL 쿼리를 실행할 수 있는 완전관리형 엔터프라이즈 데이터 웨어하우스.",
        aliases=[
            ("빅쿼리", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="DuckDB",
        entity_kind="technology",
        domain="dev",
        summary="분석용 쿼리(OLAP)에 최적화된 임베디드 컬럼 지향 인메모리 SQL 데이터베이스.",
        aliases=[
            ("덕DB", "ko", "translation"),
            ("덕디비", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Milvus",
        entity_kind="technology",
        domain="ai",
        summary="수십억 개의 벡터 임베딩을 저장, 색인, 관리하도록 설계된 오픈소스 벡터 데이터베이스.",
        aliases=[
            ("밀버스", "ko", "translation"),
            ("벡터 데이터베이스", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Qdrant",
        entity_kind="technology",
        domain="ai",
        summary="Rust로 작성된 고성능 벡터 유사도 검색 엔진 및 벡터 데이터베이스.",
        aliases=[
            ("큐드란트", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Chroma",
        entity_kind="technology",
        domain="ai",
        summary="LLM 애플리케이션 구축을 위해 설계된 오픈소스 AI 임베딩 데이터베이스.",
        aliases=[
            ("크로마", "ko", "translation"),
            ("ChromaDB", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Pgvector",
        entity_kind="technology",
        domain="dev",
        summary="PostgreSQL에 오픈소스 벡터 유사도 검색 기능을 추가하는 익스텐션.",
        aliases=[
            ("피지벡터", "ko", "translation"),
            ("pg_vector", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="ScyllaDB",
        entity_kind="technology",
        domain="dev",
        summary="C++로 재작성되어 매우 낮은 지연 시간과 높은 처리량을 제공하는 카산드라/다이나모 호환 NoSQL 데이터베이스.",
        aliases=[
            ("실라DB", "ko", "translation"),
        ],
    ),
    # -------------------------------------------------------------
    # 4. Languages, Runtimes & Protocols (25개)
    # -------------------------------------------------------------
    SeedEntity(
        canonical_name="Python",
        entity_kind="technology",
        domain="dev",
        summary="가독성이 높고 간결한 문법을 제공하여 웹 개발, 데이터 분석, AI 연구에 널리 사용되는 고급 프로그래밍 언어.",
        aliases=[
            ("파이썬", "ko", "translation"),
            ("py", "en", "abbrev"),
            ("파이썬 언어", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Go",
        entity_kind="technology",
        domain="dev",
        summary="구글이 개발한 정적 타입 컴파일 프로그래밍 언어로 간결한 구문과 강력한 동시성(고루틴)을 제공한다.",
        aliases=[
            ("고랭", "ko", "translation"),
            ("Golang", "en", "synonym"),
            ("고 언어", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Rust",
        entity_kind="technology",
        domain="dev",
        summary="소유권 모델을 통해 가비지 컬렉터 없이도 메모리 안전성과 고성능을 보장하는 시스템 프로그래밍 언어.",
        aliases=[
            ("러스트", "ko", "translation"),
            ("러스트 언어", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="TypeScript",
        entity_kind="technology",
        domain="dev",
        summary="자바스크립트에 정적 타입을 추가하여 대규모 애플리케이션 개발을 지원하는 오픈소스 언어.",
        aliases=[
            ("타입스크립트", "ko", "translation"),
            ("TS", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="JavaScript",
        entity_kind="technology",
        domain="dev",
        summary="웹 브라우저의 대화형 동작을 가능하게 하고 서버사이드(Node.js)에서도 널리 쓰이는 표준 스크립트 언어.",
        aliases=[
            ("자바스크립트", "ko", "translation"),
            ("JS", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Java",
        entity_kind="technology",
        domain="dev",
        summary="JVM 위에서 실행되어 플랫폼 독립성을 제공하는 객체지향 프로그래밍 언어.",
        aliases=[
            ("자바", "ko", "translation"),
            ("자바 언어", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Kotlin",
        entity_kind="technology",
        domain="dev",
        summary="JVM과 안드로이드 개발을 위해 JetBrains에서 개발한 현대적인 간결한 정적 타입 언어.",
        aliases=[
            ("코틀린", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Swift",
        entity_kind="technology",
        domain="dev",
        summary="애플이 iOS, macOS, watchOS, tvOS 앱 개발을 위해 설계한 직관적이고 안전한 프로그래밍 언어.",
        aliases=[
            ("스위프트", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="C++",
        entity_kind="technology",
        domain="dev",
        summary="C 언어에 객체지향 및 제네릭 프로그래밍 기능을 추가한 고성능 컴파일 시스템 프로그래밍 언어.",
        aliases=[
            ("씨플플", "ko", "translation"),
            ("Cpp", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="C",
        entity_kind="technology",
        domain="dev",
        summary="운영체제와 임베디드 시스템 개발에 널리 사용되는 절차적 시스템 프로그래밍 언어의 근간.",
        aliases=[
            ("씨 언어", "ko", "translation"),
            ("C 언어", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="C#",
        entity_kind="technology",
        domain="dev",
        summary="마이크로소프트가 .NET 플랫폼을 위해 개발한 현대적인 범용 객체지향 프로그래밍 언어.",
        aliases=[
            ("씨샵", "ko", "translation"),
            ("CSharp", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Ruby",
        entity_kind="technology",
        domain="dev",
        summary="프로그래머의 생산성과 즐거움을 강조하는 간결하고 읽기 쉬운 동적 객체지향 언어.",
        aliases=[
            ("루비", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="PHP",
        entity_kind="technology",
        domain="dev",
        summary="웹 개발에 널리 사용되는 서버 측 범용 스크립팅 언어.",
        aliases=[
            ("피에이치피", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Node.js",
        entity_kind="technology",
        domain="dev",
        summary="Chrome V8 자바스크립트 엔진으로 구동되는 이벤트 기반 비동기 I/O 런타임 환경.",
        aliases=[
            ("노드제이에스", "ko", "translation"),
            ("Node", "en", "synonym"),
            ("노드", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Deno",
        entity_kind="technology",
        domain="dev",
        summary="기본 보안과 TypeScript 지원을 내장한 V8 및 Rust 기반의 현대적인 JS/TS 런타임.",
        aliases=[
            ("디노", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Bun",
        entity_kind="technology",
        domain="dev",
        summary="번들러, 트랜스파일러, 패키지 매니저를 통합한 초고속 일체형 JavaScript 런타임.",
        aliases=[
            ("번", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="WebAssembly",
        entity_kind="standard",
        domain="dev",
        summary="웹 브라우저에서 네이티브에 가까운 속도로 코드를 실행할 수 있게 해주는 이진 명령어 형식.",
        aliases=[
            ("웹어셈블리", "ko", "translation"),
            ("Wasm", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="gRPC",
        entity_kind="technology",
        domain="dev",
        summary="HTTP/2 및 프로토콜 버퍼 기반으로 고성능 원격 프로시저 호출을 제공하는 오픈소스 프레임워크.",
        aliases=[
            ("지알피씨", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="GraphQL",
        entity_kind="standard",
        domain="dev",
        summary="클라이언트가 필요한 데이터만 정확히 요청할 수 있도록 설계된 API 쿼리 언어 및 런타임.",
        aliases=[
            ("그래프QL", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="REST",
        entity_kind="standard",
        domain="dev",
        summary="웹 표준 기술(HTTP 메서드, URI)을 활용하여 무상태(stateless)로 리소스를 다루는 소프트웨어 아키텍처 스타일.",
        aliases=[
            ("레스트", "ko", "translation"),
            ("RESTful API", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="WebSocket",
        entity_kind="standard",
        domain="dev",
        summary="단일 TCP 연결을 통해 전이중(full-duplex) 실시간 양방향 통신 채널을 제공하는 컴퓨터 통신 프로토콜.",
        aliases=[
            ("웹소켓", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="MQTT",
        entity_kind="standard",
        domain="infra",
        summary="낮은 대역폭과 제한된 네트워크 환경의 IoT 기기를 위한 경량 게시-구독 메시징 프로토콜.",
        aliases=[
            ("엠큐티티", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="HTTP/2",
        entity_kind="standard",
        domain="dev",
        summary="단일 연결 다중화(multiplexing), 헤더 압축 및 서버 푸시를 도입한 웹 전송 프로토콜의 2세대 개정판.",
        aliases=[
            ("에이치티티피2", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="HTTP/3",
        entity_kind="standard",
        domain="dev",
        summary="TCP 대신 UDP 기반의 QUIC 전송 계층을 사용하여 HOL 블로킹을 해결한 3세대 웹 프로토콜.",
        aliases=[
            ("에이치티티피3", "ko", "translation"),
            ("QUIC", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Protocol Buffers",
        entity_kind="technology",
        domain="dev",
        summary="구글이 개발한 언어 및 플랫폼 중립적인 확장 가능한 직렬화 구조화 데이터 메커니즘.",
        aliases=[
            ("프로토콜 버퍼", "ko", "translation"),
            ("Protobuf", "en", "abbrev"),
        ],
    ),
    # -------------------------------------------------------------
    # 5. Web Frameworks & Libraries (20개)
    # -------------------------------------------------------------
    SeedEntity(
        canonical_name="FastAPI",
        entity_kind="technology",
        domain="dev",
        summary="Python 표준 타입 힌트를 기반으로 초고속 성능과 자동 OpenAPI 문서를 제공하는 최신 웹 프레임워크.",
        aliases=[
            ("패스트API", "ko", "translation"),
            ("fastapi", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Django",
        entity_kind="technology",
        domain="dev",
        summary="신속한 개발과 깔끔한 실용적 디자인을 장려하는 고수준 Python 풀스택 웹 프레임워크.",
        aliases=[
            ("장고", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Flask",
        entity_kind="technology",
        domain="dev",
        summary="단순성과 유연성에 중점을 둔 가벼운 Python 마이크로 웹 프레임워크.",
        aliases=[
            ("플라스크", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Express",
        entity_kind="technology",
        domain="dev",
        summary="Node.js를 위한 작고 유연한 사실상의 표준 웹 애플리케이션 프레임워크.",
        aliases=[
            ("익스프레스", "ko", "translation"),
            ("Express.js", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="NestJS",
        entity_kind="technology",
        domain="dev",
        summary="효율적이고 확장 가능한 Node.js 서버 애플리케이션을 구축하기 위한 진보적인 TypeScript 프레임워크.",
        aliases=[
            ("네스트제이에스", "ko", "translation"),
            ("Nest", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Spring Boot",
        entity_kind="technology",
        domain="dev",
        summary="독립 실행형 프로덕션 등급의 스프링 기반 애플리케이션을 쉽게 만들 수 있는 자바 프레임워크.",
        aliases=[
            ("스프링 부트", "ko", "translation"),
            ("스프링부트", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="React",
        entity_kind="technology",
        domain="dev",
        summary="컴포넌트 기반 아키텍처와 가상 DOM을 사용하여 사용자 인터페이스를 구축하기 위한 자바스크립트 라이브러리.",
        aliases=[
            ("리액트", "ko", "translation"),
            ("ReactJS", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Next.js",
        entity_kind="technology",
        domain="dev",
        summary="서버 사이드 렌더링(SSR)과 정적 사이트 생성을 지원하는 대표적인 풀스택 React 프레임워크.",
        aliases=[
            ("넥스트제이에스", "ko", "translation"),
            ("NextJS", "en", "synonym"),
            ("Next", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Vue.js",
        entity_kind="technology",
        domain="dev",
        summary="사용자 인터페이스 구축을 위한 점진적(progressive) 자바스크립트 프레임워크.",
        aliases=[
            ("뷰제이에스", "ko", "translation"),
            ("Vue", "en", "synonym"),
            ("뷰", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Nuxt.js",
        entity_kind="technology",
        domain="dev",
        summary="Vue.js 기반으로 범용 웹 애플리케이션(SSR/SSG)을 쉽게 개발할 수 있도록 돕는 프레임워크.",
        aliases=[
            ("넉스트제이에스", "ko", "translation"),
            ("Nuxt", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Svelte",
        entity_kind="technology",
        domain="dev",
        summary="가상 DOM 대신 빌드 시점에 코드를 고효율 순수 자바스크립트로 컴파일하는 혁신적인 UI 컴파일러.",
        aliases=[
            ("스벨트", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Tailwind CSS",
        entity_kind="technology",
        domain="dev",
        summary="마크업 내에서 빠르게 스타일을 조립할 수 있는 유틸리티 우선(utility-first) CSS 프레임워크.",
        aliases=[
            ("테일윈드", "ko", "translation"),
            ("Tailwind", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="SQLAlchemy",
        entity_kind="technology",
        domain="dev",
        summary="데이터베이스 작업을 추상화하고 강력한 객체 관계형 매핑을 제공하는 Python SQL 툴킷 및 ORM.",
        aliases=[
            ("에스큐엘알케미", "ko", "translation"),
            ("알케미", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Pydantic",
        entity_kind="technology",
        domain="dev",
        summary="Python 타입 주석을 기반으로 데이터 유효성 검사 및 설정 관리를 수행하는 라이브러리.",
        aliases=[
            ("파이댄틱", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Prisma",
        entity_kind="technology",
        domain="dev",
        summary="TypeScript와 Node.js를 위한 차세대 오픈소스 ORM으로 직관적인 데이터 모델링과 타입 안전성을 제공한다.",
        aliases=[
            ("프리즈마", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Celery",
        entity_kind="technology",
        domain="dev",
        summary="분산 메시지 전달을 기반으로 하는 비동기 태스크 큐/작업 큐 시스템.",
        aliases=[
            ("샐러리", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Apache Airflow",
        entity_kind="technology",
        domain="dev",
        summary="프로그래밍 방식으로 워크플로를 작성, 예약, 모니터링하기 위한 오픈소스 데이터 파이프라인 오케스트레이션 플랫폼.",
        aliases=[
            ("에어플로우", "ko", "translation"),
            ("Airflow", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Temporal",
        entity_kind="technology",
        domain="dev",
        summary="분산 시스템에서 실패 시에도 신뢰할 수 있게 실행을 보장하는 내구성 있는 코드 기반 워크플로 엔진.",
        aliases=[
            ("템포럴", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Playwright",
        entity_kind="technology",
        domain="dev",
        summary="단일 API로 Chromium, Firefox, WebKit 전반에서 안정적인 엔드투엔드 웹 테스트 및 브라우저 자동화를 지원하는 프레임워크.",
        aliases=[
            ("플레이라이트", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Vite",
        entity_kind="technology",
        domain="dev",
        summary="기본 ES 모듈과 Rollup을 기반으로 번개처럼 빠른 로컬 서버 구동과 빌드를 제공하는 프론트엔드 도구.",
        aliases=[
            ("비트", "ko", "translation"),
            ("바이트", "ko", "translation"),
        ],
    ),
    # -------------------------------------------------------------
    # 6. AI, Machine Learning & LLM (30개)
    # -------------------------------------------------------------
    SeedEntity(
        canonical_name="Large Language Model",
        entity_kind="concept",
        domain="ai",
        summary="방대한 양의 텍스트 데이터로 학습되어 인간과 유사한 자연어 이해 및 생성 능력을 갖춘 딥러닝 신경망 모델.",
        aliases=[
            ("거대 언어 모델", "ko", "translation"),
            ("대형 언어 모델", "ko", "translation"),
            ("LLM", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Retrieval-Augmented Generation",
        entity_kind="concept",
        domain="ai",
        summary="외부 지식 소스에서 관련 정보를 검색하여 생성 모델의 입력 문맥에 결합함으로써 환각을 줄이고 정확도를 높이는 기법.",
        aliases=[
            ("검색 증강 생성", "ko", "translation"),
            ("RAG", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Transformer",
        entity_kind="concept",
        domain="ai",
        summary="어텐션 메커니즘을 전적으로 활용하여 시퀀스 내 모든 요소 간의 상호작용을 병렬로 계산하는 딥러닝 아키텍처.",
        aliases=[
            ("트랜스포머", "ko", "translation"),
            ("트랜스포머 모델", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Attention Mechanism",
        entity_kind="concept",
        domain="ai",
        summary="모델이 입력 시퀀스의 특정 부분에 동적으로 가중치를 부여하여 집중할 수 있도록 돕는 신경망 메커니즘.",
        aliases=[
            ("어텐션 메커니즘", "ko", "translation"),
            ("주의 집중 메커니즘", "ko", "translation"),
            ("Attention", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Vector Embedding",
        entity_kind="concept",
        domain="ai",
        summary="단어, 문장, 문서 등의 고차원 의미론적 특성을 저차원의 연속적인 실수 벡터로 표현한 것.",
        aliases=[
            ("벡터 임베딩", "ko", "translation"),
            ("밀집 벡터", "ko", "translation"),
            ("Embedding", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Cosine Similarity",
        entity_kind="concept",
        domain="ai",
        summary="두 벡터 간의 사잇각 코사인 값을 측정하여 방향의 유사도를 판별하는 척도.",
        aliases=[
            ("코사인 유사도", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Quantization",
        entity_kind="concept",
        domain="ai",
        summary="신경망의 가중치와 활성화 함수 값을 부동소수점(FP16/FP32)에서 낮은 비트 정수(INT8/INT4)로 변환하여 메모리와 연산량을 줄이는 기법.",
        aliases=[
            ("양자화", "ko", "translation"),
            ("모델 양자화", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Fine-tuning",
        entity_kind="concept",
        domain="ai",
        summary="사전 학습된 모델을 특정 도메인이나 작업에 맞춰 추가 데이터셋으로 재학습시키는 기법.",
        aliases=[
            ("파인튜닝", "ko", "translation"),
            ("미세조정", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="LoRA",
        entity_kind="technology",
        domain="ai",
        summary="사전 학습된 가중치를 동결하고 계층별 저순위 분해 행렬만을 훈련하여 매개변수 효율적 파인튜닝을 달성하는 기법.",
        aliases=[
            ("로라", "ko", "translation"),
            ("Low-Rank Adaptation", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Reinforcement Learning from Human Feedback",
        entity_kind="concept",
        domain="ai",
        summary="사람의 선호도 피드백을 기반으로 보상 모델을 구축하고 강화학습을 통해 모델의 행동을 인간의 의도에 정렬시키는 기법.",
        aliases=[
            ("인간 피드백 기반 강화 학습", "ko", "translation"),
            ("RLHF", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Direct Preference Optimization",
        entity_kind="concept",
        domain="ai",
        summary="명시적인 보상 모델 훈련 없이 선호/비선호 응답 쌍을 이용해 직접 언어 모델 정책을 최적화하는 기법.",
        aliases=[
            ("직접 선호 최적화", "ko", "translation"),
            ("DPO", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Prompt Engineering",
        entity_kind="concept",
        domain="ai",
        summary="생성형 AI 모델로부터 최적의 결과를 이끌어내기 위해 질문이나 지시문의 문구를 체계적으로 설계하고 정제하는 기술.",
        aliases=[
            ("프롬프트 엔지니어링", "ko", "translation"),
            ("프롬프트 기법", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="LangChain",
        entity_kind="technology",
        domain="ai",
        summary="LLM을 중심으로 다양한 컴포넌트(체인, 프롬프트, 도구, 에이전트, 메모리)를 결합하여 애플리케이션을 구축하는 프레임워크.",
        aliases=[
            ("랭체인", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="LlamaIndex",
        entity_kind="technology",
        domain="ai",
        summary="사용자 지정 데이터 소스를 대규모 언어 모델에 연결하고 검색 증강 생성을 수행하기 위한 데이터 프레임워크.",
        aliases=[
            ("라마인덱스", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Ollama",
        entity_kind="technology",
        domain="ai",
        summary="로컬 머신에서 오픈소스 대형 언어 모델(Llama, Mistral, Qwen 등)을 손쉽게 실행하고 관리하는 도구.",
        aliases=[
            ("올라마", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="vLLM",
        entity_kind="technology",
        domain="ai",
        summary="PagedAttention 메모리 관리 기술을 통해 높은 처리량과 낮은 지연 시간으로 LLM 서빙을 가속하는 라이브러리.",
        aliases=[
            ("브이엘엘엠", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Hugging Face",
        entity_kind="organization",
        domain="ai",
        summary="머신러닝 커뮤니티가 모델, 데이터셋, 데모 애플리케이션을 공유하고 협업할 수 있는 글로벌 허브 및 오픈소스 플랫폼.",
        aliases=[
            ("허깅페이스", "ko", "translation"),
            ("HF", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="PyTorch",
        entity_kind="technology",
        domain="ai",
        summary="동적 계산 그래프와 GPU 가속을 제공하여 연구와 프로덕션에서 널리 쓰이는 오픈소스 딥러닝 프레임워크.",
        aliases=[
            ("파이토치", "ko", "translation"),
            ("Torch", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="TensorFlow",
        entity_kind="technology",
        domain="ai",
        summary="구글이 개발한 대규모 수치 계산 및 딥러닝 애플리케이션 배포를 위한 종합 머신러닝 플랫폼.",
        aliases=[
            ("텐서플로우", "ko", "translation"),
            ("텐서플로", "ko", "translation"),
            ("TF", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="JAX",
        entity_kind="technology",
        domain="ai",
        summary="Autograd와 XLA 컴파일러를 결합하여 고성능 수치 계산과 기계학습 연구를 지원하는 Python 라이브러리.",
        aliases=[
            ("잭스", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="ONNX",
        entity_kind="standard",
        domain="ai",
        summary="다양한 머신러닝 프레임워크 간에 모델을 상호 교환하고 하드웨어 가속 실행을 지원하는 오픈 형식 표준.",
        aliases=[
            ("온닉스", "ko", "translation"),
            ("Open Neural Network Exchange", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="OpenAI",
        entity_kind="organization",
        domain="ai",
        summary="인공일반지능(AGI) 연구와 GPT-4, DALL-E, ChatGPT 등을 개발한 인공지능 연구 기업.",
        aliases=[
            ("오픈AI", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Anthropic",
        entity_kind="organization",
        domain="ai",
        summary="헌법적 AI(Constitutional AI)를 바탕으로 안전하고 신뢰할 수 있는 대규모 언어 모델 Claude를 개발한 AI 연구소.",
        aliases=[
            ("앤트로픽", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Claude",
        entity_kind="system",
        domain="ai",
        summary="Anthropic이 개발한 높은 정렬성과 긴 문맥 창을 갖춘 차세대 대형 언어 모델 제품군.",
        aliases=[
            ("클로드", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="ChatGPT",
        entity_kind="system",
        domain="ai",
        summary="OpenAI의 GPT 시리즈 기반으로 대화형 질의응답과 추론을 수행하는 생성형 인공지능 서비스.",
        aliases=[
            ("챗지피티", "ko", "translation"),
            ("GPT", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Gemini",
        entity_kind="system",
        domain="ai",
        summary="구글이 개발한 텍스트, 이미지, 오디오, 비디오, 코드를 넘나드는 강력한 멀티모달 대형 AI 모델.",
        aliases=[
            ("제미나이", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Qwen",
        entity_kind="system",
        domain="ai",
        summary="알리바바 클라우드가 개발한 오픈소스 기반의 고성능 대형 언어 및 멀티모달 모델 제품군.",
        aliases=[
            ("큐원", "ko", "translation"),
            ("통이치엔원", "ko", "translation"),
            ("Tongyi Qianwen", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Llama",
        entity_kind="system",
        domain="ai",
        summary="Meta가 오픈소스로 공개하여 널리 채택된 대표적인 기반 대형 언어 모델 제품군.",
        aliases=[
            ("라마", "ko", "translation"),
            ("LLaMA", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="DeepSeek",
        entity_kind="system",
        domain="ai",
        summary="효율적인 MoE 아키텍처와 추론 능력으로 주목받은 오픈소스 AI 모델 제품군.",
        aliases=[
            ("딥시크", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Mistral",
        entity_kind="system",
        domain="ai",
        summary="프랑스 Mistral AI가 개발한 가볍고 뛰어난 추론 효율을 자랑하는 오픈 가중치 언어 모델 제품군.",
        aliases=[
            ("미스트랄", "ko", "translation"),
        ],
    ),
    # -------------------------------------------------------------
    # 7. Security, Standards & Architecture (20개)
    # -------------------------------------------------------------
    SeedEntity(
        canonical_name="OAuth",
        entity_kind="standard",
        domain="security",
        summary="비밀번호를 직접 제공하지 않고 서드파티 애플리케이션에 리소스 접근 권한을 위임하는 오픈 표준 프로토콜.",
        aliases=[
            ("오스", "ko", "translation"),
            ("OAuth 2.0", "en", "synonym"),
            ("인증 인가 표준", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="OpenID Connect",
        entity_kind="standard",
        domain="security",
        summary="OAuth 2.0 프로토콜 위에 인증(Authentication) 계층을 추가한 상호 운용 가능한 신원 인증 표준.",
        aliases=[
            ("오픈아이디 커넥트", "ko", "translation"),
            ("OIDC", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="JSON Web Token",
        entity_kind="standard",
        domain="security",
        summary="당사자 간에 정보를 JSON 객체로 안전하게 전송하기 위한 간결하고 자가 수용적인 서명 기반 웹 표준.",
        aliases=[
            ("제이슨 웹 토큰", "ko", "translation"),
            ("JWT", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Transport Layer Security",
        entity_kind="standard",
        domain="security",
        summary="컴퓨터 네트워크를 통한 통신에 암호화와 무결성을 제공하여 도청과 변조를 방지하는 암호화 프로토콜.",
        aliases=[
            ("전송 계층 보안", "ko", "translation"),
            ("TLS", "en", "abbrev"),
            ("SSL", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Single Sign-On",
        entity_kind="concept",
        domain="security",
        summary="한 번의 인증 자격 증명 입력으로 연계된 여러 독립적인 소프트웨어 시스템에 로그인할 수 있는 접근 제어 속성.",
        aliases=[
            ("통합 인증", "ko", "translation"),
            ("싱글 사인온", "ko", "translation"),
            ("SSO", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Role-Based Access Control",
        entity_kind="concept",
        domain="security",
        summary="조직 내 개별 사용자에게 역할을 부여하고 역할에 권한을 할당하여 시스템 접근을 제어하는 보안 접근 방식.",
        aliases=[
            ("역할 기반 접근 제어", "ko", "translation"),
            ("RBAC", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Cross-Site Scripting",
        entity_kind="concept",
        domain="security",
        summary="웹 애플리케이션의 취약점을 이용하여 악의적인 스크립트를 다른 사용자의 브라우저에서 실행시키는 보안 공격 기법.",
        aliases=[
            ("크로스 사이트 스크립팅", "ko", "translation"),
            ("XSS", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="SQL Injection",
        entity_kind="concept",
        domain="security",
        summary="입력값 검증 부재를 악용하여 데이터베이스 쿼리를 조작하고 비인가 데이터에 접근하는 코드 인젝션 공격.",
        aliases=[
            ("에스큐엘 인젝션", "ko", "translation"),
            ("SQL 삽입 공격", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Cross-Site Request Forgery",
        entity_kind="concept",
        domain="security",
        summary="사용자가 자신의 의지와 무관하게 공격자가 의도한 행위(수정, 삭제 등)를 특정 웹사이트에 요청하게 만드는 취약점.",
        aliases=[
            ("크로스 사이트 요청 위조", "ko", "translation"),
            ("CSRF", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Zero Trust",
        entity_kind="concept",
        domain="security",
        summary="내부망이라도 신뢰하지 않고 모든 접근 요청에 대해 엄격한 신원 검증과 최소 권한을 부여하는 보안 모델 패러다임.",
        aliases=[
            ("제로 트러스트", "ko", "translation"),
            ("제로트러스트", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Public Key Infrastructure",
        entity_kind="concept",
        domain="security",
        summary="디지털 인증서를 생성, 관리, 배포, 사용, 저장 및 폐기하는 하드웨어, 소프트웨어, 사람, 정책의 집합.",
        aliases=[
            ("공개키 기반 구조", "ko", "translation"),
            ("PKI", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Multi-Factor Authentication",
        entity_kind="concept",
        domain="security",
        summary="로그인 시 지식, 소유, 생체 정보 중 두 가지 이상의 독립적인 증거 요소를 요구하는 다중 인증 기법.",
        aliases=[
            ("다중 요소 인증", "ko", "translation"),
            ("MFA", "en", "abbrev"),
            ("2FA", "en", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Microservices Architecture",
        entity_kind="concept",
        domain="dev",
        summary="단일 애플리케이션을 작고 독립적으로 배포 가능한 비즈니스 중심 서비스들의 집합으로 설계하는 아키텍처 패턴.",
        aliases=[
            ("마이크로서비스 아키텍처", "ko", "translation"),
            ("MSA", "en", "abbrev"),
            ("마이크로서비스", "ko", "synonym"),
        ],
    ),
    SeedEntity(
        canonical_name="Event-Driven Architecture",
        entity_kind="concept",
        domain="dev",
        summary="시스템 구성 요소들이 상태 변경 이벤트의 생성, 감지, 소비를 중심으로 비동기 결합되는 소프트웨어 설계 패턴.",
        aliases=[
            ("이벤트 기반 아키텍처", "ko", "translation"),
            ("EDA", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Domain-Driven Design",
        entity_kind="concept",
        domain="dev",
        summary="복잡한 도메인 모델과 유비쿼터스 언어를 소프트웨어 핵심 설계의 중심에 두는 개발 방법론.",
        aliases=[
            ("도메인 주도 설계", "ko", "translation"),
            ("DDD", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Model-View-Controller",
        entity_kind="concept",
        domain="dev",
        summary="소프트웨어 로직을 데이터(Model), 화면 표현(View), 사용자 입력 처리(Controller) 세 부분으로 분리하는 패턴.",
        aliases=[
            ("모델-뷰-컨트롤러", "ko", "translation"),
            ("MVC", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Test-Driven Development",
        entity_kind="concept",
        domain="dev",
        summary="실제 구현 코드를 작성하기 전에 실패하는 테스트 코드를 먼저 작성하고 이를 통과시키며 리팩토링하는 개발 방법론.",
        aliases=[
            ("테스트 주도 개발", "ko", "translation"),
            ("TDD", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Clean Architecture",
        entity_kind="concept",
        domain="dev",
        summary="비즈니스 규칙을 중심에 두고 외부 프레임워크나 데이터베이스 의존성이 내부를 향하도록 계층을 분리하는 아키텍처.",
        aliases=[
            ("클린 아키텍처", "ko", "translation"),
        ],
    ),
    SeedEntity(
        canonical_name="Command Query Responsibility Segregation",
        entity_kind="concept",
        domain="dev",
        summary="데이터를 변경하는 커맨드 모델과 데이터를 조회하는 쿼리 모델의 책임을 명확히 분리하는 패턴.",
        aliases=[
            ("명령 조회 책임 분리", "ko", "translation"),
            ("CQRS", "en", "abbrev"),
        ],
    ),
    SeedEntity(
        canonical_name="Hexagonal Architecture",
        entity_kind="concept",
        domain="dev",
        summary="포트와 어댑터를 통해 애플리케이션 코어와 외부 인프라/UI 간의 결합도를 낮추는 아키텍처 패턴.",
        aliases=[
            ("헥사고날 아키텍처", "ko", "translation"),
            ("포트와 어댑터", "ko", "synonym"),
        ],
    ),
]


# -------------------------------------------------------------
# 빠른 조회를 위한 정규화 인덱스 빌드
# -------------------------------------------------------------
_NORM_TO_SEED: dict[str, SeedEntity] = {}

for _entry in RAW_SEED_ENTITIES:
    _canon_norm = norm_name(_entry.canonical_name)
    if _canon_norm:
        _NORM_TO_SEED[_canon_norm] = _entry
    for _alias, _lang, _kind in _entry.aliases:
        _a_norm = norm_name(_alias)
        if _a_norm and _a_norm not in _NORM_TO_SEED:
            _NORM_TO_SEED[_a_norm] = _entry


def get_seed_entities() -> list[SeedEntity]:
    """모든 정적 시드 엔터티 목록 반환."""
    return list(RAW_SEED_ENTITIES)


def find_seed_by_norm(norm: str) -> SeedEntity | None:
    """정규화된 이름 또는 별칭으로 시드 엔터티 조회."""
    if not norm:
        return None
    return _NORM_TO_SEED.get(norm)


def is_seed_alias(alias: str) -> bool:
    """주어진 문자열이 시드 사전에 등록된 이름/별칭인지 확인."""
    n = norm_name(alias)
    return bool(n and n in _NORM_TO_SEED)


async def bootstrap_seed_aliases(
    db: AsyncSession,
    tenant_id: str = "personal",
    visibility: str = "global",
    origin_scope: str = "personal",
) -> int:
    """시드 사전을 DB kb_entities 및 kb_entity_aliases 에 멱등하게 적재.

    이미 존재하는 엔터티는 건너뛰고, 신규 생성된 엔터티 개수를 반환한다.
    """
    created_count = 0

    # 1. 기존 엔터티의 norm_name 집합 조회
    stmt = select(KbEntity.norm_name).where(
        KbEntity.tenant_id == tenant_id,
        KbEntity.visibility == visibility,
    )
    res = await db.execute(stmt)
    existing_norms = {r[0] for r in res.all()}

    for seed in RAW_SEED_ENTITIES:
        c_norm = norm_name(seed.canonical_name)
        if not c_norm or c_norm in existing_norms:
            continue

        # 새 엔터티 생성
        entity = KbEntity(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            origin_scope=origin_scope,
            visibility=visibility,
            canonical_name=seed.canonical_name,
            norm_name=c_norm,
            norm_form="NFKC",
            entity_kind=seed.entity_kind,
            domain=seed.domain,
            summary=seed.summary,
            confidence=1.0,
            status="active",
        )
        db.add(entity)
        existing_norms.add(c_norm)
        created_count += 1

        # 대표 정본 별칭 등록
        db.add(
            KbEntityAlias(
                id=uuid.uuid4(),
                entity_id=entity.id,
                alias=seed.canonical_name,
                norm_alias=c_norm,
                norm_form="NFKC",
                lang="en" if seed.canonical_name.isascii() else None,
                alias_kind="canonical",
                source="seed",
            )
        )

        # 등록된 별칭들 추가
        registered_alias_norms = {c_norm}
        for alias_text, lang, alias_kind in seed.aliases:
            a_norm = norm_name(alias_text)
            if not a_norm or a_norm in registered_alias_norms:
                continue
            registered_alias_norms.add(a_norm)
            db.add(
                KbEntityAlias(
                    id=uuid.uuid4(),
                    entity_id=entity.id,
                    alias=alias_text,
                    norm_alias=a_norm,
                    norm_form="NFKC",
                    lang=lang,
                    alias_kind=alias_kind,
                    source="seed",
                )
            )

    if created_count > 0:
        await db.flush()
        logger.info(
            "Bootstrapped %d seed entities into %s/%s",
            created_count,
            tenant_id,
            visibility,
        )

    return created_count
