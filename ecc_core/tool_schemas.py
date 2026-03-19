"""ecc_core/tool_schemas.py — Tool JSON schemas + get_tool_definitions()."""

TOOL_DEFINITIONS = [
    {
        "name": "ssh_connect",
        "description": """Connect to a board via SSH. Required before any other tool.

Connection strategy:
1. Try the hinted IP/user first if provided
2. Try known_hosts, mDNS (.local domains)
3. Scan local subnets
4. After connecting, use probe(all) to understand the environment""",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Board IP or hostname. Use 'scan' to auto-discover."},
                "user": {"type": "string", "description": "SSH user. Defaults to ECC_USERS env var order.", "default": ""},
                "port": {"type": "integer", "description": "SSH port. Default: 22.", "default": 22}
            },
            "required": ["host"]
        }
    },
    {
        "name": "bash",
        "description": """Execute a shell command on the board via SSH.

Guidelines:
- Chain independent checks with && in a single call
- Use script tool for multi-line scripts
- Use background=true for long-running scans/builds""",
        "input_schema": {
            "type": "object",
            "properties": {
                "command":     {"type": "string",  "description": "Shell command to execute."},
                "timeout":     {"type": "integer", "description": "Timeout in seconds. Default: 30.", "default": 30},
                "background":  {"type": "boolean", "description": "If true, run in background and return task_id.", "default": False},
                "description": {"type": "string",  "description": "What this command does (5-10 words)."}
            },
            "required": ["command", "description"]
        }
    },
    {
        "name": "bash_wait",
        "description": "Collect the result of a bash command run with background=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id":     {"type": "string",  "description": "task_id returned by bash(background=true)"},
                "timeout":     {"type": "integer", "description": "Max wait time in seconds. Default: 120.", "default": 120},
                "description": {"type": "string",  "description": "Purpose of this collection"}
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "script",
        "description": """Upload and execute a multi-line script on the board.

Use instead of bash when:
- Environment variables must persist across lines (e.g. ROS2 source chain)
- Writing hardware control code in Python, C, etc.
- Complex logic (loops, conditionals, error handling)""",
        "input_schema": {
            "type": "object",
            "properties": {
                "code":        {"type": "string", "description": "Full script content to execute"},
                "interpreter": {"type": "string", "description": "Interpreter. e.g. 'bash', 'python3'", "default": "bash"},
                "timeout":     {"type": "integer","description": "Timeout in seconds. Default: 60.", "default": 60},
                "description": {"type": "string", "description": "Purpose of this script"}
            },
            "required": ["code", "description"]
        }
    },
    {
        "name": "read",
        "description": "Read a file from the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string",  "description": "Absolute path to the file"},
                "head_lines": {"type": "integer", "description": "Read first N lines (0 = all)", "default": 0},
                "tail_lines": {"type": "integer", "description": "Read last N lines (0 = all)", "default": 0}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write",
        "description": "Create or overwrite a file on the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Absolute path to the file"},
                "content": {"type": "string", "description": "File content"},
                "mode":    {"type": "string", "description": "File permissions (e.g. '755'). Empty = default.", "default": ""}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "glob",
        "description": "Search for files by pattern on the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern":  {"type": "string", "description": "Glob pattern. e.g. '/dev/tty*'"},
                "base_dir": {"type": "string", "description": "Search root directory", "default": "/"}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "grep",
        "description": "Search for a pattern in files on the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern":     {"type": "string",  "description": "Regex or fixed string to search"},
                "path":        {"type": "string",  "description": "File or directory path to search"},
                "flags":       {"type": "string",  "description": "grep flags.", "default": "-rn"},
                "max_results": {"type": "integer", "description": "Max results to return", "default": 50}
            },
            "required": ["pattern", "path"]
        }
    },
    {
        "name": "probe",
        "description": """Systematically detect the board's hardware and software environment.

Available targets:
- all:           Full environment summary
- hw:            Connected hardware (USB, I2C, SPI, GPIO, serial)
- sw:            Installed software (ROS2, Python packages, services)
- net:           Network interfaces and open ports
- perf:          CPU / memory / temperature / load
- motors:        Motor controllers
- camera:        Camera devices
- lidar:         LiDAR sensors
- parallel_scan: Parallel subnet IP scan""",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Detection target",
                    "enum": ["all", "hw", "sw", "net", "perf", "motors", "camera", "lidar", "parallel_scan"]
                }
            },
            "required": ["target"]
        }
    },
    {
        "name": "serial_open",
        "description": "Open a serial communication session with a device on the board. Returns session_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "port":        {"type": "string",  "description": "Serial port path. e.g. /dev/ttyACM0"},
                "baudrate":    {"type": "integer", "description": "Baud rate. Default: 115200", "default": 115200},
                "timeout":     {"type": "number",  "description": "Read timeout in seconds. Default: 1.0", "default": 1.0},
                "description": {"type": "string",  "description": "What this device is"}
            },
            "required": ["port"]
        }
    },
    {
        "name": "serial_send",
        "description": "Send data over an open serial session and receive the response.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string",  "description": "session_id returned by serial_open"},
                "data":       {"type": "string",  "description": "Data to send."},
                "expect":     {"type": "string",  "description": "String to wait for in response.", "default": ""},
                "timeout":    {"type": "number",  "description": "Max response wait time in seconds. Default: 2.0", "default": 2.0},
                "hex_encode": {"type": "boolean", "description": "If true, parse data as hex bytes.", "default": False}
            },
            "required": ["session_id", "data"]
        }
    },
    {
        "name": "serial_close",
        "description": "Close a serial session. Omit session_id to close all sessions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "session_id to close. Omit to close all."}
            },
            "required": []
        }
    },
    {
        "name": "todo",
        "description": """Manage a task checklist. Break complex goals into steps first.
Mark each step in_progress when starting, completed when done.

Supports depends_on for dependency-aware scheduling:
  depends_on: list of task ids that must be completed before this task starts.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Full todo list (always pass the complete list)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":              {"type": "string"},
                            "content":         {"type": "string"},
                            "status":          {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            "priority":        {"type": "string", "enum": ["high", "medium", "low"], "default": "medium"},
                            "depends_on":      {"type": "array", "items": {"type": "string"}, "default": []},
                            "estimated_turns": {"type": "integer", "default": 1}
                        },
                        "required": ["id", "content", "status"]
                    }
                }
            },
            "required": ["todos"]
        }
    },
    {
        "name": "remember",
        "description": """Persist a discovered fact to Semantic Memory.

Call immediately when you discover something important from probe/verify/bash.
Survives session disconnect and context compression.

When to use:
  Physical limit found:    remember(namespace='constraints', key='min_erpm', value=2000)
  Hardware confirmed:      remember(namespace='hardware', key='motor_topic', value='/cmd_vel')
  Protocol confirmed:      remember(namespace='protocol', key='baud_rate', value=115200)
  Failed approach:         remember(namespace='failed', key='pub_once_loop', value='ARG_MAX exceeded')
  Validated script:        remember(namespace='skill', key='vesc_read', value='import serial...')

Note: constraints are automatically expired after 24h (ECC_CONSTRAINTS_TTL) to prevent
stale physical limits from persisting across firmware updates.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "enum": ["hardware", "protocol", "constraints", "failed", "skill"],
                    "description": "Storage category"
                },
                "key":   {"type": "string", "description": "Fact name. e.g. min_erpm, baud_rate, motor_topic"},
                "value": {"description": "Value to store. String, number, or list."}
            },
            "required": ["namespace", "key", "value"]
        }
    },
    {
        "name": "subagent",
        "description": """Run an exploration/investigation task in an isolated context.

⚠️  Execution must be done by the main agent. subagent is for investigation only.

Correct:   subagent("Investigate what motor controllers are on this board")
Incorrect: subagent("Run the motor at 0.1 m/s")""",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal":    {"type": "string", "description": "Goal for the subagent to achieve."},
                "context": {"type": "string", "description": "Already-known information.", "default": ""}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "verify",
        "description": """Verify that a component is actually working (not just present).

target types:
- serial_device, i2c_device, network_device, ros2_topic, process, system, custom""",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Verification type",
                    "enum": ["serial_device", "i2c_device", "network_device",
                             "ros2_topic", "process", "system", "custom"]
                },
                "device": {"type": "string", "description": "Target to verify.", "default": ""}
            },
            "required": ["target"]
        }
    },
    {
        "name": "done",
        "description": """Report goal completion or declare goal unachievable.

⚠️ Do NOT call done() when:
- Code was executed but result not read
- Command succeeded but hardware response not verified
- Any todo item is still pending""",
        "input_schema": {
            "type": "object",
            "properties": {
                "success":  {"type": "boolean", "description": "Whether the goal was achieved"},
                "summary":  {"type": "string",  "description": "What was done and what the result was."},
                "evidence": {"type": "string",  "description": "Concrete evidence that the hardware responded."},
                "notes":    {"type": "string",  "description": "Caveats, physical limits, alternatives.", "default": ""}
            },
            "required": ["success", "summary", "evidence"]
        }
    }
]


ASK_USER_TOOL = {
    "name": "ask_user",
    "description": """Ask the user directly when required information is missing or ambiguous.

Use when:
- Information cannot be inferred (passwords, certificate paths)
- Wrong assumption could damage hardware
- Meta-cognitive signal fires (system will inject [system] message)

Do NOT use when information is discoverable via probe/bash.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Question to ask the user."},
            "context":  {"type": "string", "description": "One-line explanation of why this is needed.", "default": ""}
        },
        "required": ["question"]
    }
}


def get_tool_definitions() -> list:
    import os
    tools = list(TOOL_DEFINITIONS)
    if os.environ.get("ECC_ASK_USER") == "1":
        tools.append(ASK_USER_TOOL)
    return tools