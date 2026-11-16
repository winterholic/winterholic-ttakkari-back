import type { Provenance } from "../api/types";
import { formatTime } from "./format";

/** 근거 표시. provenance가 null이면 출처 자체를 읽지 못한 것이고, verifiedAt이 null이면 검증 기록이 없는 것이다. 둘은 다르게 말한다. */
export function ProvenanceBlock({ provenance }: { provenance: Provenance | null }) {
  if (provenance === null) {
    return (
      <div className="provenance" role="group" aria-label="문서 출처">
        <p className="provenance-missing">출처 정보를 불러오지 못했습니다. 문서 목록을 다시 불러오면 표시됩니다.</p>
      </div>
    );
  }
  return (
    <div className="provenance" role="group" aria-label="문서 출처">
      <dl>
        <div>
          <dt>출처</dt>
          <dd>
            {provenance.sourceRefs.length === 0 ? (
              <span className="muted">기록 없음</span>
            ) : (
              <ul className="refs">
                {provenance.sourceRefs.map((ref) => (
                  <li key={ref}>
                    <code>{ref}</code>
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </div>
        <div>
          <dt>소스 리비전</dt>
          <dd>{provenance.sourceRevision ? <code>{provenance.sourceRevision}</code> : <span className="muted">기록 없음</span>}</dd>
        </div>
        <div>
          <dt>생성</dt>
          <dd>
            <time dateTime={provenance.generatedAt ?? undefined}>{formatTime(provenance.generatedAt)}</time>
          </dd>
        </div>
        <div>
          <dt>검증</dt>
          <dd>
            {provenance.verifiedAt === null ? (
              <span className="badge badge-warning">미검증</span>
            ) : (
              <time dateTime={provenance.verifiedAt}>{formatTime(provenance.verifiedAt)}</time>
            )}
          </dd>
        </div>
      </dl>
    </div>
  );
}
