# WiFi environments (company + mesh)

Pi uses the **same** LAN address in both places: `192.168.1.61`.
MyView stays `192.168.1.100` in both places. Only the SSID changes.

| Env | SSID | NM connection | Autoconnect priority | Default gateway |
|-----|------|---------------|----------------------|-----------------|
| company | `CU_LONGQR_5G` | `CU_LONGQR_5G` | 80 (equal) | `192.168.1.1` (internet) |
| mesh | `mesh61` | `mesh61` | 80 (equal) | none (`never-default`) |

eth0 remains `192.168.1.2` for L2 (`Wired connection 1`) and is not switched.

## Automatic (stronger signal wins)

Both profiles have `autoconnect=yes` and the **same** autoconnect priority.
Selection of which of the two to use is done by scan SIGNAL (0–100):

- Only company → joins company
- Only mesh → joins mesh
- Both visible → **stronger SIGNAL wins**
- Already connected + `desired=auto` → roam only if the other SSID is stronger by **≥15** SIGNAL points (hysteresis, avoids flapping)

Watchdog (`wifi-reconnect-watchdog`, every ~20s) recovers after drop by trying the stronger profile first, then the other. While healthy in `auto`, it may roam when the other hotspot is clearly stronger.

Desired pin file: `/var/lib/wifi-env/desired` = `company` | `mesh` | `auto`.

Default on this Pi: `auto`.

## Manual switch / lock

```bash
/home/longqr/lidar-bridge-ros/scripts/switch-wifi-env.sh company   # lock office
/home/longqr/lidar-bridge-ros/scripts/switch-wifi-env.sh mesh      # lock mesh
/home/longqr/lidar-bridge-ros/scripts/switch-wifi-env.sh auto      # unlock; pick stronger now
/home/longqr/lidar-bridge-ros/scripts/switch-wifi-env.sh status
```

Optional alias (already in `~/.bashrc` on this Pi):

```bash
alias wifi-env='/home/longqr/lidar-bridge-ros/scripts/switch-wifi-env.sh'
```

## Secrets

WiFi PSKs live only in `/etc/NetworkManager/system-connections/` on the Pi.
Examples under `host-network/` use `<SET_WIFI_PSK>` — **do not commit real passwords**.
