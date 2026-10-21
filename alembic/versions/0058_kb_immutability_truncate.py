"""범용 지식 도서관 KB1·KB2 보완: TRUNCATE 차단, 리비전/패시지 본문·해시 불변식 트리거 및 안전한 다운그레이드 가드.

- 5개 테이블(kb_sources, kb_documents, kb_revisions, kb_passages, kb_evidence)에 BEFORE TRUNCATE 트리거 추가
- kb_revisions 에 text_sha256 및 char_count 불일치 INSERT 차단 트리거 추가
- kb_passages 에 revision slice body 및 sha256 불일치 INSERT 차단 트리거 추가
- 원장에 행이 있으면 downgrade 를 거부하고 ALLOW_KB_DESTRUCTIVE_DOWNGRADE=1 명시 시에만 허용
"""

import os

import sqlalchemy as sa

from alembic import op

revision = "0058_kb_immutability_truncate"
down_revision = "0057_scheduled_actions"
branch_labels = None
depends_on = None

_TRUNCATE_TABLES = ("kb_sources", "kb_documents", "kb_revisions", "kb_passages", "kb_evidence")

TRUNCATE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_truncate_deny() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'kb tables cannot be truncated' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql
"""

REVISION_VALIDATE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_validate_revision() RETURNS trigger AS $$
BEGIN
  IF encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') <> NEW.text_sha256 THEN
    RAISE EXCEPTION 'kb revision text_sha256 mismatch' USING ERRCODE='23514';
  END IF;
  IF char_length(NEW.body) <> NEW.char_count THEN
    RAISE EXCEPTION 'kb revision char_count mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

PASSAGE_VALIDATE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_validate_passage() RETURNS trigger AS $$
DECLARE rev_body text;
BEGIN
  SELECT body INTO rev_body FROM kb_revisions WHERE id = NEW.revision_id;
  IF rev_body IS NULL THEN
    RAISE EXCEPTION 'kb revision not found' USING ERRCODE='23514';
  END IF;
  IF NEW.char_start < 0 OR NEW.char_end <= NEW.char_start OR NEW.char_end > char_length(rev_body) THEN
    RAISE EXCEPTION 'kb passage span out of revision bounds' USING ERRCODE='23514';
  END IF;
  IF substring(rev_body FROM NEW.char_start + 1 FOR NEW.char_end - NEW.char_start) <> NEW.body THEN
    RAISE EXCEPTION 'kb passage body mismatch with revision span' USING ERRCODE='23514';
  END IF;
  IF encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') <> NEW.sha256 THEN
    RAISE EXCEPTION 'kb passage sha256 mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    # 1. TRUNCATE 차단 함수 및 트리거
    op.execute(TRUNCATE_FUNCTION)
    for table in _TRUNCATE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION kb_truncate_deny()"
        )

    # 2. kb_revisions 해시·문자수 검증 트리거
    op.execute(REVISION_VALIDATE_FUNCTION)
    op.execute(
        "CREATE TRIGGER kb_revisions_validate BEFORE INSERT ON kb_revisions "
        "FOR EACH ROW EXECUTE FUNCTION kb_validate_revision()"
    )

    # 3. kb_passages 리비전 본문 슬라이스·해시 검증 트리거
    op.execute(PASSAGE_VALIDATE_FUNCTION)
    op.execute(
        "CREATE TRIGGER kb_passages_validate BEFORE INSERT ON kb_passages "
        "FOR EACH ROW EXECUTE FUNCTION kb_validate_passage()"
    )


def downgrade() -> None:
    # 이 단계는 트리거만 걷어내 데이터를 지우지 않는다. 그래도 여기서 막는 이유는, 이 리비전을 지나야만
    # 0055→0054→0053 downgrade(원장 테이블 DROP)에 닿기 때문이다. 0053~0055 는 고치지 않는 정책이라
    # 데이터 보호는 이 지점이 맡는다. 원장에 행이 있으면 명시 허용 없이는 거부한다.
    if not op.get_context().as_sql and os.environ.get("ALLOW_KB_DESTRUCTIVE_DOWNGRADE") != "1":
        populated = op.get_bind().execute(
            sa.text(
                "SELECT (SELECT count(*) FROM kb_sources) + (SELECT count(*) FROM kb_documents)"
                " + (SELECT count(*) FROM kb_revisions) + (SELECT count(*) FROM kb_passages)"
                " + (SELECT count(*) FROM kb_evidence)"
            )
        ).scalar() or 0
        if populated:
            raise RuntimeError(
                f"Refusing to downgrade populated KB ledger ({populated} rows). "
                "Lower revisions drop the KB tables. Set ALLOW_KB_DESTRUCTIVE_DOWNGRADE=1 to override."
            )

    op.execute("DROP TRIGGER IF EXISTS kb_passages_validate ON kb_passages")
    op.execute("DROP FUNCTION IF EXISTS kb_validate_passage()")
    op.execute("DROP TRIGGER IF EXISTS kb_revisions_validate ON kb_revisions")
    op.execute("DROP FUNCTION IF EXISTS kb_validate_revision()")
    for table in _TRUNCATE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_truncate ON {table}")
    op.execute("DROP FUNCTION IF EXISTS kb_truncate_deny()")
