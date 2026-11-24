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


# --------------------------------------------------- dashboard triggers

def _write_trigger(app_dir, name):
    path = os.path.join(app_dir, "data", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("1\n")
    return path


def test_take_trigger_consumes_file(tmp_path):
    app_dir = str(tmp_path)
    path = _write_trigger(app_dir, launcher.RESTART_TRIGGER_NAME)
    assert launcher.take_trigger(app_dir, launcher.RESTART_TRIGGER_NAME) is True
    assert not os.path.exists(path)
    # Consuming again finds nothing.
    assert launcher.take_trigger(app_dir, launcher.RESTART_TRIGGER_NAME) is False


def test_take_trigger_missing_file(tmp_path):
    assert launcher.take_trigger(str(tmp_path), "restart_requested") is False


def test_take_trigger_shutdown(tmp_path):
    app_dir = str(tmp_path)
    _write_trigger(app_dir, launcher.SHUTDOWN_TRIGGER_NAME)
    assert launcher.take_trigger(app_dir, launcher.SHUTDOWN_TRIGGER_NAME) is True
    assert not os.path.exists(
        os.path.join(app_dir, "data", launcher.SHUTDOWN_TRIGGER_NAME))


def test_take_update_trigger_still_works(tmp_path):
    # take_update_trigger is the old name — keep it working.
    app_dir = str(tmp_path)
    _write_trigger(app_dir, launcher.UPDATE_TRIGGER_NAME)
    assert launcher.take_update_trigger(app_dir) is True
    assert launcher.take_update_trigger(app_dir) is False


def test_trigger_names_match_app():
    # The web app writes these filenames (app/updates.py); the launcher
    # consumes them. If one side renames, the console silently stops
    # working — so assert they agree.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import updates
    assert updates.RESTART_TRIGGER_NAME == launcher.RESTART_TRIGGER_NAME
    assert updates.SHUTDOWN_TRIGGER_NAME == launcher.SHUTDOWN_TRIGGER_NAME
    assert updates.TRIGGER_NAME == launcher.UPDATE_TRIGGER_NAME
    assert updates.LAUNCHER_LOG_NAME == launcher.LAUNCHER_LOG_NAME


# ------------------------------------------------------- launcher log file

def test_log_appends_to_file(tmp_path, capsys):
    old_dir = launcher.APP_DIR
    try:
        launcher.APP_DIR = str(tmp_path)
        launcher.log("hello log file")
    finally:
        launcher.APP_DIR = old_dir
    capsys.readouterr()  # log() also prints to stdout; discard it
    path = os.path.join(str(tmp_path), "data", "launcher.log")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "hello log file" in content
    assert "[launcher]" in content


def test_log_file_trim(tmp_path):
    path = os.path.join(str(tmp_path), "launcher.log")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(launcher.LAUNCHER_LOG_MAX_LINES + 5):
            f.write(f"line {i}\n")
    launcher._trim_log_file(path)
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == launcher.LAUNCHER_LOG_KEEP_LINES
    assert lines[0] == f"line {launcher.LAUNCHER_LOG_MAX_LINES + 5 - launcher.LAUNCHER_LOG_KEEP_LINES}\n"
    assert lines[-1] == f"line {launcher.LAUNCHER_LOG_MAX_LINES + 4}\n"


def test_log_file_no_trim_under_cap(tmp_path):
    path = os.path.join(str(tmp_path), "launcher.log")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(10):
            f.write(f"line {i}\n")
    launcher._trim_log_file(path)
    with open(path, "r", encoding="utf-8") as f:
        assert len(f.readlines()) == 10


def test_trim_missing_file_is_noop(tmp_path):
    # A missing file must never raise — logging is best-effort.
    launcher._trim_log_file(os.path.join(str(tmp_path), "nope.log"))
