로보틱스·피지컬 AI 종합 기술 백과사전 구축 연구 지시서

1. 프로젝트 목적

로보틱스(Robotics), 피지컬 AI(Physical AI), Embodied AI, 로봇 학습(Robot Learning) 및 관련 기술 분야의 지식을 최대한 폭넓고 깊이 있게 수집하여, 실제 연구·설계·개발·실험에 활용할 수 있는 종합 기술 백과사전을 구축한다.

본 과제는 특정 로봇을 개발하거나 단일 AI 모델을 학습시키는 작업이 아니다.

향후 다음과 같은 활동을 수행할 때 필요한 지식과 기술적 판단 근거를 즉시 검색하고 활용할 수 있는 기반을 만드는 작업이다.

* 로봇 시스템 및 아키텍처 설계
* 피지컬 AI 모델 선정과 비교
* 로봇용 딥러닝 모델 학습 및 파인튜닝
* 로봇 행동 계획 및 제어
* 강화학습과 모방학습 적용
* 로봇 시뮬레이션 및 Sim-to-Real
* 카메라·센서 기반 환경 인식
* 로봇 데이터셋 구축 및 활용
* 로봇용 온디바이스 AI 최적화
* 실제 로봇의 개발·검증·배포
* 최신 로보틱스 연구 동향 파악

특정 기술이나 기업에 편향되지 않고, 로보틱스 전반의 학문적 기초부터 최신 연구 성과까지 포괄한다.

최종 목표는 AI가 로보틱스 관련 연구개발을 수행할 때, 필요한 지식이 부족하여 잘못된 설계나 비효율적인 실험을 진행하는 상황을 최소화하는 것이다.

⸻

2. 조사 기본 원칙

2.1 범위 제한 금지

아래에 제시된 조사 분야는 최소한의 출발점이다.

조사 과정에서 중요한 새로운 기술, 학문, 프레임워크, 연구 방법론이 발견되면 스스로 조사 범위를 확장한다.

단, 관련성이 낮은 분야까지 무분별하게 확장하지 않는다.

2.2 원천 자료 우선

자료 신뢰도는 다음 순서로 판단한다.

1. 학술 논문 및 공식 연구 결과
2. 대학·연구기관의 공개 강의와 교재
3. 공식 기술 문서 및 구현 코드
4. 기업·연구소의 공식 기술 발표
5. 재현 가능한 오픈소스 프로젝트
6. 전문 엔지니어의 기술 분석
7. 커뮤니티 및 비공식 자료

출처의 권위뿐 아니라 실제 실험 근거, 재현 가능성, 최신성도 함께 평가한다.

2.3 이론과 구현 연결

수집한 지식은 단순 개념 설명으로 끝내지 않는다.

가능하면 다음 항목을 연결한다.

* 기술이 해결하려는 문제
* 핵심 이론과 수학적 원리
* 대표 알고리즘과 모델 구조
* 실제 구현 방식
* 사용 가능한 라이브러리
* 학습·추론에 필요한 컴퓨팅 자원
* 장점과 한계
* 적용하기 적합한 상황
* 다른 기술과의 비교
* 실험 및 성능 평가 방법

2.4 과도한 검증 방지

수집한 모든 문서를 일일이 심층 검증하느라 전체 작업이 지연되는 상황을 방지한다.

신뢰할 수 있는 공식 출처는 우선 수집하고, 중요도가 높거나 근거가 충돌하는 내용에 검증 자원을 집중한다.

검증 자체가 목적이 아니라 활용 가능한 지식 기반의 구축이 목적이다.

⸻

3. 핵심 조사 분야

A. 로보틱스 기초 이론

로봇을 이해하기 위한 수학·물리·공학적 기반을 수집한다.

조사 대상

