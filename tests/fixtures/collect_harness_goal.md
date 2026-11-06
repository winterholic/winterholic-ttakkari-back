AI Harness Engineering 종합 기술 백과사전 구축 지시서

1. 프로젝트 목적

AI Harness Engineering, Agent Engineering, Agent Runtime, LLM Orchestration 및 관련 기술을 종합적으로 조사하여 실제 AI 에이전트 시스템의 설계·구현·운영·최적화에 활용할 수 있는 기술 지식 백과사전을 구축한다.

본 프로젝트의 목표는 특정 하네스를 새로 구현하거나 기존 시스템을 리팩토링하는 것이 아니다.

AI 모델이 실제 작업을 정확하고 효율적으로 수행하도록 만드는 모든 기술적·공학적 방법론을 체계적으로 수집하는 것이 목적이다.

다음 질문들에 근거를 갖고 답할 수 있는 지식 기반을 구축한다.

* 동일한 LLM을 사용하면서 에이전트의 작업 성공률을 높이는 방법은 무엇인가?
* AI가 복잡한 작업을 끝까지 수행하도록 만드는 방법은 무엇인가?
* 도구 호출, 컨텍스트 관리, 메모리, 작업 계획을 어떻게 최적화하는가?
* 단일 에이전트와 멀티 에이전트 중 어느 방식이 효율적인가?
* 작업 중단, 무한 반복, 잘못된 완료 판단을 어떻게 방지하는가?
* 불필요한 검증과 과도한 계획 수립을 어떻게 줄이는가?
* 모델의 추론 능력과 하네스의 실행 능력을 어떻게 분리하여 평가하는가?
* 장시간 실행되는 에이전트를 안정적으로 운영하는 방법은 무엇인가?
* 하네스의 품질을 어떤 지표로 평가하고 개선하는가?

프롬프트 엔지니어링을 포함하되 이에 한정하지 않는다.

기초 이론부터 최신 연구, 상용 시스템의 공개 아키텍처, 오픈소스 구현 및 실제 운영 사례까지 포괄한다.

⸻

2. 조사 기본 원칙

2.1 용어와 범위

Harness Engineering은 아직 범위와 정의가 완전히 통일된 단일 학문 분야가 아니다.

따라서 특정 기업이나 프레임워크가 사용하는 정의를 절대적인 기준으로 삼지 않는다.

다음 관련 분야를 함께 조사한다.

* AI Agent Engineering
* Agent Harness
* Agent Runtime
* Agentic Systems
* LLM Systems Engineering
* Context Engineering
* Tool-use Engineering
* Agent Orchestration
* Workflow Engineering
* Agent Reliability Engineering
* Agent Evaluation
* Autonomous Software Engineering

서로 다른 개념은 구분하되 기술적으로 연결한다.

2.2 원천 자료 우선

다음 자료를 우선적으로 수집한다.

1. 공식 기술 문서 및 엔지니어링 블로그
2. 학술 논문과 재현 가능한 연구
3. 공개된 에이전트 구현 코드
4. 실제 운영 환경의 기술 사례
5. 주요 컨퍼런스 발표 및 강의
6. 신뢰할 수 있는 기술 분석
7. 커뮤니티의 실험과 경험

기업의 성능 주장과 독립적인 검증 결과를 구분한다.

2.3 이론과 구현 연결

각 기술은 가능한 범위에서 다음 정보를 포함한다.

* 해결하려는 문제
* 핵심 원리
* 설계 패턴
* 실제 구현 방법
* 대표 프레임워크 및 코드
* 적용하기 적합한 상황
* 장점과 한계
* 실패 사례
* 성능 및 비용 영향
* 다른 접근법과의 비교
* 평가 및 검증 방법

단순한 개념 설명이나 링크 모음으로 끝내지 않는다.

2.4 폭넓은 탐색

아래 조사 분야는 최소 범위다.

조사 과정에서 발견되는 중요한 기술과 연구 방법론은 자율적으로 추가한다.

다만 관련성이 낮은 분야까지 무분별하게 확장하지 않는다.

