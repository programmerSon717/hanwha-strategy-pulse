# Strategy Pulse — 여기부터 읽어라
>
> 텔레그램 봇: @HanwhaStrategyO_bot · 표시 이름 BOT_NAME=Strategy Pulse (2026-10-01 BSP Pulse 에서 변경)
> 리포/디렉터리 이름(HanwhaBSPnews, BSP-news-bot)과 내부 키는 바꾸지 않았다.

> 한화생명 경영전략실(Business Strategy & Planning) 전용 Strategic Intelligence Agent.
> 최초 작성: 2026-09-23

---

## 0. 불변의 법칙

**기존 크립토뉴스봇 한국어판·영문판에 절대 영향을 주지 않는다.**

이 리포는 크립토 봇 한국어판을 복사해 만들었지만, 구조적으로 격리돼 있다.

| 격리 장치 | 상태 |
|---|---|
| 디렉터리 | `~/Desktop/03_한화_업무/HanwhaBSPnews/` — 크립토는 `HanwhaDAPnews/` |
| git remote | **없음.** 크립토 리포로 push 가 물리적으로 불가능하다 |
| 코드 공유 | 없음. import·심볼릭링크 없이 완전 독립 사본 |
| DB | 자체 `botstate.sqlite3` |
| 텔레그램 | 별도 봇·별도 그룹 (아래 2절) |
| Gemini 키 | **아직 미정 — 5절을 반드시 읽어라** |

새 remote 를 붙일 때 `crypto-news-bot` 이나 `crypto-news-bot-en` 을 절대 지정하지 마라.

---

## 1. 무엇을 하는 봇인가

답해야 하는 질문은 하나다.

> "한화생명 경영전략실 구성원이 오늘 아침 반드시 알아야 할 변화는 무엇인가?"

키워드 매칭 봇이 아니다. 키워드는 **후보 확보용**일 뿐이고, 실을지 말지는
모델이 전략 관련성으로 판단한다.

성공 기준은 수집량이 아니다 — 아침에 Feed 를 본 사람이 **"이 10개면 오늘 주요
전략 이슈는 대략 파악했다"** 고 느끼는 것이다. Recall 높고 Noise 많은 봇보다
적게 실어도 Strategic Precision 이 높은 봇이 낫다.

---

## 2. 텔레그램 구조 — 탭 9개

| 탭 | 내부 키 | 성격 |
|---|---|---|
| 🏢 한화그룹 | `hanwha_group` | 가장 중요. 기사의 **실질적 핵심**이 한화일 때만 |
| 🤝 M&A · 지배구조 | `ma_governance` | 거래·소유권·경영권 변화 |
| 🏦 보험 · 금융 | `insurance_finance` | 산업구조·자본·채널·투자 |
| ⚖️ 규제 · 정책 | `regulation_policy` | 당국 결정이 사건의 **원인**일 때 |
| 🔎 경쟁사 · Big Tech | `competitors_bigtech` | 다른 Player 의 전략적 행동 |
| 💡 디지털 · 신사업 | `digital_newbiz` | 기술·신규 BM. **크립토 시세 아님** |
| 🌐 Global | `global_finance` | 해외전략 시사점 |
| 🚨 주요이슈 | `key_issues` | 집계 탭. 중복 게시 허용 |
| ☀️ Morning Brief | `daily_brief` | 집계 탭. 매일 06:55 Top10 |

**일반 기사는 Primary 탭 하나에만 나간다.** 여러 탭에 복제하지 않는다.
걸치는 주제는 `secondary_topics` 로 보관하고 해시태그로만 드러낸다.
집계 탭 둘(🚨·☀️)만 중복이 허용된다.

내부 키는 **모델이 뱉는 값이자 라우팅 키**다. 절대 바꾸지 마라.
표시 이름만 바꾸는 것은 안전하다 (`bsp_topics_def.json`).

---

## 3. 파이프라인

```
STEP 1  수집           collectors/rss.py · collectors/regulation.py
STEP 2  사전필터        prefilter.gate()        ← 모델 호출 전. 한도를 지킨다
STEP 3  기사 이해   ┐
STEP 4  전략관련성  ┤   summarizer.summarize() → prompts_bsp.SYSTEM_PROMPT
STEP 6  Topic 분류  ┘   (한 번의 호출로 끝낸다. 나눠 부르면 한도가 두 배)
STEP 5  Event 중복제거  events.EventIndex + store.cluster_seen
STEP 7  Ranking        strategic_score 임계값 (config)
STEP 8  텔레그램 발행    publisher.render() → publisher.publish()
STEP 9  Morning Brief  brief.py (별도 워크플로, 06:55 KST)
```

**relevant 판정과 Topic 분류는 다른 문제다.** relevant=true 인 기사만 Topic 을 받는다.

---

## 4. 임계값 (전부 .env 에서 조정)

```
DISCARD_THRESHOLD=50          이 미만은 버린다
GENERAL_TOPIC_THRESHOLD=65    이 이상만 탭에 게시. 50~64 는 DB 에만 저장
IMPORTANT_THRESHOLD=80
KEY_ISSUE_THRESHOLD=85        주요이슈 후보 (is_key_issue 로 한 번 더 거른다)
KEY_ISSUE_DAILY_CAP=6         하루 주요이슈 상한
DAILY_BRIEF_COUNT=10
```

