"""Console-command tests for launcher.py (no real stdin, no subprocess)."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import launcher


def test_parse_console_command():
    assert launcher.parse_console_command("restart") == "restart"
    assert launcher.parse_console_command("  RESTART now ") == "restart"
    assert launcher.parse_console_command("update") == "update"
    assert launcher.parse_console_command("quit") == "quit"
    assert launcher.parse_console_command("help") == "help"
    assert launcher.parse_console_command("") is None
    assert launcher.parse_console_command("   ") is None
    assert launcher.parse_console_command("explode") is None
    assert launcher.parse_console_command("restarting") is None


def _run_reader(text):
    launcher.restart_requested.clear()
    launcher.update_requested.clear()
    launcher.stopping = False
    seen = []
    launcher.console_reader_loop(io.StringIO(text), log_fn=seen.append)
    return seen


def test_reader_restart_and_update():
    _run_reader("restart\nupdate\n")
    assert launcher.restart_requested.is_set()
    assert launcher.update_requested.is_set()
    assert launcher.stopping is False


def test_reader_quit_sets_stopping():
    _run_reader("quit\n")
    assert launcher.stopping is True


def test_reader_unknown_command_logs_and_continues():
    seen = _run_reader("bogus\nrestart\n")
    assert any("Unknown console command" in m for m in seen)
    assert launcher.restart_requested.is_set()


def test_reader_help_logs_help_text():
    seen = _run_reader("help\n")
    assert any("restart" in m and "quit" in m for m in seen)


def test_reader_empty_lines_ignored():
    seen = _run_reader("\n  \n")
    assert seen == []
    assert not launcher.restart_requested.is_set()


def test_start_console_reader_no_tty():
    # StringIO is not a tty: no thread starts, no error.
    old = sys.stdin
    try:
        sys.stdin = io.StringIO("")
        assert launcher.start_console_reader() is False
    finally:
        sys.stdin = old