⸻

3. 핵심 조사 분야

A. AI 에이전트 및 하네스 아키텍처

AI 에이전트의 구조와 실행 원리를 조사한다.

조사 대상

* Agent Loop
* ReAct
* Plan-and-Execute
* Reflection
* Self-correction
* Tool-augmented Reasoning
* Stateful / Stateless Agents
* Event-driven Agents
* Workflow-based Agents
* Autonomous Agents
* Deterministic / Probabilistic Workflows
* Agent State Machine
* Agent Runtime Architecture
* Agent Lifecycle Management
* Long-running Agents
* Durable Execution
* Checkpoint / Resume
* Event Sourcing
* Human-in-the-loop

특히 모델이 판단하는 영역과 하네스가 결정적으로 제어해야 하는 영역을 구분한다.

예를 들어 모델이 도구를 선택하는 것과 하네스가 도구 실행 권한을 검증하는 것은 서로 다른 책임이다.

이러한 책임 분리의 설계 원칙을 심층 조사한다.

B. Context Engineering

모델이 작업을 수행하는 데 필요한 정보를 효율적으로 제공하는 기술을 조사한다.

조사 대상

* Context Window Management
* Context Selection
* Context Compression
* Context Compaction
* Context Summarization
* Context Pruning
* Prompt Caching
* Dynamic Context Assembly
* Context Retrieval
* Context Relevance Ranking
* Lost-in-the-middle 문제
* Context Rot
* Instruction Hierarchy
* System Prompt Architecture
* Working Context / Persistent Context
* Task-specific Context
* Just-in-time Context Loading
* Repository Context Discovery

다음 문제를 집중 조사한다.

* 컨텍스트가 많을수록 항상 좋은가?
* 작업과 무관한 정보를 어떻게 제거하는가?
* 긴 작업에서 중요한 정보를 어떻게 유지하는가?
* 컨텍스트 압축 과정에서 의미 손실을 어떻게 방지하는가?
* 검색된 문서를 언제, 얼마나 모델에 제공해야 하는가?
* 반복적인 도구 출력으로 컨텍스트가 오염되는 문제를 어떻게 해결하는가?

토큰 비용, 정확도, 지연시간 사이의 관계를 함께 조사한다.

C. Memory Engineering

AI 에이전트의 기억 시스템을 조사한다.

* Short-term Memory
* Long-term Memory
* Episodic Memory
* Semantic Memory
* Procedural Memory
* Working Memory
* Memory Consolidation
* Memory Retrieval
* Memory Decay
* Memory Updating
* Memory Deduplication
* Memory Conflict Resolution
* Vector Memory
* Graph Memory
* Structured Memory
* Persistent Agent State
* Cross-session Memory

특히 단순 RAG와 에이전트 메모리 시스템의 차이를 명확히 조사한다.

기억할 정보와 일시적으로 사용할 정보를 구분하는 기준도 수집한다.

D. Tool-use Engineering

AI 에이전트가 외부 도구를 안정적으로 사용하는 방법을 조사한다.

주요 주제

* Function Calling
* Tool Calling
* Structured Outputs
* JSON Schema
* Tool Selection
* Tool Routing
* Tool Discovery
* Dynamic Tool Loading
* Tool Result Parsing
* Tool Error Handling
* Retry / Timeout
* Idempotency
* Parallel Tool Execution
* Sequential Tool Execution
* Tool Dependency Management
* Tool Permission Management
* Tool Result Validation
* Tool Call Optimization

관련 기술

* MCP(Model Context Protocol)
* Agent-to-Agent Protocol
* OpenAPI 기반 도구 통합
* CLI Tool Integration
* Browser Automation
* Computer Use
* Code Execution
* Shell Execution
* Database Tools
* External API Integration

MCP 자체의 구조뿐 아니라 도구를 많이 연결했을 때 발생하는 컨텍스트 증가, 선택 오류, 호출 지연, 보안 문제를 함께 조사한다.

E. Planning & Task Decomposition

