import argparse
import asyncio
import dataclasses
import glob
import json
import math
import os
import random
import sys
import re
import time
import uuid
import websockets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "vts_config.json")
TOKEN_PATH = os.path.join(BASE_DIR, ".vts_token")
STATE_LOCK_PATH = os.path.join(BASE_DIR, ".vts_default_state.json")
PLUGIN_NAME = "AntigravityVTSController"
PLUGIN_DEV = "AntigravityAgent"

DEFAULT_ASSETS_PATH = r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio\VTube Studio_Data\StreamingAssets"

# VTube Studio 기본 내장 아이템 중 투척용 Default 오브젝트 및 물체별 고유 질량(kg)·반발계수·반경 스펙
DEFAULT_OBJECT_FILE = "image-removebg-preview.png"
MAX_FRONT_ORDER_SLOTS = 26
HIT_EXPRESSION_FILES = {
    "SignShock.exp3.json",
    "EyesCry.exp3.json",
    "SignAngry.exp3.json",
    "EyesLove.exp3.json",
    "shock.exp3.json",
    "cry.exp3.json",
    "angry.exp3.json",
}
ITEM_PHYSICS_SPECS = {
    "image-removebg-preview.png":          {"mass": 0.24, "restitution": 0.22, "size": 0.16, "radius": 0.095, "softness": 0.85},
    "beverage_Bepis (@7MDigital).png":     {"mass": 0.38, "restitution": 0.32, "size": 0.16, "radius": 0.095, "softness": 0.78},
    "beverage_7Bull (@7MDigital).png":     {"mass": 0.50, "restitution": 0.35, "size": 0.17, "radius": 0.102, "softness": 0.72},
    "bread (@denchisoft).png":             {"mass": 0.18, "restitution": 0.18, "size": 0.16, "radius": 0.092, "softness": 1.25},
    "egg (by @denchisoft).png":            {"mass": 0.06, "restitution": 0.12, "size": 0.13, "radius": 0.072, "softness": 0.95},
    "beverage_Soda_Cola (@7MDigital).png": {"mass": 0.42, "restitution": 0.32, "size": 0.16, "radius": 0.095, "softness": 0.80},
}
THROWABLE_ITEMS = list(ITEM_PHYSICS_SPECS.keys())

# 바람(Wind / Pendulum / String)에 따라 흔들리도록 설정된 ArtMesh 이름 패턴 (실루엣 추출 시 제외)
STRING_SWAY_MESH_PATTERN = re.compile(
    r"^(hair_|back_hair|uh_|tie_|under_tie|skirt|under_boob_shadow)",
    re.IGNORECASE,
)


def filter_non_string_core_artmeshes(
    art_mesh_names: list[str],
    live2d_params: list[dict],
) -> tuple[list[str], list[str]]:
    """
    Live2D 파라미터의 바람 흔들림 체인(Param_Angle_Rotation_*) 및 ArtMesh 이름을 검사하여,
    바람에 흔들리는 string(머리카락 갈래, 리본/타이, 치마 프릴 등) 오브젝트를 실루엣 추출 대상에서 제외하고
    순수 체간+다리+발끝(head ~ shoe_left/shoe_right) 코어 실루엣 메쉬만 반환.
    """
    string_roots: set[str] = set()
    for p in live2d_params or []:
        pname = p.get("name", "")
        m = re.match(r"^Param_Angle_Rotation_\d+_(.+)$", pname)
        if m:
            string_roots.add(m.group(1).lower())

    core_meshes: list[str] = []
    excluded_string_meshes: list[str] = []
    for name in art_mesh_names or []:
        lower = name.lower()
        matches_string_root = any(lower.startswith(root) for root in string_roots)
        if matches_string_root or STRING_SWAY_MESH_PATTERN.search(lower):
            excluded_string_meshes.append(name)
        else:
            core_meshes.append(name)
    return core_meshes, excluded_string_meshes


# 피격 표정 프리셋 별칭 -> 모델 핫키 이름/ID 매핑
EXPRESSION_HOTKEY_MAP = {
    "shock": "Shock Sign",
    "cry": "Eyes Cry",
    "angry": "Angry Sign",
    "heart": "Heart Eyes",
    "love": "Heart Eyes",
    "shake": "Anim Shake",
    "shock_anim": "e416a7d797c94bcbaa4a453d6cb1bae0",
    "none": None,
}


def load_config() -> dict:
    cfg = {
        "VTS_URI": os.getenv("VTS_URI", "ws://127.0.0.1:8001"),
        "VTS_ASSETS_PATH": os.getenv("VTS_ASSETS_PATH", DEFAULT_ASSETS_PATH),
    }
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


CONFIG = load_config()
VTS_URI = CONFIG["VTS_URI"]
VTS_ASSETS_PATH = CONFIG["VTS_ASSETS_PATH"]


@dataclasses.dataclass
class ModelHitBox:
    """
    던지기 직전 모델 상태(positionX, positionY, size, rotation) 및
    바람에 흔들리는(string) 오브젝트(머리카락 갈래, 리본/타이, 치마 프릴 등)를 제외한
    코어 실루엣(head ~ shoe_left/shoe_right)으로부터 생성되는 전신 캡슐(Full-Body Capsule: 선분 AB + 반경 R) 히트박스.
    """
    center_x: float
    center_y: float
    half_w: float
    half_h: float
    rotation_deg: float = 0.0
    scale_factor: float = 1.0
    ax: float = 0.0
    ay: float = -0.85
    bx: float = 0.0
    by: float = 0.34
    capsule_radius: float = 0.135
    net_force_x: float = 0.0
    net_force_y: float = 0.0
    core_mesh_count: int = 163
    excluded_string_count: int = 120

    @classmethod
    def from_model_position(
        cls,
        pos: dict,
        core_mesh_count: int = 163,
        excluded_string_count: int = 120,
    ) -> "ModelHitBox":
        px = float(pos.get("positionX", 0.0))
        py = float(pos.get("positionY", -0.90))
        size = float(pos.get("size", -52.0))
        rot = float(pos.get("rotation", 0.0)) % 360.0

        scale = max(0.30, min(3.2, math.pow(2.0, (size + 52.0) / 23.5)))
        rad = math.radians(rot)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        # 바람(string) 오브젝트(옆머리/뒷머리/리본/치마) 제외 후 순수 코어 몸통+다리 폭 반경
        cap_r = 0.135 * scale

        # 발끝(shoe_left / shoe_right: -0.92 * scale)부터 정수리(head: +1.08 * scale)까지 전신 캡슐 연장
        bottom_cap_local_y = (-0.92 + 0.135) * scale
        top_cap_local_y = (1.08 - 0.135) * scale

        ax = px - bottom_cap_local_y * sin_r
        ay = py + bottom_cap_local_y * cos_r
        bx = px - top_cap_local_y * sin_r
        by = py + top_cap_local_y * cos_r

        cx = 0.5 * (ax + bx)
        cy = 0.5 * (ay + by)
        half_seg = 0.5 * math.hypot(bx - ax, by - ay)

        return cls(
            center_x=cx,
            center_y=cy,
            half_w=cap_r,
            half_h=half_seg + cap_r,
            rotation_deg=rot,
            scale_factor=scale,
            ax=ax,
            ay=ay,
            bx=bx,
            by=by,
            capsule_radius=cap_r,
            core_mesh_count=core_mesh_count,
            excluded_string_count=excluded_string_count,
        )

    def clear_frame_forces(self):
        self.net_force_x = 0.0
        self.net_force_y = 0.0

    def contains(self, x: float, y: float, obj_radius: float = 0.04) -> bool:
        """물체 좌표 (x, y)와 반경이 전신 캡슐(선분 AB + 반경 R) 내부에 들어왔는지 판정."""
        abx = self.bx - self.ax
        aby = self.by - self.ay
        ab_len_sq = abx * abx + aby * aby
        t = ((x - self.ax) * abx + (y - self.ay) * aby) / ab_len_sq if ab_len_sq > 1e-8 else 0.5
        t = max(0.0, min(1.0, t))
        qx = self.ax + t * abx
        qy = self.ay + t * aby
        return math.hypot(x - qx, y - qy) <= (self.capsule_radius + obj_radius)

    def resolve_collision(
        self,
        proj: "DefaultProjectile",
        radius_override: float = None,
        dt: float = 1.0 / 60.0,
    ) -> tuple[bool, float, float, float]:
        """
        물체(proj, 질량 m)와 전신 캡슐(선분 AB + 반경 R, 정수리~발끝) 히트박스 간의 해석적 강체 충돌 해결
        및 접촉 뉴턴 반작용 힘(합뉴턴 ∑F) 산출:
        - 반환값: (collided, normal_speed, force_x, force_y)
        """
        obj_r = radius_override if radius_override is not None else proj.radius
        sum_r = self.capsule_radius + obj_r

        abx = self.bx - self.ax
        aby = self.by - self.ay
        ab_len_sq = abx * abx + aby * aby
        t = ((proj.x - self.ax) * abx + (proj.y - self.ay) * aby) / ab_len_sq if ab_len_sq > 1e-8 else 0.5
        t = max(0.0, min(1.0, t))

        qx = self.ax + t * abx
        qy = self.ay + t * aby
        dx = proj.x - qx
        dy = proj.y - qy
        dist_sq = dx * dx + dy * dy
        if dist_sq >= sum_r * sum_r:
            return False, 0.0, 0.0, 0.0

        dist = math.sqrt(dist_sq)
        if dist > 1e-5:
            out_nx = dx / dist
            out_ny = dy / dist
        else:
            out_nx = -1.0 if proj.vx >= 0 else 1.0
            out_ny = 0.0

        proj.x = qx + out_nx * sum_r
        proj.y = qy + out_ny * sum_r

        vel_normal = proj.vx * out_nx + proj.vy * out_ny

        mass = proj.mass
        # [요구사항 1] 물체의 말랑함(softness)이 최소값(<= 0.02)이면 강체 모드(비변형, 순수 탄성 e0)
        is_rigid = getattr(proj, "softness", 0.55) <= 0.02
        s = 0.0 if is_rigid else max(0.03, min(0.98, getattr(proj, "softness", 0.55)))
        # [요구사항 3] 비탄성 충돌항: 말랑할수록(s가 클수록) 충돌 후 반발 속도가 급격히 감소
        inelastic_factor = math.pow(max(0.015, 1.0 - 0.88 * s), 1.6)
        rest = proj.restitution if is_rigid else max(0.012, proj.restitution * inelastic_factor)
        normal_impulse = 0.0

        if not proj.has_hit:
            # [첫 충돌] 아바타 넉백 충격량 산출 및 피격 운동량 흡수
            proj.has_hit = True
            normal_speed = abs(vel_normal) if vel_normal < 0 else math.hypot(proj.vx, proj.vy)
            normal_impulse = mass * (1.0 + rest) * normal_speed

            # 캡슐 바깥으로 밀착 위치 보정
            proj.x = qx + out_nx * (sum_r + 0.005)
            proj.y = qy + out_ny * (sum_r + 0.005)

            # [핵심] 탈출 속도를 단순히 방향 반전(-vx)하지 않고 피격 에너지 흡수 후 중력 자유낙하로 전환
            # 강체는 0.32 수준의 작은 반발, 말랑할수록 반발 속도가 급감하여 제자리에서 바로 툭 떨어짐
            rebound_speed = normal_speed * (0.32 if is_rigid else max(0.02, 0.25 * math.pow(1.0 - 0.85 * s, 1.5)))
            proj.vx = out_nx * rebound_speed
            # 피격 표면에 닿는 순간 수직 속도는 흡수되어 위로 솟구치지 않고 아래로 바로 떨어지도록 설정
            proj.vy = min(0.15, proj.vy * 0.20 + out_ny * 0.10)
            proj.angular_velocity = -proj.angular_velocity * (0.65 if is_rigid else 0.25)
            proj.gravity = 4.80

            # 본 카르만 진동 상태 등록
            if not is_rigid:
                beta_vk = 0.65 + 1.55 * s
                raw_amp = 0.38 * min(2.5, normal_speed / 1.5) * math.sqrt(mass / 0.35) * (0.35 + 1.25 * s) * getattr(proj, "amplitude", 1.0)
                amp = max(0.08, min(0.78, raw_amp / math.sqrt(1.0 + 0.45 * beta_vk * raw_amp * raw_amp)))
                base_freq = 5.5 + 4.5 * (1.0 - 0.4 * s)
                freq_hz = max(2.5, min(20.0, base_freq * getattr(proj, "vib_speed", 1.0)))
                omega = 2.0 * math.pi * freq_hz
                proj.vk_state = {
                    "active": True,
                    "t0": time.time(),
                    "amp": amp,
                    "omega": omega,
                    "damping": 1.8 + 1.8 * s,
                    "out_nx": out_nx,
                    "out_ny": out_ny,
                    "shift_nx": -proj.radius * 0.40 * out_nx,
                    "shift_ny": -proj.radius * 0.40 * out_ny,
                }
            else:
                proj.vk_state = {"active": False}

            contact_dt = max(0.016, dt)
            impact_force_mag = normal_impulse / contact_dt
            weight_force_mag = mass * 9.81 * max(0.0, out_ny)
            total_normal_force = impact_force_mag + weight_force_mag

            # 작용-반작용: 아바타가 받는 힘은 내향 법선(-out_nx, -out_ny) 방향!
            force_x = -out_nx * total_normal_force + 0.14 * mass * proj.vx / contact_dt
            force_y = -out_ny * total_normal_force + 0.14 * mass * proj.vy / contact_dt

            self.net_force_x += force_x
            self.net_force_y += force_y

            return True, normal_speed, force_x, force_y
        else:
            # 이미 충돌 후 중력 낙하 중: 캡슐을 파고들지 않도록 x 방향만 표면 밖으로 부드럽게 분리하고 낙하(vy)는 유지
            proj.x = qx + out_nx * sum_r
            proj.vx *= 0.92
            return False, 0.0, 0.0, 0.0


