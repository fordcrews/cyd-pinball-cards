REM ---- CYD pinball cards: add at the END of the Visual Pinball X *Close Script* ----
REM --idle sends the attract playlist from cards\_idle.json plus the PC's local time (clock screen).
START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" --idle -q
REM Python instead of the exe:
REM START "" /B pythonw "C:\cyd-pinball-cards\host\cyd_push.py" --idle -q
REM Two displays:
REM START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" --idle --port COM5 --port COM6 -q