복잡한 작업을 효율적으로 계획하고 실행하는 방법을 조사한다.

* Task Decomposition
* Hierarchical Planning
* Dynamic Planning
* Adaptive Planning
* Replanning
* Dependency Graph
* DAG-based Execution
* Goal-oriented Planning
* Milestone Management
* Work Breakdown Structure
* Critical Path
* Task Prioritization
* Budget-aware Planning
* Uncertainty-aware Planning
* Opportunistic Execution
* Progressive Planning

특히 다음 문제를 심층 조사한다.

1. 과도한 계획 수립

계획이 지나치게 세분화되어 실제 작업보다 관리 비용이 커지는 현상.

2. 불필요한 순차 실행

독립적으로 수행 가능한 작업을 순차적으로 처리하여 시간이 낭비되는 현상.

3. 계획과 실제 실행의 괴리

초기 계획이 잘못되었는데도 계획을 수정하지 않고 계속 실행하는 현상.

4. 잘못된 작업 종료

일부 작업이 완료되지 않았음에도 전체 작업을 완료했다고 판단하는 현상.

5. 불필요한 반복

동일한 실험·검증·수정 과정을 반복하면서 진전이 없는 현상.

각 문제에 대한 기존 연구와 실용적인 해결 패턴을 조사한다.

F. Multi-Agent Engineering

복수 에이전트 시스템의 설계 및 운영 방법을 조사한다.

* Supervisor Architecture
* Hierarchical Agents
* Peer-to-peer Agents
* Planner / Executor
* Worker Agents
* Specialist Agents
* Critic / Reviewer Agents
* Debate Agents
* Agent Delegation
* Agent Handoff
* Agent Communication
* Shared Memory
* Blackboard Architecture
* Multi-Agent Coordination
* Agent Scheduling
* Parallel Execution
* Resource Allocation
* Agent Conflict Resolution

특히 멀티 에이전트가 단일 에이전트보다 항상 우수하지 않다는 점을 전제로 조사한다.

다음 요소를 비교한다.

* 작업 성공률
* 총 토큰 사용량
* 실제 소요 시간
* 중복 작업 발생률
* 통신 오버헤드
* 작업 충돌
* 컨텍스트 손실
* 비용 대비 성능

멀티 에이전트를 사용하는 것이 유리한 조건과 오히려 비효율적인 조건을 구분한다.

G. Agent Reliability Engineering

에이전트가 장시간 안정적으로 작업을 수행하기 위한 기술을 조사한다.

* Fault Tolerance
* Failure Recovery
* Retry Strategy
* Exponential Backoff
* Circuit Breaker
* Timeout Management
* Deadlock Detection
* Infinite Loop Detection
* Stagnation Detection
* Progress Tracking
* Checkpointing
* Durable State
* Crash Recovery
* Transactional Execution
* Idempotent Operations
* Partial Failure Handling
* Graceful Degradation
* Resource Exhaustion Handling

특히 작업이 중간에 멈추거나, 실행 가능한 후속 작업이 있는데도 잘못 종료하는 문제를 집중 조사한다.

실패 원인을 모델의 판단 오류, 하네스 설계 문제, 도구 오류, 외부 환경 제약으로 구분하는 방법도 조사한다.

H. Agent Evaluation & Benchmarking

하네스의 품질을 객관적으로 측정하는 방법을 조사한다.

주요 지표

* Task Success Rate
* End-to-end Completion Rate
* Tool-use Accuracy
* Recovery Success Rate
* Time to Completion
* Token Efficiency
* Cost per Successful Task
* Latency
* Error Rate
* Hallucination Rate
* Context Utilization
* Planning Efficiency
* Agent Coordination Overhead

평가 방법

* Offline Evaluation
* Online Evaluation
* Trajectory Evaluation
* Outcome-based Evaluation
* LLM-as-a-Judge
* Deterministic Evaluation
* Regression Testing
* A/B Testing
* Ablation Study
* Failure Taxonomy
* Benchmark Contamination Analysis

주요 벤치마크

