"""Strict, versioned E0 input schema. Unknown config fields are errors."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]+$", min_length=1)]
Time = Annotated[str, Field(min_length=20)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def time_zone(cls, value, info):
        if (
            info.field_name
            in {"observed_at", "event_at", "received_at", "checked_at", "at"}
            and datetime.fromisoformat(value).tzinfo is None
        ):
            raise ValueError("timezone_required")
        return value


class Passage(Strict):
    id: Key
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]


class SourceIdentity(Strict):
    doc_key: Key
    source_revision_id: Key
    raw_sha256: Hash
    body_sha256: Hash
    source_uri: Annotated[str, Field(min_length=1)]
    observed_at: Time


class Source(SourceIdentity):
    raw_utf8: str
    body: str
    passages: list[Passage]


class Anchor(SourceIdentity):
    quote: Annotated[str, Field(min_length=1)]
    quote_sha256: Hash
    context_before: str = ""
    context_after: str = ""
    answer_id: Key
    match_normalization_version: Literal["exact-v1"]


class Case(Strict):
    id: Key
    query: Annotated[str, Field(min_length=1)]
    group: Key
    split: Literal["dev", "holdout"]
    documents: Annotated[list[Key], Field(min_length=1)]
    answerable: bool
    relevance: dict[Key, Annotated[int, Field(ge=0, le=3)]]
    domain: Key
    types: Annotated[list[Key], Field(min_length=1)]
    language_pair: Key
    verification: Literal["draft", "agent_verified", "llm_judged", "human_verified"]
    anchors: list[Anchor]


Failure = Literal[
    "not_in_corpus",
    "no_candidate",
    "cut_by_fusion",
    "ranked_out",
    "wrong_condition",
    "false_abstain",
    "false_strong",
    "false_answer",
    "filtered_out",
    "index_stale",
    "calibration_insufficient",
    "adapter_error",
    "missing_prediction",
]


class Trace(Strict):
    indexed: list[Key] | None = None
    routes: dict[Key, list[Key]] | None = None
    fused: list[Key] | None = None
    condition_correct: bool | None = None
    filtered: bool = False
    stale: bool = False
    calibration_sufficient: bool | None = None


class Prediction(Strict):
    ranked: list[Key]
    candidates: list[Key] = []
    coverage: Literal["strong", "weak", "none", "unknown"]
    answer_correct: bool | None = None
    score_bytes: list[Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]] = []
    failure: Failure | None = None
    trace: Trace | None = None


class ClaimedRightsReviewContext(Strict):
    claimed_issuer: str
    claimed_subject: str
    checked_at: Time


class RightsReviewEvidence(Strict):
    # Descriptive provenance from input, never a server-authenticated assertion.
    evidence_id: Key
    version: Key
    content_sha256: Hash
    original_url: str
    reviewer_context: ClaimedRightsReviewContext


class Right(Strict):
    component: Literal["code", "data", "qrels", "corpus"]
    original_url: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(min_length=1)]
    checked_at: Time
    storage_allowed: bool | None
    transform_allowed: bool | None
    redistribute_allowed: bool | None
    status: Literal["verified", "unverified"]
    basis: Annotated[str, Field(min_length=1)]
    provenance_kind: Literal["synthetic", "external"] = "external"
    review_evidence: RightsReviewEvidence | None = None


class RuntimeConfig(Strict):
    N: Annotated[int, Field(gt=0)]
    window: Key
    role: Literal["query", "passage"]


class Manifest(Strict):
    code_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    build_id: Key
    model_id: Key
    model_sha256: Hash
    analyzer_id: Key
    analyzer_sha256: Hash
    quantization: Key
    query_policy_version: Key
    seed: Annotated[int, Field(ge=0)]
    config: RuntimeConfig


class Tolerance(Strict):
    version: Literal["initial-unvalidated"]
    cosine: Literal[0.0001]
    logit: Literal[0.001]


class Policy(Strict):
    margin: Literal[0.05]
    alpha: Literal[0.05]
    bootstrap_replicates: Literal[10000]
    statistics_policy_version: Literal["kb-e0-statistics-v3"] = "kb-e0-statistics-v3"
    minimum_latency_replicates: Literal[10000] = 10000
    live_tolerance: Tolerance


class Exposure(Strict):
    benchmark_family: str
    split_overlap: Literal["verified", "unverified"]
    checkpoint_lineage: Literal["verified", "unverified"]


class Observation(Strict):
    id: Key
    source_revision_id: Key
    event_at: Time
    received_at: Time


class Observations(Strict):
    kind: Literal["fixture", "database_export"]
    database_revision: str | None
    events: list[Observation]


class Bundle(Strict):
    schema_version: Literal["kb-e0-v1"]
    kind: Literal["fixture", "public", "custom"]
    corpus_id: Key
    corpus_version: Key
    sources: Annotated[list[Source], Field(min_length=1)]
    cases: Annotated[list[Case], Field(min_length=1)]
    links: list[list[Key]]
    predictions: dict[Key, Prediction]
    rights: list[Right]
    manifest: Manifest
    policy: Policy
    exposure: Exposure
    observations: Observations


class LatencyArm(Strict):
    e2e_ms: Annotated[float, Field(gt=0)]
    queue_ms: Annotated[float, Field(ge=0)]
    service_ms: Annotated[float, Field(ge=0)]
    load: Annotated[float, Field(ge=0)]
    stages_ms: dict[str, Annotated[float, Field(ge=0)] | None]
    stages_missing: dict[str, str] = {}

    @model_validator(mode="after")
    def missing_stages_need_reasons(self):
        if any(
            value is None and not self.stages_missing.get(key)
            for key, value in self.stages_ms.items()
        ):
            raise ValueError("stage_missing_reason_required")
        return self


class LatencyBlock(Strict):
    block: Annotated[int, Field(ge=0)]
    order: Literal["AB", "BA"]
    query_id: Key
    profile: Annotated[str, Field(min_length=1)]
    elapsed_seconds: Annotated[float, Field(gt=0)]
    candidate: LatencyArm
    control: LatencyArm
    weight: float = 1.0

    @field_validator("weight")
    @classmethod
    def uniform_blocks_only(cls, value):
        if value != 1.0:
            raise ValueError("weighted_blocks_not_registered")
        return value
