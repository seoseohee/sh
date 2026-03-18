"""ecc_core/tool_schemas.py — 도구 JSON 스키마 + get_tool_definitions()."""

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


