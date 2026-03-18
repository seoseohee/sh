"""ecc_core/safety.py — 위험 명령 + 물리 안전 필터."""

DANGEROUS_PATTERNS = [
    "rm -rf /", "rm -rf /*", "dd if=", "mkfs",
    "> /dev/sd", ":(){ :|: & };:", "chmod -R 777 /", "chown -R",
]

def is_dangerous(command: str) -> bool:
    cmd_lower = command.lower()
    return any(p.lower() in cmd_lower for p in DANGEROUS_PATTERNS)
