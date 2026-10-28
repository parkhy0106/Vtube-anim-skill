# 실험 및 구현 실패 기록 (Failure Log)

## 1. Thin-Plate Spline (TPS) 및 B-Spline 기반 캔버스 왜곡 격자 (Failed & Truncated)
- **일시**: 2026-10-27
- **시도한 내용**:
  - 12개 제어 랜드마크와 2D Biharmonic 커널 $\phi(r) = r^2 \ln r$ 역행렬($\mathbf{L}^{-1}$)을 이용해 합뉴턴($\sum \vec{F}$)에 따른 Thin-Plate Spline (TPS) 변위장 $\mathbf{u}_{\text{TPS}}(\mathbf{x})$를 계산하고, HTML5 `<canvas>` 오버레이 상의 왜곡 격자 및 VTube Studio `PostProcessingUpdateRequest`(`LensDistortion`, `WaveDistortion`)에 연동하여 캔버스 왜곡을 표현하려 시도함.
- **실패 원인**:
  1. VTube Studio 네이티브 창 내부에서는 브라우저 `<canvas>`에 그린 TPS 왜곡 격자가 전혀 표시되지 않으며(VTS 내부에는 `ItemLoadRequest`로 로드된 아이템만 렌더링됨), 사용자 시점에서 왜곡 격자가 보이지 않음.
  2. VTube Studio의 전역 후처리 셰이더(`LensDistortion`, `WaveDistortion`)는 아바타 메쉬의 국소적인 TPS 탄성 찌그러짐을 표현하지 못하고 전체 화면만 부자연스럽게 흔들거나 왜곡시킴.
- **조치 결과**:
  - `vts_throw_overlay.html` 및 `vts_agent_ctl.py`에서 **TPS 행렬/커널 계산, 왜곡 격자 렌더링, `PostProcessingUpdateRequest` 왜곡 연동 코드를 완전히 제거(Truncate)** 함.
