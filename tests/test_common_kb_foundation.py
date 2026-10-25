"""Tests for common KB foundation modules: chunking, lexical, entity_norm."""

from __future__ import annotations

import hashlib

import pytest

from ttakkari.common.chunking import (
    LocatorSpan,
    chunk_structured_document,
)
from ttakkari.common.entity_norm import (
    duplicate_suspects,
    extract_explicit_definitions,
    norm_name,
    split_bilingual_label,
)
from ttakkari.common.lexical import (
    lexical_score,
    split_identifier,
    strip_korean_suffix,
    terms,
    tokenize,
)


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    """Override session DB fixture from conftest for pure unit testing without DB connection."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """Override per-test DB clean fixture from conftest."""
    yield


class TestChunkStructuredDocument:
    def test_empty_body(self):
        chunks = chunk_structured_document("")
        assert chunks == []

    def test_markdown_heading_hierarchy_preserved(self):
        md = (
            "# Kubernetes\n\n"
            "Kubernetes is a container orchestration platform.\n\n"
            "## Workloads\n\n"
            "Workloads represent applications running on Kubernetes.\n\n"
            "### Deployments\n\n"
            "A Deployment provides declarative updates for Pods and ReplicaSets.\n"
            "You describe a desired state in a Deployment, and the Deployment Controller changes the actual state to the desired state at a controlled rate.\n"
        )
        chunks = chunk_structured_document(
            md, target_chars=200, max_chars=400, min_chars=50
        )
        assert len(chunks) >= 2

        # Invariant checks: slice matches exact body, sha256 matches
        for c in chunks:
            assert md[c.char_start : c.char_end] == c.body
            assert hashlib.sha256(c.body.encode("utf-8")).hexdigest() == c.sha256
            assert c.char_start >= 0
            assert c.char_end > c.char_start
            assert c.char_end <= len(md)

        # Check section_path
        last_chunk = chunks[-1]
        assert (
            "Deployments" in last_chunk.section_path
            or "Workloads" in last_chunk.section_path
        )

    def test_code_block_and_table_atomicity(self):
        text = (
            "# API Configuration\n\n"
            "Here is the service config:\n\n"
            "```yaml\n"
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: nginx-deployment\n"
            "spec:\n"
            "  replicas: 3\n"
            "```\n\n"
            "And here is the parameter table:\n\n"
            "| Parameter | Type | Default |\n"
            "|-----------|------|---------|\n"
            "| replicas  | int  | 1       |\n"
            "| port      | int  | 80      |\n"
        )
        chunks = chunk_structured_document(
            text, target_chars=500, max_chars=1200, min_chars=50
        )

        # Ensure code block is preserved atomically as chunk_kind="code"
        code_chunks = [c for c in chunks if c.chunk_kind == "code"]
        assert len(code_chunks) == 1
        assert "```yaml" in code_chunks[0].body
        assert "replicas: 3" in code_chunks[0].body

        # Ensure table is preserved as chunk_kind="table"
        table_chunks = [c for c in chunks if c.chunk_kind == "table"]
        assert len(table_chunks) == 1
        assert "| Parameter | Type | Default |" in table_chunks[0].body

        # Every chunk satisfies slice invariant
        for c in chunks:
            assert text[c.char_start : c.char_end] == c.body
            assert hashlib.sha256(c.body.encode("utf-8")).hexdigest() == c.sha256

    def test_locator_spans_page_and_slide_preservation(self):
        doc = (
            "Page 1 Content: Introduction to cloud computing and virtualization architectures.\n\n"
            "Page 2 Content: Detailed Kubernetes control plane components including api-server, etcd, and scheduler.\n\n"
            "Page 3 Content: Node components including kubelet, kube-proxy, and container runtime engine.\n"
        )
        p1_end = doc.index("Page 2 Content:")
        p2_end = doc.index("Page 3 Content:")
        spans = [
            LocatorSpan(
                0, p1_end, section_path=["Intro"], page_no=1, chunk_kind="prose"
            ),
            LocatorSpan(
                p1_end,
                p2_end,
                section_path=["Control Plane"],
                page_no=2,
                chunk_kind="prose",
            ),
            LocatorSpan(
                p2_end, len(doc), section_path=["Node"], page_no=3, chunk_kind="prose"
            ),
        ]

        chunks = chunk_structured_document(
            doc, locator_spans=spans, target_chars=300, max_chars=600, min_chars=30
        )
        assert len(chunks) == 3
        assert chunks[0].page_no == 1
        assert chunks[1].page_no == 2
        assert chunks[2].page_no == 3

        for c in chunks:
            assert doc[c.char_start : c.char_end] == c.body
            assert hashlib.sha256(c.body.encode("utf-8")).hexdigest() == c.sha256

    def test_embedding_cutoff_prevention_max_chars(self):
        # Long text exceeding max_chars
        long_para = "이 문장은 긴 문장 테스트를 위한 텍스트입니다. " * 50  # ~1250 chars
        chunks = chunk_structured_document(
            long_para, target_chars=400, max_chars=600, min_chars=100
        )
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c.body) <= 600
            assert long_para[c.char_start : c.char_end] == c.body
            assert hashlib.sha256(c.body.encode("utf-8")).hexdigest() == c.sha256


class TestLexical:
    def test_strip_korean_suffix(self):
        assert strip_korean_suffix("쿠버네티스는") == "쿠버네티스"
        assert strip_korean_suffix("파드를") == "파드"
        assert strip_korean_suffix("배포에서") == "배포"
        assert strip_korean_suffix("도커와") == "도커"
        # Preserve short root
        assert strip_korean_suffix("날") == "날"

    def test_split_identifier(self):
        assert split_identifier("hybridSearchKb") == ["hybrid", "search", "kb"]
        assert split_identifier("get_user_by_id") == ["get", "user", "by", "id"]
        assert split_identifier("KbPassageVector") == ["kb", "passage", "vector"]
        assert split_identifier("APIEndpoint_v2") == ["api", "endpoint", "v2"]

    def test_tokenize_mixed_korean_english_code(self):
        text = "Kubernetes 환경에서 DeploymentController를 설정하여 파드를 관리한다."
        tokens = tokenize(text)
        assert "kubernetes" in tokens
        assert "deployment" in tokens
        assert "controller" in tokens
        assert "파드" in tokens
        assert "관리" in tokens

    def test_terms_and_scoring(self):
        q = "쿠버네티스 디플로이먼트 배포 방법 알려줘"
        t = terms(q)
        assert "쿠버네티스" in t
        assert "디플로이먼트" in t
        assert "배포" in t
        assert "알려줘" not in t  # Stopword

        score1 = lexical_score(t, "쿠버네티스 디플로이먼트 배포 안내 문서")
        score2 = lexical_score(t, "파이썬 데이터 분석 기초")
        assert score1 > score2


class TestEntityNorm:
    def test_norm_name(self):
        assert norm_name("Kubernetes") == "kubernetes"
        assert norm_name("k8s") == "k8s"
        assert norm_name("  쿠버네티스  ") == "쿠버네티스"
        assert norm_name("Postgre-SQL") == "postgresql"
        assert norm_name("Skein_Design.System") == "skeindesignsystem"

    def test_split_bilingual_label(self):
        canonical, aliases = split_bilingual_label("디플로이먼트(Deployment)")
        assert canonical == "Deployment"
        assert "디플로이먼트" in aliases
        assert "Deployment" in aliases

        canonical2, aliases2 = split_bilingual_label("쿠버네티스 (Kubernetes)")
        assert canonical2 == "Kubernetes"
        assert "쿠버네티스" in aliases2
        assert "Kubernetes" in aliases2

        canonical3, aliases3 = split_bilingual_label("PostgreSQL (Postgres)")
        assert canonical3 == "PostgreSQL"
        assert "Postgres" in aliases3

    def test_multi_alias_and_bracket_split(self):
        canonical1, aliases1 = split_bilingual_label("Foo (Bar) (Baz)")
        assert canonical1 == "Foo"
        assert aliases1 == ["Foo", "Bar", "Baz"]

        canonical2, aliases2 = split_bilingual_label("쿠버네티스(Kubernetes, k8s)")
        assert canonical2 == "Kubernetes"
        assert "쿠버네티스" in aliases2
        assert "Kubernetes" in aliases2
        assert "k8s" in aliases2

    def test_nested_or_unbalanced_parentheses_are_not_split(self):
        for label in ("Foo (Bar (Baz))", "Foo (Bar", "Foo Bar)", "((Foo))", "A (B) (C (D))"):
            assert split_bilingual_label(label) == (label, [label])
        # flat multi-parentheses keep working
        assert split_bilingual_label("Foo (Bar) (Baz)") == ("Foo", ["Foo", "Bar", "Baz"])

    def test_adversarial_chunking_contracts(self):
        fence = chr(96) * 3
        cases = [
            ("markdown headings", "# Title\n\npara one\n\n## Sub\n\npara two"),
            ("CRLF", "# T\r\n\r\nfirst\r\n\r\nsecond"),
            ("oversize code line", fence + "py\n" + "x" * 80 + "\n" + fence),
            ("oversize table row", "| a | b |\n|---|---|\n|" + "x" * 80 + "|z|"),
        ]
        for name, body in cases:
            max_c = 20
            chunks = chunk_structured_document(
                body, target_chars=12, max_chars=max_c, min_chars=1
            )
            gaps, pos = [], 0
            for c in chunks:
                # Contract 1: exact slice invariant
                assert body[c.char_start : c.char_end] == c.body
                # Contract 2: strictly <= max_chars
                assert len(c.body) <= max_c
                # Contract 3: no overlapping and ordered
                assert c.char_start >= pos
                if c.char_start > pos:
                    gaps.append(body[pos : c.char_start])
                pos = c.char_end
            if pos < len(body):
                gaps.append(body[pos:])

            # Contract 4: coverage - no non-whitespace characters in gaps
            for g in gaps:
                assert g.strip() == "", f"Non-whitespace gap in {name}: {g!r}"

        # Contract 5: lone surrogate tolerance without UnicodeEncodeError
        chunks_surr = chunk_structured_document("ok" + chr(0xD800) + "end")
        assert len(chunks_surr) >= 1
        assert len(chunks_surr[0].sha256) == 64

    def test_extract_explicit_definitions_no_hallucinations(self):
        text = (
            "디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 쿠버네티스 워크로드 리소스이다.\n\n"
            "Kubernetes refers to an open-source container orchestration system.\n\n"
            "| 항목 | 설명 |\n"
            "| --- | --- |\n"
            "| kube-apiserver | 쿠버네티스 제어판의 프론트엔드 컴포넌트이다. |\n"
        )
        defs = extract_explicit_definitions(text)
        assert len(defs) >= 2

        terms_found = [d["term"] for d in defs]
        assert any("디플로이먼트" in t or "Deployment" in t for t in terms_found)
        assert any("Kubernetes" in t for t in terms_found)

        # Every definition quote must be an exact substring of the original text
        for d in defs:
            assert d["quote"] in text

    def test_duplicate_suspects(self):
        entities = [
            {"id": "e1", "name": "Kubernetes", "aliases": ["k8s"]},
            {"id": "e2", "name": "쿠버네티스", "aliases": ["Kubernetes"]},
            {"id": "e3", "name": "PostgreSQL", "aliases": ["Postgres"]},
            {"id": "e4", "name": "MySQL", "aliases": []},
        ]
        suspects = duplicate_suspects(entities)
        assert len(suspects) >= 1
        assert suspects[0]["a"] in ("e1", "e2")
        assert suspects[0]["b"] in ("e1", "e2")
