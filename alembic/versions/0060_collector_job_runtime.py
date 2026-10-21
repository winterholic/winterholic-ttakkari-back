"""수집 플랫폼 S2-02: collector_jobs 에 claim·cursor·취소·오류 기록 컬럼 추가.

- cursor: 한 page 의 결과와 같은 트랜잭션으로 전진하는 재개 지점
- cancel_requested: running job 은 batch 경계에서 취소를 확인한다
- last_error: 마지막 재시도/차단 사유(코드·시각·상세)
- heartbeat_at: 소유 세대가 살아 있음을 알리는 시각. 이것만으로 다른 세대가 작업을 가져가지 않는다
- 기존 collector_jobs 에 nullable/기본값 컬럼만 더하는 online-safe 마이그레이션(테이블 재작성 없음)
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0060_collector_job_runtime"
down_revision = "0059_collector_intake"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collector_jobs", sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column(
        "collector_jobs",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("collector_jobs", sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("collector_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("collector_jobs", "heartbeat_at")
    op.drop_column("collector_jobs", "last_error")
    op.drop_column("collector_jobs", "cancel_requested")
    op.drop_column("collector_jobs", "cursor")
