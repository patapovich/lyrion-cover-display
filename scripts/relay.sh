#!/bin/sh
# HDMI converter power relay (GPIO17) + Pi HDMI signal (vcgencmd), driven together
# by lms_cover_display via hdmi_on_cmd / hdmi_off_cmd / hdmi_query_cmd.
#
#   on    Pi HDMI signal up, then close relay (converter powered).
#   off   Open relay (converter unpowered), then drop Pi HDMI signal.
#   query Report Pi HDMI state ("display_power=0|1"); the app parses the last char.
#
# ACTIVE_LOW=1 => driving IN low energizes the relay (common opto-isolated
# default). If the relay is inverted (converter dark at boot / on when idle),
# set ACTIVE_LOW=0 AND change /boot/firmware/config.txt to `gpio=17=op,dh`.
PIN=17
ACTIVE_LOW=1

on_level()  { [ "$ACTIVE_LOW" = 1 ] && echo dl || echo dh; }
off_level() { [ "$ACTIVE_LOW" = 1 ] && echo dh || echo dl; }

case "$1" in
  on)    vcgencmd display_power 1 >/dev/null; pinctrl set "$PIN" op "$(on_level)"  ;;
  off)   pinctrl set "$PIN" op "$(off_level)"; vcgencmd display_power 0 >/dev/null ;;
  query) vcgencmd display_power ;;
  *)     echo "usage: $0 {on|off|query}" >&2; exit 2 ;;
esac
