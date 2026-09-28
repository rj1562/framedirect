#!/bin/bash
# Resource snapshot of a running kiosk, the same way for any kiosk version
# (Chromium v8/v9 or this native one), so the numbers can be compared.
#   tools/measure-usage.sh [seconds]      (default 120; no sudo needed)
SECS="${1:-120}"
echo "=== $(hostname)  $(date '+%F %T')  up since $(uptime -s)"
grep PRETTY_NAME /etc/os-release | cut -d= -f2
uname -r
echo
echo "--- boot time"
systemd-analyze 2>/dev/null | head -1
systemd-analyze critical-chain 2>/dev/null | sed -n '2,4p'
echo
echo "--- memory (MB)"
free -m
grep -E "^(CmaTotal|CmaFree|MemAvailable|SwapTotal|SwapFree)" /proc/meminfo
echo
echo "--- top 12 processes by memory (RSS MB)"
ps -eo rss,pid,user,comm,args --sort=-rss | awk 'NR==1{print "   RSS_MB  PID USER     COMMAND"; next} NR<=13{printf "%8.1f %5d %-8s %s\n", $1/1024, $2, $3, substr($0, index($0,$5), 90)}'
echo
echo "--- sampling CPU, temperature and throttling for ${SECS}s ..."
read -r _ u n s i w q sq st _ < /proc/stat
t0=$((u+n+s+i+w+q+sq+st)); idle0=$((i+w))
declare -A cpu0
while read -r pid t; do cpu0[$pid]=$t; done < <(ps -eo pid=,times=)
temps=(); steps=$((SECS/10)); [ "$steps" -lt 1 ] && steps=1
for ((k=0; k<steps; k++)); do
    temps+=("$(vcgencmd measure_temp 2>/dev/null | tr -dc '0-9.')")
    sleep "$((SECS/steps))"
done
read -r _ u n s i w q sq st _ < /proc/stat
t1=$((u+n+s+i+w+q+sq+st)); idle1=$((i+w))
busy=$(( ( (t1-t0) - (idle1-idle0) ) * 1000 / (t1-t0) ))
echo "whole-system CPU busy: $((busy/10)).$((busy%10))% (all 4 cores = 100%)"
hz=$(getconf CLK_TCK)
echo "top CPU users over the window (% of ONE core):"
while read -r pid t comm; do
    [ -n "${cpu0[$pid]:-}" ] || continue
    d=$(( t - cpu0[$pid] )); [ "$d" -gt 0 ] || continue
    printf "%6.1f%%  %6d  %s\n" "$(echo "$d $SECS" | awk '{print $1*100/$2}')" "$pid" "$comm"
done < <(ps -eo pid=,times=,comm=) | sort -rn | head -8
printf "temperature samples (C): %s\n" "${temps[*]}"
echo "throttled flags: $(vcgencmd get_throttled 2>/dev/null)   (0x0 = never throttled/under-volted since boot)"
echo "load average: $(cut -d' ' -f1-3 /proc/loadavg)"
echo
echo "--- GPU / display memory"
vcgencmd get_mem gpu 2>/dev/null
grep -E "dtoverlay=vc4|cma" /boot/firmware/config.txt /boot/firmware/cmdline.txt 2>/dev/null
echo
echo "--- disk"
df -h / | tail -1
echo
echo "--- kiosk-related services"
systemctl list-units --type=service --state=running --no-legend 2>/dev/null | grep -iE "kiosk|chrom|labwc|cage|wayfire|renderer|watchdog|lightdm|getty" | awk '{print "  " $1}'
