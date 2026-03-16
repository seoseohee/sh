"""
ecc_core/tools.py

Claude Code의 도구 체계를 임베디드용으로 재설계.

변경 이력:
  v2 — remember 도구 추가 (Semantic Memory 영속 저장)
       에이전트가 발견한 물리 제약/하드웨어 사실을 세션 간 보존.
"""

# ─────────────────────────────────────────────────────────────
# 도구 스키마 정의
# ─────────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [

    # ── 0. ssh_connect ─────────────────────────────────────────
    {
        "name": "ssh_connect",
        "description": """SSH로 보드에 연결한다. 연결이 없는 상태에서 모든 작업의 첫 번째 단계.

bash/script/probe 등 다른 도구를 쓰기 전에 반드시 연결이 되어 있어야 한다.
연결되지 않은 상태에서 다른 도구를 호출하면 [no connection] 에러가 반환된다.

연결 전략:
1. 힌트가 있으면 그 IP/user부터 시도
2. known_hosts, mDNS (.local 도메인) 시도
3. 로컬 서브넷 스캔
4. 연결 성공 후 probe all로 환경 파악""",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "보드 IP 또는 hostname. 모르면 'scan'을 입력하면 자동 탐색."
                },
                "user": {
                    "type": "string",
                    "description": "SSH 사용자. 기본: ECC_USERS 환경변수 순서대로 시도.",
                    "default": ""
                },
                "port": {
                    "type": "integer",
                    "description": "SSH 포트. 기본: 22.",
                    "default": 22
                }
            },
            "required": ["host"]
        }
    },

    # ── 1. bash ────────────────────────────────────────────────
    {
        "name": "bash",
        "description": """SSH를 통해 보드에서 셸 명령을 실행한다.

사용 지침:
- 독립적인 여러 정보가 필요하면 && 로 한 명령에 묶어라
- 멀티라인 스크립트는 script 도구를 써라
- 오래 걸리는 스캔/빌드는 background=true로 띄워라""",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "실행할 셸 명령."
                },
                "timeout": {
                    "type": "integer",
                    "description": "타임아웃(초). 기본 30.",
                    "default": 30
                },
                "background": {
                    "type": "boolean",
                    "description": "true이면 백그라운드 실행 후 task_id 반환.",
                    "default": False
                },
                "description": {
                    "type": "string",
                    "description": "이 명령이 하는 일 (5~10단어)."
                }
            },
            "required": ["command", "description"]
        }
    },

    # ── 1b. bash_wait ──────────────────────────────────────────
    {
        "name": "bash_wait",
        "description": """background=true로 실행한 bash 명령의 결과를 수집한다.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "bash(background=true)가 반환한 task_id"
                },
                "timeout": {
                    "type": "integer",
                    "description": "최대 대기 시간(초). 기본 120.",
                    "default": 120
                },
                "description": {
                    "type": "string",
                    "description": "수집 목적 요약"
                }
            },
            "required": ["task_id"]
        }
    },

    # ── 2. script ──────────────────────────────────────────────
    {
        "name": "script",
        "description": """멀티라인 스크립트를 보드에 파일로 업로드하고 실행한다.

다음 경우에 bash 대신 사용:
- ROS2 source 체인 등 환경변수가 여러 줄에 걸쳐 유지돼야 할 때
- Python, C 등 다른 언어로 하드웨어 제어 코드를 작성할 때
- 복잡한 로직 (루프, 조건문, 에러 핸들링)""",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "실행할 스크립트 전체 내용"},
                "interpreter": {
                    "type": "string",
                    "description": "인터프리터. 예: 'bash', 'python3'",
                    "default": "bash"
                },
                "timeout": {"type": "integer", "description": "타임아웃(초). 기본 60.", "default": 60},
                "description": {"type": "string", "description": "스크립트 목적 요약"}
            },
            "required": ["code", "description"]
        }
    },

    # ── 3. read ────────────────────────────────────────────────
    {
        "name": "read",
        "description": "보드의 파일 내용을 읽는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "읽을 파일의 절대 경로"},
                "head_lines": {"type": "integer", "description": "앞에서 N줄만 읽기 (0 = 전체)", "default": 0},
                "tail_lines": {"type": "integer", "description": "뒤에서 N줄만 읽기 (0 = 전체)", "default": 0}
            },
            "required": ["path"]
        }
    },

    # ── 4. write ───────────────────────────────────────────────
    {
        "name": "write",
        "description": "보드에 파일을 생성하거나 덮어쓴다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "생성할 파일의 절대 경로"},
                "content": {"type": "string", "description": "파일 내용"},
                "mode": {"type": "string", "description": "파일 권한 (예: '755'). 빈 문자열이면 기본값.", "default": ""}
            },
            "required": ["path", "content"]
        }
    },

    # ── 5. glob ────────────────────────────────────────────────
    {
        "name": "glob",
        "description": "보드에서 파일을 패턴으로 검색한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 패턴. 예: '/dev/tty*'"},
                "base_dir": {"type": "string", "description": "검색 시작 디렉터리", "default": "/"}
            },
            "required": ["pattern"]
        }
    },

    # ── 6. grep ────────────────────────────────────────────────
    {
        "name": "grep",
        "description": "보드의 파일에서 패턴을 검색한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "검색할 정규식 또는 고정 문자열"},
                "path": {"type": "string", "description": "검색할 파일 또는 디렉터리 경로"},
                "flags": {"type": "string", "description": "grep 플래그.", "default": "-rn"},
                "max_results": {"type": "integer", "description": "최대 결과 수", "default": 50}
            },
            "required": ["pattern", "path"]
        }
    },

    # ── 7. probe ───────────────────────────────────────────────
    {
        "name": "probe",
        "description": """보드의 하드웨어/소프트웨어 환경을 체계적으로 탐지한다.

탐지 가능한 항목:
- all:           전체 환경 요약
- hw:            연결된 하드웨어 (USB, I2C, SPI, GPIO, 시리얼)
- sw:            설치된 소프트웨어 (ROS2, Python 패키지, 서비스)
- net:           네트워크 인터페이스, 포트
- perf:          CPU/메모리/온도/전원
- motors:        모터 컨트롤러
- camera:        카메라 디바이스
- lidar:         LiDAR 센서
- parallel_scan: 동일 네트워크 IP 병렬 스캔""",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "탐지 대상",
                    "enum": ["all", "hw", "sw", "net", "perf", "motors", "camera", "lidar", "parallel_scan"]
                }
            },
            "required": ["target"]
        }
    },

    # ── 8. serial_open ─────────────────────────────────────────
    {
        "name": "serial_open",
        "description": "보드에 연결된 시리얼 장치와 통신 세션을 연다. 반환값: session_id",
        "input_schema": {
            "type": "object",
            "properties": {
                "port": {"type": "string", "description": "시리얼 포트 경로. 예: /dev/ttyACM0"},
                "baudrate": {"type": "integer", "description": "통신 속도. 기본 115200", "default": 115200},
                "timeout": {"type": "number", "description": "읽기 타임아웃(초). 기본 1.0", "default": 1.0},
                "description": {"type": "string", "description": "이 장치가 무엇인지"}
            },
            "required": ["port"]
        }
    },

    # ── 9. serial_send ─────────────────────────────────────────
    {
        "name": "serial_send",
        "description": "열린 시리얼 세션으로 데이터를 전송하고 응답을 받는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "serial_open이 반환한 session_id"},
                "data": {"type": "string", "description": "전송할 데이터."},
                "expect": {"type": "string", "description": "응답에서 기다릴 문자열.", "default": ""},
                "timeout": {"type": "number", "description": "응답 대기 최대 시간(초). 기본 2.0", "default": 2.0},
                "hex_encode": {"type": "boolean", "description": "true면 data를 hex 바이트열로 파싱해서 전송.", "default": False}
            },
            "required": ["session_id", "data"]
        }
    },

    # ── 10. serial_close ───────────────────────────────────────
    {
        "name": "serial_close",
        "description": "시리얼 세션을 닫는다. session_id 없이 호출하면 전체 닫기.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "닫을 session_id. 미지정 시 전체 닫기."}
            },
            "required": []
        }
    },

    # ── 11. todo ───────────────────────────────────────────────
    {
        "name": "todo",
        "description": """작업 계획을 체크리스트로 관리한다. Claude Code의 TodoWrite/TodoRead와 동일.

복잡한 goal을 받으면 먼저 단계를 나눠라.
각 단계를 시작할 때 in_progress, 끝나면 completed로 업데이트해라.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "전체 todo 목록 (항상 전체를 넘겨라)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":       {"type": "string"},
                            "content":  {"type": "string"},
                            "status":   {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            "priority": {"type": "string", "enum": ["high", "medium", "low"], "default": "medium"}
                        },
                        "required": ["id", "content", "status"]
                    }
                }
            },
            "required": ["todos"]
        }
    },

    # ── 12. remember ───────────────────────────────────────────
    {
        "name": "remember",
        "description": """발견한 사실을 Semantic Memory에 영속 저장한다.

probe/verify/bash 결과에서 중요한 사실을 발견하면 즉시 호출한다.
세션이 끊기거나 컨텍스트가 압축되어도 보존되며,
같은 보드에 다음 세션에서 연결하면 자동으로 복원된다.

언제 써야 하는가:
  물리 한계 발견 시: remember(namespace='constraints', key='min_erpm', value=2000)
  하드웨어 확인 시: remember(namespace='hardware', key='motor_topic', value='/cmd_vel')
  통신 설정 확인 시: remember(namespace='protocol', key='baud_rate', value=115200)
  실패 이력 기록 시: remember(namespace='failed', key='pub_once_loop', value='ARG_MAX 초과')
  검증된 스크립트: remember(namespace='skill', key='vesc_read', value='import serial...')

언제 쓰지 않는가:
  일시적 상태값 (현재 속도, 센서 순간값 등)
  probe/bash로 5초 내 재확인 가능한 정보

namespace 가이드:
  hardware    — 디바이스 경로, topic명, ROS 환경 (ros_distro, serial_port 등)
  protocol    — baud rate, QoS, 통신 파라미터
  constraints — 물리 한계 (min_erpm, max_speed, deadband 등)
  failed      — 실패한 접근법 (재시도 방지)
  skill       — 재사용 가능한 검증된 스크립트""",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "enum": ["hardware", "protocol", "constraints", "failed", "skill"],
                    "description": "저장 카테고리"
                },
                "key": {
                    "type": "string",
                    "description": "사실의 이름. 예: min_erpm, baud_rate, motor_topic"
                },
                "value": {
                    "description": "저장할 값. 문자열/숫자/리스트 모두 가능. 예: 2000, '/cmd_vel', 115200"
                }
            },
            "required": ["namespace", "key", "value"]
        }
    },

    # ── 13. subagent ───────────────────────────────────────────
    {
        "name": "subagent",
        "description": """독립 컨텍스트에서 순수 탐색/조사 작업을 실행한다.

⚠️  실행(제어, 설정, 물리 동작)은 메인 에이전트가 직접 해야 한다.
subagent는 "뭐가 있는지 알아봐줘" 전용이다.

올바른 사용:
  ✅ subagent("이 보드에 어떤 모터 컨트롤러가 있는지 조사해라")
잘못된 사용:
  ❌ subagent("모터를 0.1 m/s로 달려라")""",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "subagent가 달성해야 할 목표."
                },
                "context": {
                    "type": "string",
                    "description": "이미 발견한 정보. device 경로, 파라미터, IP 등.",
                    "default": ""
                }
            },
            "required": ["goal"]
        }
    },

    # ── 14. verify ─────────────────────────────────────────────
    {
        "name": "verify",
        "description": """probe(존재 확인)와 달리, 컴포넌트가 실제로 동작하는지 확인한다.

target 종류:
- serial_device:  시리얼 장치 통신 확인
- i2c_device:     I2C 주소 응답 확인
- network_device: IP 응답 + 포트 확인
- ros2_topic:     토픽 실제 데이터 발행 확인
- process:        프로세스/서비스 실행 확인
- system:         전체 시스템 상태 확인
- custom:         자유형 확인""",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "확인 유형",
                    "enum": ["serial_device", "i2c_device", "network_device",
                             "ros2_topic", "process", "system", "custom"]
                },
                "device": {
                    "type": "string",
                    "description": "확인 대상. target에 따라 다름.",
                    "default": ""
                }
            },
            "required": ["target"]
        }
    },

    # ── 15. done ───────────────────────────────────────────────
    {
        "name": "done",
        "description": """goal 달성 완료 또는 달성 불가 판정 시 최종 보고.

⚠️ 다음 상황에서 done() 호출 금지:
- 코드를 실행했지만 결과를 읽지 않은 경우
- 명령이 성공했지만 하드웨어 반응을 확인하지 않은 경우
- todo 항목이 아직 pending인 경우""",
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean", "description": "goal 달성 여부"},
                "summary": {
                    "type": "string",
                    "description": "무엇을 했고 결과가 어떻게 됐는지. 수치 포함."
                },
                "evidence": {
                    "type": "string",
                    "description": "하드웨어가 실제로 반응했다는 증거."
                },
                "notes": {
                    "type": "string",
                    "description": "주의사항, 물리적 한계, 대안 제안",
                    "default": ""
                }
            },
            "required": ["success", "summary", "evidence"]
        }
    }
]


