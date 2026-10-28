# Vtube-anim-skill (VTube Studio Physics & Animation Skill)

**Vtube-anim-skill**은 **VTube Studio (`ws://127.0.0.1:8001`)**의 Live2D 모델과 실시간으로 상호작용하는 **2D 물리 엔진 + 본 카르만(von Kármán) 비선형 타원 진동 애니메이션 오버레이(`vts_throw_overlay.html`)** 및 **AI 에이전트용 CLI 컨트롤러(`vts_agent_ctl.py`)** 스킬 패키지입니다.

---

## ✨ 핵심 기능 (Key Features)

1. **Non-String 코어 실루엣 기반 전신 캡슐 히트박스 (Full-Body Capsule Hitbox)**
   - 바람에 흔들리는 머리카락·리본·치마 등 `Param_Angle_Rotation_*` 계열 String 물리 메쉬를 자동 필터링하고, 코어 신체(정수리~발끝) 실루엣만을 추출하여 전신 캡슐 히트박스를 생성합니다.
   - 오버레이 내 **[히트박스 위치 조정]** 모달에서 캡슐의 길이(`Length`), 반경(`Radius`), 오프셋(`OffsetX`, `OffsetY`)을 드래그 또는 슬라이더로 실시간 미세 조정할 수 있습니다.
   - 투척 시 기본적으로 **정수리(머리 상단 캡)** 부근을 우선 조준하며, 시작 위치를 좌·우·상단 랜덤 또는 고정으로 전환할 수 있습니다.

2. **본 카르만(von Kármán) 비선형 타원 변형 & 반주기 B-Spline 접촉 물리**
   - 충돌 시 물체의 질량($m$), 말랑한 정도($s \in [0, 1]$), 법선 충돌 속도($v_n$)로부터 본 카르만-더핑 비선형 고유진동수 $f$, 반주기 $T_{1/2} = \frac{1}{2f}$, 경도 감쇠가 반영된 유효 진폭 $A_{\text{eff}} \propto A_0 \cdot s^{1.20}$를 산출합니다.
   - **단단한 물체($s \to 0$)일수록** 변형 저항 강성이 커져 진동 진폭이 자연스럽게 감소하며, $s = 0.00$일 때는 **순수 강체(Rigid Body) 모드**로 동작하여 변형 오버헤드가 `0`이 됩니다.
   - **반주기 접촉 페이즈($0 \le \tau \le T_{1/2}$)**: 1D Clamped Cubic B-Spline 곡선을 따라 표면에 부착된 채 충돌 법선축 방향으로 들어갔다 나오며 압축됩니다.
   - **이탈 후 자유 진동 페이즈($\tau > T_{1/2}$)**: **충돌 법선축(`outNx`, `outNy`) 방향의 변위 진동**, **상대각($\Delta\theta$)에 따른 아핀 전단(Shear) 및 비틀림 각도 요동(`#twistSlider`)**, **기하학적 단면 투영 크기**가 VTube Studio 네이티브 아이템(`ItemMoveRequest`)과 HTML5 Canvas(`ctx.transform`)에 동시 동기화됩니다.

3. **인터랙션 후 Default 위치·포즈 약한 복원력($k_r$) & 댐핑($c_r$) 제어**
   - Live2D 모델이 피격 또는 외부 인터랙션 이후 Default 위치(`baselineModelPos`)와 Default 포즈(`FaceAngleX/Y/Z`, `FacePositionX/Y`)에서 멀어지면, 2차 스프링-댐퍼 운동 방정식($\mathbf{a} = \mathbf{F}_{\text{contact}} - k_r \Delta\mathbf{x} - c_r \mathbf{v}$)에 의해 **약한 복원력(`#restoreStiffSlider`, $k_r$)** 과 **복원 댐핑(`#restoreDampSlider`, $c_r$)** 이 연속 작용하여 누적 위치 드리프트 없이 원래 위치와 포즈로 부드럽게 복귀합니다.

4. **사용자 커스텀 사진 업로드 & 오브젝트별 스펙 개별 캐싱**
   - 브라우저에서 PNG/JPG/WebP/GIF 이미지를 업로드하면 256×256 투명 PNG(`customDataBase64`) 및 VTS 규격 파일명(영숫자·하이픈 8~32자)으로 자동 정규화되어 VTube Studio 씬 내부와 캔버스에 동시에 투척됩니다.
   - 오브젝트별 **크기(`Size`)**, **말랑함(`s`)**, **진동 진폭(`A`)** 설정이 각 물체별로 개별 캐싱됩니다.

