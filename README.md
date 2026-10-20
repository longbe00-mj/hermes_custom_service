# Hermes Custom Service

로컬 Docker Compose로 실행하는 **자체 계정·관리자 승인 기반 Hermes 중앙 서비스**입니다. upstream Hermes 코어를 수정하지 않고 사용자별 전체 런타임을 분리합니다.

## 제공 기능

- ID/PW 가입, 개발 SMTP 이메일 확인, 관리자 승인·거절·정지·재활성화.
- Argon2id 비밀번호, 중앙 세션 폐기·회전, 관리자 TOTP, 감사 기록.
- 웹 포털과 실제 Hermes Dashboard 연결, 기존 Desktop용 원격 인증 계약·PKCE·WS ticket.
- 동적 개인 컨테이너·영속 볼륨·격리 네트워크·자원 한도.
- 원본 모델 키를 사용자 런타임에 주지 않는 중앙 모델 gateway.
- 사용량 예약·정산·동시 요청 제한, 정상 종료·오프라인 백업·빈 환경 복원.

**Team 공유, SSO·소셜 공급자, AWS Terraform은 설계 범위이며 현재 활성 기능은 아닙니다.** [상세 아키텍처](docs/architecture.md), [Terraform 확장 설계](deploy/terraform/README.md)를 참고하세요.

## 빠른 시작

Windows는 Docker Desktop의 Linux/WSL2 engine을 사용합니다. Docker Compose v2가 필요합니다. Python·Node·PostgreSQL을 호스트에 설치할 필요는 없습니다. 최초 Hermes 이미지 다운로드에 수 GB의 저장 공간이 필요합니다.

```powershell
git clone https://github.com/longbe00-mj/hermes_custom_service.git
cd hermes_custom_service
./scripts/setup.ps1
docker compose up -d --build
docker compose exec platform python -m service.cli bootstrap-admin --username admin --email admin@example.com
```

Linux/macOS의 secret 준비는 `sh scripts/setup.sh`를 사용하고 이후 명령은 같습니다. 현재 runtime image의 실제 검증 host는 Linux amd64 컨테이너입니다.

Linux setup은 host `.secrets` directory를 0700으로 보호하고 파일은 bind mount로 부여한 container UID가 읽을 수 있게 설정합니다. 이 directory의 권한을 임의로 공개하지 마세요.

bootstrap 명령은 관리자 비밀번호를 대화형으로 받고 authenticator 등록 URI를 한 번 출력합니다. URI를 본인 OTP 앱에 등록하세요. **URI는 비밀정보이며 채팅·로그·Git에 올리지 마세요.** 고정 기본 비밀번호는 없습니다.

- 사용자·관리자 포털: **http://localhost:8088**
- 개발용 메일함: **http://localhost:8025**
- 일반 사용자는 가입 → Mailpit의 확인 링크 → 관리자 승인 → 개인 환경 시작 → Hermes GUI 순서로 사용합니다.
- 관리자는 현재 로컬 설정에서 ID/PW로 로그인해 사용자 승인 화면을 이용합니다.
- 관리자 복구: `docker compose exec platform python -m service.cli recover-admin --username admin`.

로컬 메일은 실제 수신자에게 전송하지 않고 Mailpit에 보관합니다. 파일·명령은 로컬 PC 전체가 아니라 사용자 컨테이너의 작업 공간에서 처리됩니다.

## 모델 설정

`.secrets/provider_api_key`에 사용할 공급자의 API 키를 넣고 `config.yaml`의 `provider_base_url`, `allowed_models`, `default_model`을 설정하세요. 기본 예시는 OpenAI 호환 Chat Completions입니다. 실제 key는 Git에 포함하지 않습니다.

```powershell
docker compose up -d --force-recreate platform
```

키가 없어도 계정·승인·GUI·런타임을 검증할 수 있습니다. 실제 모델 호출은 명시적으로 503을 반환합니다. 가짜 모델 응답을 제공하지 않습니다.

최초 model gateway는 `/v1/chat/completions`와 `/v1/models`를 지원합니다. native Responses, 개인 구독 OAuth, 공급자별 별도 API는 지원 범위가 아닙니다. upstream runtime 기본 모델 경로는 중앙 Chat Completions에 연결됩니다.

한도는 token 예약·provider usage 정산이며 정확한 청구액 보증이 아닙니다. 응답 usage가 없거나 중단되면 예약량을 유지합니다. crash 후에는 먼저 drain하고 `python -m service.cli reconcile-calls`를 운영자가 실행해 남은 동시 호출 claim을 정리할 수 있습니다.

## Desktop

기존 Hermes Desktop의 원격 gateway 연결에 `http://localhost:8088`을 입력하세요. password-only provider에서는 Desktop 버전에 따라 embedded login 창을 사용할 수 있습니다. 서버는 native PKCE endpoint도 지원합니다.

초기 계정 승인이 필요하며 개인 환경은 웹 포털에서 먼저 시작하세요. Desktop의 모든 OS·버전별 native 기능은 별도 QA 대상입니다. 서버 인증/RPC 계약 검증과 실제 Desktop 앱 전체 QA는 구분합니다.

## 설정과 보존