@dataclasses.dataclass
class DefaultProjectile:
    """
    초기 위치(x, y), 초기 속도(vx, vy), 고유 질량(mass, kg)을 가지고 중력장 내에서 날아가는 오브젝트 모델.
    """
    file_name: str = DEFAULT_OBJECT_FILE
    x: float = -0.95
    y: float = -0.05
    vx: float = 2.85          # 초기 수평 속도 (units/s)
    vy: float = 1.65          # 초기 수직 속도 (units/s)
    gravity: float = 4.80     # 중력 가속도 (units/s^2)
    rotation: float = 0.0     # 현재 각도 (deg)
    angular_velocity: float = 600.0  # 초기 회전 각속도 (deg/s)
    size: float = 0.16        # 기본 오브젝트 크기
    radius: float = 0.095     # 충돌 반경
    mass: float = 0.38        # 물체 질량 (kg)
    restitution: float = 0.32 # 충돌 반발 계수 (0 ~ 1)
    softness: float = 0.78    # 물체 말랑한 정도 (0.05 ~ 1.0)
    damping: float = 6.0      # 감쇠 비율 (0.5 이하 시 강체 모드)
    amplitude: float = 1.0    # 진동 진폭 배율
    vib_speed: float = 1.0    # 진동 애니메이션 배속
    order: int = 10           # 고유 레이어 순서 (2 ~ 28)
    instance_id: str = None
    has_hit: bool = False
    vk_state: dict = None
    age: float = 0.0

    def __post_init__(self):
        spec = ITEM_PHYSICS_SPECS.get(self.file_name)
        if spec:
            if self.mass == 0.38:
                self.mass = spec["mass"]
            if self.radius == 0.095:
                self.radius = spec["radius"]
            if self.size == 0.16:
                self.size = spec["size"]
            if self.restitution == 0.32:
                self.restitution = spec["restitution"]
            if self.softness == 0.78:
                self.softness = spec.get("softness", 0.78)

    def step(self, dt: float):
        """dt(초) 동안의 속도·위치·회전 오일러 적분."""
        self.age += dt
        self.x += self.vx * dt
        self.y += self.vy * dt - 0.5 * self.gravity * (dt ** 2)
        self.vy -= self.gravity * dt
        self.rotation = (self.rotation + self.angular_velocity * dt) % 360.0

    def check_collision(self, hitbox: ModelHitBox) -> bool:
        """던지기 전 상태로부터 계산된 모델 충돌 박스(ModelHitBox)와의 충돌 판정."""
        if self.has_hit:
            return False
        return hitbox.contains(self.x, self.y, obj_radius=self.radius)

    def apply_bounce(self, target_x: float, target_y: float) -> tuple[float, float]:
        """
        충돌 시 아바타가 받는 뉴턴 충격력 벡터(force_x, force_y)를 반환하고,
        비탄성 반발 계수(restitution)를 적용해 오브젝트를 튕겨나가는 속도로 전환.
        """
        in_vx, in_vy = self.vx, self.vy
        self.has_hit = True

        nx = self.x - target_x
        ny = self.y - target_y
        norm = math.hypot(nx, ny) or 1.0
        nx /= norm
        ny /= norm

        is_rigid = self.softness <= 0.02
        s = 0.0 if is_rigid else max(0.03, min(0.98, self.softness))
        inelastic_factor = math.pow(max(0.015, 1.0 - 0.88 * s), 1.6)
        eff_rest = self.restitution if is_rigid else max(0.012, self.restitution * inelastic_factor)

        normal_speed = abs(in_vx * nx + in_vy * ny)
        if normal_speed < 0.1:
            normal_speed = math.hypot(in_vx, in_vy)
        force_x = in_vx * self.mass / 0.016
        force_y = in_vy * self.mass / 0.016

        # [요구사항 반영] 탈출 속도를 단순히 방향 반전(-vx)하지 않고 피격 에너지 흡수 후 중력 자유낙하로 전환
        rebound_speed = normal_speed * (0.32 if is_rigid else max(0.02, 0.25 * math.pow(1.0 - 0.85 * s, 1.5)))
        self.vx = nx * rebound_speed
        self.vy = min(0.15, in_vy * 0.20 + ny * 0.10)
        self.angular_velocity = -self.angular_velocity * (0.65 if is_rigid else 0.25)
        self.gravity = 4.80

        if not is_rigid:
            beta_vk = 0.65 + 1.55 * s
            in_speed = math.hypot(in_vx, in_vy)
            raw_amp = 0.38 * min(2.5, in_speed / 1.5) * math.sqrt(self.mass / 0.35) * (0.35 + 1.25 * s) * self.amplitude
            amp = max(0.08, min(0.78, raw_amp / math.sqrt(1.0 + 0.45 * beta_vk * raw_amp * raw_amp)))
            base_freq = 5.5 + 4.5 * (1.0 - 0.4 * s)
            freq_hz = max(2.5, min(20.0, base_freq * self.vib_speed))
            omega = 2.0 * math.pi * freq_hz
            self.vk_state = {
                "active": True,
                "t0": time.time(),
                "amp": amp,
                "omega": omega,
                "damping": 1.8 + 1.8 * s,
                "out_nx": nx,
                "out_ny": ny,
                "shift_nx": -self.radius * 0.40 * nx,
                "shift_ny": -self.radius * 0.40 * ny,
            }
        else:
            self.vk_state = {"active": False}

        return force_x, force_y


