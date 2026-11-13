#!/bin/sh
# Run as root by pistatus-power.path when the Status page asks to reboot or shut down.
# Only these exact words are acted on; anything else is logged and dropped.
REQUEST=/run/pistatus/power-request
[ -f "$REQUEST" ] || exit 0
ACTION=$(head -c 16 "$REQUEST" | tr -d '\n')
rm -f "$REQUEST"
case "$ACTION" in
  reboot) echo "Status page asked to reboot"; exec systemctl reboot ;;
  poweroff) echo "Status page asked to shut down"; exec systemctl poweroff ;;
  test) echo "Status page power test, nothing done" ;;
  *) echo "Ignored power request: $ACTION" ;;
esac
