# Host network config (Pi5) — reference copy for git

Authoritative runtime config lives under `/etc` on the Pi.
This directory is a **sanitized mirror** of the working setup as of the last export.

## Fixed addresses (do not change casually)

| Interface | IP | NM connection |
|-----------|----|---------------|
| `eth0` | `192.168.1.2/24` | `Wired connection 1` (`never-default`, route `192.168.1.62/32`) |
| `wlan0` | `192.168.1.61/24` | `CU_LONGQR_5G` (gateway `192.168.1.1`) |

## Layout

```
host-network/
  NetworkManager/NetworkManager.conf
  NetworkManager/conf.d/dns.conf
  NetworkManager/system-connections/
    CU_LONGQR_5G.nmconnection.example   # PSK redacted — set before install
    Wired connection 1.nmconnection     # eth0 (no secrets)
  netplan/90-NM-*.yaml                  # renderer: NetworkManager only
  cloud.cfg.d/99-disable-network-config.cfg
  dhcpcd.conf.stub                      # dhcpcd not used; comments only
  LIVE_SNAPSHOT.txt                     # non-secret status dump
```

Related host units (already in [`scripts/`](../scripts/)):

- WiFi power-save off + reconnect watchdog
- Network status logger

## Secrets

- **WiFi PSK is never committed.** Use `CU_LONGQR_5G.nmconnection.example` and replace `<SET_WIFI_PSK>`.
- Do not copy real `*.nmconnection` with a live `psk=` into this tree.

## Restore sketch (manual, careful)

```bash
# 1) Fill PSK
cp host-network/NetworkManager/system-connections/CU_LONGQR_5G.nmconnection.example /tmp/CU_LONGQR_5G.nmconnection
# edit psk=... then:
sudo install -m 600 /tmp/CU_LONGQR_5G.nmconnection \
  /etc/NetworkManager/system-connections/CU_LONGQR_5G.nmconnection
sudo install -m 600 host-network/NetworkManager/system-connections/Wired\ connection\ 1.nmconnection \
  /etc/NetworkManager/system-connections/
sudo cp host-network/NetworkManager/NetworkManager.conf /etc/NetworkManager/
sudo cp host-network/NetworkManager/conf.d/dns.conf /etc/NetworkManager/conf.d/
sudo cp host-network/cloud.cfg.d/99-disable-network-config.cfg /etc/cloud/cloud.cfg.d/
sudo cp host-network/netplan/*.yaml /etc/netplan/
sudo nmcli connection reload
# Prefer: nmcli connection up CU_LONGQR_5G
# Avoid: systemctl restart NetworkManager unless necessary
```

See also [`.cursor/rules/fixed-pi-ips.mdc`](../.cursor/rules/fixed-pi-ips.mdc).