* 선형대수, 미적분, 확률통계
* 최적화 이론
* 강체 운동학 및 동역학
* 좌표계 및 좌표 변환
* 회전행렬, Quaternion, SE(3), SO(3)
* Forward / Inverse Kinematics
* Jacobian
* 로봇 동역학
* Lagrangian / Newton-Euler
* 궤적 생성과 최적화
* 경로 계획
* PID, MPC, LQR
* 상태 추정과 Kalman Filter
* 접촉 역학, 마찰, 충돌
* 로봇 안정성과 안전성

MIT, Stanford, ETH Zurich, CMU 등 주요 대학의 공개 강의·교재를 우선적으로 조사한다.

각 개념의 수학적 정의뿐 아니라 실제 로봇 시스템에서 사용되는 이유를 연결한다.

B. 피지컬 AI 및 Embodied AI

현실 세계를 인식하고 행동하는 AI 시스템의 핵심 기술을 조사한다.

조사 대상

* Physical AI와 Embodied AI의 개념 및 차이
* Embodied Intelligence
* Vision-Language-Action(VLA)
* Vision-Language Models(VLM)
* World Models
* Action-conditioned Models
* Multimodal Foundation Models
* Diffusion Policy
* Transformer 기반 로봇 정책
* Action Tokenization
* Continuous Action Prediction
* Flow Matching 기반 정책
* Hierarchical Policy
* Long-horizon Planning
* Task and Motion Planning
* Affordance Learning
* Spatial Intelligence
* 3D Scene Understanding
* Object-centric Representation
* Memory-augmented Robotics
* 언어 명령 기반 로봇 행동 생성

특히 LLM/VLM이 실제 로봇의 행동을 어떻게 결정하고, 그 행동이 제어 신호로 변환되는지를 상세하게 조사한다.

고수준 작업 계획과 저수준 모터 제어를 구분하고 두 계층을 연결하는 방법을 조사한다.

C. 로봇 딥러닝 및 모델 학습

본 백과사전에서 가장 중요하게 다룰 분야 중 하나다.

학습 방법론

* Supervised / Self-supervised Learning
* Imitation Learning
* Behavior Cloning
* Inverse Reinforcement Learning
* Offline / Online Reinforcement Learning
* Model-based / Model-free RL
* Multi-task Learning
* Transfer Learning
* Curriculum Learning
* Domain Adaptation
* Few-shot Learning
* Meta Learning
* Continual Learning
* Human-in-the-loop Learning
* Preference-based Learning
* Learning from Demonstrations
* Teleoperation 기반 데이터 수집
* Synthetic Data Training

주요 알고리즘

* PPO, SAC, TD3, DQN
* Behavior Cloning
* DAgger
* Decision Transformer
* Diffusion Policy
* ACT(Action Chunking Transformer)
* Offline RL 계열 알고리즘
* 정책 증류 및 모델 압축

위 목록 외에도 현재 로봇 학습에 실질적으로 사용되는 주요 알고리즘을 발굴한다.

학습 파이프라인

* 데이터 수집
* 데이터 정제
* 행동·관측 데이터 표현
* 데이터셋 구조와 포맷
* 학습 환경 구축
* 사전학습 및 파인튜닝
* Loss Function 설계
* Reward Function 설계
* Hyperparameter Optimization
* Evaluation
* Checkpoint 관리
* 재현성 확보
* 실패 사례 분석

모델 학습에 필요한 GPU·메모리·데이터 규모·학습 시간 등을 실제 공개 실험 근거에 기반하여 정리한다.

추정치와 실측치는 반드시 구분한다.

D. 주요 로봇 파운데이션 모델

대표적인 로봇 AI 모델과 연구 계열을 폭넓게 조사한다.

우선 조사 대상

* Google DeepMind RT-1 / RT-2
* Open X-Embodiment
* Octo
* OpenVLA
* OpenVLA-OFT
* Physical Intelligence π0 계열
* NVIDIA GR00T 계열
* Hugging Face LeRobot 관련 모델
* Diffusion Policy
* ACT
* RoboCat
* 기타 최신 공개 VLA 및 로봇 파운데이션 모델

