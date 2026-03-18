"""ecc_core/safety.py — Dangerous command + physical safety filter."""

DANGEROUS_PATTERNS = [
    "rm -rf /", "rm -rf /*", "dd if=", "mkfs",
    "> /dev/sd", ":(){ :|: & };:", "chmod -R 777 /", "chown -R",
]

def is_dangerous(command: str) -> bool:
    cmd_lower = command.lower()
    return any(p.lower() in cmd_lower for p in DANGEROUS_PATTERNS)