일반 설정은 `config.yaml`, 비밀정보는 `.secrets/`에 있습니다. setup은 기존 secret을 덮어쓰지 않습니다. HTTPS 배포 시 public_url과 secure_cookies를 함께 바꾸고 SMTP·trusted proxy·운영 보안을 검증해야 합니다. 현재 기본 포트는 loopback으로만 publish합니다.

`postgres` Compose volume은 중앙 정보를, `hc-user-<uuid>-data` volume은 개인 데이터를 보존합니다. 동적 사용자 런타임은 Manager가 생성하므로 **Compose down만으로 중지되지 않습니다.**

정상 종료:

```powershell
./scripts/stop.ps1
# Linux/macOS: sh scripts/stop.sh
```

stop script는 개인 runtime drain 후 Compose를 내리며 데이터를 삭제하지 않습니다. 재시작은 `docker compose up -d`입니다. Manager는 실제 컨테이너 상태를 조회하고 재생성된 control 서비스의 network 연결을 복구합니다.

`docker compose down -v`, Docker volume prune은 일반 종료 명령으로 사용하지 마세요. 사용자 볼륨과 중앙 DB의 수명주기는 다릅니다.

## 백업·복원

백업은 짧은 서비스 중단을 포함합니다. 개인 작업을 정지하고 중앙 API·Manager·진입 Proxy를 잠시 내린 뒤 DB dump와 사용자 volume을 저장합니다. 작업 후 중앙 서비스를 다시 시작하며 개인 환경은 명시적으로 시작할 때까지 중지 상태입니다.

```powershell
docker compose --profile tools run --rm ops backup local-20261020
docker compose --profile tools run --rm ops verify local-20261020
# 같은 secrets를 준비한, 사용자가 없고 tenant volume도 없는 초기 target에서만:
docker compose --profile tools run --rm ops restore local-20261020
```

산출물은 gitignored `artifacts/backups/`입니다. `.secrets/`는 백업에 포함하지 않습니다. **원본 secrets는 별도 안전한 경로에 보존해야 합니다.** 백업은 사용자 파일·대화·token·비밀번호 hash를 포함할 수 있으므로 민감정보로 다루세요. 현재 archive는 자동 암호화·오프호스트 보관을 하지 않습니다.

restore는 기존 계정·tenant volume 또는 동일 이름의 foreign volume이 있으면 거부합니다. archive 외부 path/link와 checksum 불일치도 거부합니다. 운영 RPO/RTO는 schedule·오프호스트 보관·정기복원 훈련을 추가해야 달성할 수 있습니다.

## 검증

```powershell
docker compose config --quiet
docker compose exec -T platform sh scripts/run_tests.sh
docker compose --profile tools run --rm --entrypoint python ops -m scripts.smoke
# 계정이 없는 일회성 개발 환경에서만, 실제 복원 계약 테스트:
docker compose --profile tools run --rm --entrypoint python ops -m scripts.storage_smoke
```

- contract suite는 별도 PostgreSQL DB를 만들고 종료 후 삭제합니다.
- smoke는 임의 prefix의 테스트 계정만 만들고 실제 메일·승인·Hermes GUI·RPC·격리를 검증한 뒤 해당 fixture만 제거합니다.
- storage smoke는 비어 있지 않은 계정 DB에서 실행을 거부합니다. 파일과 SQLite를 백업·복원하고 검사한 후 fixture만 삭제합니다.
- 실제 유료 모델 API는 자동 테스트에서 호출하지 않습니다.

검증 상세는 [검증 기록](docs/verification.md)에 기록합니다.

## 확장 설계

계정은 개인별로 관리합니다. 향후 Team은 중앙 관리자 생성·리더 구성원 관리, 프로젝트 작업 공간과 선택 세션 공유, viewer/contributor 권한을 지원하도록 설계합니다. 개인 자원의 공유 방식은 원본 Team 소유권 이전이며 개인 credential·전체 memory는 이전하지 않습니다.

SSO·소셜은 provider identity를 기존 내부 사용자에 연결하며 email 자동 병합을 금지합니다. 서비스 승인·권한·세션은 인증 방식에 관계없이 중앙에서 관리합니다.

AWS는 Terraform으로 VPC·ALB/TLS·EC2·EBS·IAM·ECR·secret 참조·로그·backup을 구축하고 동일 이미지를 배포하는 구조입니다. 동적 runtime은 Terraform resource로 관리하지 않습니다. 현재 AWS resource를 생성하지 않습니다.

## 초기 운영 한계

이 버전은 사내 소규모용 로컬 파일럿입니다. 공유 Docker host와 중앙 Manager는 신뢰 영역이며 외부 불특정 고객의 강한 격리·HA를 보장하지 않습니다. browser/도구의 직접 인터넷 접근은 제한되며 HTTP proxy를 사용하는 backend 설정이 필요합니다. 인터넷 서비스로 공개하기 전에 TLS·실제 SMTP·자원 부하·egress·dependency 보안 검토가 필요합니다.

관리자 OTP 정책: 로컬 초기 설정은 `admin_otp_required: false`로 ID/PW만 사용합니다. `config.yaml`에서 true로 변경하고 platform을 재시작하면 OTP가 필수가 됩니다. 설정 누락 시 기본은 필수입니다. 가입 신청·계정 복구 신청은 로그인 화면의 버튼으로 각각 이동합니다.