class HitInteractionAnimator:
    """
    물체 충돌 시 아바타가 받는 합뉴턴(force_x, force_y)에 의해 구동되는 피격 인터랙션 애니메이션 제어기.
    - force_x > 0 (왼쪽에서 날아와 오른쪽으로 미는 힘) -> 아바타가 오른쪽(+x)으로 넉백!
    - force_x < 0 (오른쪽에서 날아와 왼쪽으로 미는 힘) -> 아바타가 왼쪽(-x)으로 넉백!
    """

    def __init__(
        self,
        impact_vx: float,
        impact_vy: float,
        knockback: float = 0.40,
        damping: float = 6.0,
        expression: str = "shock",
        duration: float = 0.80,
    ):
        self.force_x = impact_vx
        self.force_y = impact_vy
        self.knockback = max(0.0, knockback)
        self.damping = max(0.5, damping)
        self.expression = (expression or "shock").lower()
        self.duration = duration
        self.force_mag = math.hypot(impact_vx, impact_vy)
        self.dir_x = 1.0 if impact_vx >= 0 else -1.0
        force_scale = max(0.65, min(1.85, self.force_mag / 25.0))
        self.intensity = self.knockback * force_scale
        self.norm_fx = max(-1.5, min(1.5, (impact_vx / 25.0) * self.knockback))
        self.norm_fy = max(-1.5, min(1.5, (impact_vy / 25.0) * self.knockback))

    def sample_parameters(self, t: float) -> list[dict]:
        """충돌 후 경과 시간 t(초)에서의 실시간 트래킹 주입 파라미터 계산."""
        env = math.exp(-self.damping * t)
        osc_main = math.cos(2.0 * math.pi * 3.4 * t)
        osc_sub = math.sin(2.0 * math.pi * 5.0 * t)

        angle_x = 18.0 * self.norm_fx * env * osc_main
        angle_y = (14.0 * self.norm_fy - 8.0 * abs(self.norm_fx)) * env * math.cos(2.0 * math.pi * 2.8 * t)
        angle_z = 15.0 * self.norm_fx * env * (0.85 * osc_main + 0.15 * osc_sub)

        pos_x = 2.8 * self.norm_fx * env * osc_main
        pos_y = (2.2 * self.norm_fy - 1.2 * abs(self.norm_fx)) * env * abs(osc_main)

        return [
            {"id": "FaceAngleX", "value": angle_x, "weight": 1.0},
            {"id": "FaceAngleY", "value": angle_y, "weight": 1.0},
            {"id": "FaceAngleZ", "value": angle_z, "weight": 1.0},
            {"id": "FacePositionX", "value": pos_x, "weight": 1.0},
            {"id": "FacePositionY", "value": pos_y, "weight": 1.0},
        ]


class AvatarRestoringState:
    """
    Live2D 모델이 인터랙션 후 Default 위치(baseline_model_pos) 및 Default 포즈(0.0)에서 멀어졌을 때,
    약한 복원력(kr)과 복원 댐핑(cr)을 적용하여 Default 위치와 포즈로 자연스럽게 되돌리는 2차 스프링-댐퍼 상태 제어기.
    """

    def __init__(self, restore_stiffness: float = 3.5, restore_damping: float = 3.0):
        self.kr = max(0.2, float(restore_stiffness))
        self.cr = max(0.2, float(restore_damping))
        self.dx = 0.0
        self.dy = 0.0
        self.d_rot = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.v_rot = 0.0
        self.pose = {
            "FaceAngleX": 0.0,
            "FaceAngleY": 0.0,
            "FaceAngleZ": 0.0,
            "FacePositionX": 0.0,
            "FacePositionY": 0.0,
        }
        self.pose_vel = {k: 0.0 for k in self.pose}
        self.active = False

    def inject_impulse(self, norm_fx: float, norm_fy: float):
        self.active = True
        self.vx = max(-1.6, min(1.6, self.vx + 0.85 * norm_fx))
        self.vy = max(-1.2, min(0.6, self.vy + min(0.28, 0.52 * norm_fy - 0.18 * abs(norm_fx))))
        self.v_rot = max(-120.0, min(120.0, self.v_rot + 64.0 * norm_fx))

        self.pose_vel["FaceAngleX"] += 85.0 * norm_fx
        self.pose_vel["FaceAngleY"] += 68.0 * norm_fy - 38.0 * abs(norm_fx)
        self.pose_vel["FaceAngleZ"] += 72.0 * norm_fx
        self.pose_vel["FacePositionX"] += 14.0 * norm_fx
        self.pose_vel["FacePositionY"] += 11.0 * norm_fy - 6.0 * abs(norm_fx)

    def step(self, dt: float, cont_fx: float = 0.0, cont_fy: float = 0.0) -> bool:
        if math.hypot(cont_fx, cont_fy) > 0.015:
            self.active = True
        if not self.active:
            return False

        ax = 0.22 * cont_fx - self.kr * self.dx - self.cr * self.vx
        ay = 0.14 * cont_fy - self.kr * self.dy - self.cr * self.vy
        a_rot = 18.0 * cont_fx - self.kr * self.d_rot - self.cr * self.v_rot

        self.vx += ax * dt
        self.vy += ay * dt
        self.v_rot += a_rot * dt

        self.dx = max(-0.45, min(0.45, self.dx + self.vx * dt))
        self.dy = max(-0.35, min(0.35, self.dy + self.vy * dt))
        self.d_rot = max(-28.0, min(28.0, self.d_rot + self.v_rot * dt))

        pose_targets = {
            "FaceAngleX": 10.0 * cont_fx,
            "FaceAngleY": 8.0 * cont_fy,
            "FaceAngleZ": 9.0 * cont_fx,
            "FacePositionX": 1.8 * cont_fx,
            "FacePositionY": 1.4 * cont_fy,
        }
        kr_pose = self.kr * 1.15
        cr_pose = self.cr * 1.10
        for k in self.pose:
            disp = self.pose[k]
            vel = self.pose_vel[k]
            acc = pose_targets[k] - kr_pose * disp - cr_pose * vel
            next_vel = vel + acc * dt
            self.pose_vel[k] = next_vel
            self.pose[k] = max(-30.0, min(30.0, disp + next_vel * dt))

        pos_err = math.hypot(self.dx, self.dy) + abs(self.d_rot) * 0.01
        vel_norm = math.hypot(self.vx, self.vy) + abs(self.v_rot) * 0.01
        pose_err = sum(abs(v) for v in self.pose.values())
        if pos_err < 0.0008 and vel_norm < 0.002 and pose_err < 0.04 and math.hypot(cont_fx, cont_fy) < 0.015:
            self.dx = self.dy = self.d_rot = 0.0
            self.vx = self.vy = self.v_rot = 0.0
            for k in self.pose:
                self.pose[k] = 0.0
                self.pose_vel[k] = 0.0
            self.active = False
            return False

        return True