* SWE-bench
* SWE-bench Verified
* GAIA
* WebArena
* BrowserGym
* OSWorld
* τ-bench
* AgentBench
* 기타 관련 벤치마크

각 벤치마크가 실제로 무엇을 평가하는지 구분한다.

모델 성능과 하네스 성능을 분리하여 비교하는 실험 설계 방법을 특히 중요하게 다룬다.

I. Agent Observability

AI 에이전트의 실행 과정과 실패 원인을 추적하는 기술을 조사한다.

* Agent Tracing
* Tool Call Tracing
* Distributed Tracing
* OpenTelemetry
* Execution Timeline
* Structured Logging
* Token Usage Tracking
* Cost Tracking
* Error Classification
* Agent Decision Logging
* Trajectory Visualization
* Replay / Debugging
* Root Cause Analysis
* Performance Profiling

에이전트가 왜 특정 행동을 했는지, 어떤 도구에서 실패했는지, 어디에서 시간을 낭비했는지 분석할 수 있는 방법을 조사한다.

관측 가능한 실행 기록과 모델 내부 추론을 구분한다.

J. Agent Optimization

동일한 모델을 사용하면서 작업 효율과 성공률을 개선하는 기술을 조사한다.

* Model Routing
* Dynamic Model Selection
* Small / Large Model Cascades
* Adaptive Reasoning
* Token Budget Optimization
* Prompt Optimization
* Tool Call Batching
* Parallel Execution
* Caching
* Speculative Execution
* Early Exit
* Adaptive Verification
* Cost-aware Orchestration
* Latency-aware Scheduling
* Context Optimization
* Workflow Optimization

특히 다음 관점에서 조사한다.

더 강력한 모델을 사용하는 것보다 하네스를 개선하는 편이 효과적인 상황은 무엇인가?

실제 성능 향상이 확인된 연구와 사례를 우선적으로 수집한다.

K. Verification & Quality Control

에이전트의 결과를 검증하는 방법을 조사한다.

* Self-verification
* Cross-model Verification
* Critic Models
* Independent Review
* Test-driven Agents
* Assertion-based Validation
* Formal Verification
* Property-based Testing
* Differential Testing
* Confidence Estimation
* Risk-based Verification
* Adaptive Verification
* Human Approval

특히 검증 비용이 실제 작업 비용보다 커지는 문제를 집중 조사한다.

모든 작업을 동일한 강도로 검증하는 방식과 위험도에 따라 검증 수준을 조절하는 방식을 비교한다.

검증을 추가할수록 품질이 향상되는지, 어느 시점부터 수익이 감소하는지 실험 근거를 조사한다.

L. Agent Security & Isolation

* Prompt Injection
* Tool Output Injection
* Indirect Prompt Injection
* Data Exfiltration
* Privilege Escalation
* Tool Permission Boundaries
* Sandboxing
* Container Isolation
* Filesystem Isolation
* Network Isolation
* Secret Management
* Credential Delegation
* Least Privilege
* Approval Gates
* Audit Logging
* Supply Chain Security
* Untrusted Repository Handling

신뢰할 수 없는 외부 데이터가 에이전트의 명령으로 해석되는 문제를 중점적으로 조사한다.

보안성과 작업 효율 사이의 균형도 함께 연구한다.

M. Coding Agent Engineering

실제 소프트웨어 개발 에이전트의 구조와 방법론을 조사한다.

* Repository Exploration
* Codebase Indexing
* Symbol Search
* AST-based Navigation
* Dependency Analysis
* Code Editing
* Patch Generation
* Test Execution
* Build Validation
* Git Integration
* Branch Management
* PR Automation
* Code Review
* Debugging Agents
* Refactoring Agents
* Software Maintenance Agents
* Long-horizon Coding Tasks

특히 다음 문제를 조사한다.

