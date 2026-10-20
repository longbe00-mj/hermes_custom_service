# AWS Terraform 설계 — 후속 구현

현재는 설계 명세다. 실행 가능한 `.tf` 또는 AWS 배포 완료를 의미하지 않는다. 최초 제품은 로컬 Compose에서 실행하며, 이후 같은 image/config/API를 AWS 통합 EC2에 배포한다. 처음부터 ECS/EKS로 재설계하지 않는다.

## 책임 분리

Terraform은 VPC·routing·보안 그룹·ALB·EC2·EBS·IAM·ECR·secret 자원·로그·백업 저장소를 만든다. 배포 파이프라인은 image digest 선택·DB migration·service drain·Compose 업데이트·health 확인·rollback을 담당한다. 사용자/Team runtime은 Manager가 생성하며 Terraform resource가 아니다.

## 모듈 계약

| 모듈 | 주요 입력 | 주요 출력 |
|---|---|---|
| state-bootstrap | region, naming, 운영 IAM | versioning·암호화된 S3 backend와 lockfile 권한 |
| network | CIDR, AZ, outbound 정책 | private subnet, ALB subnet, security groups, NAT/endpoint |
| ingress | domain, certificate ARN, target | HTTPS URL, target group |
| compute | AMI, instance type, release digest, volume | EC2 ID, SSM 접속, private address |
| storage | 용량, KMS, snapshot retention | 암호화 EBS, backup bucket |
| identity | 필요한 secret ARN·ECR repository | 최소 권한 instance profile |
| observability | 로그 보존, 경보 대상 | log group, metrics, alarms |

초기 region 기본은 서울(ap-northeast-2), 통합 서버 한 대다. EC2 크기는 로컬에서 동시 활성 사용자 5명 및 browser 부하 측정 후 결정한다. public ingress는 ALB의 HTTPS만 허용하고 EC2·PostgreSQL·Manager·사용자 runtime의 포트를 직접 공개하지 않는다. 관리 접속은 SSM으로 제한한다.

## State·secret

Terraform state는 별도 S3 bucket, versioning, KMS, `use_lockfile=true`로 관리한다. 신규 구성에서 deprecated DynamoDB locking을 도입하지 않는다. provider 버전 상한·lockfile, 외부 module의 immutable version/SHA를 고정한다.

Terraform에는 secret metadata·권한·참조만 관리하고 모델 key·DB password·signing key·관리자 password 원문을 tfvars/output/user-data로 전달하지 않는다. secret 실제 값은 별도 승인된 운영 경로로 등록한다. `sensitive=true`가 state에서 값을 제거한다고 가정하지 않는다.

## Compose 이식 계약

공통 service names, runtime labels, 고정 image digest, mounted config, secret file 경로, health endpoints를 유지한다. 로컬의 Mailpit은 운영 SMTP adapter로 교체한다. public_url을 HTTPS domain으로, secure_cookies를 true로 바꾼다. 호스트 공개 bind는 ALB 전용 private ingress 설정으로 교체한다. local development 포트·SMTP viewer는 AWS에 노출하지 않는다.

PostgreSQL과 사용자 볼륨은 우선 EBS에 둔다. 선택적 RDS 전환은 중앙 metadata 연결만 변경하며 Hermes SQLite schema를 RDS로 이전하지 않는다. SQLite WAL 파일을 EFS로 여러 host에 공유하지 않는다.

## 배포·데이터 보존

인프라 apply와 데이터 migration은 별도 단계다. EBS는 데이터 삭제 의도 없는 instance 교체에서 보존하고 DB volume 및 사용자 volume의 단일 host 소유권을 보장한다. instance replacement는 drain→backup→분리→attach→검증→서비스 복구 순서다. 자동 이중 실행하지 않는다.

destroy가 사용자 데이터 삭제와 동의어가 되지 않게 data resource의 prevent_destroy와 별도 폐기 절차를 둔다. 로컬→AWS는 백업 검증·동일 secrets의 안전한 주입·빈 target 복원·URL 전환·로그인/WS/PTY 검증 순서다. 이전 시 개인 user/space ID를 유지한다.

## 검증·출시 gate

1. terraform fmt/validate 및 검토 가능한 plan.
2. 별도 시험 AWS 계정/환경의 apply와 이미지 배포.
3. HTTPS·origin·cookie·native redirect·WS/PTy·로그 정제 검증.
4. 사용자 간 file/network/credential 격리와 부하 검증.
5. 정지·재시작·instance 교체·backup/restore·rollback 검증.
6. 의도된 resource만 삭제되고 보존 데이터는 남는지 확인.

단일 EC2는 전체 장애 영역이다. HA·다중 실행 노드·외부 고객 서비스는 별도 아키텍처/보안 검토 후 확장한다. Terraform 구현 전에 region·domain·실제 계정·certificate·비용 예산을 운영 입력으로 확정한다.

공식 참고: [S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3), [민감정보](https://developer.hashicorp.com/terraform/language/manage-sensitive-data).