# ─────────────────────────────────────────────────────────────
# 위험 명령 필터
# ─────────────────────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "dd if=",
    "mkfs",
    "> /dev/sd",
    ":(){ :|: & };:",
    "chmod -R 777 /",
    "chown -R",
]

def is_dangerous(command: str) -> bool:
    cmd_lower = command.lower()
    return any(p.lower() in cmd_lower for p in DANGEROUS_PATTERNS)


# ─────────────────────────────────────────────────────────────
# probe 명령 매핑
# ─────────────────────────────────────────────────────────────

PROBE_COMMANDS: dict[str, str] = {
    "hw": """
echo "=== USB 장치 ===" && lsusb 2>/dev/null || echo "(lsusb 없음)"
echo "=== 시리얼 포트 ===" && ls -la /dev/ttyACM* /dev/ttyUSB* /dev/ttyS* 2>/dev/null | head -20
echo "=== I2C 버스 ===" && ls /dev/i2c-* 2>/dev/null && for b in /dev/i2c-*; do echo "  $b:"; i2cdetect -y ${b##*-} 2>/dev/null | head -5; done
echo "=== SPI 장치 ===" && ls /dev/spi* /dev/spidev* 2>/dev/null || echo "(없음)"
echo "=== GPIO ===" && ls /dev/gpiochip* 2>/dev/null || echo "(없음)"
echo "=== dmesg 최근 HW 이벤트 ===" && dmesg --time-format iso 2>/dev/null | grep -iE "(usb|tty|i2c|spi|gpio)" | tail -20
""".strip(),

    "sw": """
echo "=== OS ===" && uname -a && cat /etc/os-release 2>/dev/null | head -6
echo "=== Python ===" && python3 --version 2>/dev/null && pip3 list 2>/dev/null | grep -iE "(ros|serial|gpio|numpy|cv2|torch)" | head -20
echo "=== ROS2 ===" && ls /opt/ros/ 2>/dev/null || echo "(ROS2 없음)"
echo "=== 실행 중인 서비스 ===" && systemctl list-units --state=running --type=service 2>/dev/null | grep -v "^$" | tail -20
""".strip(),

    "net": """
echo "=== 네트워크 인터페이스 ===" && ip addr show 2>/dev/null | grep -E "(inet |^[0-9])"
echo "=== 연결된 외부 IP ===" && ip route 2>/dev/null
echo "=== 열린 포트 ===" && ss -tlnp 2>/dev/null | head -20
echo "=== ARP 캐시 ===" && ip neigh show 2>/dev/null | grep -v "FAILED" | head -20
""".strip(),

    "perf": """
echo "=== CPU ===" && cat /proc/cpuinfo 2>/dev/null | grep -E "(model name|processor)" | head -4
echo "=== 메모리 ===" && free -h 2>/dev/null
echo "=== 디스크 ===" && df -h 2>/dev/null | grep -v tmpfs
echo "=== 온도 ===" && cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | while read t; do echo "$((t/1000))°C"; done || echo "(온도 센서 없음)"
echo "=== 부하 ===" && uptime 2>/dev/null
""".strip(),

    "motors": r"""
echo "=== 시리얼 장치 (모터 컨트롤러 후보) ==="
ls -la /dev/ttyACM* /dev/ttyUSB* /dev/ttyS* 2>/dev/null || echo "(시리얼 장치 없음)"
echo "=== CAN 인터페이스 ==="
ip link show type can 2>/dev/null || echo "(CAN 없음)"
echo "=== 모터 관련 Python 패키지 ==="
pip3 list 2>/dev/null | grep -iE "(serial|can|motor|odrive|dynamixel|roboclaw)" || echo "(없음)"
echo "=== 모터 관련 실행 중인 프로세스 ==="
ps aux 2>/dev/null | grep -iE "(motor|drive|servo|actuator|controller)" | grep -v grep | head -10
echo "=== dmesg 모터/시리얼 관련 이벤트 ==="
dmesg 2>/dev/null | grep -iE "(ttyACM|ttyUSB|usb|serial)" | tail -10
""".strip(),

    "camera": """
echo "=== V4L2 장치 ===" && ls /dev/video* 2>/dev/null || echo "(없음)"
echo "=== USB 카메라 ===" && lsusb 2>/dev/null | grep -iE "(camera|webcam|imaging|logitech)" || echo "(없음)"
echo "=== CSI 카메라 ===" && ls /dev/nvargus-daemon 2>/dev/null && echo "Jetson CSI 가능" || echo "(CSI 없음)"
echo "=== 카메라 도구 ===" && which v4l2-ctl 2>/dev/null && v4l2-ctl --list-devices 2>/dev/null | head -20 || echo "(v4l2-utils 없음)"
""".strip(),

    "lidar": r"""
echo "=== USB/시리얼 LiDAR ==="
lsusb 2>/dev/null | grep -iE "(laser|lidar|hokuyo|rplidar|sick|velodyne|ouster|urg)" || echo "(USB에서 못 찾음)"
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -10
echo "=== 네트워크 LiDAR ==="
ip neigh 2>/dev/null | grep "REACHABLE\|STALE" | awk '{print $1}' | head -20
echo "=== LiDAR Python 패키지 ==="
pip3 list 2>/dev/null | grep -iE "(rplidar|ydlidar|sick|hokuyo|velodyne|pcl|laser)" || echo "(없음)"
echo "=== ROS2 LiDAR 관련 토픽 ==="
source /opt/ros/$(ls /opt/ros/ 2>/dev/null | tail -1)/setup.bash 2>/dev/null
timeout 3 ros2 topic list 2>/dev/null | grep -iE "(scan|lidar|laser|point)" || echo "(ROS2 없거나 토픽 없음)"
""".strip(),
}