각 모델에 대해 다음을 조사한다.

1. 모델 구조와 핵심 아이디어
2. 입력·출력 형식
3. 지원 로봇 및 작업
4. 학습 데이터
5. 학습 방법론
6. 추론 방식과 지연시간
7. 컴퓨팅 자원 요구사항
8. 오픈소스 및 가중치 공개 여부
9. 파인튜닝 가능 여부
10. 실제 성능과 실패 사례
11. 기존 모델 대비 개선점
12. 재현 가능한 코드 및 실험

논문 발표 당시의 주장과 이후 독립적인 검증 결과를 구분한다.

E. 로봇 하드웨어와 센서

AI 모델이 실제 물리 환경과 상호작용하기 위한 하드웨어 지식을 수집한다.

로봇 형태

* Humanoid
* Quadruped
* Robotic Arm
* Mobile Manipulator
* AMR / AGV
* Autonomous Vehicle
* Drone
* Soft Robotics
* Dexterous Hand
* Collaborative Robot

센서 및 장치

* RGB / Depth / Stereo Camera
* LiDAR
* IMU
* Force / Torque Sensor
* Tactile Sensor
* Encoder
* GPS / GNSS
* Motor / Servo / Actuator
* End Effector
* Embedded Controller

센서별 원리, 정확도, 노이즈, 지연시간, 캘리브레이션, 데이터 융합 방법을 조사한다.

F. 컴퓨터 비전과 환경 인식

* Object Detection / Tracking
* Semantic / Instance Segmentation
* Depth Estimation
* Pose Estimation
* 3D Reconstruction
* SLAM / Visual SLAM
* Visual Odometry
* Sensor Fusion
* Point Cloud Processing
* 3D Gaussian Splatting
* NeRF
* Scene Graph
* Grasp Detection
* Visual Servoing
* Active Perception

단순 영상 인식 기술과 로봇 행동에 직접 활용되는 공간 인식 기술의 차이를 설명한다.

G. 로봇 제어와 행동 계획

* Motion Planning
* Trajectory Optimization
* Sampling-based Planning
* RRT / RRT*
* A*
* MPC
* Whole-body Control
* Impedance / Admittance Control
* Force Control
* Grasp Planning
* Manipulation Planning
* Locomotion Control
* Balance Control
* Collision Avoidance
* Real-time Control
* Feedback Control
* Safety-constrained Control

학습 기반 정책과 전통적인 제어 알고리즘을 어떻게 결합하는지 중점적으로 조사한다.

H. 시뮬레이션과 Sim-to-Real

주요 도구

* NVIDIA Isaac Sim / Isaac Lab
* MuJoCo
* Gazebo
* PyBullet
* Webots
* Genesis
* ManiSkill
* Habitat
* robosuite
* 기타 주요 물리 시뮬레이터

주요 연구 주제

* Physics Simulation
* Digital Twin
* Domain Randomization
* System Identification
* Sim-to-Real Transfer
* Real-to-Sim
* Synthetic Data Generation
* Parallel Simulation
* GPU-accelerated Simulation
* Contact Simulation
* Reality Gap

시뮬레이터별 장단점, 물리 정확도, 속도, GPU 요구사항, 로봇 학습 적합성을 비교한다.

I. 로보틱스 소프트웨어 생태계

* ROS 2
* DDS
* MoveIt 2
* Nav2
* ros2_control
* TF2
* URDF / Xacro / SDF
* LeRobot
* OpenCV
* Open3D
* Pinocchio
* Drake
* PyTorch / JAX
* ONNX Runtime / TensorRT
* 로봇 미들웨어
* 실시간 운영체제
* Edge AI Runtime
* 통신 프로토콜 및 하드웨어 인터페이스

각 도구의 실제 역할과 기술적 연결 관계를 정리한다.

J. 데이터셋 및 벤치마크

데이터셋

* Open X-Embodiment
* DROID
* BridgeData
* RoboNet
* CALVIN
* LIBERO 관련 데이터
* LeRobot 데이터셋
* 기타 공개 로봇 학습 데이터

