REM ---- CYD pinball cards: add near the TOP of the Visual Pinball X *Launch Script* ----
REM [GAMENAME] is Popper's table file name without extension.
REM START "" /B runs it in the background so it never delays the table launch.
REM Adjust C:\cyd-pinball-cards to wherever you put the kit.
START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" "[GAMENAME]" -q
REM Python instead of the exe:
REM START "" /B pythonw "C:\cyd-pinball-cards\host\cyd_push.py" "[GAMENAME]" -q
REM Several displays (right, left, topper...): the same line updates every connected CYD;
REM each shows the cards for its role (see README "Multiple displays"). Only some of them:
REM START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" "[GAMENAME]" --target right,left -q
