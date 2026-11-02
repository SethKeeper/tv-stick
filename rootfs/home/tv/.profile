# Login shell for the TV user. On the TV screen (tty1) it starts the TV session,
# unless the stick was started from the "text console" boot menu entry.
if [ "$(tty)" = "/dev/tty1" ] && ! grep -qw tv.console /proc/cmdline; then
    exec /usr/local/bin/tv-sway
fi
