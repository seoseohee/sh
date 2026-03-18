"""ecc_core/probe_commands.py — Board detection shell command mapping."""

PROBE_COMMANDS: dict[str, str] = {
    "hw": """
echo "=== USB devices ===" && lsusb 2>/dev/null || echo "(lsusb not found)"
echo "=== Serial ports ===" && ls -la /dev/ttyACM* /dev/ttyUSB* /dev/ttyS* 2>/dev/null | head -20
echo "=== I2C buses ===" && ls /dev/i2c-* 2>/dev/null && for b in /dev/i2c-*; do echo "  $b:"; i2cdetect -y ${b##*-} 2>/dev/null | head -5; done
echo "=== SPI devices ===" && ls /dev/spi* /dev/spidev* 2>/dev/null || echo "(none)"
echo "=== GPIO ===" && ls /dev/gpiochip* 2>/dev/null || echo "(none)"
echo "=== dmesg recent HW events ===" && dmesg --time-format iso 2>/dev/null | grep -iE "(usb|tty|i2c|spi|gpio)" | tail -20
""".strip(),

    "sw": """
echo "=== OS ===" && uname -a && cat /etc/os-release 2>/dev/null | head -6
echo "=== Python ===" && python3 --version 2>/dev/null && pip3 list 2>/dev/null | grep -iE "(ros|serial|gpio|numpy|cv2|torch)" | head -20
echo "=== ROS2 ===" && ls /opt/ros/ 2>/dev/null || echo "(ROS2 not found)"
echo "=== Running services ===" && systemctl list-units --state=running --type=service 2>/dev/null | grep -v "^$" | tail -20
""".strip(),

    "net": """
echo "=== 4. Network interfaces ===" && ip addr show 2>/dev/null | grep -E "(inet |^[0-9])"
echo "=== External IPs ===" && ip route 2>/dev/null
echo "=== Open ports ===" && ss -tlnp 2>/dev/null | head -20
echo "=== ARP cache ===" && ip neigh show 2>/dev/null | grep -v "FAILED" | head -20
""".strip(),

    "perf": """
echo "=== CPU ===" && cat /proc/cpuinfo 2>/dev/null | grep -E "(model name|processor)" | head -4
echo "=== Memory ===" && free -h 2>/dev/null
echo "=== Disk ===" && df -h 2>/dev/null | grep -v tmpfs
echo "=== Temperature ===" && cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | while read t; do echo "$((t/1000))°C"; done || echo "(no thermal sensors)"
echo "=== Load ===" && uptime 2>/dev/null
""".strip(),

    "motors": r"""
echo "=== Serial devices (motor controller candidates) ==="
ls -la /dev/ttyACM* /dev/ttyUSB* /dev/ttyS* 2>/dev/null || echo "(no serial devices)"
echo "=== CAN interfaces ==="
ip link show type can 2>/dev/null || echo "(no CAN)"
echo "=== Motor-related Python packages ==="
pip3 list 2>/dev/null | grep -iE "(serial|can|motor|odrive|dynamixel|roboclaw)" || echo "(none)"
echo "=== Motor-related running processes ==="
ps aux 2>/dev/null | grep -iE "(motor|drive|servo|actuator|controller)" | grep -v grep | head -10
echo "=== dmesg motor/serial events ==="
dmesg 2>/dev/null | grep -iE "(ttyACM|ttyUSB|usb|serial)" | tail -10
""".strip(),

    "camera": """
echo "=== V4L2 devices ===" && ls /dev/video* 2>/dev/null || echo "(none)"
echo "=== USB cameras ===" && lsusb 2>/dev/null | grep -iE "(camera|webcam|imaging|logitech)" || echo "(none)"
echo "=== CSI cameras ===" && ls /dev/nvargus-daemon 2>/dev/null && echo "Jetson CSI available" || echo "(no CSI)"
echo "=== Camera tools ===" && which v4l2-ctl 2>/dev/null && v4l2-ctl --list-devices 2>/dev/null | head -20 || echo "(v4l2-utils not found)"
""".strip(),

    "lidar": r"""
echo "=== USB/serial LiDAR ==="
lsusb 2>/dev/null | grep -iE "(laser|lidar|hokuyo|rplidar|sick|velodyne|ouster|urg)" || echo "(not found via USB)"
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -10
echo "=== 4. Network LiDAR ==="
ip neigh 2>/dev/null | grep "REACHABLE\|STALE" | awk '{print $1}' | head -20
echo "=== LiDAR Python packages ==="
pip3 list 2>/dev/null | grep -iE "(rplidar|ydlidar|sick|hokuyo|velodyne|pcl|laser)" || echo "(none)"
echo "=== ROS2 LiDAR topics ==="
source /opt/ros/$(ls /opt/ros/ 2>/dev/null | tail -1)/setup.bash 2>/dev/null
timeout 3 ros2 topic list 2>/dev/null | grep -iE "(scan|lidar|laser|point)" || echo "(no ROS2 or no topics)"
""".strip(),
}

PROBE_COMMANDS["all"] = """
echo "======= Full board environment detection ======="
echo "=== 1. 1. Base system ===" && uname -a && cat /etc/os-release 2>/dev/null | grep -E "^(NAME|VERSION)=" | head -2
echo "=== 2. Connected hardware ===" && lsusb 2>/dev/null | head -10 && ls /dev/ttyACM* /dev/ttyUSB* /dev/i2c-* /dev/video* 2>/dev/null
echo "=== 3. 3. Key software ===" && ls /opt/ros/ 2>/dev/null && python3 --version 2>/dev/null
echo "=== 4. 4. Network ===" && ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1"
echo "=== 5. 5. Resources ===" && free -h 2>/dev/null && cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null && echo " (milli°C)"
echo "=== 6. 6. Running services ===" && systemctl list-units --state=running --type=service 2>/dev/null | grep -iE "(ros|motor|camera|lidar|serial)" | head -10
echo "======= Detection complete ======="
""".strip()

PROBE_COMMANDS["parallel_scan"] = r"""
echo "=== Board network interfaces ==="
ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1"
echo ""
echo "=== Starting parallel subnet scan ==="
SUBNETS=$(ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1" \
  | awk '{print $2}' | sed 's/\.[0-9]*\/.*//g' | sort -u)
if [ -z "$SUBNETS" ]; then echo "Subnet detection failed"; exit 1; fi
for SUBNET in $SUBNETS; do
  echo "--- Scan: ${SUBNET}.0/24 ---"
  for i in $(seq 1 254); do
    (ping -c 1 -W 1 ${SUBNET}.${i} >/dev/null 2>&1 && echo "${SUBNET}.${i}") &
  done
  wait
done | grep -E "^[0-9]" | sort -t. -k4 -n
echo ""
echo "=== ARP cache ==="
ip neigh show 2>/dev/null | grep -v "FAILED" | sort -t. -k4 -n
echo "=== Scan complete ==="
""".strip()


# ─────────────────────────────────────────────────────────────
# verify command mapping
# ─────────────────────────────────────────────────────────────