class VTSClient:
    def __init__(self, uri=VTS_URI):
        self.uri = uri
        self.ws = None
        self.run_id = uuid.uuid4().hex
        self.baseline_model_pos = None
        self.baseline_expressions: dict[str, bool] = {}

    async def __aenter__(self):
        self.ws = await websockets.connect(self.uri)
        await self.authenticate()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.ws:
            await self.ws.close()

    async def request(self, msg_type: str, data: dict = None, req_id: str = "ag_req") -> dict:
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": req_id,
            "messageType": msg_type,
            "data": data or {},
        }
        await self.ws.send(json.dumps(payload))
        return json.loads(await self.ws.recv())

    async def authenticate(self):
        if not os.path.exists(TOKEN_PATH):
            print("[*] VTube Studio 화면에서 플러그인 승인(Allow) 버튼을 눌러주세요...", flush=True)
            res = await self.request("AuthenticationTokenRequest", {
                "pluginName": PLUGIN_NAME,
                "pluginDeveloper": PLUGIN_DEV,
            })
            token = res.get("data", {}).get("authenticationToken")
            if not token:
                raise RuntimeError(f"토큰 발급 거부됨: {res}")
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(token)

        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            token = f.read().strip()

        auth_res = await self.request("AuthenticationRequest", {
            "pluginName": PLUGIN_NAME,
            "pluginDeveloper": PLUGIN_DEV,
            "authenticationToken": token,
        })
        if not auth_res.get("data", {}).get("authenticated"):
            os.remove(TOKEN_PATH)
            raise RuntimeError("저장된 토큰 인증 실패. 다시 실행하여 재승인하세요.")

    def _read_state_lock(self) -> dict | None:
        if not os.path.exists(STATE_LOCK_PATH):
            return None
        try:
            with open(STATE_LOCK_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_state_lock(self, in_progress: bool):
        if self.baseline_model_pos is None:
            prev = self._read_state_lock()
            if prev:
                self.baseline_model_pos = prev.get("baseline_model_pos")
                self.baseline_expressions = prev.get("baseline_expressions", {})
        data = {
            "in_progress": in_progress,
            "active_run_id": self.run_id,
            "updated_at": time.time(),
            "baseline_model_pos": self.baseline_model_pos,
            "baseline_expressions": self.baseline_expressions,
        }
        with open(STATE_LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    async def capture_default_state(self, force_fresh: bool = False) -> tuple[dict, ModelHitBox]:
        """
        던지기/애니메이션 수행 직전 모델의 Default State를 확보:
        - 만약 이미 이전 투척/피격 애니메이션이 진행 중(in_progress=True)이라면,
          맞고 있는 도중의 왜곡된 위치/표정을 읽지 않고 기존에 저장된 Default State를 그대로 유지·재사용하며,
          최종 복귀 제어권(active_run_id)만 현재 실행으로 갱신.
        - 진행 중인 애니메이션이 없을 때만 VTS에서 현재 위치·크기·회전 및 표정 상태를 새로 읽어 저장.
        """
        lock = self._read_state_lock()
        now = time.time()
        is_locked = (
            not force_fresh
            and lock is not None
            and lock.get("in_progress") is True
            and (now - float(lock.get("updated_at", 0.0))) < 15.0
            and lock.get("baseline_model_pos") is not None
        )

        if is_locked:
            # 투척/피격 도중 재호출됨 -> 기존 Default State 유지!
            self.baseline_model_pos = lock["baseline_model_pos"]
            self.baseline_expressions = lock.get("baseline_expressions", {})
            self._write_state_lock(in_progress=True)
            hitbox = ModelHitBox.from_model_position(self.baseline_model_pos)
            return self.baseline_model_pos, hitbox

        # 유휴 상태에서 새로 시작됨 -> 현재 VTS 상태(위치 + 표정 + non-string 코어 실루엣)를 새로 읽어 Default State로 기록
        model = (await self.request("CurrentModelRequest")).get("data", {})
        pos = model.get("modelPosition") or {}
        self.baseline_model_pos = {
            "positionX": float(pos.get("positionX", 0.0)),
            "positionY": float(pos.get("positionY", -0.90)),
            "rotation": float(pos.get("rotation", 0.0)),
            "size": float(pos.get("size", -52.0)),
        }

        exp_res = (await self.request("ExpressionStateRequest", {"details": False})).get("data", {})
        self.baseline_expressions = {
            e["file"]: bool(e.get("active", False))
            for e in exp_res.get("expressions", [])
            if e.get("file") and e["file"] not in HIT_EXPRESSION_FILES
        }

        mesh_res = (await self.request("ArtMeshListRequest")).get("data", {})
        l2d_res = (await self.request("Live2DParameterListRequest")).get("data", {})
        core_meshes, excluded_strings = filter_non_string_core_artmeshes(
            mesh_res.get("artMeshNames", []),
            l2d_res.get("parameters", []),
        )

        self._write_state_lock(in_progress=True)
        hitbox = ModelHitBox.from_model_position(
            self.baseline_model_pos,
            core_mesh_count=len(core_meshes),
            excluded_string_count=len(excluded_strings),
        )
        return self.baseline_model_pos, hitbox

    async def reset_to_default_state(
        self,
        reset_delay: float = 0.5,
        restore_position: bool = True,
        restore_expressions: bool = True,
        unload_items: bool = True,
        force: bool = False,
    ) -> dict:
        """
        모든 애니메이션 종료 후 reset_delay(초)만큼 대기한 뒤 모델을 던지기 직전의 Default State로 완전히 복귀.
        단, 대기 도중 새로운 투척 명령이 들어와 active_run_id가 바뀐 경우에는 중간 리셋을 생략하고
        마지막 투척 명령이 끝날 때 원래 Default State로 복귀하도록 위임.
        """
        if reset_delay is None or reset_delay < 0:
            self._write_state_lock(in_progress=False)
            return {"reset": False, "reason": "reset_delay < 0 (disabled)"}

        if reset_delay > 0:
            await asyncio.sleep(reset_delay)

        lock = self._read_state_lock()
        if not force and lock and lock.get("in_progress") and lock.get("active_run_id") != self.run_id:
            return {"reset": False, "reason": "superseded_by_newer_throw"}

        if lock and lock.get("baseline_model_pos") and not self.baseline_model_pos:
            self.baseline_model_pos = lock["baseline_model_pos"]
            self.baseline_expressions = lock.get("baseline_expressions", {})

        # 1. 던지기 직전 표정 상태(baseline_expressions)로 정확히 복원
        if restore_expressions:
            for hit_exp in HIT_EXPRESSION_FILES:
                await self.request("ExpressionActivationRequest", {
                    "expressionFile": hit_exp,
                    "active": False,
                })
            if self.baseline_expressions:
                for exp_file, target_active in self.baseline_expressions.items():
                    if not target_active:
                        await self.request("ExpressionActivationRequest", {
                            "expressionFile": exp_file,
                            "active": False,
                        })
                for exp_file, target_active in self.baseline_expressions.items():
                    if target_active:
                        await self.request("ExpressionActivationRequest", {
                            "expressionFile": exp_file,
                            "active": True,
                        })
            elif force:
                # 강제 초기화 시 모든 활성 표정 해제
                exp_res = (await self.request("ExpressionStateRequest", {"details": False})).get("data", {})
                for e in exp_res.get("expressions", []):
                    if e.get("file") and e.get("active"):
                        await self.request("ExpressionActivationRequest", {
                            "expressionFile": e["file"],
                            "active": False,
                        })

        # 2. 모델 캔버스 트랜스폼을 초기 Default 위치로 정밀 복원
        if restore_position and self.baseline_model_pos:
            await self.request("MoveModelRequest", {
                "timeInSeconds": 0.20,
                "valuesAreRelativeToModel": False,
                "positionX": self.baseline_model_pos["positionX"],
                "positionY": self.baseline_model_pos["positionY"],
                "rotation": self.baseline_model_pos["rotation"],
                "size": self.baseline_model_pos["size"],
            })

        # 3. 주입 파라미터를 VTS 중립값(EyeOpen=1.0, MouthSmile=0.5, Brows=0.5, 나머지=0.0)으로 복원 후 추적 해제
        neutral_params = [
            {"id": "FaceAngleX", "value": 0.0, "weight": 1.0},
            {"id": "FaceAngleY", "value": 0.0, "weight": 1.0},
            {"id": "FaceAngleZ", "value": 0.0, "weight": 1.0},
            {"id": "FacePositionX", "value": 0.0, "weight": 1.0},
            {"id": "FacePositionY", "value": 0.0, "weight": 1.0},
            {"id": "EyeOpenLeft", "value": 1.0, "weight": 1.0},
            {"id": "EyeOpenRight", "value": 1.0, "weight": 1.0},
            {"id": "MouthSmile", "value": 0.5, "weight": 1.0},
            {"id": "MouthOpen", "value": 0.0, "weight": 1.0},
            {"id": "Brows", "value": 0.5, "weight": 1.0},
            {"id": "FaceAngry", "value": 0.0, "weight": 1.0},
            {"id": "CheekPuff", "value": 0.0, "weight": 1.0},
        ]
        await self.request("InjectParameterDataRequest", {
            "faceFound": False,
            "mode": "set",
            "parameterValues": neutral_params,
        })

        # 4. 잔여 스폰 아이템 모두 정리
        if unload_items:
            await self.request("ItemUnloadRequest", {
                "unloadAllInScene": False,
                "unloadAllLoadedByThisPlugin": True,
                "allowUnloadingItemsLoadedByUserOrOtherPlugins": False,
                "instanceIDs": [],
                "fileNames": [],
            })

        self._write_state_lock(in_progress=False)
        return {"reset": True, "reset_delay": reset_delay}

    async def inspect_model(self) -> dict:
        model = await self.request("CurrentModelRequest")
        hotkeys = await self.request("HotkeysInCurrentModelRequest")
        expressions = await self.request("ExpressionStateRequest", {"details": True})
        params = await self.request("InputParameterListRequest")
        return {
            "model": model.get("data"),
            "expressions": expressions.get("data", {}).get("expressions", []),
            "hotkeys": hotkeys.get("data", {}).get("availableHotkeys", []),
            "parameters": params.get("data", {}).get("defaultParameters", []),
        }

    async def trigger_hotkey_by_name(self, name: str, reset_delay: float = None):
        if reset_delay is not None and reset_delay >= 0:
            await self.capture_default_state()
        hotkeys = (await self.request("HotkeysInCurrentModelRequest")).get("data", {}).get("availableHotkeys", [])
        target = next((h for h in hotkeys if h["name"] == name or h["hotkeyID"] == name), None)
        if not target:
            raise ValueError(f"핫키를 찾을 수 없음: {name}")
        res = await self.request("HotkeyTriggerRequest", {"hotkeyID": target["hotkeyID"]})
        if reset_delay is not None and reset_delay >= 0:
            await self.reset_to_default_state(reset_delay=reset_delay)
        return res

    async def set_wind_and_physics(
        self,
        wind: float = 100.0,
        strength: float = 100.0,
        duration: float = 5.0,
        reset_delay: float = None,
    ):
        if reset_delay is not None and reset_delay >= 0:
            await self.capture_default_state()
        phys_info = await self.request("GetCurrentModelPhysicsRequest")
        groups = phys_info.get("data", {}).get("physicsGroups", [])
        if groups:
            strength_overrides = [
                {"id": g.get("groupID") or g.get("id"), "value": strength, "setBaseValue": False, "overrideSeconds": duration}
                for g in groups
            ]
            wind_overrides = [
                {"id": g.get("groupID") or g.get("id"), "value": wind, "setBaseValue": False, "overrideSeconds": duration}
                for g in groups
            ]
        else:
            strength_overrides = [{"id": "", "value": strength, "setBaseValue": True, "overrideSeconds": duration}]
            wind_overrides = [{"id": "", "value": wind, "setBaseValue": True, "overrideSeconds": duration}]
        res = await self.request("SetCurrentModelPhysicsRequest", {
            "strengthOverrides": strength_overrides,
            "windOverrides": wind_overrides,
        })
        if reset_delay is not None and reset_delay >= 0:
            await self.reset_to_default_state(reset_delay=reset_delay)
        return res

    async def play_procedural_wave(
        self,
        preset: str = "headpat",
        duration: float = 3.0,
        fps: int = 30,
        reset_delay: float = 0.5,
    ):
        await self.capture_default_state()
        steps = int(duration * fps)
        last_res = None
        for i in range(steps):
            t = i / fps
            if preset == "headpat":
                press = -8.0 * (1 - math.cos(2 * math.pi * t * 2.0))
                params = [
                    {"id": "FaceAngleY", "value": press, "weight": 1.0},
                    {"id": "FacePositionY", "value": press * 0.15, "weight": 1.0},
                ]
            elif preset == "earthquake":
                shake_x = 12.0 * math.sin(2 * math.pi * t * 12.0)
                shake_y = 6.0 * math.cos(2 * math.pi * t * 15.0)
                params = [
                    {"id": "FaceAngleX", "value": shake_x, "weight": 1.0},
                    {"id": "FaceAngleZ", "value": shake_x * 0.5, "weight": 1.0},
                    {"id": "FacePositionY", "value": shake_y * 0.2, "weight": 1.0},
                ]
            else:
                break

            last_res = await self.request("InjectParameterDataRequest", {
                "faceFound": True,
                "mode": "add",
                "parameterValues": params,
            })
            await asyncio.sleep(1.0 / fps)

        await self.reset_to_default_state(reset_delay=reset_delay)
        return last_res

    async def jump_model(
        self,
        height: float = 0.15,
        jumps: int = 2,
        reset_delay: float = 0.5,
    ):
        await self.capture_default_state()
        for _ in range(jumps):
            await self.request("MoveModelRequest", {
                "timeInSeconds": 0.12, "valuesAreRelativeToModel": True,
                "positionX": 0.0, "positionY": height, "rotation": 0.0, "size": 0.0
            })
            await asyncio.sleep(0.13)
            await self.request("MoveModelRequest", {
                "timeInSeconds": 0.12, "valuesAreRelativeToModel": True,
                "positionX": 0.0, "positionY": -height, "rotation": 0.0, "size": 0.0
            })
            await asyncio.sleep(0.13)

        await self.reset_to_default_state(reset_delay=reset_delay)

    async def spawn_projectile(self, proj: DefaultProjectile) -> str:
        """DefaultProjectile 인스턴스를 캔버스에 스폰하고 instanceID를 할당."""
        res = await self.request("ItemLoadRequest", {
            "fileName": proj.file_name,
            "positionX": proj.x,
            "positionY": proj.y,
            "size": proj.size,
            "rotation": proj.rotation,
            "fadeTime": 0.0,
            "order": proj.order,
            "failIfOrderTaken": False,
        })
        proj.instance_id = res.get("data", {}).get("instanceID")
        return proj.instance_id

    async def sync_projectile_transform(self, proj: DefaultProjectile, dt: float):
        """DefaultProjectile의 본 카르만 타원 변형 및 트랜스폼을 VTube Studio 캔버스에 시각화 동기화."""
        if not proj.instance_id:
            return
        pos_x = proj.x
        pos_y = proj.y
        rot = proj.rotation
        size = proj.size

        is_rigid = proj.softness <= 0.02
        s = 0.0 if is_rigid else max(0.03, min(0.98, proj.softness))

        if not proj.has_hit and not is_rigid:
            # 1) 비행 중: 날아가는 도중의 공기역학적 탄성 진동 및 찌그러짐 (실시간 시각화)
            flight_freq = 7.5 * getattr(proj, "vib_speed", 1.0)
            age = getattr(proj, "age", 0.0)
            q_flight = 0.22 * s * getattr(proj, "amplitude", 1.0) * math.sin(2.0 * math.pi * flight_freq * age)
            v_dir = math.atan2(proj.vy, proj.vx if abs(proj.vx) > 1e-5 else 1e-5)
            body_rot_rad = (rot * math.pi) / 180.0
            delta_phi = body_rot_rad - v_dir
            size = max(0.04, min(1.0, proj.size * (1.0 - 0.45 * q_flight * math.cos(2.0 * delta_phi))))
            rot += 16.0 * q_flight * math.sin(2.0 * delta_phi)
        elif proj.vk_state and proj.vk_state.get("active"):
            vk = proj.vk_state
            t_el = time.time() - vk["t0"]
            if t_el < 2.5:
                damping_rate = vk.get("damping", 3.0)
                env = math.exp(-damping_rate * t_el)
                inst_amp = vk.get("amp", 0.35) * env
                if inst_amp >= 0.005:
                    phase = vk.get("omega", 20.0) * t_el
                    q = inst_amp * math.sin(phase)

                    # 1) 접촉점 앵커 변위 (shiftX, shiftY)
                    lambda_n = max(0.28, min(1.55, 1.0 - q))
                    r0 = getattr(proj, "radius", 0.095)
                    anchor_shift = -r0 * (1.0 - lambda_n)
                    out_nx = vk.get("out_nx", 0.0)
                    out_ny = vk.get("out_ny", 0.0)
                    pos_x += anchor_shift * out_nx
                    pos_y += anchor_shift * out_ny

                    # 2) 2D 투영 타원 겉보기 크기 변조 (vtsSize)
                    contact_angle = math.atan2(out_ny, out_nx) if (out_nx != 0 or out_ny != 0) else 0.0
                    body_rot_rad = (rot * math.pi) / 180.0
                    delta_phi = body_rot_rad - contact_angle
                    size = max(0.04, min(1.0, proj.size * (1.0 - 0.45 * q * math.cos(2.0 * delta_phi))))

                    # 3) 전단 변형 각도 요동 (vtsRot)
                    rot += 18.0 * q * math.sin(2.0 * delta_phi)

        await self.request("ItemMoveRequest", {
            "itemsToMove": [{
                "itemInstanceID": proj.instance_id,
                "timeInSeconds": max(0.016, dt),
                "fadeMode": "linear",
                "positionX": pos_x,
                "positionY": pos_y,
                "size": size,
                "rotation": (rot + 360) % 360,
                "order": -1000,
                "setFlip": False,
                "flip": False,
                "userCanStop": False,
            }]
        })

    async def despawn_projectile(self, proj: DefaultProjectile):
        if not proj.instance_id:
            return
        iid = proj.instance_id
        proj.instance_id = None
        await self.request("ItemUnloadRequest", {
            "unloadAllInScene": False,
            "unloadAllLoadedByThisPlugin": False,
            "allowUnloadingItemsLoadedByUserOrOtherPlugins": True,
            "instanceIDs": [iid],
            "fileNames": [],
        })

    async def resolve_expression_hotkey(self, expression: str) -> dict | None:
        """표정 별칭(shock, cry, angry, heart 등) 또는 핫키 이름/ID로 모델 핫키 객체 탐색."""
        if not expression or expression.lower() == "none":
            return None
        key = expression.lower()
        target_name = EXPRESSION_HOTKEY_MAP.get(key, expression)
        if not target_name:
            return None
        hotkeys = (await self.request("HotkeysInCurrentModelRequest")).get("data", {}).get("availableHotkeys", [])
        return next(
            (
                h for h in hotkeys
                if h.get("name", "").lower() == target_name.lower()
                or h.get("hotkeyID", "").lower() == target_name.lower()
                or h.get("file", "").lower() == target_name.lower()
            ),
            None,
        )

    async def simulate_projectile_hit(
        self,
        file_name: str = DEFAULT_OBJECT_FILE,
        x0: float = -0.95,
        y0: float = -0.05,
        vx0: float = None,
        vy0: float = None,
        gravity: float = 4.80,
        knockback: float = 0.40,
        damping: float = 6.0,
        restore_stiffness: float = 3.5,
        restore_damping: float = 3.0,
        amplitude: float = 1.0,
        vib_speed: float = 1.0,
        expression: str = "shock",
        reset_delay: float = 0.6,
        fps: int = 60,
    ) -> dict:
        """
        1) 던지기 직전 모델의 현재 상태를 새로 읽어 Default State 및 전신 캡슐 히트박스(ModelHitBox) 산출
        2) 초기 속도(vx0, vy0)를 가진 Default 오브젝트를 생성하여 중력 궤적으로 비행
        3) 전신 캡슐 히트박스(ModelHitBox)와 충돌 감지 시 피격 인터랙션 및 Default 위치·포즈 복원 스프링-댐퍼 구동
        4) 애니메이션 종료 후 reset_delay(초) 뒤에 던지기 직전 Default State로 복원
        """
        _, hitbox = await self.capture_default_state()
        visible_ay = max(-0.65, hitbox.ay)
        visible_by = min(0.65, hitbox.by)
        target_x = hitbox.center_x
        target_y = 0.5 * (visible_ay + visible_by)

        t_flight_est = 0.34
        if vx0 is None:
            vx0 = (target_x - x0) / t_flight_est
        if vy0 is None:
            vy0 = (target_y - y0 + 0.5 * gravity * (t_flight_est ** 2)) / t_flight_est

        proj = DefaultProjectile(
            file_name=file_name,
            x=x0,
            y=y0,
            vx=vx0,
            vy=vy0,
            gravity=gravity,
            angular_velocity=600.0 if vx0 >= 0 else -600.0,
            damping=damping,
            amplitude=amplitude,
            vib_speed=vib_speed,
        )
        await self.spawn_projectile(proj)

        dt = 1.0 / fps
        max_flight_steps = int(1.5 * fps)
        impact_force = (vx0 * proj.mass / dt, vy0 * proj.mass / dt)

        # Phase 1: 초기 속도로 날아가는 Default 오브젝트 비행 및 전신 캡슐 히트박스 충돌 판정 루프
        for _ in range(max_flight_steps):
            hitbox.clear_frame_forces()
            proj.step(dt)
            collided, _, fx, fy = hitbox.resolve_collision(proj, dt=dt)
            await self.sync_projectile_transform(proj, dt)
            if collided:
                proj.has_hit = True
                impact_force = (fx, fy)
                break
            if abs(proj.x) > 1.3 or proj.y < -1.2:
                break
            await asyncio.sleep(dt)

        if not proj.has_hit:
            impact_force = proj.apply_bounce(target_x, target_y)

        # Phase 2: 합뉴턴(impact_force) 방향에 따른 피격 인터랙션 및 Default 위치·포즈 약한 복원력(kr)/댐핑(cr) 구동
        anim_duration = max(0.55, min(1.50, 4.5 / max(1.0, damping)))
        animator = HitInteractionAnimator(
            impact_vx=impact_force[0],
            impact_vy=impact_force[1],
            knockback=knockback,
            damping=damping,
            expression=expression,
            duration=anim_duration,
        )
        restore_state = AvatarRestoringState(
            restore_stiffness=restore_stiffness,
            restore_damping=restore_damping,
        )
        restore_state.inject_impulse(animator.norm_fx, animator.norm_fy)

        wind_power = max(-100.0, min(100.0, 68.0 * animator.norm_fx))
        phys_strength = min(100.0, 50.0 + 40.0 * animator.intensity)
        phys_duration = max(0.4, min(1.5, 3.6 / max(1.0, damping)))
        await self.set_wind_and_physics(wind=wind_power, strength=phys_strength, duration=phys_duration)

        exp_hk = await self.resolve_expression_hotkey(expression)
        if exp_hk:
            if exp_hk.get("file", "").endswith(".exp3.json"):
                for prev_file, was_active in self.baseline_expressions.items():
                    if was_active and prev_file != exp_hk["file"]:
                        await self.request("ExpressionActivationRequest", {
                            "expressionFile": prev_file,
                            "active": False,
                        })
                await self.request("ExpressionActivationRequest", {
                    "expressionFile": exp_hk["file"],
                    "active": True,
                })
            else:
                await self.request("HotkeyTriggerRequest", {"hotkeyID": exp_hk["hotkeyID"]})

        base_pos = self.baseline_model_pos or {"positionX": 0.0, "positionY": 0.0, "rotation": 0.0, "size": -52.0}
        anim_steps = int(max(animator.duration, 1.2) * fps)
        for i in range(anim_steps):
            t = i * dt
            hitbox.clear_frame_forces()

            if proj.instance_id:
                proj.step(dt)
                hitbox.resolve_collision(proj, dt=dt)
                floor_y = -0.80
                if proj.y <= floor_y:
                    proj.y = floor_y
                    if abs(proj.vy) > 0.14:
                        floor_rest = 0.32 if proj.softness <= 0.02 else max(0.02, 0.28 * math.pow(1.0 - 0.75 * proj.softness, 1.4))
                        proj.vy = abs(proj.vy) * floor_rest
                        proj.vx *= 0.65
                    else:
                        proj.vy = 0.0
                        proj.vx *= 0.50
                await self.sync_projectile_transform(proj, dt)

            cont_fx = max(-1.2, min(1.2, (hitbox.net_force_x / 35.0) * knockback))
            cont_fy = max(-1.2, min(1.2, (hitbox.net_force_y / 35.0) * knockback))
            if restore_state.step(dt, cont_fx, cont_fy):
                await self.request("MoveModelRequest", {
                    "timeInSeconds": 0.0,
                    "valuesAreRelativeToModel": False,
                    "positionX": float(base_pos["positionX"]) + restore_state.dx,
                    "positionY": float(base_pos["positionY"]) + restore_state.dy,
                    "rotation": float(base_pos["rotation"]) + restore_state.d_rot,
                    "size": float(base_pos["size"]),
                })

            params = animator.sample_parameters(t) if t <= animator.duration else []
            merged = dict(restore_state.pose)
            for p in params:
                pid = p["id"]
                merged[pid] = merged.get(pid, 0.0) + p["value"]
            clamped_params = [
                {"id": pid, "value": max(-30.0, min(30.0, val)), "weight": 1.0}
                for pid, val in merged.items()
            ]
            await self.request("InjectParameterDataRequest", {
                "faceFound": True,
                "mode": "add",
                "parameterValues": clamped_params,
            })
            await asyncio.sleep(dt)

        await self.despawn_projectile(proj)

        # Phase 3: 지정된 reset_delay(초) 경과 후 Default State로 완전 복귀
        reset_info = await self.reset_to_default_state(reset_delay=reset_delay)

        return {
            "status": "ok",
            "settings": {
                "knockback": knockback,
                "damping": damping,
                "restore_stiffness": restore_stiffness,
                "restore_damping": restore_damping,
                "expression": expression,
                "triggered_hotkey": exp_hk.get("name") or exp_hk.get("file") if exp_hk else None,
                "reset_delay": reset_delay,
                "default_state_restored": reset_info.get("reset", False),
            },
            "projectile": {
                "file_name": file_name,
                "mass_kg": proj.mass,
                "initial_pos": {"x": x0, "y": y0},
                "initial_velocity": {"vx": round(vx0, 3), "vy": round(vy0, 3)},
                "impact_force_newton": {"fx": round(impact_force[0], 3), "fy": round(impact_force[1], 3)},
            },
            "hit_target": {
                "capsule_bottom": {"ax": round(hitbox.ax, 3), "ay": round(hitbox.ay, 3)},
                "capsule_top": {"bx": round(hitbox.bx, 3), "by": round(hitbox.by, 3)},
                "capsule_radius": round(hitbox.capsule_radius, 3),
                "core_mesh_count": hitbox.core_mesh_count,
                "excluded_string_count": hitbox.excluded_string_count,
                "scale_factor": round(hitbox.scale_factor, 3),
            },
        }

    async def simulate_multi_projectile_hits(
        self,
        count: int = 5,
        interval: float = 0.12,
        knockback: float = 0.30,
        damping: float = 6.5,
        restore_stiffness: float = 3.5,
        restore_damping: float = 3.0,
        expression: str = "shock",
        reset_delay: float = 0.6,
        fps: int = 60,
    ) -> dict:
        """
        던지기 직전 모델의 현재 상태를 새로 읽어 Default State 및 전신 캡슐 히트박스(ModelHitBox)로 설정한 뒤,
        다중 물체 동시 투척 및 피격 처리(Default 위치·포즈 복원력 및 댐핑 적용) 후 reset_delay(초) 뒤에 Default State로 복귀.
        """
        _, hitbox = await self.capture_default_state()
        visible_ay = max(-0.65, hitbox.ay)
        visible_by = min(0.65, hitbox.by)
        target_x = hitbox.center_x

        dt = 1.0 / fps
        gravity = 4.80
        t_flight_est = 0.34

        spawn_queue = []
        for idx in range(count):
            side = -1.0 if idx % 2 == 0 else 1.0
            x0 = 0.95 * side
            y0 = random.uniform(-0.15, 0.15)
            tx = target_x + random.uniform(-hitbox.half_w * 0.35, hitbox.half_w * 0.35)
            ty = random.uniform(visible_ay, visible_by)
            vx0 = (tx - x0) / t_flight_est
            vy0 = (ty - y0 + 0.5 * gravity * (t_flight_est ** 2)) / t_flight_est
            spawn_queue.append({
                "spawn_time": idx * interval,
                "spawned": False,
                "proj": DefaultProjectile(
                    file_name=random.choice(THROWABLE_ITEMS),
                    x=x0,
                    y=y0,
                    vx=vx0,
                    vy=vy0,
                    gravity=gravity,
                    angular_velocity=600.0 if vx0 >= 0 else -600.0,
                    order=2 + (idx % 26),
                ),
            })

        active_projectiles: list[DefaultProjectile] = []
        active_impulses: list[tuple[float, HitInteractionAnimator]] = []
        restore_state = AvatarRestoringState(
            restore_stiffness=restore_stiffness,
            restore_damping=restore_damping,
        )
        base_pos = self.baseline_model_pos or {"positionX": 0.0, "positionY": 0.0, "rotation": 0.0, "size": -52.0}

        exp_hk = await self.resolve_expression_hotkey(expression)
        exp_triggered = False

        total_duration = (count - 1) * interval + t_flight_est + max(0.8, 4.5 / max(1.0, damping))
        total_steps = int(total_duration * fps)

        for step in range(total_steps):
            now = step * dt

            for item in spawn_queue:
                if not item["spawned"] and now >= item["spawn_time"]:
                    item["spawned"] = True
                    await self.spawn_projectile(item["proj"])
                    active_projectiles.append(item["proj"])

            hitbox.clear_frame_forces()
            items_to_move = []
            to_despawn = []
            for proj in active_projectiles:
                proj.step(dt)
                collided, _, fx, fy = hitbox.resolve_collision(proj, dt=dt)
                if collided and not proj.has_hit:
                    proj.has_hit = True
                    anim = HitInteractionAnimator(
                        impact_vx=fx,
                        impact_vy=fy,
                        knockback=knockback,
                        damping=damping,
                        expression=expression,
                    )
                    active_impulses.append((now, anim))
                    restore_state.inject_impulse(anim.norm_fx, anim.norm_fy)

                    if exp_hk and not exp_triggered:
                        exp_triggered = True
                        if exp_hk.get("file", "").endswith(".exp3.json"):
                            for prev_file, was_active in self.baseline_expressions.items():
                                if was_active and prev_file != exp_hk["file"]:
                                    await self.request("ExpressionActivationRequest", {
                                        "expressionFile": prev_file,
                                        "active": False,
                                    })
                            await self.request("ExpressionActivationRequest", {
                                "expressionFile": exp_hk["file"],
                                "active": True,
                            })
                        else:
                            await self.request("HotkeyTriggerRequest", {"hotkeyID": exp_hk["hotkeyID"]})
                    await self.set_wind_and_physics(
                        wind=max(-100.0, min(100.0, 68.0 * anim.norm_fx)),
                        strength=min(100.0, 50.0 + 35.0 * anim.intensity),
                        duration=0.8,
                    )

                # 바닥 충돌 및 감쇠 바운스
                floor_y = -0.80
                if proj.y <= floor_y:
                    proj.y = floor_y
                    if abs(proj.vy) > 0.14:
                        floor_rest = 0.32 if proj.softness <= 0.02 else max(0.02, 0.28 * math.pow(1.0 - 0.75 * proj.softness, 1.4))
                        proj.vy = abs(proj.vy) * floor_rest
                        proj.vx *= 0.65
                    else:
                        proj.vy = 0.0
                        proj.vx *= 0.50

                if proj.y < -1.15 or abs(proj.x) > 1.40:
                    to_despawn.append(proj)
                elif proj.instance_id:
                    items_to_move.append({
                        "itemInstanceID": proj.instance_id,
                        "timeInSeconds": dt,
                        "fadeMode": "linear",
                        "positionX": proj.x,
                        "positionY": proj.y,
                        "size": proj.size,
                        "rotation": proj.rotation,
                        "order": -1000,
                        "setFlip": False,
                        "flip": False,
                        "userCanStop": False,
                    })

            if items_to_move:
                await self.request("ItemMoveRequest", {"itemsToMove": items_to_move})

            if to_despawn:
                unload_ids = [p.instance_id for p in to_despawn if p.instance_id]
                for p in to_despawn:
                    p.instance_id = None
                    active_projectiles.remove(p)
                if unload_ids:
                    await self.request("ItemUnloadRequest", {
                        "unloadAllInScene": False,
                        "unloadAllLoadedByThisPlugin": False,
                        "allowUnloadingItemsLoadedByUserOrOtherPlugins": True,
                        "instanceIDs": unload_ids,
                        "fileNames": [],
                    })

            cont_fx = max(-1.2, min(1.2, (hitbox.net_force_x / 35.0) * knockback))
            cont_fy = max(-1.2, min(1.2, (hitbox.net_force_y / 35.0) * knockback))
            if restore_state.step(dt, cont_fx, cont_fy):
                await self.request("MoveModelRequest", {
                    "timeInSeconds": 0.0,
                    "valuesAreRelativeToModel": False,
                    "positionX": float(base_pos["positionX"]) + restore_state.dx,
                    "positionY": float(base_pos["positionY"]) + restore_state.dy,
                    "rotation": float(base_pos["rotation"]) + restore_state.d_rot,
                    "size": float(base_pos["size"]),
                })

            if active_impulses or restore_state.active:
                merged: dict[str, float] = dict(restore_state.pose)
                for hit_t, anim in active_impulses:
                    elapsed = now - hit_t
                    if 0.0 <= elapsed <= anim.duration:
                        for p in anim.sample_parameters(elapsed):
                            pid = p["id"]
                            merged[pid] = merged.get(pid, 0.0) + p["value"]

                if merged:
                    clamped_params = [
                        {"id": pid, "value": max(-30.0, min(30.0, val)), "weight": 1.0}
                        for pid, val in merged.items()
                    ]
                    await self.request("InjectParameterDataRequest", {
                        "faceFound": True,
                        "mode": "add",
                        "parameterValues": clamped_params,
                    })

            await asyncio.sleep(dt)

        # 지정된 reset_delay(초) 경과 후 Default State로 완전 복귀
        reset_info = await self.reset_to_default_state(reset_delay=reset_delay)

        return {
            "status": "ok",
            "count": count,
            "total_hits": len(active_impulses),
            "settings": {
                "knockback": knockback,
                "damping": damping,
                "expression": expression,
                "reset_delay": reset_delay,
                "default_state_restored": reset_info.get("reset", False),
            },
        }

    async def simulate_avalanche_pile(
        self,
        count: int = 18,
        interval: float = 0.055,
        knockback: float = 0.25,
        damping: float = 6.5,
        restore_stiffness: float = 3.5,
        restore_damping: float = 3.0,
        expression: str = "cry",
        reset_delay: float = 1.5,
        fps: int = 60,
    ) -> dict:
        """
        물체가 화면 위에서 와르르 쏟아져 아바타 전신 캡슐 히트박스에 부딪히고
        바닥과 서로의 위에 차곡차곡 쌓여 있다가 reset_delay(초) 후 Default State로 복귀하는 애니메이션.
        인터랙션 중 모델이 Default 위치/포즈에서 멀어지면 약한 복원력(kr)과 댐핑(cr)이 상시 작용.
        """
        _, hitbox = await self.capture_default_state()
        dt = 1.0 / fps
        gravity = 3.40
        floor_y = -0.82
        wall_x = 0.88

        list_res = await self.request("ItemListRequest", {
            "includeAvailableSpots": True,
            "includeItemInstancesInScene": True,
            "includeAvailableItemFiles": False,
        })
        list_data = list_res.get("data", {})
        total_allowed = int(list_data.get("totalItemsAllowedCount", 60))
        in_scene = int(list_data.get("itemsInSceneCount", 0))
        max_spawnable = max(0, min(MAX_FRONT_ORDER_SLOTS, total_allowed - in_scene))
        count = max(0, min(count, max_spawnable))

        spawn_queue = []
        for idx in range(count):
            x0 = max(-0.75, min(0.75, hitbox.center_x + random.uniform(-0.42, 0.42)))
            y0 = 1.08 + random.uniform(0.0, 0.25)
            vx0 = random.uniform(-0.22, 0.22)
            vy0 = random.uniform(-1.4, -0.6)
            spawn_queue.append({
                "spawn_time": idx * interval,
                "spawned": False,
                "settled": False,
                "still_time": 0.0,
                "age": 0.0,
                "radius": 0.11,
                "proj": DefaultProjectile(
                    file_name=random.choice(THROWABLE_ITEMS),
                    x=x0,
                    y=y0,
                    vx=vx0,
                    vy=vy0,
                    gravity=gravity,
                    rotation=random.uniform(0.0, 360.0),
                    angular_velocity=random.uniform(-520.0, 520.0),
                    restitution=0.22,
                    order=2 + (idx % MAX_FRONT_ORDER_SLOTS),
                ),
            })

        active_entries: list[dict] = []
        active_impulses: list[tuple[float, HitInteractionAnimator]] = []
        restore_state = AvatarRestoringState(
            restore_stiffness=restore_stiffness,
            restore_damping=restore_damping,
        )
        base_pos = self.baseline_model_pos or {"positionX": 0.0, "positionY": 0.0, "rotation": 0.0, "size": -52.0}

        exp_hk = await self.resolve_expression_hotkey(expression)
        exp_triggered = False

        max_sim_duration = max(0, count - 1) * interval + 2.4
        max_steps = int(max_sim_duration * fps)

        for step in range(max_steps):
            now = step * dt

            for entry in spawn_queue:
                if not entry["spawned"] and now >= entry["spawn_time"]:
                    entry["spawned"] = True
                    if len(active_entries) >= max_spawnable:
                        continue
                    iid = await self.spawn_projectile(entry["proj"])
                    if iid:
                        active_entries.append(entry)

            hitbox.clear_frame_forces()

            # 1. 중력 낙하 및 아바타 전신 캡슐 히트박스/바닥/벽 상시 충돌 해결
            for entry in active_entries:
                p = entry["proj"]
                entry["age"] += dt
                if not entry["settled"]:
                    p.step(dt)

                collided, norm_speed, fx, fy = hitbox.resolve_collision(p, radius_override=entry["radius"], dt=dt)
                if collided:
                    entry["settled"] = False
                    entry["still_time"] = 0.0
                    can_trigger = (not p.has_hit) or (norm_speed > 0.55 and (now - entry.get("last_hit_t", 0.0)) > 0.25)
                    if can_trigger:
                        is_first = not p.has_hit
                        p.has_hit = True
                        entry["last_hit_t"] = now
                        kb_eff = knockback if is_first else knockback * 0.45
                        anim = HitInteractionAnimator(
                            impact_vx=fx,
                            impact_vy=fy,
                            knockback=kb_eff,
                            damping=damping,
                            expression=expression,
                        )
                        active_impulses.append((now, anim))
                        restore_state.inject_impulse(anim.norm_fx, anim.norm_fy)

                        if exp_hk and not exp_triggered:
                            exp_triggered = True
                            if exp_hk.get("file", "").endswith(".exp3.json"):
                                for prev_file, was_active in self.baseline_expressions.items():
                                    if was_active and prev_file != exp_hk["file"]:
                                        await self.request("ExpressionActivationRequest", {
                                            "expressionFile": prev_file,
                                            "active": False,
                                        })
                                await self.request("ExpressionActivationRequest", {
                                    "expressionFile": exp_hk["file"],
                                    "active": True,
                                })
                            else:
                                await self.request("HotkeyTriggerRequest", {"hotkeyID": exp_hk["hotkeyID"]})

                if p.y < floor_y:
                    p.y = floor_y
                    p.vy = -p.vy * p.restitution
                    p.vx *= 0.78
                    p.angular_velocity *= 0.70
                if p.x < -wall_x:
                    p.x = -wall_x
                    p.vx = abs(p.vx) * 0.5
                elif p.x > wall_x:
                    p.x = wall_x
                    p.vx = -abs(p.vx) * 0.5

            # 2. 질량 가중 물체 간 상호 충돌 + 아바타 전신 캡슐 히트박스 재충돌 해결 (3회 반복)
            for _ in range(3):
                n = len(active_entries)
                for i in range(n):
                    for j in range(i + 1, n):
                        a = active_entries[i]["proj"]
                        b = active_entries[j]["proj"]
                        min_dist = active_entries[i]["radius"] + active_entries[j]["radius"]
                        dx = b.x - a.x
                        dy = b.y - a.y
                        dist = math.hypot(dx, dy)
                        if 0.0001 < dist < min_dist:
                            overlap = min_dist - dist
                            nx = dx / dist
                            ny = dy / dist
                            ma = a.mass * 8.0 if a.y <= floor_y + 0.01 else a.mass
                            mb = b.mass * 8.0 if b.y <= floor_y + 0.01 else b.mass
                            inv_a = 1.0 / ma
                            inv_b = 1.0 / mb
                            inv_sum = inv_a + inv_b
                            weight_a = inv_a / inv_sum
                            weight_b = inv_b / inv_sum
                            a.x -= nx * overlap * weight_a
                            a.y = max(floor_y, a.y - ny * overlap * weight_a)
                            b.x += nx * overlap * weight_b
                            b.y = max(floor_y, b.y + ny * overlap * weight_b)

                            rvx = b.vx - a.vx
                            rvy = b.vy - a.vy
                            vel_normal = rvx * nx + rvy * ny
                            if vel_normal < 0:
                                j_imp = -(1.0 + 0.18) * vel_normal / inv_sum
                                a.vx -= j_imp * nx * inv_a
                                a.vy -= j_imp * ny * inv_a
                                b.vx += j_imp * nx * inv_b
                                b.vy += j_imp * ny * inv_b
                                a.vx *= 0.88
                                b.vx *= 0.88
                                a.angular_velocity *= 0.75
                                b.angular_velocity *= 0.75

                    pi = active_entries[i]["proj"]
                    hitbox.resolve_collision(pi, radius_override=active_entries[i]["radius"], dt=dt)
                    if pi.y < floor_y:
                        pi.y = floor_y
                    if pi.x < -wall_x:
                        pi.x = -wall_x
                    elif pi.x > wall_x:
                        pi.x = wall_x

            # 3. 안착 판정 및 단일 배치 이동 패킷 전송
            items_to_move = []
            all_settled = all(e["spawned"] for e in spawn_queue)
            for entry in active_entries:
                p = entry["proj"]
                if not entry["settled"]:
                    speed = math.hypot(p.vx, p.vy)
                    if (speed < 0.08 and p.y <= 0.25) or entry["age"] > 2.0:
                        entry["still_time"] += dt
                        if entry["still_time"] > 0.25 or entry["age"] > 2.0:
                            p.vx = 0.0
                            p.vy = 0.0
                            p.angular_velocity = 0.0
                            entry["settled"] = True
                    else:
                        entry["still_time"] = 0.0
                if not entry["settled"]:
                    all_settled = False

                if p.instance_id:
                    items_to_move.append({
                        "itemInstanceID": p.instance_id,
                        "timeInSeconds": 0.0,
                        "fadeMode": "linear",
                        "positionX": p.x,
                        "positionY": p.y,
                        "size": p.size,
                        "rotation": p.rotation % 360.0,
                        "order": -1000,
                        "setFlip": False,
                        "flip": False,
                        "userCanStop": False,
                    })

            if items_to_move:
                await self.request("ItemMoveRequest", {"itemsToMove": items_to_move})

            cont_fx = max(-1.2, min(1.2, (hitbox.net_force_x / 35.0) * knockback))
            cont_fy = max(-1.2, min(1.2, (hitbox.net_force_y / 35.0) * knockback))
            if restore_state.step(dt, cont_fx, cont_fy):
                await self.request("MoveModelRequest", {
                    "timeInSeconds": 0.0,
                    "valuesAreRelativeToModel": False,
                    "positionX": float(base_pos["positionX"]) + restore_state.dx,
                    "positionY": float(base_pos["positionY"]) + restore_state.dy,
                    "rotation": float(base_pos["rotation"]) + restore_state.d_rot,
                    "size": float(base_pos["size"]),
                })

            if active_impulses or restore_state.active:
                merged: dict[str, float] = dict(restore_state.pose)
                for hit_t, anim in active_impulses:
                    elapsed = now - hit_t
                    if 0.0 <= elapsed <= anim.duration:
                        for param in anim.sample_parameters(elapsed):
                            pid = param["id"]
                            merged[pid] = merged.get(pid, 0.0) + param["value"]
                if merged:
                    clamped_params = [
                        {"id": pid, "value": max(-30.0, min(30.0, val)), "weight": 1.0}
                        for pid, val in merged.items()
                    ]
                    await self.request("InjectParameterDataRequest", {
                        "faceFound": True,
                        "mode": "add",
                        "parameterValues": clamped_params,
                    })

            if all_settled and not restore_state.active and not any(0.0 <= (now - ht) <= an.duration for ht, an in active_impulses):
                break

            await asyncio.sleep(dt)

        # 쌓인 상태로 reset_delay(초)만큼 유지한 뒤 Default State 복귀 및 일괄 언로드
        reset_info = await self.reset_to_default_state(reset_delay=reset_delay)
        return {
            "status": "ok",
            "mode": "avalanche_pile",
            "count": count,
            "total_hits": len(active_impulses),
            "reset_delay": reset_delay,
            "default_state_restored": reset_info.get("reset", False),
        }

    async def trigger_bound_action(self, action_key: str, reset_delay: float = 0.6) -> dict:
        preset_map = {
            "hit": lambda: self.simulate_projectile_hit(reset_delay=reset_delay),
            "hit_right": lambda: self.simulate_projectile_hit(x0=0.95, y0=-0.05, reset_delay=reset_delay),
            "headpat": lambda: self.play_procedural_wave("headpat", 3.0, reset_delay=reset_delay),
            "earthquake": lambda: self.play_procedural_wave("earthquake", 2.5, reset_delay=reset_delay),
            "wind": lambda: self.set_wind_and_physics(100.0, 100.0, 5.0, reset_delay=reset_delay),
            "jump": lambda: self.jump_model(0.15, 2, reset_delay=reset_delay),
            "heart": lambda: self.trigger_hotkey_by_name("Heart Eyes", reset_delay=reset_delay),
            "cry": lambda: self.trigger_hotkey_by_name("Eyes Cry", reset_delay=reset_delay),
            "angry": lambda: self.trigger_hotkey_by_name("Angry Sign", reset_delay=reset_delay),
            "shock": lambda: self.trigger_hotkey_by_name("Shock Sign", reset_delay=reset_delay),
            "reset": lambda: self.reset_to_default_state(reset_delay=0.0),
            "love_anim": lambda: self.trigger_hotkey_by_name("Anim Shake", reset_delay=reset_delay),
            "shock_anim": lambda: self.trigger_hotkey_by_name("e416a7d797c94bcbaa4a453d6cb1bae0", reset_delay=reset_delay),
        }
        if action_key in preset_map:
            res = await preset_map[action_key]()
            return {"status": "ok", "action": action_key, "reset_delay": reset_delay, "response": res}
        res = await self.trigger_hotkey_by_name(action_key, reset_delay=reset_delay)
        return {"status": "ok", "action": action_key, "reset_delay": reset_delay, "response": res}


async def cli_main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "inspect"

    async with VTSClient() as vts:
        if cmd in ("--init-auth", "init-auth"):
            print("[+] VTube Studio 인증 완료 (.vts_token 저장됨)")
        elif cmd == "inspect":
            info = await vts.inspect_model()
            print(json.dumps(info, indent=2, ensure_ascii=False))
        elif cmd == "reset":
            delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
            res = await vts.reset_to_default_state(reset_delay=delay, force=True)
            print("[+] Default State 복원 완료:", json.dumps(res, ensure_ascii=False))
        elif cmd == "wind":
            delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
            res = await vts.set_wind_and_physics(100.0, 100.0, 5.0, reset_delay=delay)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif cmd == "headpat":
            delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
            res = await vts.play_procedural_wave("headpat", 3.0, reset_delay=delay)
            print("[+] headpat 파형 주입 및 복원 완료:", json.dumps(res, ensure_ascii=False))
        elif cmd == "earthquake":
            delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
            res = await vts.play_procedural_wave("earthquake", 2.5, reset_delay=delay)
            print("[+] earthquake 파형 주입 및 복원 완료:", json.dumps(res, ensure_ascii=False))
        elif cmd == "jump":
            delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
            await vts.jump_model(reset_delay=delay)
            print("[+] jump 및 복원 완료")
        elif cmd in ("hit", "throw"):
            parser = argparse.ArgumentParser(prog="vts_agent_ctl.py hit")
            parser.add_argument("pos_args", nargs="*", help="[knockback] [damping] [expression] [reset_delay]")
            parser.add_argument("-k", "--knockback", type=float, default=None, help="넉백 세기 배율 (기본값: 0.40)")
            parser.add_argument("-d", "--damping", type=float, default=None, help="감쇠 계수 (기본값: 6.0)")
            parser.add_argument("--kr", "--restore-stiffness", dest="kr", type=float, default=3.5, help="모델 복원력 세기 (기본값: 3.5)")
            parser.add_argument("--cr", "--restore-damping", dest="cr", type=float, default=3.0, help="모델 복원 댐핑 (기본값: 3.0)")
            parser.add_argument("-e", "--expression", type=str, default=None, help="피격 표정 (shock, cry, angry, heart, shock_anim, none)")
            parser.add_argument("-r", "--reset-delay", type=float, default=None, help="애니메이션 종료 후 Default State 복귀 지연 시간(초, 기본값: 0.6, -1이면 복귀 안 함)")
            parser.add_argument("--vx", type=float, default=None, help="초기 수평 속도 vx0")
            parser.add_argument("--vy", type=float, default=None, help="초기 수직 속도 vy0")
            parser.add_argument("--item", type=str, default=DEFAULT_OBJECT_FILE, help="투척 오브젝트 파일명")
            parser.add_argument("--amp", type=float, default=1.0, help="진동 진폭 배율 (기본값: 1.0)")
            parser.add_argument("--vib-speed", type=float, default=1.0, help="진동 애니메이션 배속 (기본값: 1.0)")
            args = parser.parse_args(sys.argv[2:])

            kb = args.knockback if args.knockback is not None else (float(args.pos_args[0]) if len(args.pos_args) > 0 else 0.40)
            dmp = args.damping if args.damping is not None else (float(args.pos_args[1]) if len(args.pos_args) > 1 else 6.0)
            exp = args.expression if args.expression is not None else (args.pos_args[2] if len(args.pos_args) > 2 else "shock")
            r_delay = args.reset_delay if args.reset_delay is not None else (float(args.pos_args[3]) if len(args.pos_args) > 3 else 0.6)

            res = await vts.simulate_projectile_hit(
                file_name=args.item,
                vx0=args.vx,
                vy0=args.vy,
                knockback=kb,
                damping=dmp,
                restore_stiffness=args.kr,
                restore_damping=args.cr,
                amplitude=args.amp,
                vib_speed=args.vib_speed,
                expression=exp,
                reset_delay=r_delay,
            )
            print("[+] 피격 시뮬레이션 완료:", json.dumps(res, ensure_ascii=False))
        elif cmd == "barrage":
            parser = argparse.ArgumentParser(prog="vts_agent_ctl.py barrage")
            parser.add_argument("count", nargs="?", type=int, default=5, help="동시/연속 투척 물체 개수")
            parser.add_argument("-k", "--knockback", type=float, default=0.30, help="개별 물체 넉백 세기")
            parser.add_argument("-d", "--damping", type=float, default=6.5, help="감쇠 계수")
            parser.add_argument("--kr", "--restore-stiffness", dest="kr", type=float, default=3.5, help="모델 복원력 세기 (기본값: 3.5)")
            parser.add_argument("--cr", "--restore-damping", dest="cr", type=float, default=3.0, help="모델 복원 댐핑 (기본값: 3.0)")
            parser.add_argument("-e", "--expression", type=str, default="shock", help="피격 표정")
            parser.add_argument("-i", "--interval", type=float, default=0.12, help="투척 간격(초)")
            parser.add_argument("-r", "--reset-delay", type=float, default=0.6, help="애니메이션 종료 후 Default State 복귀 지연 시간(초)")
            args = parser.parse_args(sys.argv[2:])
            res = await vts.simulate_multi_projectile_hits(
                count=args.count,
                interval=args.interval,
                knockback=args.knockback,
                damping=args.damping,
                restore_stiffness=args.kr,
                restore_damping=args.cr,
                expression=args.expression,
                reset_delay=args.reset_delay,
            )
            print("[+] 다중 피격 시뮬레이션 완료:", json.dumps(res, ensure_ascii=False))
        elif cmd in ("avalanche", "pile"):
            parser = argparse.ArgumentParser(prog="vts_agent_ctl.py avalanche")
            parser.add_argument("count", nargs="?", type=int, default=18, help="쏟아지는 물체 개수 (기본값: 18)")
            parser.add_argument("-k", "--knockback", type=float, default=0.25, help="개별 물체 넉백 세기")
            parser.add_argument("-d", "--damping", type=float, default=6.5, help="감쇠 계수")
            parser.add_argument("--kr", "--restore-stiffness", dest="kr", type=float, default=3.5, help="모델 복원력 세기 (기본값: 3.5)")
            parser.add_argument("--cr", "--restore-damping", dest="cr", type=float, default=3.0, help="모델 복원 댐핑 (기본값: 3.0)")
            parser.add_argument("-e", "--expression", type=str, default="cry", help="피격 표정")
            parser.add_argument("-i", "--interval", type=float, default=0.055, help="낙하 간격(초)")
            parser.add_argument("-r", "--reset-delay", type=float, default=1.5, help="다 쌓인 후 유지 시간(초, 기본값: 1.5)")
            args = parser.parse_args(sys.argv[2:])
            res = await vts.simulate_avalanche_pile(
                count=args.count,
                interval=args.interval,
                knockback=args.knockback,
                damping=args.damping,
                restore_stiffness=args.kr,
                restore_damping=args.cr,
                expression=args.expression,
                reset_delay=args.reset_delay,
            )
            print("[+] 와르르 적층 시뮬레이션 완료:", json.dumps(res, ensure_ascii=False))
        elif cmd == "hotkey" and len(sys.argv) > 2:
            delay = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
            res = await vts.trigger_hotkey_by_name(sys.argv[2], reset_delay=delay)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif cmd == "trigger" and len(sys.argv) > 2:
            delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.6
            res = await vts.trigger_bound_action(sys.argv[2], reset_delay=delay)
            print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(cli_main())
