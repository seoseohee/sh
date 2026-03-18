"""ecc_core/probe_commands.py — 보드 탐지 쉘 명령 매핑."""

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
