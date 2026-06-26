# Rollback / install notes for safe WiFi reconnect (host, not Docker).

## Dual environments

See [WIFI_ENV_SWITCH.md](WIFI_ENV_SWITCH.md) for company (`CU_LONGQR_5G`) vs mesh (`mesh61`).

```bash
./scripts/switch-wifi-env.sh company   # office + internet
./scripts/switch-wifi-env.sh mesh      # offline mesh LAN
./scripts/switch-wifi-env.sh auto      # unpin; connect stronger of the two
./scripts/switch-wifi-env.sh status
```

## Install (does NOT restart NetworkManager, does NOT change IPs)

```bash
cd /home/longqr/lidar-bridge-ros
sudo cp scripts/wifi-power-save-off.service /etc/systemd/system/
sudo cp scripts/wifi-reconnect-watchdog.service /etc/systemd/system/
sudo cp scripts/wifi-reconnect-watchdog.timer /etc/systemd/system/
sudo cp scripts/99-wifi-powersave-off /etc/NetworkManager/dispatcher.d/99-wifi-powersave-off
sudo chmod 755 /home/longqr/lidar-bridge-ros/scripts/wifi-reconnect-watchdog.sh
sudo chmod 755 /home/longqr/lidar-bridge-ros/scripts/switch-wifi-env.sh
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

If WiFi is broken: restore from `~/wifi-working-backup/` (secrets; not in git).

## Soft test

```bash
nmcli device disconnect wlan0
# wait <= 90s
ip -4 -br addr show wlan0
iwconfig wlan0 | grep -E 'ESSID|Access Point'
tail -20 /var/log/wifi-reconnect-watchdog.log
```
