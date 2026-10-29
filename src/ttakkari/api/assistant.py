"""비서 관리(ADR 0007): 따까리 전용 하네스(지침)·기억·온톨로지·지식(RAG 출처)을 사용자가 고친다."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.background_consumers import deferred
from ttakkari.broker.policy import is_sensitive, is_within
from ttakkari.config import get_settings
from ttakkari.context import index_queue, store
from ttakkari.context.library import library_catalog
from ttakkari.models import (
    CtxChunk,
    CtxDocument,
    CtxEntity,
    CtxMemory,
    CtxRelation,
    CtxSource,
    utcnow,
)
from ttakkari.runs import instructions as instructions_mod
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
log = logging.getLogger(__name__)
MAX_INSTRUCTIONS = 20_000

Kind = Literal["fact", "preference", "decision", "event", "instruction", "note"]
EType = Literal["person", "project", "org", "place", "tool", "topic", "preference", "other"]

# 비서 관리 > 도구 탭에 보이는 목록. 실제 등록은 orchestrator/mcp_server.py 가 한다.
TOOLS: list[dict[str, str]] = [
    {"name": "knowledge_extraction_status", "group": "맥락", "who": "모든 실행", "desc": "추출 실행 기록·실패 사유·재처리 계보"},
    {"name": "knowledge_retry_extraction", "group": "맥락", "who": "모든 실행", "desc": "재처리 가능한 실패만 최대3회 재개"},
    {"name": "kb_search", "group": "지식 도서관", "who": "모든 실행", "desc": "일반 지식 문서 검색·원문 근거 후보"},
    {"name": "kb_documents", "group": "지식 도서관", "who": "모든 실행", "desc": "일반 지식 도서관 문서 목록"},
    {"name": "kb_document", "group": "지식 도서관", "who": "모든 실행", "desc": "일반 지식 문서와 현재 버전"},
    {"name": "kb_evidence", "group": "지식 도서관", "who": "모든 실행", "desc": "일반 지식 인용 위치·해시 검증"},
    {"name": "knowledge_sources", "group": "맥락", "who": "모든 실행", "desc": "범위 내 원문 버전 목록"},
    {"name": "knowledge_recall", "group": "맥락", "who": "모든 실행", "desc": "원문 근거·시점·상태를 가진 지식과 관계 검색"},
    {"name": "knowledge_assertion", "group": "맥락", "who": "모든 실행", "desc": "관계의 근거와 정정 이력 조회"},
    {"name": "knowledge_start_extraction", "group": "맥락", "who": "모든 실행", "desc": "증분 후보 추출 작업 시작·재개"},
    {"name": "knowledge_next_batch", "group": "맥락", "who": "모든 실행", "desc": "제한된 원본 묶음 조회"},
    {"name": "knowledge_submit_batch", "group": "맥락", "who": "모든 실행", "desc": "원본별 처리결과와 정확한 근거가 있는 후보 제출"},
    {"name": "knowledge_review_assertion", "group": "맥락", "who": "모든 실행", "desc": "원문 대조 검토·충돌·철회 기록"},
    {"name": "library_catalog", "group": "맥락", "who": "모든 실행", "desc": "맥락 도서관의 종류·지식/작업 기록 모음·프로젝트 이정표를 본다"},
    {"name": "recall", "group": "맥락", "who": "모든 실행", "desc": "나에 관한 기억·관계·색인된 문서를 찾는다"},
    {"name": "remember", "group": "맥락", "who": "모든 실행", "desc": "새 사실·취향·결정을 기억한다"},
    {"name": "get_entity", "group": "맥락", "who": "모든 실행", "desc": "사람·프로젝트 등의 요약과 관계를 본다"},
    {"name": "describe_entity", "group": "맥락", "who": "모든 실행", "desc": "엔터티를 만들거나 요약을 고친다"},
    {"name": "link", "group": "맥락", "who": "모든 실행", "desc": "두 엔터티 사이 관계를 기록한다"},
    {"name": "forget", "group": "맥락", "who": "모든 실행", "desc": "에이전트가 넣은 기억을 보관 처리한다"},
    {"name": "list_artifacts", "group": "파일", "who": "모든 실행", "desc": "보관함 파일 목록과 경로(내 보관함·AI 작업 파일 탭, 카테고리로 거르기)"},
    {"name": "set_artifact_area", "group": "파일", "who": "모든 실행", "desc": "파일을 AI 작업 파일로 내린다(내 보관함으로 올리기와 사용자가 보관한 파일은 불가)"},
    {"name": "share_file", "group": "파일", "who": "모든 실행", "desc": "파일을 대화로 넘겨 폰에서 열고 내려받게 한다(AI 작업 파일에 남음)"},
    {"name": "keep_artifact", "group": "파일", "who": "모든 실행", "desc": "사용자가 보관을 요청한 말을 그대로 인용해 내 보관함에 보관한다(실제 사용자 발화에서 확인하고 없으면 거부)"},
    {"name": "github_accounts", "group": "서버", "who": "모든 실행", "desc": "등록된 GitHub 계정과 현재 활성 계정을 조회한다(토큰 제외)"},
    {"name": "github_switch_account", "group": "서버", "who": "모든 실행", "desc": "등록된 GitHub 활성 계정을 전환한다. 개인·회사 세션 모두 사용 가능"},
    {"name": "activate_copilot_account", "group": "서버", "who": "모든 실행", "desc": "Copilot 계정을 등록·재확인하고 쿼터를 갱신한다. 자격 증명은 읽지 않는다"},
    {"name": "request_backend_deploy", "group": "서버", "who": "모든 실행", "desc": "검증·커밋한 백엔드 배포를 예약한다. 무중단 모드는 기존 작업을 유지하며 새 요청을 전환한다. 슬롯이 차면 자동 대기한다"},
    {"name": "backend_deploy_status", "group": "서버", "who": "모든 실행", "desc": "배포 예약의 진행 상태·성공·실패 이유를 조회한다"},
    {"name": "ops_list_issues", "group": "서버", "who": "모든 실행", "desc": "운영 오류를 이슈로 묶어 횟수·최근 발생·배포 SHA와 함께 요약한다(마스킹·크기 상한)"},
    {"name": "ops_get_issue", "group": "서버", "who": "모든 실행", "desc": "이슈 상세: 대표 스택·직전 맥락(breadcrumbs)·마스킹된 로그 발췌·릴리스별 발생"},
    {"name": "ops_search_logs", "group": "서버", "who": "모든 실행", "desc": "기간·키워드·레벨·출처·릴리스로 저장된 오류 이벤트를 검색한다"},
    {"name": "ops_list_deploys", "group": "서버", "who": "모든 실행", "desc": "최근 배포 job과 실패 단계·원인 한 줄·연결된 이슈"},
    {"name": "ops_deploy_log", "group": "서버", "who": "모든 실행", "desc": "배포 job 로그를 백엔드를 통해 읽는다(원인 요약·오류 구간·tail·구간 읽기, 마스킹)"},
    {"name": "ops_update_issue", "group": "서버", "who": "모든 실행", "desc": "운영 이슈를 해결·무시·재오픈하고 원인 메모를 남긴다(재발하면 회귀로 자동 재오픈)"},
    {"name": "design_system_catalog", "group": "맥락", "who": "모든 실행", "desc": "개인 디자인 시스템의 버전·항목 수·커밋 리비전 목록(작업 트리 변경 제외)"},
    {"name": "design_system_search", "group": "맥락", "who": "모든 실행", "desc": "디자인 시스템의 토큰·문서·컴포넌트·예시를 검색해 항목 ID를 얻는다"},
    {"name": "design_system_item", "group": "맥락", "who": "모든 실행", "desc": "항목 ID의 토큰 값·문서·컴포넌트·예시 본문을 구간 단위로 읽는다"},
    {"name": "reverse_start", "group": "맥락", "who": "모든 실행", "desc": "자연어로 설명한 대상의 동작을 분석하고 작업공간에 소스·테스트로 재현"},
    {"name": "reverse_list", "group": "맥락", "who": "모든 실행", "desc": "리버스 엔지니어링 작업 목록과 현재 단계"},
    {"name": "reverse_get", "group": "맥락", "who": "모든 실행", "desc": "대상 설명·관찰·소스·실행 로그·독립 검증·후속 지시 조회"},
    {"name": "reverse_message", "group": "맥락", "who": "모든 실행", "desc": "역분석 작업에 대상 설명을 보충하거나 소스 수정을 이어서 지시"},
    {"name": "reverse_retry", "group": "맥락", "who": "모든 실행", "desc": "중단한 역분석 작업을 같은 소스 폴더·실행 기록에서 재개"},
    {"name": "manage_research", "group": "연구", "who": "개인 실행", "desc": "Research Lab: 공개 논문 탐색, 원문 개정·정확 인용 보존, 방법·주장 기록, 환경·비용별 방법 선택과 제한된 실험 계획·실행, 연구 복원"},
    {"name": "schedule_action", "group": "예약", "who": "모든 실행", "desc": "정해진 시각에 전체 중지·Run 취소·세션 메시지 전송을 영속 예약한다. 재배포·세대 전환·재시작 뒤에도 유지된다"},
    {"name": "list_scheduled_actions", "group": "예약", "who": "모든 실행", "desc": "영속 예약 작업 목록과 상태(대기·실행 중·완료·실패·취소)"},
    {"name": "cancel_scheduled_action", "group": "예약", "who": "모든 실행", "desc": "아직 실행되지 않은 영속 예약 작업을 취소한다"},
    {"name": "server_resources", "group": "서버", "who": "모든 실행", "desc": "이 맥의 CPU·메모리·디스크·네트워크와 상위 프로세스"},
    {"name": "comfy_profiles", "group": "제작", "who": "모든 실행", "desc": "등록된 managed Comfy 프로필·고정 workflow·지원 능력"},
    {"name": "comfy_readiness", "group": "제작", "who": "모든 실행", "desc": "백엔드 호스트의 설정된 Comfy Python에서 모델을 로드하지 않는 장치 확인"},
    {"name": "comfy_jobs", "group": "제작", "who": "모든 실행", "desc": "현재 세션 소유 Comfy 작업과 진행·결과를 페이지 단위로 조회"},
    {"name": "comfy_submit", "group": "제작", "who": "모든 실행", "desc": "허용 프로필·프롬프트·seed로 managed Comfy 작업을 멱등 제출·실행"},
    {"name": "comfy_job", "group": "제작", "who": "모든 실행", "desc": "작업 상태·진행·제한된 실패 로그·생성 자산 ID 조회"},
    {"name": "comfy_cancel", "group": "제작", "who": "모든 실행", "desc": "소유한 managed Comfy 작업 취소 요청"},
    {"name": "comfy_reconcile", "group": "제작", "who": "모든 실행", "desc": "소유 및 프로세스 검증을 통과한 Comfy 작업만 안전하게 조정"},
    {"name": "creative_v2_catalog", "group": "제작", "who": "모든 실행", "desc": "Creative Studio v2 컨셉·전환·스타일·능력 목록"},
    {"name": "creative_v2_capabilities", "group": "제작", "who": "모든 실행", "desc": "제작 능력·공급자와 자동 자산 생성/파일 공유 가능 여부"},
    {"name": "creative_v2_recipe_matrix", "group": "제작", "who": "모든 실행", "desc": "등록 recipe와 필수 능력 조건"},
    {"name": "creative_v2_registries", "group": "제작", "who": "모든 실행", "desc": "현재 registry snapshot 및 출처 정보"},
    {"name": "creative_v2_styles", "group": "제작", "who": "모든 실행", "desc": "등록 Style DNA 목록과 렌더러가 실제 소비하는 매개변수(움직임·전환·자막 스타일) 허용 값"},
    {"name": "creative_v2_style", "group": "제작", "who": "모든 실행", "desc": "Style DNA 하나와 R21 렌더에서 적용·무시될 매개변수 미리보기"},
    {"name": "creative_v2_style_put", "group": "제작", "who": "모든 실행", "desc": "불변 새 Style DNA 버전 등록(엄격 검증, dry_run 지원); 기존 버전·시드 변경 불가"},
    {"name": "creative_v2_plans", "group": "제작", "who": "모든 실행", "desc": "v2 계획·revision·승인/렌더 gate 상태"},
    {"name": "creative_v2_plan", "group": "제작", "who": "모든 실행", "desc": "ID로 v2 계획과 현재 revision·렌더 gate 조회"},
    {"name": "creative_v2_plan_create", "group": "제작", "who": "모든 실행", "desc": "기존 catalog matcher로 v2 계획 생성"},
    {"name": "creative_v2_plan_edit", "group": "제작", "who": "모든 실행", "desc": "예상 revision을 지정해 계획 편집; 기존 승인은 무효화"},
    {"name": "creative_v2_plan_approve", "group": "제작", "who": "모든 실행", "desc": "현재 revision 계획의 차단 조건을 검사하고 승인"},
    {"name": "creative_v2_production_assets", "group": "제작", "who": "모든 실행", "desc": "최대 10 MiB base64 이미지·음성을 계획 자산으로 등록"},
    {"name": "creative_v2_auto_assets_status", "group": "제작", "who": "모든 실행", "desc": "자동 이미지·TTS·음악 생성 지원 recipe·엔진·취소 지원과 결과 흐름"},
    {"name": "creative_v2_auto_assets_start", "group": "제작", "who": "모든 실행", "desc": "승인된 정확한 revision의 이미지·장면별 음성·음악 자동 생성 시작"},
    {"name": "creative_v2_auto_assets_retry", "group": "제작", "who": "모든 실행", "desc": "실패한 승인 revision의 생성 재시도; 미확정 Comfy 작업은 재제출하지 않음"},
    {"name": "creative_v2_production_dispatch", "group": "제작", "who": "모든 실행", "desc": "승인된 렌더 가능 revision의 제작 dispatch"},
    {"name": "creative_v2_production_render", "group": "제작", "who": "모든 실행", "desc": "승인된 dispatch의 가능한 render 구간 실행"},
    {"name": "creative_v2_production_status", "group": "제작", "who": "모든 실행", "desc": "제작 상태·구간·제한 로그·QA·최종 미디어 ID 조회"},
    {"name": "creative_v2_production_media_link", "group": "제작", "who": "모든 실행", "desc": "완료 미디어의 짧은 만료 시간·계획 한정 다운로드 링크"},
    {"name": "tossinvest_status", "group": "주식 투자", "who": "모든 실행", "desc": "토스증권의 마지막 연결 상태와 확인·동기화 기록을 조회한다. 브라우저를 열거나 주문하지 않는다"},
    {"name": "notebooklm_status", "group": "NotebookLM", "who": "모든 실행", "desc": "NotebookLM 연결 상태(로그인·재인증·쿨다운·보호 신호)와 지원/미지원 기능. Google 요청을 보내지 않는다"},
    {"name": "google_account_status", "group": "Google 계정", "who": "모든 실행", "desc": "Google 계정별 로그인·재인증 상태와 이 계정을 쓰는 기능(NotebookLM 등)의 쿨다운·보호 신호 상태. Google 요청을 보내지 않고 로그인 창·세션 삭제는 화면에서만 한다"},
    {"name": "notebooklm_list", "group": "NotebookLM", "who": "모든 실행", "desc": "노트북 목록(ID·제목·소스 수)"},
    {"name": "notebooklm_get", "group": "NotebookLM", "who": "모든 실행", "desc": "노트북 상세와 소스 목록·처리 상태"},
    {"name": "notebooklm_create", "group": "NotebookLM", "who": "모든 실행", "desc": "노트북을 만든다(자동 재시도 없음)"},
    {"name": "notebooklm_rename", "group": "NotebookLM", "who": "모든 실행", "desc": "노트북 제목을 바꾼다"},
    {"name": "notebooklm_delete", "group": "NotebookLM", "who": "모든 실행", "desc": "노트북을 영구 삭제한다(confirm=true 필요)"},
    {"name": "notebooklm_add_source", "group": "NotebookLM", "who": "모든 실행", "desc": "웹/YouTube URL 또는 텍스트를 소스로 등록하는 job을 시작한다(중복 URL/내용은 거절, 파일 업로드는 화면에서만)"},
    {"name": "notebooklm_delete_source", "group": "NotebookLM", "who": "모든 실행", "desc": "소스 하나를 삭제한다(confirm=true 필요)"},
    {"name": "notebooklm_query", "group": "NotebookLM", "who": "모든 실행", "desc": "소스 기반 질의. 답변(분석)·인용(출처 ID·원문 인용·좌표)·출처를 분리해 반환, 원장용 내보내기 옵션"},
    {"name": "notebooklm_job", "group": "NotebookLM", "who": "모든 실행", "desc": "소스 등록 job의 상태(queued/running/succeeded/failed/cancelled/unknown)와 결과"},
    {"name": "notebooklm_job_cancel", "group": "NotebookLM", "who": "모든 실행", "desc": "대기·진행 중인 NotebookLM job을 취소한다"},
    {"name": "execution_capabilities", "group": "서버", "who": "모든 실행", "desc": "엔진별 실행 환경 능력(GPU/Metal·ps·sysctl·네트워크·쓰기 경로)을 프로브 실측 시각과 함께 조회한다. 샌드박스 제한은 그 엔진 한정이다"},
    {"name": "begin_environment_probe", "group": "서버", "who": "모든 실행", "desc": "자기 환경을 잴 일회용 challenge와 프로브 실행 명령을 받는다(현재 Run에 귀속, 15분)"},
    {"name": "report_execution_environment", "group": "서버", "who": "모든 실행", "desc": "challenge로 서명된 프로브 출력을 그대로 보고해 Run의 실제 엔진 이름으로 기록한다(손으로 쓴 보고는 거부)"},
    {"name": "host_gpu_start", "group": "서버", "who": "모든 실행", "desc": "엔진과 무관하게 호스트 GPU에서 llama-server 시험 인스턴스 하나를 띄운다(허용 모델·플래그만, 스왑·시간 가드)"},
    {"name": "host_gpu_status", "group": "서버", "who": "모든 실행", "desc": "호스트 GPU 시험 인스턴스의 준비 상태·남은 시간·스왑 증가·장치 근거 로그"},
    {"name": "host_gpu_bench", "group": "서버", "who": "모든 실행", "desc": "시험 인스턴스에서 벤치 프롬프트를 돌려 토큰 속도를 잰다(호스트 GPU 경로 측정으로 표시)"},
    {"name": "host_gpu_stop", "group": "서버", "who": "모든 실행", "desc": "호스트 GPU 시험 인스턴스를 멈춘다"},
    {"name": "host_operation_start", "group": "서버", "who": "모든 실행", "desc": "승인된 호스트 운영 작업 하나를 실행한다(백업·격리 복원·소유권/성능 확인). 종류만 고르고 경로·셸은 받지 않는다"},
    {"name": "host_operation_status", "group": "서버", "who": "모든 실행", "desc": "운영 작업 목록과 상태·결과(개수·해시·시각)를 조회한다"},
    {"name": "host_operation_cancel", "group": "서버", "who": "모든 실행", "desc": "진행 중인 운영 작업을 취소한다(이미 만든 백업은 보존)"},
    {"name": "collection_mission_status", "group": "수집", "who": "모든 실행", "desc": "수집 임무의 실제 수집·적재 수, 현재 회차의 실패 종류와 출처 정책 누락을 확인한다"},
    {"name": "collection_mission_retry", "group": "수집", "who": "모든 실행", "desc": "실패 원인을 확인한 임무를 새 회차로 재시도한다. 회차가 달라졌거나 진행 중인 작업이 있으면 거절한다"},
    {"name": "notify_user", "group": "알림", "who": "모든 실행", "desc": "폰으로 푸시 알림을 보낸다"},
    {"name": "list_sessions", "group": "대화", "who": "모든 실행", "desc": "최근 대화 목록"},
    {"name": "read_session", "group": "대화", "who": "모든 실행", "desc": "다른 대화의 최근 지시·결과"},
    {"name": "list_memo_categories", "group": "메모", "who": "모든 실행", "desc": "메모 카테고리 목록(순서대로)"},
    {"name": "list_memo_tags", "group": "메모", "who": "모든 실행", "desc": "메모 태그와 쓰인 수(많은 순)"},
    {"name": "list_memos", "group": "메모", "who": "모든 실행", "desc": "메모 목록(고정 먼저)과 발췌, 카테고리·태그로 거르기"},
    {"name": "search_memos", "group": "메모", "who": "모든 실행", "desc": "제목·본문·태그로 메모를 찾는다(카테고리·태그로 좁히기)"},
    {"name": "get_memo", "group": "메모", "who": "모든 실행", "desc": "메모 본문 전체를 읽는다"},
    {"name": "create_memo", "group": "메모", "who": "모든 실행", "desc": "새 메모를 쓴다(카테고리·태그 지정)"},
    {"name": "update_memo", "group": "메모", "who": "모든 실행", "desc": "메모에 덧붙이거나 태그·카테고리를 고친다(사용자 메모 본문은 덮어쓰지 못함)"},
    {"name": "delete_memo", "group": "메모", "who": "모든 실행", "desc": "에이전트가 만든 메모만 보관 처리"},
    {"name": "list_checklist", "group": "체크리스트", "who": "모든 실행", "desc": "날짜별 항목·완료 현황 조회"},
    {"name": "create_checklist_item", "group": "체크리스트", "who": "모든 실행", "desc": "날짜별 항목 추가"},
    {"name": "update_checklist_item", "group": "체크리스트", "who": "모든 실행", "desc": "날짜별 항목 수정·체크·체크 해제"},
    {"name": "delete_checklist_item", "group": "체크리스트", "who": "모든 실행", "desc": "해당 날짜의 항목 삭제"},
    {"name": "list_checklist_templates", "group": "체크리스트", "who": "모든 실행", "desc": "고정 항목 조회"},
    {"name": "create_checklist_template", "group": "체크리스트", "who": "모든 실행", "desc": "매일 반복할 고정 항목 추가"},
    {"name": "update_checklist_template", "group": "체크리스트", "who": "모든 실행", "desc": "앞으로 생성할 고정 항목 수정"},
    {"name": "delete_checklist_template", "group": "체크리스트", "who": "모든 실행", "desc": "고정 항목의 앞으로 반복 중단"},
    {"name": "list_events", "group": "캘린더", "who": "모든 실행", "desc": "기간의 일정과 한국 공휴일"},
    {"name": "create_event", "group": "캘린더", "who": "모든 실행", "desc": "일정을 만든다(종일·시간·반복)"},
    {"name": "update_event", "group": "캘린더", "who": "모든 실행", "desc": "일정을 고친다"},
    {"name": "delete_event", "group": "캘린더", "who": "모든 실행", "desc": "에이전트가 만든 일정만 보관 처리"},
    {"name": "list_meetings", "group": "회의록", "who": "모든 실행", "desc": "회의록 목록(제목·프로젝트·상태·요약). 읽기 전용"},
    {"name": "get_meeting", "group": "회의록", "who": "모든 실행", "desc": "회의록 한 건의 요약·논의·결정·액션·미해결 쟁점. 읽기 전용"},
    {"name": "search_meeting_items", "group": "회의록", "who": "모든 실행", "desc": "회의의 결정·액션 아이템을 찾는다(프로젝트·상태로 좁히기). 읽기 전용"},
    {"name": "search_meetings", "group": "회의록", "who": "모든 실행", "desc": "회의록·전사 구간을 뜻으로 찾는다(로컬 벡터 + 키워드). 읽기 전용"},
    {"name": "spawn_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션을 만들어 일을 맡긴다"},
    {"name": "account_usage", "group": "작업 세션", "who": "오케스트레이터", "desc": "계정별 모델·5시간/주간 잔여·Antigravity 모델 그룹 quota와 AI 크레딧·초기화·한도 대기·실행 중 Run 수"},
    {"name": "list_pms_projects", "group": "PMS", "who": "모든 실행", "desc": "PMS 프로젝트와 진행 상태를 찾는다(회사 실행은 회사 범위)"},
    {"name": "create_pms_task", "group": "PMS", "who": "모든 실행", "desc": "WBS 작업·계층·선행관계·담당·일정·진행률을 등록한다"},
    {"name": "manage_work", "group": "기획", "who": "기획", "desc": "원래 목표·요구·전략·증거·다음 동작을 영속 보존하고 완료/자동 재개를 연결한다"},
    {"name": "dispatch_queued_worker_run", "group": "작업 세션", "who": "오케스트레이터", "desc": "소유 중인 작업 세션의 기존 queued Run 하나를 generation·epoch·pool·순서 fence로 재개한다(새 Run 미생성)"},
    {"name": "reconcile_worker_lease", "group": "작업 세션", "who": "오케스트레이터", "desc": "소유 중인 Run 한 건의 고아 lease를 generation·epoch·process identity fence로 확인하고 안전할 때만 회수한다"},
    {"name": "manage_pms", "group": "PMS", "who": "모든 실행", "desc": "사용자를 대신해 프로젝트·작업 트리·선행관계·마일스톤·이슈를 생성·조회·수정·삭제한다"},
    {"name": "message_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션에 추가 지시"},
    {"name": "list_workers", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션 목록"},
    {"name": "get_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션 상세"},
    {"name": "stop_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션 멈추기"},
    {"name": "stop_agents", "group": "작업 세션", "who": "오케스트레이터", "desc": "사용자 지시에 따라 세션 소유권과 배포 세대를 넘어 활성·대기 에이전트를 전역 중지한다"},
    {"name": "my_context", "group": "작업 세션", "who": "오케스트레이터", "desc": "내 대화의 컨텍스트 사용률"},
    {"name": "reset_my_context", "group": "작업 세션", "who": "오케스트레이터", "desc": "다음 턴부터 내 대화를 새로 시작"},
]


class InstructionsIO(BaseModel):
    text: str = Field(default="", max_length=MAX_INSTRUCTIONS)


class InstructionsOut(InstructionsIO):
    role: instructions_mod.Role = "common"
    # 저장한 적 없이 기본값을 보여 주는 중(접수만 기본값이 있다)
    is_default: bool = False


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: Kind = "fact"
    entities: list[str] = Field(default_factory=list, max_length=10)
    pinned: bool = False
    scope: Literal["personal", "work", "shared"] = "personal"
    source_ref: str | None = Field(default=None, max_length=4096)


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    kind: Kind | None = None
    pinned: bool | None = None


class EntityIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: EType = "other"
    summary: str | None = Field(default=None, max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    pinned: bool = False
    scope: Literal["personal", "work", "shared"] = "personal"
    attributes: dict[str, Any] = Field(default_factory=dict)


class EntityPatch(BaseModel):
    summary: str | None = Field(default=None, max_length=2000)
    aliases: list[str] | None = Field(default=None, max_length=20)
    pinned: bool | None = None
    type: EType | None = None
    attributes: dict[str, Any] | None = None


class RelationIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=60)
    object: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class SourceIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    pattern: str = Field(default="**/*.md", max_length=100)
    scope: Literal["personal", "work", "shared"] = "personal"
    category: Literal["projects", "work-history", "knowledge", "health"] = "knowledge"
    entity_id: uuid.UUID | None = None


class RecallIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=8, ge=1, le=30)
    scope: Literal["all", "personal", "work"] = "all"


def _mem(m: CtxMemory, names: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": str(m.id), "kind": m.kind, "content": m.content, "pinned": m.pinned, "source": m.source,
            "entities": [names.get(i, i) for i in m.entity_ids] if names else m.entity_ids,
            "created_at": m.created_at.isoformat(), "scope": m.scope, "source_ref": m.source_ref}


def _ent(e: CtxEntity) -> dict[str, Any]:
    return {"id": str(e.id), "type": e.type, "name": e.name, "aliases": e.aliases, "summary": e.summary,
            "pinned": e.pinned, "updated_at": e.updated_at.isoformat(), "scope": e.scope, "attributes": e.attributes}


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": str(e)})


# ---- 상태·지침·도구

@router.get("/status")
async def status_(_: User, db: Db) -> dict[str, Any]:
    counts = {
        "memories": await db.scalar(select(func.count()).select_from(CtxMemory).where(CtxMemory.archived_at.is_(None))),
        "entities": await db.scalar(select(func.count()).select_from(CtxEntity)),
        "relations": await db.scalar(select(func.count()).select_from(CtxRelation)),
        "documents": await db.scalar(select(func.count()).select_from(CtxDocument)),
        "chunks": await db.scalar(select(func.count()).select_from(CtxChunk)),
    }
    return {"embedder": "none", "retrieval": "lexical", "model": None,
            "ready": True, "counts": counts,
            "brief": await store.pinned_brief(db)}


@router.get("/instructions", response_model=InstructionsOut)
async def get_instructions(_: User, role: instructions_mod.Role = "common") -> InstructionsOut:
    """역할별 지침(ADR 0013). common 은 접수를 뺀 모든 실행, desk·lead·worker 는 그 역할에만 붙는다."""
    return InstructionsOut(text=instructions_mod.read(role), role=role, is_default=instructions_mod.is_default(role))


@router.put("/instructions", response_model=InstructionsOut)
async def put_instructions(body: InstructionsIO, _: User, db: Db, role: instructions_mod.Role = "common") -> InstructionsOut:
    instructions_mod.write(role, body.text)
    record(db, action="assistant.instructions", detail={"role": role, "chars": len(body.text)})
    await db.commit()
    return InstructionsOut(text=body.text, role=role)


@router.delete("/instructions", response_model=InstructionsOut)
async def reset_instructions(_: User, db: Db, role: instructions_mod.Role = "common") -> InstructionsOut:
    """저장한 지침을 지워 기본값으로 되돌린다."""
    instructions_mod.reset(role)
    record(db, action="assistant.instructions.reset", detail={"role": role})
    await db.commit()
    return InstructionsOut(text=instructions_mod.read(role), role=role, is_default=True)


@router.get("/tools")
async def tools(_: User) -> dict[str, Any]:
    return {"tools": TOOLS}


@router.get("/library")
async def library(_: User, db: Db, scope: Literal["all", "personal", "work"] = "all") -> dict[str, Any]:
    return await library_catalog(db, scope=scope)


@router.post("/recall")
async def recall(body: RecallIn, _: User, db: Db) -> dict[str, Any]:
    hits = await store.recall(db, body.query, body.k, scope=body.scope)
    return {"results": [{"type": h.kind, "id": h.id, "score": round(h.score, 3), "text": h.text, **h.meta} for h in hits]}


# ---- 기억

@router.get("/memories")
async def memories(_: User, db: Db, q: str | None = None, kind: Kind | None = None, pinned: bool | None = None,
                   limit: int = 100) -> dict[str, Any]:
    stmt = select(CtxMemory).where(CtxMemory.archived_at.is_(None))
    if kind:
        stmt = stmt.where(CtxMemory.kind == kind)
    if pinned is not None:
        stmt = stmt.where(CtxMemory.pinned.is_(pinned))
    if q:
        stmt = stmt.where(CtxMemory.content.ilike(f"%{q}%"))
    rows = list(await db.scalars(stmt.order_by(CtxMemory.pinned.desc(), CtxMemory.created_at.desc()).limit(min(limit, 500))))
    ids = {i for m in rows for i in m.entity_ids}
    names = {str(e.id): e.name for e in await db.scalars(select(CtxEntity).where(CtxEntity.id.in_([uuid.UUID(i) for i in ids])))} if ids else {}
    return {"memories": [_mem(m, names) for m in rows]}


@router.post("/memories", status_code=201)
async def add_memory(body: MemoryIn, _: User, db: Db) -> dict[str, Any]:
    try:
        m = await store.remember(db, body.content, kind=body.kind, entities=body.entities, pinned=body.pinned,
                                 scope=body.scope, source_ref=body.source_ref)
    except store.ContextError as e:
        raise _bad(e) from e
    await db.commit()
    return _mem(m)


@router.patch("/memories/{mid}")
async def patch_memory(mid: uuid.UUID, body: MemoryPatch, _: User, db: Db) -> dict[str, Any]:
    m = await db.get(CtxMemory, mid)
    if m is None or m.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기억이 없어요.")
    if body.content is not None and body.content != m.content:
        m.content = body.content
    if body.kind is not None:
        m.kind = body.kind
    if body.pinned is not None:
        m.pinned = body.pinned
    m.updated_at = utcnow()
    await db.commit()
    return _mem(m)


@router.delete("/memories/{mid}", status_code=204)
async def delete_memory(mid: uuid.UUID, _: User, db: Db) -> None:
    if not await store.forget(db, mid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기억이 없어요.")
    await db.commit()


# ---- 온톨로지

@router.get("/entities")
async def entities(_: User, db: Db, type: EType | None = None, q: str | None = None) -> dict[str, Any]:
    stmt = select(CtxEntity)
    if type:
        stmt = stmt.where(CtxEntity.type == type)
    if q:
        stmt = stmt.where(or_(CtxEntity.name.ilike(f"%{q}%"), CtxEntity.summary.ilike(f"%{q}%")))
    rows = await db.scalars(stmt.order_by(CtxEntity.pinned.desc(), CtxEntity.updated_at.desc()).limit(300))
    return {"entities": [_ent(e) for e in rows]}


@router.get("/entities/{eid}")
async def entity(eid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    e = await db.get(CtxEntity, eid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목이 없어요.")
    return await store.entity_detail(db, e)


@router.post("/entities", status_code=201)
async def add_entity(body: EntityIn, _: User, db: Db) -> dict[str, Any]:
    try:
        e = await store.upsert_entity(db, body.name, body.type, summary=body.summary, aliases=body.aliases, pinned=body.pinned,
                                      scope=body.scope, attributes=body.attributes)
    except store.ContextError as ex:
        raise _bad(ex) from ex
    await db.commit()
    return _ent(e)


@router.patch("/entities/{eid}")
async def patch_entity(eid: uuid.UUID, body: EntityPatch, _: User, db: Db) -> dict[str, Any]:
    e = await db.get(CtxEntity, eid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목이 없어요.")
    if body.type is not None:
        e.type = body.type
    if body.aliases is not None:
        e.aliases = sorted({a.strip() for a in body.aliases if a.strip()})
    e = await store.upsert_entity(db, e.name, e.type, summary=body.summary, pinned=body.pinned,
                                  scope=e.scope, attributes=body.attributes)
    await db.commit()
    return _ent(e)


@router.delete("/entities/{eid}", status_code=204)
async def delete_entity(eid: uuid.UUID, _: User, db: Db) -> None:
    e = await db.get(CtxEntity, eid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목이 없어요.")
    await db.delete(e)
    await db.commit()


@router.post("/relations", status_code=201)
async def add_relation(body: RelationIn, _: User, db: Db) -> dict[str, Any]:
    try:
        r = await store.link(db, body.subject, body.predicate, body.object, note=body.note)
    except store.ContextError as e:
        raise _bad(e) from e
    await db.commit()
    return {"id": str(r.id)}


@router.delete("/relations/{rid}", status_code=204)
async def delete_relation(rid: uuid.UUID, _: User, db: Db) -> None:
    r = await db.get(CtxRelation, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "관계가 없어요.")
    await db.delete(r)
    await db.commit()


# ---- 지식(RAG 출처)

def _check_source(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("절대 경로나 ~ 로 시작하는 경로로 적어 주세요.")
    real = p.resolve()
    if not real.exists():
        raise ValueError("그런 폴더나 파일이 없어요.")
    s = get_settings()
    if not any(is_within(real, Path(r).expanduser().resolve()) for r in s.context_source_roots) \
            or is_within(real, s.home) or is_sensitive(real):
        raise ValueError("홈 폴더 안의 일반 폴더만 색인할 수 있어요(따까리 데이터·민감 폴더 제외).")
    return real


async def _source_out(db: Db, s: CtxSource) -> dict[str, Any]:
    docs = await db.scalar(select(func.count()).select_from(CtxDocument).where(CtxDocument.source_id == s.id))
    chunks = await db.scalar(select(func.coalesce(func.sum(CtxDocument.chunk_count), 0)).where(CtxDocument.source_id == s.id))
    return {"id": str(s.id), "path": s.path, "pattern": s.pattern, "enabled": s.enabled, "documents": docs,
            "chunks": int(chunks or 0), "last_indexed_at": s.last_indexed_at.isoformat() if s.last_indexed_at else None,
            "last_error": s.last_error, "indexing": index_queue.is_active(s),
            "scope": s.scope, "category": s.category, "entity_id": str(s.entity_id) if s.entity_id else None}


_tasks: set[asyncio.Task] = set()


async def _start_index(source_id: uuid.UUID) -> None:
    """색인을 시작한다. 원격 모드의 API 는 요청만 남기고 실행부 소비자가 돌린다(Step 6).

    로컬 모드는 예전처럼 이 프로세스에서 바로 돌리되, 같은 출처를 다른 곳이 돌리는 중이면 겹치지 않는다."""
    if deferred():
        await index_queue.request(source_id)
        return
    if not await index_queue.claim(source_id):
        return
    started = False
    try:
        t = asyncio.create_task(index_queue.run_claimed(source_id))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        started = True
    finally:
        if not started:
            await asyncio.shield(index_queue.release(source_id, cancelled=True))


@router.get("/sources")
async def sources(_: User, db: Db) -> dict[str, Any]:
    rows = list(await db.scalars(select(CtxSource).order_by(CtxSource.created_at)))
    return {"sources": [await _source_out(db, s) for s in rows]}


@router.post("/sources", status_code=201)
async def add_source(body: SourceIn, _: User, db: Db) -> dict[str, Any]:
    try:
        real = _check_source(body.path)
    except ValueError as e:
        raise _bad(e) from e
    if await db.scalar(select(CtxSource).where(CtxSource.path == str(real))):
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 등록된 출처예요.")
    if body.entity_id is not None:
        entity = await db.get(CtxEntity, body.entity_id)
        if entity is None or entity.scope != body.scope:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "같은 범위의 프로젝트 항목을 지정해 주세요.")
    s = CtxSource(path=str(real), pattern=body.pattern or "**/*.md",
                  scope=body.scope, category=body.category, entity_id=body.entity_id)
    db.add(s)
    await db.commit()
    await _start_index(s.id)
    await db.refresh(s)
    return await _source_out(db, s)


@router.post("/sources/{sid}/reindex")
async def reindex(sid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    s = await db.get(CtxSource, sid)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "출처가 없어요.")
    await _start_index(s.id)
    await db.refresh(s)
    return await _source_out(db, s)


@router.delete("/sources/{sid}", status_code=204)
async def delete_source(sid: uuid.UUID, _: User, db: Db) -> None:
    s = await db.get(CtxSource, sid)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "출처가 없어요.")
    await db.delete(s)
    await db.commit()


async def wait_indexing(timeout: float = 30) -> None:
    """테스트 전용: 백그라운드 색인이 끝날 때까지 기다린다."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.gather(*list(_tasks)), timeout)
