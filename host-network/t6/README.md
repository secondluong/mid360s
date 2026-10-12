# Host network (NanoPC-T6 + Livox Mid-360)

Authoritative runtime config lives under `/etc` on the board.
`scripts/install-t6.sh` writes these profiles; the files here are templates.

## Roles

| Role | Typical iface | Address | Default route? |
|------|---------------|---------|----------------|
| Mid-360 | `lidar0` (pinned, see below) | `192.168.1.50/24` | no |
| WiFi STA | `wlan0` / `wlp*` | DHCP from existing router | no (`never-default`) |
| 4G | `wwan0` / `usb0` / `ppp0` | carrier DHCP | **yes** |

Mid-360 itself: `192.168.1.1XX` (last two digits of the serial). Same Livox-SDK2 `lidar_type: 8` for Mid-360 / Mid-360s.

## T6 eth0/eth1 swap

Do not rely on `eth0` staying on the same RJ45. The two r8125 ports sit on
separate PCIe controllers, and the vendor's `50-friendlyelec.link` sets no
`NamePolicy`, so both keep the kernel's probe-order names — a swap would hand
`PC_IP` to the LAN-facing port and leave the Mid-360 unreachable.

`scripts/install-t6.sh` pins it on both sides. To do it by hand:

```bash
# 1. The port's stable PCI path
udevadm info -q property /sys/class/net/eth0 | sed -n 's/^ID_PATH=//p'

# 2. Name it lidar0 from the next boot (see lidar-nic.link.example)
sudo install -m 644 /tmp/10-lidar-nic.link /etc/systemd/network/10-lidar-nic.link
udevadm test /sys/class/net/eth0 2>&1 | grep "NAME 'lidar0'"   # verify first

# 3. Make the 192.168.1.50 profile follow the port, not the name
sudo nmcli con mod Wired-lidar \
  match.path "platform-fe190000.pcie-pci-0004:41:00.0" connection.interface-name ""
```

Then leave `LIDAR_INTERFACE=auto` in `.env`. `detect_lidar_interface()` in
`scripts/net-common.sh` prefers `lidar0`, falls back to whichever port holds
`PC_IP`, and only then to `eth0` — so it is correct before and after the reboot
that applies the rename, and on boards with no `.link` file at all.

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

蒲公英客户端已从这块板卸掉（`pgyvpn` 包、`oray_vnc`、主机名挂钩）。远程查看走板子当前上网的地址，例如公司 WiFi 的 `wlan0`。
