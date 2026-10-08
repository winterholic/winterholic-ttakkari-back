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


def _cmd_migrate(_: argparse.Namespace) -> int:
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    return 0


async def _upsert_password(password_hash: str) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from ttakkari import db
    from ttakkari.models import Credential, utcnow

    db.init_engine()
    try:
        async with db.sessionmaker()() as session:
            stmt = insert(Credential).values(id=1, password_hash=password_hash, updated_at=utcnow())
            stmt = stmt.on_conflict_do_update(
                index_elements=[Credential.id],
                set_={"password_hash": stmt.excluded.password_hash, "updated_at": stmt.excluded.updated_at},
            )
            await session.execute(stmt)
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ttakkari")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="API 서버 실행")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=_cmd_serve)

    sub.add_parser("migrate", help="DB 마이그레이션을 head 까지 적용").set_defaults(func=_cmd_migrate)
    sub.add_parser("set-password", help="로그인 비밀번호 설정").set_defaults(func=_cmd_set_password)
    sub.add_parser("init-config", help="~/.ttakkari/config.env 생성").set_defaults(func=_cmd_init_config)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
