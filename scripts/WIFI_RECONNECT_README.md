# Rollback / install notes for safe WiFi reconnect (host, not Docker).

## Install (does NOT restart NetworkManager, does NOT change IPs)

```bash
cd /home/longqr/lidar-bridge-ros
# Copy units (use sudo or docker-root). Paths assume this repo location.
sudo cp scripts/wifi-power-save-off.service /etc/systemd/system/
sudo cp scripts/wifi-reconnect-watchdog.service /etc/systemd/system/
sudo cp scripts/wifi-reconnect-watchdog.timer /etc/systemd/system/
sudo cp scripts/99-wifi-powersave-off /etc/NetworkManager/dispatcher.d/99-wifi-powersave-off
sudo chmod 755 /home/longqr/lidar-bridge-ros/scripts/wifi-reconnect-watchdog.sh
sudo chmod 755 /etc/NetworkManager/dispatcher.d/99-wifi-powersave-off
sudo touch /var/log/wifi-reconnect-watchdog.log
sudo chmod 644 /var/log/wifi-reconnect-watchdog.log
sudo systemctl daemon-reload
sudo systemctl enable --now wifi-power-save-off.service
sudo systemctl enable --now wifi-reconnect-watchdog.timer
```

## Rollback (undo watchdog only; netplan/NM profiles untouched)

```bash
sudo systemctl disable --now wifi-reconnect-watchdog.timer wifi-reconnect-watchdog.service wifi-power-save-off.service
sudo rm -f /etc/NetworkManager/dispatcher.d/99-wifi-powersave-off
sudo rm -f /etc/systemd/system/wifi-power-save-off.service
sudo rm -f /etc/systemd/system/wifi-reconnect-watchdog.service
sudo rm -f /etc/systemd/system/wifi-reconnect-watchdog.timer
sudo systemctl daemon-reload
```

If WiFi is broken and you need configs: restore from `~/wifi-working-backup/` (contains secrets; not in git).

## Soft test

```bash
nmcli device disconnect wlan0
# wait <= 90s
ip -4 -br addr show wlan0
iwconfig wlan0 | grep -E 'ESSID|Access Point'
tail -20 /var/log/wifi-reconnect-watchdog.log
```