평가 체계

* Task Success Rate
* Generalization
* Robustness
* Sample Efficiency
* Data Efficiency
* Inference Latency
* Control Frequency
* Sim-to-Real Performance
* Safety
* Long-horizon Task Completion

평가 환경과 성공 기준이 다른 벤치마크 점수를 단순 비교하지 않는다.

데이터 누수, 평가 편향, 재현성 문제도 조사한다.

K. 로봇 AI 추론 최적화

* On-device Inference
* Edge AI
* Quantization
* Pruning
* Distillation
* KV Cache Optimization
* Efficient Attention
* Kernel Optimization
* GPU / NPU 가속
* Real-time Inference
* Latency / Throughput Optimization
* Model Compression
* Heterogeneous Computing
* Asynchronous Inference
* 고수준 정책과 실시간 제어의 실행 주기 분리

특히 제한된 컴퓨팅 자원에서 AI 모델을 실제 로봇에 배포하는 방법을 심층 조사한다.

L. 로봇 안전성 및 신뢰성

* Functional Safety
* Collision Detection
* Emergency Stop
* Human-Robot Interaction
* Safe Reinforcement Learning
* Runtime Monitoring
* Fail-safe Design
* Out-of-distribution Detection
* Uncertainty Estimation
* Sim-to-Real Safety Validation
* AI 행동 검증
* 로봇 보안
* 관련 국제 표준 및 규제

실험실에서의 성공과 실제 환경에서의 안전한 동작을 구분한다.

M. 최신 연구 및 산업 생태계

주요 연구기관, 기업, 학회, 오픈소스 커뮤니티를 조사한다.

예시:

* NVIDIA Robotics
* Google DeepMind Robotics
* Physical Intelligence
* Toyota Research Institute
* Boston Dynamics
* Figure AI
* Agility Robotics
* Tesla Optimus
* Stanford Robotics
* MIT CSAIL
* CMU Robotics Institute
* ETH Zurich Robotics
* Berkeley Robotics

기업의 홍보 자료와 독립적으로 검증된 기술 성과를 구분한다.

연구기관·기업의 공개 모델, 논문, 데이터셋, 기술 문서 및 구현 사례를 연결한다.

⸻

4. 필수 참고 자료 및 수집 채널

다음 채널을 우선적으로 탐색한다.

학술 자료

* arXiv
* IEEE Xplore
* ACM Digital Library
* Google Scholar
* OpenReview
* Robotics: Science and Systems(RSS)
* ICRA
* IROS
* CoRL
* NeurIPS
* ICML
* ICLR
* CVPR
* ECCV / ICCV

교육 및 기초 이론

* Modern Robotics
* Underactuated Robotics
* MIT OpenCourseWare
* Stanford Robotics 관련 공개 강의
* Berkeley Robot Learning 관련 공개 강의
* CMU Robotics Institute 공개 자료
* ETH Zurich Robotics 강의 및 연구 자료

구현 및 실습

* ROS 공식 문서
* NVIDIA Isaac 공식 문서
* Hugging Face LeRobot
* MuJoCo 공식 문서
* Google DeepMind 공개 연구 코드
* PyTorch 공식 문서
* 주요 로봇 학습 프로젝트 GitHub

여기에 없는 공신력 높은 출처도 적극적으로 발굴한다.

자료를 수집할 때 논문, 공식 구현, 프로젝트 페이지, 학습 데이터, 벤치마크를 서로 연결한다.

접근 제한 또는 라이선스 문제가 있는 자료는 무단 복제하지 않고 허용된 범위에서 메타데이터, 출처, 요약 및 참고 링크를 보관한다.

⸻

5. 지식 도서관 저장 구조

수집 결과는 기존 지식 도서관의 저장·검색 구조를 우선 활용한다.

단순 문서 목록이 아니라 기술 개념 간 관계를 표현할 수 있어야 한다.

