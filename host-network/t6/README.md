# Host network (NanoPC-T6 + Livox Mid-360)

Authoritative runtime config lives under `/etc` on the board.
`scripts/install-t6.sh` writes these profiles; the files here are templates.

## Roles

| Role | Typical iface | Address | Default route? |
|------|---------------|---------|----------------|
| Mid-360 | `eth0` / `enP*` (pin with udev) | `192.168.1.50/24` | no |
| WiFi STA | `wlan0` / `wlp*` | DHCP from existing router | no (`never-default`) |
| 4G | `wwan0` / `usb0` / `ppp0` | carrier DHCP | **yes** |

Mid-360 itself: `192.168.1.1XX` (last two digits of the serial). Same Livox-SDK2 `lidar_type: 8` for Mid-360 / Mid-360s.

## T6 eth0/eth1 swap

Do not rely on `eth0` staying on the same RJ45. After first boot:

```bash
ip -br link
udevadm info -a -p /sys/class/net/<iface>
```

Bind a stable name with a `.link` file on the PCI path, then set `LIDAR_INTERFACE` in `.env`.

## Install sketch

```bash
# Prefer: sudo ./scripts/install-t6.sh --lidar-ip 192.168.1.1XX

# Manual Ethernet → Mid-360 (edit interface-name + IPs first):
sudo install -m 600 host-network/t6/Wired-lidar.nmconnection \
  /etc/NetworkManager/system-connections/Wired-lidar.nmconnection

# WiFi STA — copy example, set ssid + psk, then:
# sudo install -m 600 /tmp/wifi-sta.nmconnection \
#   /etc/NetworkManager/system-connections/wifi-sta.nmconnection

sudo nmcli connection reload
sudo nmcli connection up Wired-lidar
# 4G: use the board vendor's ModemManager profile; keep it as the default route.
```

WiFi PSK is never committed.