PROBE_COMMANDS["all"] = """
echo "======= 보드 전체 환경 탐지 ======="
echo "=== 1. 기본 시스템 ===" && uname -a && cat /etc/os-release 2>/dev/null | grep -E "^(NAME|VERSION)=" | head -2
echo "=== 2. 연결된 하드웨어 ===" && lsusb 2>/dev/null | head -10 && ls /dev/ttyACM* /dev/ttyUSB* /dev/i2c-* /dev/video* 2>/dev/null
echo "=== 3. 주요 소프트웨어 ===" && ls /opt/ros/ 2>/dev/null && python3 --version 2>/dev/null
echo "=== 4. 네트워크 ===" && ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1"
echo "=== 5. 리소스 ===" && free -h 2>/dev/null && cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null && echo " (milli°C)"
echo "=== 6. 실행 중인 주요 서비스 ===" && systemctl list-units --state=running --type=service 2>/dev/null | grep -iE "(ros|motor|camera|lidar|serial)" | head -10
echo "======= 탐지 완료 ======="
""".strip()

PROBE_COMMANDS["parallel_scan"] = r"""
echo "=== 보드 네트워크 인터페이스 ==="
ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1"
echo ""
echo "=== 병렬 서브넷 스캔 시작 ==="
SUBNETS=$(ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1" \
  | awk '{print $2}' | sed 's/\.[0-9]*\/.*//g' | sort -u)
if [ -z "$SUBNETS" ]; then echo "서브넷 감지 실패"; exit 1; fi
for SUBNET in $SUBNETS; do
  echo "--- 스캔: ${SUBNET}.0/24 ---"
  for i in $(seq 1 254); do
    (ping -c 1 -W 1 ${SUBNET}.${i} >/dev/null 2>&1 && echo "${SUBNET}.${i}") &
  done
  wait
done | grep -E "^[0-9]" | sort -t. -k4 -n
echo ""
echo "=== ARP 캐시 ==="
ip neigh show 2>/dev/null | grep -v "FAILED" | sort -t. -k4 -n
echo "=== 스캔 완료 ==="
""".strip()