5. **최대 소환 가능 슬롯 자동 캡핑 (No Over-Spawn / Over-Computation)**
   - `ItemListRequest`를 통해 VTube Studio 씬의 잔여 슬롯과 전방 레이어 한도(`MAX_FRONT_ORDER_SLOTS = 27`)를 실시간 조회하여, 연속 폭격(`spawnBarrage`)이나 와르르 쏟아붓기(`spawnAvalanche`) 시 최대 소환 가능 개수를 초과하는 불필요한 API 호출 및 물리 연산을 원천 방지합니다.

---

## 🛠️ 사전 준비 및 설치 (Prerequisites & Setup)

### 1. 요구 사항
- **VTube Studio** (Steam 버전 실행 중)
- **Python 3.10 이상** (CLI 및 AI 에이전트 연동 시)
- 최신 웹 브라우저 (Chrome / Edge 등) 또는 **OBS Studio Browser Source**

### 2. 저장소 클론 및 패키지 설치
```bash
git clone https://github.com/parkhy0106/Vtube-anim-skill.git
cd Vtube-anim-skill
pip install -r requirements.txt
```

### 3. VTube Studio API 활성화
1. **VTube Studio** 실행 후 좌측 톱니바퀴(설정) 아이콘 클릭
2. **일반 설정(General Settings)** 하단의 **Start API (Allow Plugins)** 스위치를 **ON**으로 설정 (기본 포트: `8001`)
3. 커스텀 이미지나 기본 내장 치즈(`image-removebg-preview.png`)를 네이티브 아이템으로도 즉시 로드하려면, `image-removebg-preview.png` 파일을 아래 VTS 아이템 폴더에 복사해 두면 권한 팝업 없이 더욱 빠르게 동작합니다:
   ```text
   C:\Program Files (x86)\Steam\steamapps\common\VTube Studio\VTube Studio_Data\StreamingAssets\Items\
   ```

### 4. 환경 설정 (`vts_config.json` / `.env`)
기본 설정 파일(`vts_config.json` 및 `.env`)은 표준 Steam 설치 경로와 `ws://127.0.0.1:8001` 포트를 가리킵니다. VTube Studio가 다른 드라이브나 포트에 설치된 경우 두 파일의 값을 수정하세요:
```json
{
  "VTS_URI": "ws://127.0.0.1:8001",
  "VTS_ASSETS_PATH": "C:\\Program Files (x86)\\Steam\\steamapps\\common\\VTube Studio\\VTube Studio_Data\\StreamingAssets"
}
```

---

## 🚀 사용 방법 (Usage)

### 방법 A: 대화형 물리 오버레이 (`vts_throw_overlay.html`) 실행
1. `vts_throw_overlay.html` 파일을 브라우저(Chrome/Edge)에서 열거나, **OBS 브라우저 소스(Browser Source, 1920×1080)** 로 추가합니다.
   - HUD 패널을 숨기고 캔버스만 표시하려면 URL 뒤에 `?hud=0` 쿼리를 붙입니다 (예: `vts_throw_overlay.html?hud=0`).
2. 최초 연결 시 VTube Studio 화면에 나타나는 플러그인 승인 팝업에서 **[Allow / 승인]** 을 클릭합니다.
   - 커스텀 이미지 업로드 기능을 사용할 때는 `LoadCustomImagesAsItems` 권한 팝업도 **[Allow]** 해주세요.
3. 좌측 상단 HUD 컨트롤 패널에서 모든 물리 파라미터를 슬라이더로 실시간 조절할 수 있습니다:

| 슬라이더 / 옵션 | 설명 | 기본값 (범위) |
| :--- | :--- | :--- |
| **던질 물체** (`#itemSelect`) | 투척할 기본/업로드 오브젝트 선택 | `치즈 (0.24 kg)` |
| **📷 커스텀 물체 사진 업로드** | 사용자 이미지(PNG/JPG 등) 업로드 및 즉시 투척 | 다중 파일 지원 |
| **오브젝트 크기 (`Size`)** | 물체별 렌더링 크기 및 충돌 반경 비례 조절 | `0.16` (`0.05` ~ `0.50`) |
| **던질 물체 속도 (`v`)** | 투척 비행 속도 배율 | `1.00x` (`0.35x` ~ `2.50x`) |
| **물체별 말랑한 정도 (`s`)** | `0.00`(강체 모드) ~ `1.00`(최대 말랑함), 단단할수록 진폭 자동 감소 | `0.45` (`0.00` ~ `1.00`) |
| **진동 진폭 (`A`)** | 충돌 시 본 카르만 타원 변형 기준 진폭 | `0.30` (`0.05` ~ `0.75`) |
| **비틀림 요동 세기 (`θ`)** | 충돌 법선축 대비 상대각에 따른 회전 비틀림 요동 각도 | `14°` (`0°` ~ `45°`) |
| **물체 질량 배율 (`m`)** | 물체 질량 및 합뉴턴 충격량 배율 | `1.0x` (`0.2x` ~ `3.0x`) |
| **넉백 세기 (`k`)** | 피격 시 아바타가 밀려나는 넉백 강도 | `0.45` (`0.10` ~ `1.50`) |
| **감쇠 정도 (`d`)** | 고개/상체 피격 진동 감쇠율 | `6.0` (`2.0` ~ `12.0`) |
| **모델 복원력 세기 (`kr`)** | Default 위치·포즈로 끌어당기는 약한 스프링 복원 계수 | `3.5` (`0.5` ~ `15.0`) |
| **모델 복원 댐핑 (`cr`)** | Default 위치·포즈 복원 과정의 속도 감쇠 계수 | `3.0` (`0.5` ~ `12.0`) |
| **복귀 시간 (`r초`)** | 모든 물체 안착 후 Default State 완전 초기화까지 대기 시간 | `0.8s` (`0.0s` ~ `5.0s`) |

---

### 방법 B: Python CLI 및 AI 에이전트 연동 (`vts_agent_ctl.py`)

터미널 또는 AI 에이전트(Antigravity Skill 등)에서 명령줄로 즉시 물리 액션을 실행할 수 있습니다.

```bash
# 1. 최초 1회 VTube Studio 플러그인 인증 (.vts_token 생성)
python vts_agent_ctl.py --init-auth

# 2. 현재 아바타 상태 및 전신 캡슐 히트박스 정보 확인
python vts_agent_ctl.py inspect

# 3. 단일 물체 투척 (넉백 0.45, 감쇠 6.0, 복원력 kr=3.5, 복원댐핑 cr=3.0, 표정 shock)
python vts_agent_ctl.py hit -k 0.45 -d 6.0 --kr 3.5 --cr 3.0 -e shock -r 0.8

# 4. 다중 물체 연속 투척 (5개, 0.12초 간격)
python vts_agent_ctl.py barrage 5 -k 0.30 -d 6.5 --kr 3.5 --cr 3.0 -e shock -i 0.12 -r 0.6

# 5. 와르르 쏟아붓기 적층 시뮬레이션 (최대 가용 슬롯 내 자동 캡핑)
python vts_agent_ctl.py avalanche 18 -k 0.25 -d 6.5 --kr 3.5 --cr 3.0 -e cry -i 0.055 -r 1.5

# 6. 절차적 파형 및 물리 효과
python vts_agent_ctl.py headpat 0.5
python vts_agent_ctl.py earthquake 0.5
python vts_agent_ctl.py wind 0.5
python vts_agent_ctl.py jump 0.5

# 7. 즉시 Default 위치·포즈·표정 복원 및 씬 아이템 정리
python vts_agent_ctl.py reset
```

---

## 📁 프로젝트 구조 (Directory Structure)

```text
Vtube-anim-skill/
├── vts_throw_overlay.html      # 60Hz 본 카르만 물리 & 캡슐 히트박스 오버레이 (독립 실행 HTML)
├── vts_agent_ctl.py            # Python 비동기 CLI & AI 에이전트 컨트롤러
├── SKILL.md                    # Antigravity 에이전트 스킬 정의서
├── vts_config.json             # VTube Studio WebSocket 및 에셋 경로 설정
├── .env                        # 환경 변수 설정
├── image-removebg-preview.png  # 기본 제공 커스텀 투척 오브젝트 이미지 (치즈)
├── FAILURE_LOG.md              # 실험 및 구현 실패/제거(Truncate) 이력 기록
├── requirements.txt            # Python 의존성 목록
└── LICENSE                     # MIT License
```

---

## 📄 라이선스 (License)

이 프로젝트는 **MIT License** 하에 배포됩니다. 자세한 내용은 [LICENSE](./LICENSE) 파일을 참고하세요.
