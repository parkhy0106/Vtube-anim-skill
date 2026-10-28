---
name: vts-anim-skill
description: VTube Studio (Live2D) 실시간 물리 시뮬레이션, 본 카르만(von Kármán) 비선형 타원 진동 애니메이션, 전신 캡슐 히트박스 및 약한 복원력(kr)/댐핑(cr) 기반 Default 위치·포즈 제어 스킬.
---

# VTube Studio Physics & Animation Skill (`vts-anim-skill`)

이 스킬은 **VTube Studio Public API (`ws://127.0.0.1:8001`)** 와 연동하여 Live2D 아바타에 물체를 던지거나(`hit`, `barrage`, `avalanche`), 절차적 물리 파형(`headpat`, `earthquake`, `wind`, `jump`) 및 표정 핫키를 구동하고, 인터랙션 후 Default 위치·포즈에서 멀어진 모델을 약한 복원력($k_r$)과 댐핑($c_r$)으로 자연스럽게 복원하는 에이전트 제어 스킬입니다.

## 주요 구성 파일
- `vts_throw_overlay.html`: 브라우저 또는 OBS 브라우저 소스에서 독립 실행 가능한 60Hz 실시간 물리 & 본 카르만 타원 변형 오버레이 UI
- `vts_agent_ctl.py`: 에이전트 및 CLI에서 직접 호출 가능한 Python 비동기 VTube Studio 제어 스크립트
- `vts_config.json` / `.env`: VTube Studio WebSocket URI 및 `StreamingAssets` 경로 설정

## 에이전트 실행 가이드 (CLI)
1. **최초 1회 플러그인 인증 토큰 발급**:
   ```bash
   python vts_agent_ctl.py --init-auth
   ```
2. **현재 로드된 Live2D 모델 및 캡슐 히트박스 상태 조회**:
   ```bash
   python vts_agent_ctl.py inspect
   ```
3. **단일 물체 투척 및 피격 시뮬레이션 (`hit`)**:
   ```bash
   python vts_agent_ctl.py hit -k 0.45 -d 6.0 --kr 3.5 --cr 3.0 -e shock -r 0.8
   ```
4. **다중 연속 투척 (`barrage`)**:
   ```bash
   python vts_agent_ctl.py barrage 5 -k 0.30 -d 6.5 --kr 3.5 --cr 3.0 -e shock -i 0.12 -r 0.6
   ```
5. **와르르 쏟아붓기 및 적층 (`avalanche`)**:
   ```bash
   python vts_agent_ctl.py avalanche 18 -k 0.25 -d 6.5 --kr 3.5 --cr 3.0 -e cry -i 0.055 -r 1.5
   ```
6. **기타 인터랙션 프리셋**:
   - 쓰다듬기: `python vts_agent_ctl.py headpat 0.5`
   - 지진 효과: `python vts_agent_ctl.py earthquake 0.5`
   - 강풍 물리: `python vts_agent_ctl.py wind 0.5`
   - 점프: `python vts_agent_ctl.py jump 0.5`
   - 즉시 Default 상태 복원: `python vts_agent_ctl.py reset`
