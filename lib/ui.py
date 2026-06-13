"""Terminal UI helpers: colored logging, prompts, menus, tables.

Standard library only. All interactive reads use the real tty so the tool
works even when stdin is piped.
"""

import sys

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"

_USE_COLOR = sys.stdout.isatty()


def _c(color, text):
    return f"{color}{text}{NC}" if _USE_COLOR else text


def info(msg):
    print(f"{_c(BLUE, '[INFO]')} {msg}")


def success(msg):
    print(f"{_c(GREEN, '[OK]')} {msg}")


def warn(msg):
    print(f"{_c(YELLOW, '[WARN]')} {msg}")


def error(msg):
    print(f"{_c(RED, '[ERROR]')} {msg}", file=sys.stderr)


def fatal(msg, code=1):
    error(msg)
    sys.exit(code)


def header(title):
    line = "=" * 46
    print()
    print(_c(BOLD, line))
    print(_c(BOLD, f"  {title}"))
    print(_c(BOLD, line))
    print()


def _read(prompt_text):
    """Read a line from the controlling terminal, falling back to stdin."""
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt_text)
            tty.flush()
            return tty.readline().rstrip("\n")
    except OSError:
        return input(prompt_text)


def prompt(message, default=None):
    suffix = f" [{default}]" if default is not None else ""
    value = _read(f"{message}{suffix}: ").strip()
    if not value and default is not None:
        return default
    return value


def prompt_required(message):
    while True:
        value = prompt(message)
        if value:
            return value
        warn("값이 필요합니다.")


def confirm(message, default=False):
    hint = "Y/n" if default else "y/N"
    value = _read(f"{message} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def select(title, options, labeler=str, default_index=0, allow_cancel=True):
    """Numbered single-choice menu.

    options: list of items.
    labeler: item -> display string.
    Returns the chosen item, or None if cancelled.
    """
    if not options:
        return None
    print()
    if title:
        print(_c(BOLD, title))
    for i, opt in enumerate(options, start=1):
        print(f"  {_c(CYAN, str(i))}) {labeler(opt)}")
    if allow_cancel:
        print(f"  {_c(DIM, '0')}) {_c(DIM, '취소')}")
    print()

    while True:
        raw = _read(f"선택 [{default_index + 1}]: ").strip()
        if not raw:
            return options[default_index]
        if not raw.isdigit():
            warn("숫자를 입력하세요.")
            continue
        n = int(raw)
        if allow_cancel and n == 0:
            return None
        if 1 <= n <= len(options):
            return options[n - 1]
        warn(f"1 ~ {len(options)} 사이로 입력하세요.")


def table(rows, headers):
    """Render a simple aligned text table. rows: list of tuples/lists."""
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(row[i])))

    def fmt(cells, dim=False):
        line = "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))
        return _c(DIM, line) if dim else line

    print()
    print(_c(BOLD, fmt(headers)))
    print(_c(DIM, "  ".join("-" * w for w in widths)))
    for row in rows:
        print(fmt(row))
    print()
