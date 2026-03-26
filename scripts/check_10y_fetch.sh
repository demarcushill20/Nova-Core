#!/bin/bash
# Quick status check for the 10-year Dukascopy fetch
PIDFILE="/home/nova/nova-core/LOGS/fetch_10y_dukascopy.pid"
LOGFILE=$(ls -t /home/nova/nova-core/LOGS/fetch_10y_dukascopy_*.log 2>/dev/null | head -1)

echo "=== 10-Year Dukascopy Fetch Status ==="
if [ -f "$PIDFILE" ] && ps -p "$(cat $PIDFILE)" > /dev/null 2>&1; then
    PID=$(cat $PIDFILE)
    ELAPSED=$(ps -p $PID -o etime= 2>/dev/null | xargs)
    RSS=$(ps -p $PID -o rss= 2>/dev/null | xargs)
    RSS_MB=$((RSS / 1024))
    echo "Status:  RUNNING (PID $PID)"
    echo "Elapsed: $ELAPSED"
    echo "Memory:  ${RSS_MB}MB"
else
    echo "Status:  STOPPED"
fi

echo ""
echo "=== Cache ==="
for y in $(ls /home/nova/nova-core/data/dukascopy/EURUSD/ 2>/dev/null); do
    SIZE=$(du -sh /home/nova/nova-core/data/dukascopy/EURUSD/$y 2>/dev/null | cut -f1)
    echo "  $y: $SIZE"
done
echo "  Total: $(du -sh /home/nova/nova-core/data/dukascopy/EURUSD/ 2>/dev/null | cut -f1)"

echo ""
echo "=== Latest Log ==="
if [ -f "$LOGFILE" ]; then
    tail -5 "$LOGFILE"
fi

echo ""
echo "=== Output Files ==="
ls -lh /home/nova/nova-core/data/candles/EURUSD_*10*.csv 2>/dev/null || echo "  (none yet)"
ls -lh /home/nova/nova-core/data/snapshots/*_meta.json 2>/dev/null || echo "  (no new snapshots yet)"
