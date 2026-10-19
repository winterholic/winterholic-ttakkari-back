from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import sys
from pathlib import Path

from ttakkari.config import DEFAULT_HOME

# alembic.ini 는 패키지가 아니라 레포 루트에 있다.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("ttakkari.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _cmd_serve_games(args: argparse.Namespace) -> int:
    """별도 게임 origin 만 띄운다(API·DB 라우트 없음). 로컬 검증이나 별도 포트/터널 운영용이다."""
    import uvicorn

    from ttakkari.games.origin import install_access_log_filter

    install_access_log_filter()
    uvicorn.run("ttakkari.games.origin:app", host=args.host, port=args.port)
    return 0


def _cmd_migrate(_: argparse.Namespace) -> int:
    from alembic.config import Config

    from alembic import command
    from ttakkari.generation_bootstrap import assert_legacy_jobs_idle, holding
    if holding():
        asyncio.run(assert_legacy_jobs_idle())
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    return 0


async def _upsert_password(password_hash: str) -> None:
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert

    from ttakkari import db
    from ttakkari.models import Credential, RefreshToken, utcnow

    db.init_engine()
    try:
        async with db.sessionmaker()() as session:
            stmt = insert(Credential).values(id=1, password_hash=password_hash, updated_at=utcnow())
            stmt = stmt.on_conflict_do_update(
                index_elements=[Credential.id],
                set_={"password_hash": stmt.excluded.password_hash, "updated_at": stmt.excluded.updated_at},
            )
            await session.execute(stmt)
            await session.execute(
                update(RefreshToken).where(RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow())
            )
            await session.commit()
    finally:
        await db.dispose_engine()


def _cmd_set_password(_: argparse.Namespace) -> int:
    from ttakkari.security.passwords import hash_password

    # 비대화형 테스트용. 셸 히스토리에 남을 수 있으니 실사용은 프롬프트로 한다.
    password = os.environ.get("TTAKKARI_NEW_PASSWORD")
    if password is None:
        password = getpass.getpass("새 비밀번호: ")
        if password != getpass.getpass("다시 입력: "):
            print("비밀번호가 일치하지 않습니다.", file=sys.stderr)
            return 1
    try:
        password_hash = hash_password(password)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    asyncio.run(_upsert_password(password_hash))
    print("비밀번호를 저장했습니다.")
    return 0


def _cmd_init_config(_: argparse.Namespace) -> int:
    home = Path(os.environ.get("TTAKKARI_HOME") or DEFAULT_HOME).expanduser()
    path = home / "config.env"
    if path.exists():
        print(f"이미 존재합니다. 덮어쓰지 않습니다: {path}")
        return 0
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = (
        "TTAKKARI_ENV=dev\n"
        f"TTAKKARI_JWT_SECRET={secrets.token_urlsafe(48)}\n"
        "TTAKKARI_DATABASE_URL=postgresql+asyncpg://localhost/ttakkari\n"
    )
    # O_EXCL 로 확인과 생성 사이의 경쟁을 막고, 비밀이 담기므로 처음부터 0600 으로 만든다.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"생성했습니다: {path}")
    return 0


def _cmd_gen_vapid(_: argparse.Namespace) -> int:
    from ttakkari.push import vapid

    path = vapid.vapid_path()
    if path.exists():
        print(f"이미 존재합니다. 덮어쓰지 않습니다(기존 구독이 무효가 됩니다): {path}")
        return 0
    vapid.generate(path)
    print(f"생성했습니다: {path}")
    return 0


def _cmd_context_import(args: argparse.Namespace) -> int:
    import json

    from ttakkari import db
    from ttakkari.broker.policy import is_sensitive
    from ttakkari.context.library import import_catalog

    path = Path(args.catalog).expanduser().resolve()
    if is_sensitive(path) or path.suffix.lower() != ".json" or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("2MB 이하의 일반 JSON 목록 파일을 지정해 주세요.")
    data = json.loads(path.read_text())

    async def run():
        db.init_engine()
        try:
            async with db.sessionmaker()() as session:
                return await import_catalog(session, data)
        finally:
            await db.dispose_engine()

    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
    return 0


def _cmd_context_inventory(args: argparse.Namespace) -> int:
    import json
    import signal

    from ttakkari.context.inventory import (
        PARSER_VERSION,
        inspect_export,
        write_inventory,
    )

    class InventoryDeadlineExceeded(Exception):
        pass

    def deadline(signum, frame):
        raise InventoryDeadlineExceeded

    try:
        if args.timeout <= 0:
            raise ValueError('invalid_timeout')
        # This standalone macOS CLI owns its process timer. Never arm this in the API.
        previous = signal.getsignal(signal.SIGALRM)
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise ValueError('process_timer_already_active')
        signal.signal(signal.SIGALRM, deadline)
        try:
            signal.setitimer(signal.ITIMER_REAL, args.timeout)
            try:
                report = inspect_export(Path(args.source), expected_shards=args.expected_shards)
            except InventoryDeadlineExceeded:
                report = {'schema_version': 1, 'parser_version': PARSER_VERSION,
                          'status': 'incomplete', 'counts': {}, 'units': [],
                          'corpus_fingerprint': None,
                          'completeness_scope': 'unobserved_due_to_deadline',
                          'errors': [{'code': 'inventory_deadline_exceeded',
                                      'shard': None, 'json_pointer': None}],
                          'capabilities': {'semantic_extraction': 'not_run',
                                           'index_reconciliation': 'not_run',
                                           'attachment_payloads': 'not_read',
                                           'database_writes': False}}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        write_inventory(report, Path(args.output), source=Path(args.source))
    except (OSError, ValueError):
        # Avoid exception text: a parser or path error can contain private data.
        print('Inventory output failed; use a new directory outside the source.', file=sys.stderr)
        return 2
    print(json.dumps({'status': report['status'], 'counts': report['counts'],
                      'corpus_fingerprint': report['corpus_fingerprint']}, ensure_ascii=False))
    return 0 if report['status'] == 'complete' else 2


