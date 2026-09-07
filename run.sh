#!/bin/bash
# Start/stop the exrstream server by PID file.
# Do NOT use `pkill -f server.py`: the pattern matches the invoking shell's own
# command line and kills the caller.
cd "$(dirname "$0")"
PIDF=/tmp/exrstream.pid
case "$1" in
  stop|restart)
    [ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null; rm -f $PIDF; sleep 2
    [ "$1" = stop ] && exit 0; shift ;;
esac
nohup .venv/bin/python -m exrstream.server "$@" > /tmp/exrstream.log 2>&1 &
echo $! > $PIDF
sleep 10
kill -0 "$(cat $PIDF)" 2>/dev/null && echo "up, pid $(cat $PIDF)" || { echo FAILED; cat /tmp/exrstream.log; }
