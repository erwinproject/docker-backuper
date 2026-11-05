import os
import sys
from typing import Any


def is_interactive_terminal() -> bool:
    """Checks whether the current environment supports interactive TTY control."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def read_key() -> str:
    """
    Reads a single keypress cross-platform.
    Returns: 'UP', 'DOWN', 'SPACE', 'ENTER', 'ALL', 'QUIT', or the raw character.
    """
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            if ch2 == b"H":
                return "UP"
            elif ch2 == b"P":
                return "DOWN"
            return "UNKNOWN"
        if ch in (b"\r", b"\n"):
            return "ENTER"
        if ch == b" ":
            return "SPACE"
        if ch in (b"a", b"A"):
            return "ALL"
        if ch in (b"q", b"Q", b"\x1b", b"\x03"):  # q, Esc, or Ctrl+C
            return "QUIT"
        try:
            return ch.decode("utf-8")
        except Exception:
            return "UNKNOWN"
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                # Check for escape sequence
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    seq = sys.stdin.read(2)
                    if seq == "[A":
                        return "UP"
                    elif seq == "[B":
                        return "DOWN"
                return "QUIT"
            elif ch in ("\r", "\n"):
                return "ENTER"
            elif ch == " ":
                return "SPACE"
            elif ch in ("a", "A"):
                return "ALL"
            elif ch in ("q", "Q", "\x03"):
                return "QUIT"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def fallback_checklist(items: list[dict[str, Any]], title: str = "Select targets:") -> list[int]:
    """Fallback numbered selection prompt for non-TTY / dumb terminal environments."""
    print(f"\n{title}")
    for idx, item in enumerate(items, 1):
        name = item.get("name", "unknown")
        image = item.get("image", "")
        vols = item.get("volumes", [])
        v_str = f"Volumes: [{', '.join(vols)}]" if vols else "(No volumes)"
        print(f"  [{idx}] {name:<20} {image:<24} {v_str}")

    selected_input = input("\nEnter numbers separated by comma (e.g. 1, 3) or 'all': ").strip()
    if not selected_input:
        return []
    if selected_input.lower() == "all":
        return list(range(len(items)))

    selected = []
    for part in selected_input.split(","):
        part = part.strip()
        if part.isdigit():
            val = int(part) - 1
            if 0 <= val < len(items) and val not in selected:
                selected.append(val)
    return selected


def interactive_checklist(
    items: list[dict[str, Any]],
    title: str = "Select Docker Containers to Backup (docker ps):",
    page_size: int = 10,
) -> list[int]:
    """
    Renders an interactive terminal checklist with keyboard navigation.
    [UP/DOWN]: Move cursor
    [SPACE]: Toggle item [X] / [ ]
    [a]: Select / Deselect all
    [ENTER]: Confirm selection
    [q / Esc]: Abort
    """
    if not items:
        return []

    if not is_interactive_terminal():
        return fallback_checklist(items, title=title)

    cursor = 0
    checked = set()
    total = len(items)

    # ANSI helpers
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"
    CLEAR_LINE = "\033[2K"
    UP_LINE = "\033[A"

    num_rendered_lines = 0

    try:
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()

        while True:
            # Calculate pagination window
            start_idx = (cursor // page_size) * page_size
            end_idx = min(start_idx + page_size, total)

            # Build lines
            lines = [
                "=" * 90,
                f"🐳 {title}",
                "Use [↑/↓] Navigate | [Space] Toggle | [a] Select All | [Enter] Confirm | [q] Cancel",
                "=" * 90,
            ]

            if start_idx > 0:
                lines.append(f"  ▲ ({start_idx} more items above)")

            for idx in range(start_idx, end_idx):
                item = items[idx]
                is_cursor = (idx == cursor)
                is_checked = (idx in checked)

                pointer = "> " if is_cursor else "  "
                box = "[X]" if is_checked else "[ ]"

                name = item.get("name", "unknown")
                image = item.get("image") or ""
                if len(image) > 22:
                    image = image[:19] + "..."

                vols = item.get("volumes", [])
                if vols:
                    v_str = f"Volumes: [{', '.join(vols[:2])}{'...' if len(vols) > 2 else ''}]"
                else:
                    v_str = "\033[33m(No volumes)\033[0m" if sys.stdout.isatty() else "(No volumes)"

                status = item.get("status") or ""

                line_str = f"{pointer}{box} {idx+1:>2}. {name:<20} {image:<22} {status:<14} {v_str}"
                lines.append(line_str)

            if end_idx < total:
                lines.append(f"  ▼ ({total - end_idx} more items below)")

            lines.append("=" * 90)
            lines.append(f"Selected: {len(checked)} of {total} container(s) | Press [Enter] to proceed")

            # Erase previous frame
            if num_rendered_lines > 0:
                sys.stdout.write(f"\r{UP_LINE * num_rendered_lines}")
                for _ in range(num_rendered_lines):
                    sys.stdout.write(f"{CLEAR_LINE}\n")
                sys.stdout.write(f"\r{UP_LINE * num_rendered_lines}")

            # Render current frame
            output_text = "\n".join(lines) + "\n"
            sys.stdout.write(output_text)
            sys.stdout.flush()
            num_rendered_lines = len(lines)

            # Read user keypress
            key = read_key()

            if key == "UP":
                cursor = (cursor - 1) % total
            elif key == "DOWN":
                cursor = (cursor + 1) % total
            elif key == "SPACE":
                if cursor in checked:
                    checked.remove(cursor)
                else:
                    checked.add(cursor)
            elif key == "ALL":
                if len(checked) == total:
                    checked.clear()
                else:
                    checked = set(range(total))
            elif key == "ENTER":
                break
            elif key == "QUIT":
                checked.clear()
                break

    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()

    return sorted(list(checked))
