# 관리자 중앙 OAuth 연결

## 기존 Keys와의 차이

원본 Hermes Keys에는 ChatGPT/Codex 기기 코드 로그인과 Anthropic 외부 CLI 인증 항목이 있다.
그 자격증명은 해당 사용자 프로필 소유다. 이 서비스는 개인 프로필 credential 파일을 다른 사용자에게
복사하지 않으며, Keys의 개인 OAuth endpoint를 전체 사용자에게 열지 않는다.

중앙 포털 **관리자 모델 연결 → 모델 프로바이더 관리**에서 중앙 연결을 별도로 관리한다.
로그인 요청 자체는 OAuth 표준 PKCE를 사용한다. OpenAI는 공식 ChatGPT plan usage의 공개
Responses 경로, Anthropic은 등록된 클라이언트에 부여된 inference grant의 native Messages 경로다.
Codex/Claude의 agent loop를 중첩 실행하지 않고 Hermes의 기존 대화·도구 실행을 유지한다.

## 연결 절차

1. `config.yaml`의 `provider_oauth`에 배포 환경과 공급자에 맞는 public OAuth client 정보를 설정한다.
   confidential client secret 방식은 지원하지 않는다. 설정 변경 후 platform을 재시작한다.
2. 관리자 계정으로 포털 로그인 후 헤더의 **관리자 모델 연결**을 선택한다.
3. 공급자 선택 → **OAuth 연결 / 재연결** → **공급자 로그인 열기**.
4. 관리자가 공급자 화면에서 직접 인증·동의한다. 동일한 서비스 관리자 기기로 돌아와 연결을 완료한다.
5. 인증 방식을 **관리자 OAuth 계정**으로 선택한다. 조회된 계정 모델 중 허용 모델과 기본 모델을 입력한다.
6. **사용자에게 활성화**를 체크하고 저장한다. 사용자 Models 목록에는 계정 모델과 허용 목록의 교집합만 보인다.

연결 또는 grant 검증 성공은 모델 생성 검증과 다르다. 실제 계정으로 한 번의 생성과 tool call을 확인해야 한다.

### OpenAI 로컬 개발

기본값은 공식 `dynamic_agent_client` 등록과 `http://127.0.0.1:8088/auth/callback`이다.
서비스를 실행하는 PC의 브라우저에서 연결한다. 승인 후 받은 issued client ID와 안정적인 host ID를 보관한다.
state, PKCE, nonce, 서명된 ID token의 issuer/audience/만료/subject와 inference scope를 검증한다.
로그인에 성공했더라도 `chatgpt.tokens.use.direct` scope가 없으면 사용하지 않는다.

AWS/원격 서버에서는 브라우저의 127.0.0.1이 서버를 가리키지 않는다. localhost 주소만 서버 주소로 바꾸면
되는 것이 아니며, 원격 서비스에 승인·등록된 client_id와 HTTPS callback 설정이 필요하다.
이 코드가 호스팅·다중 사용자 계정 공유 권한을 부여하지는 않는다.

### Anthropic

이 서비스용으로 승인·등록된 client_id와 callback, inference scope가 필요하다.
기본 client_id는 비어 있으며 이 상태에서 연결을 누르면 설정 필요 오류를 표시한다.
Claude Code나 다른 앱의 client ID를 기본값으로 가져오거나, Claude Code로 위장한 헤더를 보내지 않는다.
기존 Keys에 계정 항목이 있다는 사실이 중앙 공유 grant를 제공한다는 의미는 아니다.

## 구현 계약

- 새 테이블: `provider_oauth`, `provider_oauth_flows`, `provider_calls`. 마이그레이션 v3는 기존 행·키를 보존한다.
- access/refresh/code/verifier는 기존 중앙 vault로 암호화한다. 조회 API, 모델 호출 응답, 감사 기록에는 넣지 않는다.
- callback은 코드를 임시 보관한다. 토큰 교환은 시작한 관리자 로그인 session에서 CSRF를 포함한 완료 요청으로 수행한다.
- OAuth state는 일회용, flow는 10분 만료다. 로그아웃·정지·다른 기기·generation 변경·동의 거부·중복 callback은 완료할 수 없다.
- 연결 교체 시 기존 계정 subject와 일치해야 한다. 다른 계정으로 바꾸려면 연결을 해제하고 새로 시작한다.
- 토큰 refresh는 DB 행 잠금으로 직렬화하고 worker thread에서 수행한다. 이벤트 루프에서 DB 잠금을 잡고
  네트워크 응답을 기다리지 않는다. refresh 실패·권한 철회는 재인증 필요 상태로 전환한다.
- 공유 OAuth 연결별 동시 실행은 `oauth_max_parallel`로 제한하며 기본 1이다. 초과 요청은 429를 반환한다.
  현재 구현은 영속 대기열이 아니라 즉시 거부 방식이다. 사용자별 기존 token quota도 함께 적용한다.
- OAuth 해제는 중앙의 token을 지우고 신규 실행을 막는다. 공급자 측 앱 권한 철회는 별도로 수행해야 한다.
- API 키 fallback은 자동으로 하지 않는다. API 키 전환은 관리자가 인증 방식을 명시적으로 선택해 저장한다.
- SDK 요청은 중앙에서 실행되며 사용자 코드는 중앙 SDK 프로세스에서 실행하지 않는다. 실제 tool 실행은 기존 개인 Hermes 환경에서 수행한다.
- 공급자에 사용자별 thread/resume ID를 공유하지 않는다. OpenAI는 `store=false`, 매 요청의 독립된 입력을 전송한다.

## 프로토콜 범위

공식 OpenAI Python SDK Responses 스트림과 Anthropic Python SDK Messages 스트림을 사용하고,
기존 Hermes Chat Completions 형태로 텍스트·함수 도구·finish reason·usage를 변환한다.
도구 결과를 다음 입력으로 다시 전달할 수 있다. SSE와 비스트리밍 응답을 지원한다.
이미지·오디오·구조화 출력은 명시적으로 거부한다. thinking/reasoning 내용을 보존하거나 native agent SDK의
전체 기능을 제공한다고 보장하지 않는다. 실제 native agent SDK·Codex app-server를 실행하는 모드는 이번 범위가 아니다.

## 검증과 미확인 사항

임시 PostgreSQL과 mock OAuth 공급자로 인증 요청 바인딩, 재사용 거부, OIDC 서명·nonce·audience 검증,
refresh 경쟁, 공유 동시 실행 제한, 모델 교집합과 연결 해제를 검증한다.
공식 SDK는 mock HTTP/SSE 응답을 실제로 파싱해 도구·텍스트·사용량 변환을 검증한다.
실제 관리자 동의, Anthropic 승인 client, 실제 inference·과금·계정 공유 계약은 아직 검증하지 않았다.

공식 자료:
- https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- https://auth.openai.com/.well-known/openid-configuration
- https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
