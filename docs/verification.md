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

HTTP/RPC 계약 통과를 모든 GUI·provider·보안 계약의 검증으로 확대하지 않는다. 초기 배포 대상은 로컬 사내 파일럿이다.
