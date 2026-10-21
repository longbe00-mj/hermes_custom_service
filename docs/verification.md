# 검증 기록

검증일: 2026-10-20. Host: Windows / Docker Desktop Linux engine 29.8.0, Linux amd64 containers. Local URL: http://localhost:8088.

## 완료한 검증

| 검사 | 결과 | 증거 범위 |
|---|---|---|
| Compose config 및 image build | 통과 | 실제 manifest·고정 digest image·locked Python dependencies |
| `docker compose up -d --build --wait` | 통과 | PostgreSQL·migration·central·manager·mail·egress·edge 시작 |
| `scripts/run_tests.sh` | 18개 통과 | disposable PostgreSQL DB, 실제 ASGI 호출 |
| local SMTP·이메일 확인 | 통과 | Mailpit에서 실제 메시지 조회·일회성 token 소비 |
| 승인 전 실행 차단·승인 idempotency | 통과 | 실제 중앙 HTTP API |
| 개인 Hermes image 시작 | 통과 | non-root·read-only runtime·upstream Dashboard HTML |
| 실제 runtime 모델 해석 | 통과 | 중앙 base URL·owner scoped credential 일치, provider 실제 호출 아님 |
| 실제 browser navigation | 통과 | runtime의 Chromium 도구가 격리 HTTP proxy로 example.com 접속 |
| GUI bootstrap metadata | 통과 | 개인 runtime profile 조회 허용·관리 변경 거부 |
| 사용자 간 runtime 접근 차단 | 통과 | 다른 사용자 session credential의 검증 거부 |
| JSON-RPC WebSocket | 통과 | 실제 runtime greeting·session.list 응답 |
| 웹 채팅 PTY | 통과 | 실제 Ink/TUI 출력 수신 |
| 로그아웃 시 열린 WS 종료 | 통과 | 기존 socket 폐쇄 확인 |
| 환경 stop/start·계정 정지 | 통과 | 실제 Docker lifecycle·폐기된 계정 access 거부 |
| 병렬 모델 한도 | 통과 | 6개 PostgreSQL transaction 경합·설정 상한만 승인 |
| 모델 stream usage 정산 | 통과 | mock provider SSE·실제 DB 정산, live provider 아님 |
| PKCE | 통과 | 잘못된 verifier·non-loopback·code 재사용 거부 |
| 파일·SQLite·metadata 백업/복원 | 통과 | 실제 dump·volume archive·빈 target 복원·내용 대조 |
| Chrome 웹 포털 DOM | 확인 | 로그인·가입·이메일 복구·관리자 OTP 항목 렌더링 |

## 실행 명령

```sh
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T platform sh scripts/run_tests.sh
docker compose --profile tools run --rm --entrypoint python ops -m scripts.smoke
docker compose --profile tools run --rm --entrypoint python ops -m scripts.storage_smoke
```

contract suite는 별도 DB를 사용하고, live smoke는 자신이 만든 임의 prefix 계정과 runtime만 정리한다. storage smoke는 계정 DB가 비어 있지 않으면 거부한다. 시험 archive는 gitignored artifacts에만 남긴다. 실제 관리자는 operator가 bootstrap해야 하며 시험용 기본 계정·고정 비밀번호는 배포하지 않는다.

## 검증하지 않은 항목

- 실제 유료 모델 provider 응답·청구·이미지/비전 토큰 정책.
- 실제 Desktop 앱의 전체 사용자 흐름·OS별 native bridge.
- 실제 브라우저 터미널의 모든 키 입력·도구 실행·브라우저 조작.
- 동시 활성 사용자 5명의 장시간 CPU·메모리·latency 부하.
- 외부 인터넷 HTTPS·운영 SMTP·AWS 자원·Terraform apply.
- Team 공유·소유권 이전·SSO·소셜 로그인: 설계만 제공.
- production 침투 테스트·container escape·공급망 취약점 감사.

최초 GitHub Linux CI에서 local secret bind mount의 host/container UID 차이로 migration이 실패했다. setup.sh를 host secret directory 0700과 명시적으로 부여된 read-only secret file 0644로 수정했다. Windows 로컬 결과를 Linux 최초 실행 증거로 취급하지 않으며 원격 CI 결과를 별도 확인한다.

Linux의 빠른 연속 backup/restore에서 container start와 HTTP readiness 사이의 race도 발견해 bounded health polling을 추가했다. 복원 전에 Manager readiness를 확인하고, backup 종료 시 Manager와 platform의 HTTP readiness까지 기다린다.

HTTP/RPC 계약 통과를 모든 GUI·provider·보안 계약의 검증으로 확대하지 않는다. 초기 배포 대상은 로컬 사내 파일럿이다.

## 중앙 모델 관리 추가 검증 (2026-10-20)

