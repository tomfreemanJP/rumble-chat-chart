"""Windowless entry point.

Frozen by PyInstaller into rumble-chat-chartw.exe, the counterpart to
rumble-chat-chart.exe in the same way pythonw.exe is to python.exe. Two jobs:

  - with no arguments it runs `watch`, which is what the scheduled task launches
  - with arguments it runs them without a console window, which is how the
    Start Menu reaches `configure` and its dialogs

A windowed build has no stdout or stderr at all, so those are pointed at the
null device before anything can try to write to them.
"""
import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from rumble_chat_chart import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["watch"]))