---

## 5. 지금 당장 해야 할 것 — 사람이 해야 하는 일

### ① 새 텔레그램 봇 + 새 그룹

크립토 봇 토큰을 재사용하지 마라. BotFather 로 새로 만든다.

1. BotFather → `/newbot` → 토큰 받기
2. 새 그룹 생성 → **설정에서 '주제(Topics)' 켜기** (슈퍼그룹이어야 한다)
3. 봇을 그룹에 초대하고 **관리자** 권한 부여 (토픽 생성에 필요)
4. `.env` 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID` 입력
5. 탭 생성:
   ```bash
   venv/bin/python scripts/setup_topics.py --plan     # 미리보기
   venv/bin/python scripts/setup_topics.py --create   # 실제 생성
   ```
   출력된 `TG_TOPIC_*` 값을 `.env` 에 붙여 넣는다.

### ② Gemini API 키 — 크립토 봇과 공유하면 안 된다

**크립토 봇의 키를 그대로 쓰면 불변의 법칙을 어긴다.**
Gemini 무료 티어 일일 한도(flash-lite 500건)는 **프로젝트 단위**다.
같은 키를 쓰면 BSP 가 쓴 만큼 크립토 봇이 못 쓴다. 실제로 크립토 봇은
한도 소진으로 발행이 멈춘 전례가 있다.

→ **별도 Google Cloud 프로젝트에서 새 키를 발급**해 `GEMINI_API_KEY` 에 넣는다.

### ③ 물려받은 자격증명 삭제

복사 과정에서 크립토 봇의 자격증명이 따라왔다. 격리해 뒀으니 확인 후 지워라.

```
.env.crypto-inherited.DANGEROUS              크립토 봇 토큰·그룹ID·API키
tg_session.session.crypto-inherited.DANGEROUS  개인 텔레그램 계정 세션
```

둘 다 `.gitignore` 에 걸려 있어 커밋되지 않는다. 필요 없으면 지워라.

### ④ Ground Truth 파일

스펙이 지정한 `붙여넣은 텍스트(1).txt` 가 디스크에 없다. 이 파일에는
경영전략실이 실제로 공유한 과거 기사(Positive Sample)가 들어 있고,
**§37 Historical Backtest 는 이것 없이는 할 수 없다.**

파일을 받으면 다음을 한다.
- 과거 공유기사를 `relevant=true` 로 판정하는지 검증
- Category 가 타당한지 확인
- 놓치는 유형이 있으면 `bsp_topics_def.json` 의 keywords 와
  `bsp_entities.json` 을 보강 (코드가 아니라 설정을 고친다)

---

## 6. 실행 방법

```bash
cd ~/Desktop/03_한화_업무/HanwhaBSPnews/BSP-news-bot

# 회귀 검사 (모델 호출 없음. 배포 전 반드시)
venv/bin/python tests/bsp_cases.py

# 모델 판정까지 검사 (§38 A~H. 한도를 8건 쓴다)
venv/bin/python tests/bsp_cases.py --llm

# DRY RUN — 발행하지 않고 판정만 본다 (.env 의 DRY_RUN=true 가 기본)
venv/bin/python main.py --once

# Morning Brief 미리보기
venv/bin/python main.py --brief --dry-run

# 실제 운영 전환 — .env 에서 DRY_RUN=false 로 내린다
```

**DRY_RUN 기본값은 true 다.** 실수로 발행되지 않게 일부러 그렇게 뒀다.

---

## 7. 어디를 고쳐야 하나

| 바꾸고 싶은 것 | 고칠 곳 |
|---|---|
| 어떤 뉴스를 가져올까 | `config.py` 의 `rss_sources` / `regulation_sources` |
| 회사 이름·별칭 | `bsp_entities.json` |
| 탭 정의·키워드 | `bsp_topics_def.json` |
| 판정 기준·문체 | `prompts_bsp.py` |
| 점수 문턱 | `.env` |
| 모델 호출 전 필터 | `prefilter.py` |
| 발행 양식 | `publisher.render()` |
| 브리프 선정 로직 | `brief.py` |

**코드에 회사명이나 키워드를 흩뿌리지 마라.** JSON 두 개에서만 관리한다.

---

## 8. 남은 위험

1. **§37 Backtest 미실시** — Ground Truth 파일이 없어 실제 공유기사로
   검증하지 못했다. 놓치는 유형이 있을 수 있다.
2. **모델 판정 미검증** — API 키가 없어 §38 A~H 를 실제로 돌리지 못했다.
   오프라인 검사(라우팅·스키마·중복제거·필터)는 전부 통과했다.
3. **한도** — 실측 수집 459건 중 사전필터 통과 218건. 첫 스윕에 218 호출이
   필요하고 일일 한도는 500이다. 이후 스윕은 새 기사만 처리하므로 크게 줄지만,
   **초기 며칠은 한도를 지켜보고** 필요하면 `GENERAL_TOPIC_THRESHOLD` 를 올리거나
   `regulation_sources` 의 `when:` 범위를 줄여라.
4. **배포처 없음** — GitHub 리포를 아직 안 만들었다. 워크플로(`bot.yml`,
   `brief.yml`)는 준비돼 있으나 remote 가 없어 돌지 않는다.