예시:

Physical AI
→ Robot Learning
→ Imitation Learning
→ Behavior Cloning
→ ACT
→ LeRobot
→ 학습 데이터셋
→ 실제 구현 코드
→ 평가 방법

각 지식 항목에는 가능한 범위에서 다음 정보를 저장한다.

* 고유 식별자
* 기술명 및 동의어
* 분야 및 하위 분류
* 개념 설명
* 핵심 원리
* 선행 지식
* 관련 기술
* 대표 구현
* 활용 사례
* 기술적 한계
* 비교 대상
* 원본 출처
* 발표일 및 최종 확인일
* 라이선스
* 신뢰도 및 검증 상태
* 최신성
* 관련 논문·코드·데이터셋

기존 RAG 검색, 자동 분류, 온톨로지 구조가 활용 가능하다면 재사용한다.

별도의 중복 플랫폼을 만들지 않는다.

동일 개념을 설명하는 자료는 무조건 하나로 합치지 않고, 원본 출처를 보존하면서 개념 단위로 연결한다.

검색 시 개념 설명뿐 아니라 관련 구현·논문·실험 사례까지 함께 탐색할 수 있도록 한다.

⸻

6. 수집 결과의 활용성 강화

지식 도서관에 축적된 자료는 실제 연구개발 과정에서 활용 가능해야 한다.

예를 들어 다음 질문에 근거를 갖고 답할 수 있어야 한다.

* 로봇팔의 물체 집기 작업에는 어떤 학습 방식이 적합한가?
* ACT와 Diffusion Policy의 구조적 차이는 무엇인가?
* 소량의 시연 데이터로 로봇 정책을 학습하려면 어떻게 해야 하는가?
* 로봇용 VLA를 파인튜닝하려면 어떤 데이터 형식이 필요한가?
* Isaac Lab과 MuJoCo 중 어떤 환경이 특정 실험에 적합한가?
* 로봇 모델의 추론 지연시간을 줄이려면 무엇을 최적화해야 하는가?
* 강화학습과 모방학습 중 어떤 접근이 더 적합한가?
* Sim-to-Real 성능이 떨어지는 원인은 무엇인가?
* 공개된 모델을 실제 로봇에 적용하려면 어떤 수정이 필요한가?

답변에 필요한 정보가 부족하다면 누락된 지식을 식별하고 후속 수집 대상으로 등록할 수 있어야 한다.

⸻

7. 연구 및 수집 실행 절차

1단계: 기존 환경 파악

현재 따까리의 수집 플랫폼과 지식 도서관 구조를 확인한다.

* 수집 가능한 자료 유형
* 기존 자동 분류 방식
* RAG 저장 방식
* 원본 문서 관리
* 중복 제거
* 수집 작업의 지속 실행
* 실패 복구 기능

기존 기능을 재사용할 수 있다면 새로운 기능을 불필요하게 개발하지 않는다.

2단계: 지식 분류 체계 수립

로보틱스 전반의 기술 분류 체계를 구축한다.

본 지시서의 분야 목록을 출발점으로 사용하되, 실제 학술 자료와 오픈소스 생태계를 조사하여 보완한다.

특정 기술이 여러 분야에 속할 수 있으므로 단일 계층 구조에 억지로 제한하지 않는다.

3단계: 핵심 원천 자료 확보

주요 대학 강의, 교재, 공식 문서, 대표 논문, 공개 구현을 우선 수집한다.

기초 이론과 핵심 기술을 먼저 확보하되, 이후 최신 연구를 수집할 수 있도록 구조를 설계한다.

4단계: 분야별 심층 확장

각 분야에서 발견된 하위 기술, 주요 모델, 학습 알고리즘, 구현체, 데이터셋을 확장 조사한다.

특히 피지컬 AI와 로봇 딥러닝 학습 분야는 기술적 깊이를 충분히 확보한다.

5단계: 지식 연결 및 검색 가능성 확보

