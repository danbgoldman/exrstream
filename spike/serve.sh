#!/bin/bash
# Start/stop the spike server by PID file. Matching on `pgrep -f` is a trap here:
# the pattern appears in the invoking shell's own command line, so it kills the
# caller.
cd "$(dirname "$0")/.."
PIDF=/tmp/exrstream.pid
case "$1" in
  stop|restart)
    [ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null; rm -f $PIDF; sleep 2
    [ "$1" = stop ] && exit 0 ;;
esac
shift_args=("${@:2}")
PYTHONPATH=spike nohup .venv/bin/python spike/10_server.py "${shift_args[@]}" \
  > /tmp/exrstream.log 2>&1 &
echo $! > $PIDF
sleep 12
kill -0 "$(cat $PIDF)" 2>/dev/null && echo "up, pid $(cat $PIDF)" || { echo "FAILED"; cat /tmp/exrstream.log; }
