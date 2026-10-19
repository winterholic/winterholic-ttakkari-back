"""Google 계정 공통 상태(ADR 0051). 신규 표만 추가하고 notebooklm_accounts 에서 계정 수준 상태를 복사한다.

무중단 배포 규칙: 기존 표·컬럼을 바꾸거나 지우지 않는다(이전 세대가 notebooklm_accounts 를 계속 읽고 쓴다).
복사는 멱등이라 다시 실행해도 같은 결과이고, 이미 있는 행은 건드리지 않는다.

계정 수준 상태로의 매핑:
- reauth_required → reauth_required (로그인 만료는 계정 수준이다)
- ready, cooldown, blocked → ready 또는 session_saved. 쿨다운·보호 신호는 기능(NotebookLM) 상태라 notebooklm_accounts 에 남고,
  세션 자체는 마지막으로 성공한 적이 있으면 ready 로 본다.
- session_saved → session_saved
- 그 밖(login_required)은 login_required
"""

import sqlalchemy as sa

from alembic import op

revision = "0049_google_accounts"
down_revision = "0048_game_active_build"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "google_accounts",
        sa.Column("account_id", sa.String(32), primary_key=True),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="login_required"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(40), nullable=True),
        sa.Column("last_login_at", _tz(), nullable=True),
        sa.Column("last_ok_at", _tz(), nullable=True),
        sa.Column("last_checked_at", _tz(), nullable=True),
        sa.Column("reauth_notified_at", _tz(), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
    )
    op.execute(
        """
        INSERT INTO google_accounts
            (account_id, state, message, last_error_code, last_ok_at, last_checked_at, reauth_notified_at,
             created_at, updated_at)
        SELECT account_id,
               CASE
                   WHEN state = 'reauth_required' THEN 'reauth_required'
                   WHEN state = 'session_saved' THEN 'session_saved'
                   WHEN state IN ('ready', 'cooldown', 'blocked') AND last_ok_at IS NOT NULL THEN 'ready'
                   WHEN state IN ('ready', 'cooldown', 'blocked') THEN 'session_saved'
                   ELSE 'login_required'
               END,
               CASE WHEN state IN ('reauth_required', 'session_saved', 'login_required') THEN message ELSE NULL END,
               CASE WHEN state = 'reauth_required' THEN last_error_code ELSE NULL END,
               last_ok_at, last_checked_at, reauth_notified_at, created_at, updated_at
        FROM notebooklm_accounts
        ON CONFLICT (account_id) DO NOTHING
        """
    )


def downgrade():
    op.drop_table("google_accounts")