수집 자료를 분류하고 개념 간 관계를 구축한다.

원본 출처를 유지하고, 실제 개발 질문에서 검색 가능한 형태로 저장한다.

6단계: 활용성 점검

대표적인 로봇 개발·연구 질문을 선정하여 검색 결과를 확인한다.

검색에 실패하거나 지식이 부족한 영역을 찾아 보완한다.

단, 모든 문서를 개별 검증하는 방식으로 시간을 낭비하지 않는다.

7단계: 지속 확장 체계 구축

신규 논문, 모델, 공식 문서 변경, 주요 오픈소스 업데이트를 후속 수집할 수 있도록 한다.

가능하면 기존 수집 플랫폼의 주기적 수집 기능을 활용한다.

⸻

8. 작업 효율 및 완료 기준

이 프로젝트는 범위가 방대하므로 모든 지식을 한 번에 수집하는 것을 완료 조건으로 삼지 않는다.

대신 다음을 기준으로 진행한다.

1. 핵심 분야의 분류 체계가 구축되었는가?
2. 주요 분야마다 신뢰할 수 있는 원천 자료가 확보되었는가?
3. 개념·논문·구현·데이터셋이 연결되었는가?
4. 수집한 자료가 실제 지식 도서관에서 검색 가능한가?
5. 부족한 분야를 식별할 수 있는가?
6. 후속 수집을 이어갈 수 있는가?

각 단계는 실제 산출물을 남겨야 한다.

조사 계획만 작성하고 종료하거나, 자료 목록만 만든 뒤 실제 수집·저장을 수행하지 않는 것을 금지한다.

다만 접근 불가능한 자료, 라이선스 제한, 시스템 장애 등은 실제 제약으로 인정하고 명확히 기록한다.

모든 분야를 무조건 동일한 깊이로 조사하기보다는 핵심도, 활용도, 기술적 중요성에 따라 조사 깊이를 조절한다.

반복적이고 독립적인 수집 작업은 가능한 범위에서 병렬화한다.

과도한 검증·보고서 작성·계획 수정 때문에 실제 수집량이 부족해지지 않도록 한다.

⸻

9. 최종 보고 요구사항

작업 결과는 다음 내용을 포함하여 보고한다.

1. 구축된 로보틱스 지식 분류 체계
2. 분야별 수집 문서 및 출처 수
3. 주요 논문·모델·프레임워크 확보 현황
4. 지식 도서관 저장 및 검색 적용 결과
5. 실제 검색 사례
6. 자료가 부족한 분야
7. 수집 실패 및 접근 제한 자료
8. 후속 수집 계획
9. 자동 업데이트 가능 여부
10. 실제 수행하지 못한 작업

수집 성공, 부분 성공, 미수행을 구분한다.

확보하지 않은 자료를 확보했다고 보고하지 않는다.

문서 수량만으로 성공을 평가하지 말고, 기술 범위의 충실도·지식의 깊이·원천 자료의 신뢰성·실제 연구개발 활용성을 중심으로 평가한다.

⸻

10. 최종 지시

본 과제의 핵심은 로봇 개발이 아니라 로보틱스와 피지컬 AI의 지식을 체계적으로 축적하는 것이다.

따라서 불필요한 로봇 구현, 모델 학습, 시뮬레이터 설치, 대규모 벤치마크 실행에 자원을 사용하지 않는다.

대신 실제 개발에 필요한 이론, 방법론, 구현 지식, 연구 성과와 실패 사례를 폭넓게 수집한다.

특히 최신 피지컬 AI 기술에만 집중하여 전통적인 로봇 공학 지식을 누락하지 않도록 한다.

자료 수집 → 구조화 → 저장 → 검색 검증 → 부족한 지식 보완의 흐름을 완성한다.

최종적으로 향후 새로운 로봇 연구개발 과제가 주어졌을 때, AI가 지식 도서관을 검색하여 적절한 기술과 연구 방법론을 선택할 수 있는 수준을 목표로 한다.
