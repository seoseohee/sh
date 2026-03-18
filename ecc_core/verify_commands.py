"""ecc_core/verify_commands.py — Verification shell command mapping."""

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