* 코드베이스 전체를 읽지 않고 필요한 부분만 찾는 방법
* 변경 범위를 최소화하면서 정확하게 수정하는 방법
* 테스트를 과도하게 반복하지 않는 방법
* 복잡한 개발 작업을 중단 없이 진행하는 방법
* Git diff와 실행 결과를 이용하여 작업 진척도를 판단하는 방법
* 개발 완료 여부를 객관적으로 판단하는 방법

N. Agent Skills & Knowledge Reuse

* Agent Skills
* Reusable Workflows
* Skill Discovery
* Skill Selection
* Dynamic Skill Loading
* Procedural Knowledge
* Tool / Skill Composition
* Skill Versioning
* Skill Evaluation
* Skill Learning
* Skill Generation
* Knowledge Transfer
* Cross-agent Skill Sharing

반복적인 작업을 재사용 가능한 절차로 만드는 방법을 조사한다.

스킬과 프롬프트 템플릿, 도구, 워크플로, 메모리의 차이를 명확히 구분한다.

O. Agent Runtime & Infrastructure

* Agent Execution Environment
* Containerized Agents
* Ephemeral Environments
* Persistent Workspaces
* Remote Execution
* Cloud-based Agents
* Local Agents
* Background Agents
* Queue-based Execution
* Distributed Workers
* Job Scheduling
* Resource Management
* Session Management
* State Persistence
* Artifact Management
* Workspace Synchronization
* Multi-tenant Isolation

장시간 실행되는 에이전트를 서버에서 안정적으로 운영하는 방법을 조사한다.

P. Human-Agent Interaction

* Human-in-the-loop
* Human-on-the-loop
* Approval Workflow
* Interactive Planning
* Clarification Strategy
* Progress Reporting
* User Intent Tracking
* User Preference Management
* Trust Calibration
* Explainability
* Interrupt / Resume
* User Feedback Integration

언제 사용자에게 질문해야 하고, 언제 스스로 진행해야 하는지에 대한 판단 기준을 조사한다.

불필요한 질문이나 승인 요청으로 작업 흐름이 끊기는 문제도 포함한다.

⸻

4. 주요 연구 대상 및 구현체

다음 프로젝트와 기술을 우선적으로 조사한다.

상용 에이전트 및 개발 도구

* OpenAI Codex
* Claude Code
* Cursor Agent
* GitHub Copilot
* Gemini CLI
* OpenAI Agents SDK
* 기타 주요 Coding Agent

오픈소스 에이전트 프레임워크

* LangGraph
* LangChain
* AutoGen
* Microsoft Agent Framework
* CrewAI
* Semantic Kernel
* OpenHands
* SWE-agent
* smolagents
* PydanticAI
* Mastra
* 기타 주요 프로젝트

연구 기반 에이전트

* ReAct
* Reflexion
* Tree of Thoughts
* Graph of Thoughts
* Voyager
* AutoGPT
* BabyAGI
* MetaGPT
* ChatDev
* 기타 주요 에이전트 연구

프로젝트별로 다음 내용을 조사한다.

1. 아키텍처
2. 실행 루프
3. 컨텍스트 관리 방식
4. 메모리 구조
5. 도구 호출 방식
6. 작업 계획 및 스케줄링
7. 멀티 에이전트 지원
8. 오류 복구
9. 검증 체계
10. 성능 최적화
11. 보안 및 권한 관리
12. 실제 구현 코드
13. 알려진 한계
14. 다른 접근법과의 차이

공개되지 않은 내부 구현은 추측으로 단정하지 않는다.

⸻

5. 필수 참고 자료 및 조사 채널

공식 기술 자료

* OpenAI 공식 개발자 문서 및 엔지니어링 자료
* Anthropic 공식 엔지니어링 자료
* Google DeepMind 및 Google Developers
* Microsoft Research
* GitHub Engineering
* LangChain / LangGraph 공식 문서
* 주요 에이전트 프레임워크 공식 문서

학술 연구

* arXiv
* OpenReview
* NeurIPS
* ICML
* ICLR
* ACL
* EMNLP
* AAAI
* ACM 및 IEEE 관련 학술 자료

구현 자료