def _cmd_desk_eval(args: argparse.Namespace) -> int:
    from ttakkari import db
    from ttakkari.runs import desk_eval

    async def run():
        db.init_engine()
        try:
            async with db.sessionmaker()() as session:
                return await desk_eval.run_eval(session, args.limit, args.engine)
        finally:
            await db.dispose_engine()

    try:
        report = asyncio.run(run())
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(desk_eval.format_report(report))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ttakkari")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="API 서버 실행")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("serve-games", help="별도 게임 origin(정적 빌드 서빙, 자격증명 없음)만 실행")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8795)
    p.set_defaults(func=_cmd_serve_games)

    sub.add_parser("migrate", help="DB 마이그레이션을 head 까지 적용").set_defaults(func=_cmd_migrate)
    sub.add_parser("set-password", help="로그인 비밀번호 설정").set_defaults(func=_cmd_set_password)
    sub.add_parser("init-config", help="~/.ttakkari/config.env 생성").set_defaults(func=_cmd_init_config)

    sub.add_parser("gen-vapid", help="Web Push VAPID 키 생성(~/.ttakkari/vapid.json)").set_defaults(func=_cmd_gen_vapid)
    p = sub.add_parser("context-import", help="출처가 명시된 프로젝트 목록을 맥락 도서관에 등록·색인")
    p.add_argument("catalog", help="JSON 목록 경로")
    p.set_defaults(func=_cmd_context_import)

    p = sub.add_parser('context-inventory', help='ChatGPT 원본의 모든 분기를 읽기 전용으로 집계(추출·색인 아님)')
    p.add_argument('source', help='내보내기 폴더 또는 conversations*.json 파일')
    p.add_argument('--output', required=True, help='원본 밖의 새 출력 폴더(덮어쓰기 안 함)')
    p.add_argument('--expected-shards', type=int, default=None, help='알고 있는 전체 shard 수; 불일치 시 실패')
    p.add_argument('--timeout', type=float, default=60, help='원본 관측 제한 시간(초, 기본 60); 초과는 unknown/실패')
    p.set_defaults(func=_cmd_context_inventory)

    from ttakkari.knowledge.commands import run as knowledge_command
    p=sub.add_parser('knowledge-import-chatgpt',help='지정한 원본 shard의 모든 분기·정확한 텍스트를 원장에 보존')
    p.add_argument('source')
    p.add_argument('--namespace',required=True,help='동일 원본의 재실행에 유지할 식별자')
    p.add_argument('--scope',choices=['personal','work','shared'],default='personal')
    p.add_argument('--timeout',type=float,default=60)
    p.add_argument('--output',help='새 JSON 결과 파일(덮어쓰기 금지)')
    p.set_defaults(func=knowledge_command)
    p=sub.add_parser('knowledge-backfill',help='등록된 문서의 원문 버전을 제한된 묶음으로 보존(삭제·모델 호출 없음)')
    p.add_argument('--limit',type=int,default=100)
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--timeout',type=float,default=120)
    p.add_argument('--output',required=True)
    p.set_defaults(func=knowledge_command)
    p=sub.add_parser('knowledge-eval',help='원문 gold로 원본검색·BM25·기존검색·근거검색·관계확장을 비교')
    p.add_argument('gold',help='dev/holdout/group/relevance가 명시된 JSON')
    p.add_argument('--split',choices=['dev','holdout'],default='holdout')
    p.add_argument('--scope',choices=['all','personal','work'],default='all')
    p.add_argument('--k',type=int,default=5)
    p.add_argument('--timeout',type=float,default=120)
    p.add_argument('--output',required=True,help='새 JSON 결과 파일(덮어쓰기 금지)')
    p.set_defaults(func=knowledge_command)

    p = sub.add_parser("desk-eval", help="접수 판정 모음을 지금 접수 지침·모델로 다시 분류해 정확도를 잰다(계정 사용량을 쓴다)")
    p.add_argument("--limit", type=int, default=None, help="최근 판정 N개만")
    p.add_argument("--engine", choices=["codex", "claude"], default=None, help="이 엔진 계정만(기본: 접수 엔진 순서)")
    p.set_defaults(func=_cmd_desk_eval)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
