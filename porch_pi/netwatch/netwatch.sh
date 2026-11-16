#!/bin/sh
# Run each minute by netwatch.timer. The RTL8188CUS dongle (rtl8192cu) sometimes stops passing traffic while
# the system carries on, which systemd's hardware watchdog can't see. If the router stops answering:
# after 3 minutes reconnect Wi-Fi, after 6 reload the driver, after 10 reboot (not within 15 minutes of boot).
set -u
state=/run/netwatch/fails
mkdir -p /run/netwatch
gateway=$(ip route show default | awk '{print $3; exit}')

if [ -n "$gateway" ] && ping -c 3 -W 2 -q "$gateway" >/dev/null 2>&1; then
  if [ -s "$state" ] && [ "$(cat "$state")" -gt 0 ]; then
    echo "router answering again after $(cat "$state") minutes"
  fi
  echo 0 > "$state"
  exit 0
fi

fails=$(( $(cat "$state" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$state"
echo "router ${gateway:-(no default route)} not answering for $fails minutes"

case "$fails" in
  3)
    # The Wi-Fi tracker's dongle is unmanaged, so the managed one is the network's, whatever it's called
    iface=$(nmcli -t -f DEVICE,TYPE,STATE device | awk -F: '$2 == "wifi" && $3 != "unmanaged" {print $1; exit}')
    echo "reconnecting ${iface:-wlan0}"
    nmcli device disconnect "${iface:-wlan0}"
    nmcli device connect "${iface:-wlan0}"
    ;;
  6)
    echo "reloading the rtl8192cu driver"
    modprobe -r rtl8192cu && sleep 2 && modprobe rtl8192cu
    ;;
esac

if [ "$fails" -ge 10 ]; then
  if [ "$(cut -d. -f1 /proc/uptime)" -ge 900 ]; then
    echo "rebooting"
    systemctl reboot
  fi
fi