* GitHub 공식 저장소
* 프레임워크 예제
* 공개 벤치마크 구현
* 실제 에이전트 런타임 코드
* 기술 발표 및 아키텍처 문서

각 연구 결과를 가능하면 논문, 구현 코드, 실험 결과, 후속 연구와 연결한다.

발표 시점의 성능 주장과 현재의 재현 가능성을 구분한다.

⸻

6. 지식 도서관 구조

기존 따까리의 수집 플랫폼과 지식 도서관을 활용한다.

별도의 중복 시스템을 개발하지 않는다.

각 지식 항목에 다음 정보를 가능한 범위에서 저장한다.

* 기술명
* 분류
* 정의
* 해결하는 문제
* 핵심 원리
* 아키텍처 및 알고리즘
* 구현 방법
* 적용 조건
* 장단점
* 실패 사례
* 성능 영향
* 관련 기술
* 관련 논문
* 구현 코드
* 공식 문서
* 출처
* 발표일 및 확인일
* 검증 상태
* 최신성

단순한 문서 저장을 넘어 기술 간 관계를 구축한다.

예시:

Agent Reliability
→ Long-running Agent
→ Durable Execution
→ Checkpoint
→ Failure Recovery
→ Idempotent Tool Execution
→ 실제 구현 패턴

또 다른 예시:

Context Engineering
→ Context Compression
→ Summarization
→ Information Loss
→ Retrieval Augmentation
→ Context Quality Evaluation

한 기술이 여러 분야에 속할 수 있으므로 다중 분류와 관계 기반 검색을 지원한다.

원본 출처를 보존하고, 중복 자료는 개념 단위로 연결한다.

⸻

7. 실제 활용을 위한 지식 구조화

단순히 기술을 설명하는 것만으로는 부족하다.

실제 엔지니어링 문제에서 어떤 기술을 선택해야 하는지 판단할 수 있도록 정리한다.

예를 들어 다음 문제 상황에 대한 자료를 연결한다.

문제 1. 에이전트가 작업을 끝까지 수행하지 못함

관련 기술:

* Durable Execution
* Progress Tracking
* Completion Criteria
* Checkpointing
* Task State Management
* Failure Recovery

문제 2. 작업 시간이 지나치게 오래 걸림

관련 기술:

* Task Decomposition
* Parallel Execution
* Token Optimization
* Tool Call Optimization
* Adaptive Verification
* Workflow Optimization

문제 3. 동일한 실수를 반복함

관련 기술:

* Episodic Memory
* Failure Tracking
* Reflection
* Regression Testing
* State Management
* Loop Detection

문제 4. 멀티 에이전트의 효율이 낮음

관련 기술:

* Agent Coordination
* Task Allocation
* Communication Optimization
* Shared Context
* Dependency Scheduling

문제 5. 에이전트가 불필요한 검증을 반복함

관련 기술:

* Risk-based Verification
* Verification Budget
* Adaptive Testing
* Outcome-based Evaluation
* Stopping Criteria

각 문제에 대해 가능한 원인, 진단 방법, 적용 가능한 해결책, 관련 근거 자료를 연결한다.

⸻

8. 수집 및 구축 실행 절차

1단계. 기존 환경 확인

현재 수집 플랫폼과 지식 도서관의 구조를 확인한다.

기존 기능을 최대한 재사용한다.

2단계. 기술 분류 체계 구축

본 지시서의 조사 분야를 바탕으로 하네스 엔지니어링의 지식 분류 체계를 만든다.

실제 연구 자료를 조사하면서 누락된 영역을 보완한다.

3단계. 핵심 원천 자료 수집

공식 문서, 대표 논문, 공개 구현 코드, 기술 사례를 우선 확보한다.

4단계. 심층 확장

각 분야의 세부 기술과 연구 방법론을 조사한다.

중요한 기술은 구현 사례와 실패 사례까지 연결한다.

5단계. 지식 구조화 및 저장

수집한 자료를 분류하고 관련 기술 간 관계를 구축한다.

기존 지식 도서관에서 검색 가능한 상태로 저장한다.