# ─────────────────────────────────────────────────────────────
# verify 명령 매핑
# ─────────────────────────────────────────────────────────────

VERIFY_COMMANDS: dict[str, str] = {
    "serial_device": r"""
DEV=${ECC_DEVICE:-$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1)}
if [ -z "$DEV" ]; then echo "serial FAIL: no serial device found"; exit 1; fi
echo "serial device: $DEV"
if [ ! -r "$DEV" ]; then echo "serial FAIL: no read permission on $DEV"; exit 1; fi
stty -F "$DEV" 2>/dev/null && echo "serial PASS: $DEV accessible" || echo "serial WARN: stty failed"
python3 -c "import serial; print('pyserial OK:', serial.VERSION)" 2>/dev/null || echo "pyserial not installed"
""".strip(),

    "i2c_device": r"""
BUS=$(echo "${ECC_DEVICE:-1:0x00}" | cut -d: -f1)
ADDR=$(echo "${ECC_DEVICE:-1:0x00}" | cut -d: -f2)
DEV="/dev/i2c-$BUS"
if [ ! -e "$DEV" ]; then echo "i2c FAIL: $DEV does not exist"; exit 1; fi
i2cdetect -y "$BUS" 2>/dev/null || echo "i2cdetect not available"
if [ -n "$ADDR" ] && [ "$ADDR" != "0x00" ]; then
  i2cget -y "$BUS" "$ADDR" 2>/dev/null && echo "i2c PASS: $ADDR responded" || echo "i2c FAIL: $ADDR did not respond"
fi
""".strip(),

    "network_device": r"""
HOST=$(echo "${ECC_DEVICE:-}" | cut -d: -f1)
PORT=$(echo "${ECC_DEVICE:-}" | cut -d: -f2)
if [ -z "$HOST" ]; then echo "network FAIL: no host specified"; exit 1; fi
ping -c 2 -W 2 "$HOST" 2>/dev/null && echo "network PASS: $HOST reachable" || echo "network FAIL: $HOST not reachable"
if [ -n "$PORT" ] && [ "$PORT" != "$HOST" ]; then
  timeout 3 bash -c "echo > /dev/tcp/$HOST/$PORT" 2>/dev/null && echo "port $PORT: OPEN" || echo "port $PORT: CLOSED"
fi
""".strip(),

    "ros2_topic": r"""
TOPIC="${ECC_DEVICE:-}"
ROS_DISTRO=$(ls /opt/ros/ 2>/dev/null | tail -1)
if [ -z "$ROS_DISTRO" ]; then echo "ros2 FAIL: ROS2 not installed"; exit 1; fi
source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null
echo "=== ROS2 nodes ==="
timeout 3 ros2 node list 2>/dev/null || echo "(no nodes running)"
if [ -n "$TOPIC" ]; then
  echo "=== Topic info: $TOPIC ==="
  timeout 3 ros2 topic info "$TOPIC" --verbose 2>/dev/null || echo "topic not found"
  timeout 4 ros2 topic hz "$TOPIC" 2>/dev/null | grep -E "average|no new" | head -2 || echo "no data in 4s"
else
  timeout 3 ros2 topic list 2>/dev/null || echo "(no topics)"
fi
""".strip(),

    "process": r"""
PROC="${ECC_DEVICE:-}"
if [ -z "$PROC" ]; then echo "process FAIL: no process name"; exit 1; fi
PIDS=$(pgrep -f "$PROC" 2>/dev/null)
if [ -n "$PIDS" ]; then
  echo "process PASS: $PROC running (pids: $PIDS)"
  ps -p $PIDS -o pid,pcpu,pmem,etime,cmd 2>/dev/null | head -5
else
  echo "process FAIL: $PROC not running"
  systemctl status "$PROC" 2>/dev/null | head -10 || echo "(not a systemd service)"
fi
""".strip(),

    "system": r"""
echo "=== Recent errors (dmesg) ==="
dmesg --time-format iso 2>/dev/null | grep -iE "error|warn|fail|disconnect|killed" | tail -15 || echo "(dmesg not available)"
echo "=== Memory ===" && free -h 2>/dev/null
echo "=== Disk ===" && df -h 2>/dev/null | awk 'NR==1 || ($5+0 > 85) {print}'
echo "=== CPU Temperature ==="
for f in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$f" ] || continue
  t=$(cat "$f" 2>/dev/null); c=$((t/1000)); zone=$(dirname $f | xargs basename)
  if [ $c -gt 80 ]; then echo "TEMP WARN $zone: ${c}C"; else echo "TEMP OK $zone: ${c}C"; fi
done 2>/dev/null || echo "(no thermal sensors)"
echo "=== Load ===" && uptime 2>/dev/null
""".strip(),

    "custom": r"""
echo "custom verify: ${ECC_DEVICE}"
""".strip(),
}


# ─────────────────────────────────────────────────────────────
# Optional tools (opt-in)
# ─────────────────────────────────────────────────────────────

ASK_USER_TOOL = {
    "name": "ask_user",
    "description": """목표 수행에 필요한 정보가 부족하거나 명확하지 않을 때 사용자에게 직접 질문한다.

사용 조건:
- 추정/추론으로 진행할 수 없는 정보 (비밀번호, 인증서 경로)
- 잘못 추정하면 하드웨어 손상/데이터 손실 위험

사용 금지:
- probe/bash로 확인 가능한 정보
- 합리적인 기본값이 있는 설정""",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "사용자에게 물을 질문."},
            "context":  {"type": "string", "description": "왜 이 정보가 필요한지 한 줄 설명", "default": ""}
        },
        "required": ["question"]
    }
}


def get_tool_definitions() -> list:
    """환경변수에 따라 활성 도구 목록을 반환한다."""
    import os
    tools = list(TOOL_DEFINITIONS)
    if os.environ.get("ECC_ASK_USER") == "1":
        tools.append(ASK_USER_TOOL)
    return tools