- 계약 테스트 24개 통과: 관리자 API 권한, API 키 암호화·조회 비노출·빈 값 유지, 연결 테스트, 사용자 모델 선택, 미허용·비활성 모델 거부, Anthropic 호스트·workspace 헤더·도구 전달·usage 정산 포함.
- 최종 수정 이미지로 live smoke 통과: 실제 Hermes GUI, runtime provider endpoint/scoped key, WS/PTY, 브라우저 실행, 로그아웃 취소, 파일 중지·시작 보존, 정지 계정 차단. 부팅 YAML 의존성과 이미지 교체 시 비교 문제를 수정한 뒤 재검증했다.
- 실제 longbe00 포털에서 관리자 허용 모델 선택 UI 표시를 DOM으로 확인했다. 관리자 폼 전체 브라우저 저장·외부 연결은 미검증이다.
- 공급자 자격증명이 없어 실제 유료 모델 생성, OAuth 동의·갱신은 실행하지 않았다. OAuth 중앙 공유는 현재 활성 기능이 아니다.

## GUI 모델 목록 정책 검증 (2026-10-21)

계약 테스트 25개 통과. GUI REST 목록은 관리자 허용 모델만 포함하고 include_unconfigured·타 프로필·refresh로 우회할 수 없는지, 비활성 공급자 제외·금지 모델 저장 거부·허용 모델 저장·빈 목록을 확인했다. live smoke에는 WebSocket model.options 결과와 중앙 목록의 일치 검증을 추가했다.

## 중앙 OAuth / 공식 SDK 어댑터 검증 (2026-10-21)

- 계약 테스트 33개 통과: 기존 25개와 OAuth 관리자/기기 바인딩·일회용 callback, OIDC 서명/nonce/audience, 토큰 refresh 동시성, 권한 철회·연결 generation·미등록 client 차단, 공유 동시 실행 제한, 연결 해제·모델 교집합 포함.
- 공식 OpenAI·Anthropic SDK가 mock HTTP/SSE를 실제 파싱하는 방식으로 Responses 도구 스트림, Messages 텍스트, 인증 Bearer 헤더, API 키 자동 fallback 부재와 사용량 정산을 확인했다. 실제 공급자 생성 응답은 아니다.
- live smoke 통과: Hermes GUI, JSON-RPC 모델 목록, WS/PTY, 브라우저, 프로필 격리, 로그아웃/정지, 개인 파일 재시작 보존. 임시 계정/컨테이너만 정리했다.
- 포털 JavaScript 구문 검사 통과. 실제 관리자 OAuth 동의와 Anthropic 승인 client 등록, 실제 모델 사용·과금, 중앙 계정 공유 계약은 미검증이다.

## GUI 모델 저장 CSRF 호환 수정 (2026-10-21)

GUI fetchJSON은 중앙 포털의 X-CSRF-Token을 보내지 않는다. `/api/model/set`이 포털 저장 핸들러를 직접 호출하면서 정상 쿠키+Origin 요청에 403 `CSRF 검증 실패. 다시 로그인하세요.`를 반환하는 것을 회귀 테스트로 재현했다. GUI 저장 핸들러만 명시적으로 exact Origin + JSON 검증을 적용하고, 모델 저장 로직은 공통 함수로 분리했다. 중앙 포털의 `/platform/api/models/selection`은 계속 CSRF를 요구한다. 수정 후 테스트 34개 통과: 정상 GUI 저장·외부 Origin 차단·브라우저 Origin 누락 차단·포털 CSRF 유지 포함.

## 채팅 모델 불일치 수정 (2026-10-21)

중앙 허용 목록·기본값은 gpt-6.1-sol이었지만 admin/longbe00 런타임이 이전 이미지로 실행되어 config.model.default=gpt-4.1-mini를 유지하는 것을 확인했다. 시작 시 사용자별 선택 모델을 인증된 runtime-config에서 조회하고 GUI/포털 저장 시 기존 Hermes 모델 설정 API에도 반영하도록 수정했다. 두 기존 런타임을 데이터 볼륨을 보존해 재생성한 후 실제 설정이 gpt-6.1-sol임을 확인했다.

테스트 36개 통과. live smoke에서 허용 모델 저장 후 실제 런타임 config와 RUNTIME_TOKEN key_env 유지, 재시작 파일 보존을 확인했다. longbe00 런타임에서 실제 gpt-6.1-sol OAuth 추론 요청이 HTTP 200, 정상 텍스트, finish_reason=stop으로 완료됐다. 입력 11·출력 5·총 16토큰. 사용자의 기존 대화 내용은 이 확인 요청에 포함하지 않았다. 기존 채팅의 모델은 유지되므로 새 채팅으로 적용한다.