6단계. 활용성 검증

대표적인 에이전트 엔지니어링 문제를 선정하여 검색 결과를 확인한다.

부족한 영역은 후속 수집 대상으로 등록한다.

7단계. 지속 업데이트

신규 논문, 공식 문서 변경, 주요 에이전트 프레임워크 업데이트를 지속적으로 수집할 수 있도록 한다.

기존 수집 플랫폼의 자동화 기능을 우선 활용한다.

⸻

9. 작업 효율 및 실행 지침

본 과제는 방대한 지식 수집 프로젝트다.

따라서 모든 분야를 한 번에 완벽하게 조사하는 것을 목표로 하지 않는다.

대신 분야별 핵심 자료 확보, 지식 구조화, 검색 가능성, 후속 확장 가능성을 기준으로 진행한다.

다음 원칙을 준수한다.

1. 계획 작성만 하고 작업을 종료하지 않는다.
2. 자료 목록만 만들고 실제 저장을 생략하지 않는다.
3. 모든 문서를 과도하게 검증하지 않는다.
4. 독립적인 자료 수집은 가능한 범위에서 병렬화한다.
5. 이미 수집한 자료를 불필요하게 반복 수집하지 않는다.
6. 검증과 보고보다 실제 지식 확보에 자원을 우선 배분한다.
7. 접근 제한이나 외부 제약은 명확히 기록한다.
8. 작업 중단 시 진행 상태를 저장하여 재개 가능하게 한다.
9. 완료하지 않은 작업을 완료했다고 보고하지 않는다.
10. 현재 작업과 무관한 하네스 리팩토링이나 신규 기능 개발을 진행하지 않는다.

특히 수집 시스템을 개선하는 작업이 지식 수집 자체보다 커지는 상황을 방지한다.

기존 시스템으로 수집 가능한 자료는 우선 수집한다.

필수 기능이 부족할 때만 최소한의 개선을 수행한다.

⸻

10. 최종 결과물

다음 산출물을 제공한다.

1. Harness Engineering 전체 기술 분류 체계
2. 분야별 확보 자료 목록 및 수량
3. 주요 연구 논문 및 구현체 목록
4. 핵심 기술 간 관계 구조
5. 주요 에이전트 프레임워크 비교 자료
6. 하네스 설계 패턴 및 안티패턴
7. 실제 실패 사례와 해결 방법
8. 성능 최적화 방법론
9. 평가 및 벤치마크 방법론
10. 지식 도서관 저장·검색 결과
11. 미수집 및 부족 분야
12. 후속 수집 계획

수집 성공, 부분 성공, 미수행을 구분한다.

실제로 저장되지 않은 자료를 저장했다고 보고하지 않는다.

최종 평가는 문서 수량뿐 아니라 기술 범위, 자료의 신뢰성, 지식의 깊이, 실제 활용 가능성을 중심으로 수행한다.

⸻

11. 최종 지시

본 프로젝트의 목적은 특정 AI 에이전트의 즉각적인 성능 개선이 아니다.

AI 에이전트가 실제 작업을 수행하는 데 필요한 하네스 엔지니어링 지식을 종합적으로 확보하는 것이다.

따라서 프롬프트 엔지니어링이나 멀티 에이전트에만 집중하지 말고, 컨텍스트·메모리·실행 환경·작업 계획·도구 사용·오류 복구·검증·관측성·보안·최적화까지 포괄한다.

특히 동일한 모델을 사용하더라도 하네스 설계에 따라 작업 성공률, 실행 시간, 비용, 신뢰성이 크게 달라질 수 있다는 관점에서 관련 연구를 수집한다.

이론적 설명과 실제 구현을 연결하고, 성공 사례뿐 아니라 실패 사례와 설계상의 트레이드오프까지 축적한다.

최종적으로 향후 AI 에이전트 시스템의 설계·개발·운영·개선 과제가 주어졌을 때, 지식 도서관을 검색하여 적절한 아키텍처와 엔지니어링 방법론을 선택할 수 있는 수준을 목표로 한다.
