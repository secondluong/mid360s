#!/usr/bin/env python3
"""Board settings from the :8080 page: lidar IP, MyView push, egress selection.

Shared by the on-board viewer (settings page), the relay (where to push) and
fix-routes.sh (which uplink carries the push). The settings live in
myview.json next to route.json, both under the settings dir that
docker-compose mounts from ./data/settings.

Egress policy for the MyView host only (the board's default route is left to
fix-routes.sh): WiFi when it can reach MyView, otherwise 4G when 4G reaches
the public internet. The choice is a /32 in the main table, so the relay
needs no socket options. Each uplink also gets a source-address table so the
other one can still be probed while it is not carrying the push.

CLI:
  myview_settings.py shell            shell assignments for entrypoint/fix-routes
  myview_settings.py lidar-ip         lidar IP in effect (settings page, else .env)
  myview_settings.py gateway DEV      router on DEV, even without nmcli
  myview_settings.py signature        changes whenever the relay must restart
  myview_settings.py route [--force]  pick and pin the MyView egress
  myview_settings.py test             probe both uplinks, print JSON
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from typing import Dict, Optional, Tuple

SETTINGS = "myview.json"
LIDAR = "lidar.json"
ROUTE = "route.json"
ROUTE_STATE = ".route-state.json"
RELAY_STATUS = "relay-status.json"

DEFAULT_PORT = 3000
PIN_METRIC = "50"
WIFI_TABLE, WAN_TABLE = "101", "102"
WIFI_PRIO, WAN_PRIO = "1101", "1102"
PUBLIC_PROBES = (("223.5.5.5", 53), ("114.114.114.114", 53), ("8.8.8.8", 53))
CHECK_S = float(os.environ.get("MYVIEW_ROUTE_CHECK_S", "10"))
# Consecutive failed WiFi probes before moving the push to 4G, and
# consecutive good ones before moving it back. Stops a flaky AP from
# restarting the relay every check.
SWITCH_STREAK = int(os.environ.get("MYVIEW_ROUTE_SWITCH_STREAK", "2"))

HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)
DEVICE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PLACEHOLDER_HOSTS = ("your-myview-host", "host.docker.internal")


# ── settings file ────────────────────────────────────────────────────

def settings_dir() -> str:
    d = os.environ.get("MYVIEW_SETTINGS_DIR")
    if d:
        return d
    d = "/var/lib/myview-relay/settings"
    if os.path.isdir(d):
        return d
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "data", "settings"))


def path(name: str) -> str:
    return os.path.join(settings_dir(), name)


def read_json(name: str) -> dict:
    try:
        with open(path(name), "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(name: str, obj: dict) -> None:
    d = settings_dir()
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        # myview.json carries the bridge token.
        os.chmod(tmp, 0o600 if name == SETTINGS else 0o644)
        os.replace(tmp, path(name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def env_defaults() -> dict:
    """Used only until the settings page has saved myview.json once."""
    host, port, secure = "", DEFAULT_PORT, False
    m = re.match(r"^(wss?)://([^/:]+)(?::(\d+))?", _env("MYVIEW_SERVER_URL"))
    if m and not any(p in m.group(2) for p in PLACEHOLDER_HOSTS):
        secure = m.group(1) == "wss"
        host = m.group(2)
        port = int(m.group(3) or DEFAULT_PORT)
    return {
        "enabled": _truthy(_env("MYVIEW_RELAY", "false")) and bool(host),
        "host": host,
        "port": port,
        "secure": secure,
        "token": _env("MYVIEW_BRIDGE_TOKEN"),
        "deviceId": _env("MYVIEW_DEVICE_ID", "lidar-t6-mid360s-01"),
        "cloudHz": float(_env("RELAY_CLOUD_HZ", "5")),
        "cloudMaxPoints": int(_env("RELAY_CLOUD_MAX_POINTS", "8000")),
    }


def _valid_ipv4(h: str) -> bool:
    try:
        ipaddress.IPv4Address(h)
        return True
    except ValueError:
        return False


def merge(update: dict, base: dict) -> Tuple[dict, Dict[str, str]]:
    """Apply update on top of base. Invalid fields keep the base value."""
    out = dict(base)
    errors: Dict[str, str] = {}
    out.pop("updatedAt", None)

    if "enabled" in update:
        out["enabled"] = _truthy(update["enabled"]) if isinstance(update["enabled"], str) else bool(update["enabled"])
    if "secure" in update:
        out["secure"] = bool(update["secure"])

    port_given = "port" in update and str(update.get("port") or "").strip() != ""
    if "host" in update:
        h = str(update.get("host") or "").strip()
        h = re.sub(r"^(wss?|https?)://", "", h, flags=re.I).split("/")[0]
        if h.count(":") == 1:
            h, p = h.split(":", 1)
            if not port_given:
                update = dict(update, port=p)
                port_given = True
        if h and (not HOST_RE.match(h) or (re.match(r"^[\d.]+$", h) and not _valid_ipv4(h))):
            errors["host"] = "IP 或域名格式不对"
        else:
            out["host"] = h

    if port_given:
        try:
            p = int(str(update["port"]).strip())
            if not 1 <= p <= 65535:
                raise ValueError
            out["port"] = p
        except ValueError:
            errors["port"] = "端口要在 1-65535 之间"

    if update.get("clearToken"):
        out["token"] = ""
    elif isinstance(update.get("token"), str) and update["token"].strip():
        out["token"] = update["token"].strip()

    if "deviceId" in update:
        d = str(update.get("deviceId") or "").strip()
        if not DEVICE_RE.match(d):
            errors["deviceId"] = "设备 ID 只能用字母、数字、点、下划线和横线，最长 64 位"
        else:
            out["deviceId"] = d

    if "cloudHz" in update:
        try:
            v = float(update["cloudHz"])
            if not 0.2 <= v <= 20:
                raise ValueError
            out["cloudHz"] = v
        except (TypeError, ValueError):
            errors["cloudHz"] = "点云频率要在 0.2-20 Hz 之间"

    if "cloudMaxPoints" in update:
        try:
            v = int(float(update["cloudMaxPoints"]))
            if not 500 <= v <= 100000:
                raise ValueError
            out["cloudMaxPoints"] = v
        except (TypeError, ValueError):
            errors["cloudMaxPoints"] = "每帧最大点数要在 500-100000 之间"

    if out.get("enabled") and not out.get("host"):
        errors.setdefault("host", "启用推送前要先填 MyView IP")
    return out, errors


def load() -> dict:
    s, _ = merge(read_json(SETTINGS), env_defaults())
    return s


def save(update: dict) -> Tuple[Optional[dict], Dict[str, str]]:
    new, errors = merge(update, load())
    if errors:
        return None, errors
    new["updatedAt"] = time.time()
    write_json(SETTINGS, new)
    return new, {}


def load_lidar() -> dict:
    """Lidar IP from the settings page, else LIDAR_IP from .env."""
    ip = str(read_json(LIDAR).get("lidarIp") or "").strip()
    if not _valid_ipv4(ip):
        ip = _env("LIDAR_IP", "192.168.1.12")
    return {"lidarIp": ip, "pcIp": _env("PC_IP", "192.168.1.50")}


def save_lidar(update: dict) -> Tuple[Optional[dict], Dict[str, str]]:
    ip = str(update.get("lidarIp") or "").strip()
    pc = _env("PC_IP", "192.168.1.50")
    if not _valid_ipv4(ip):
        return None, {"lidarIp": "雷达 IP 格式不对，例如 192.168.1.114"}
    if ip == pc:
        return None, {"lidarIp": f"不能和主机电口地址 {pc} 相同"}
    # The Mid-360 only streams to a host on its own /24.
    if ip.rsplit(".", 1)[0] != pc.rsplit(".", 1)[0]:
        return None, {"lidarIp": f"雷达 IP 要和主机电口 {pc} 在同一网段（{pc.rsplit('.', 1)[0]}.x）"}
    last = int(ip.rsplit(".", 1)[1])
    if last in (0, 255):
        return None, {"lidarIp": "雷达 IP 最后一段不能是 0 或 255"}
    write_json(LIDAR, {"lidarIp": ip, "updatedAt": time.time()})
    return load_lidar(), {}


def server_url(s: dict) -> str:
    if not s.get("host"):
        return ""
    return f"{'wss' if s.get('secure') else 'ws'}://{s['host']}:{int(s.get('port') or DEFAULT_PORT)}"


def public_view(s: dict) -> dict:
    v = {k: s[k] for k in s if k != "token"}
    v["hasToken"] = bool(s.get("token"))
    v["url"] = server_url(s)
    return v


def signature(s: dict) -> str:
    """Changes whenever the relay has to reconnect with new parameters."""
    body = json.dumps({k: s.get(k) for k in sorted(s) if k != "updatedAt"}, sort_keys=True)
    route = read_json(ROUTE)
    enabled = "1" if s.get("enabled") and s.get("host") else "0"
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    return f"{enabled}|{digest}|{route.get('via', '')}|{route.get('iface', '')}"


# ── network helpers ──────────────────────────────────────────────────

def _run(args, timeout: float = 5.0) -> Tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def _ip(*args: str) -> Tuple[int, str]:
    return _run(["ip", *args])


def _is_wifi(name: str) -> bool:
    return name.startswith(("wl",))


def _is_wan(name: str) -> bool:
    return name.startswith(("wwan", "wwp", "ppp")) or re.match(r"^usb\d+$", name) is not None


def uplinks() -> Dict[str, dict]:
    """WiFi and 4G interfaces that currently hold an IPv4 address."""
    lan = _env("LAN_INTERFACE", "auto")
    wan = _env("WAN_INTERFACE", "auto")
    lidar = _env("LIDAR_INTERFACE", "")
    found: Dict[str, dict] = {}
    _, out = _ip("-4", "-o", "addr", "show", "scope", "global")
    for line in out.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        name, addr, plen = m.group(1), m.group(2), int(m.group(3))
        if name == lidar or name.startswith(("lidar", "docker", "br-", "veth")):
            continue
        info = {"iface": name, "addr": addr, "prefix": plen}
        if "wifi" not in found and (name == lan if lan != "auto" else _is_wifi(name)):
            found["wifi"] = info
        elif "wan" not in found and (name == wan if wan != "auto" else _is_wan(name)):
            found["wan"] = info
    return found


def _first_ipv4(text: str) -> Optional[str]:
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", text or "")
    return m.group(1) if m else None


def gateway(dev: str, table: str, cache: dict) -> Optional[str]:
    for extra in ((), ("table", table)):
        _, out = _ip("-4", "route", "show", "default", "dev", dev, *extra)
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    # WiFi is never-default on this board, so the kernel may hold no gateway.
    rc, out = _run(["nmcli", "-g", "IP4.GATEWAY", "device", "show", dev], timeout=3)
    gw = _first_ipv4(out) if rc == 0 else None
    if gw:
        return gw
    rc, out = _run(["nmcli", "-g", "DHCP4.OPTION", "device", "show", dev], timeout=3)
    if rc == 0:
        m = re.search(r"routers\s*=\s*(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    return cache.get(dev) or guess_gateway(dev)


def _nameservers() -> list:
    found = []
    for name in ("/run/NetworkManager/resolv.conf", "/etc/resolv.conf"):
        try:
            with open(name, encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == "nameserver" and parts[1] not in found:
                        found.append(parts[1])
        except OSError:
            continue
    return found


def guess_gateway(dev: str) -> Optional[str]:
    """Router on dev when neither the kernel nor nmcli knows it.

    The container has no nmcli. An on-link nameserver is usually the home
    router, else the subnet's .1. Taken only if it answers on dev.
    """
    _, out = _ip("-4", "-o", "addr", "show", "dev", dev, "scope", "global")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out)
    if not m:
        return None
    iface = ipaddress.ip_interface(m.group(1))
    candidates = []
    for ns in _nameservers():
        try:
            if ipaddress.IPv4Address(ns) in iface.network and ns != str(iface.ip):
                candidates.append(ns)
        except ValueError:
            continue
    first = str(next(iface.network.hosts(), ""))
    if first and first != str(iface.ip) and first not in candidates:
        candidates.append(first)
    for ip in candidates:
        rc, _ = _run(["ping", "-c", "1", "-W", "1", "-I", dev, ip], timeout=3)
        if rc == 0:
            return ip
    return None


def _rule_lines(prio: str):
    _, out = _ip("rule", "show")
    return [ln for ln in out.splitlines() if ln.startswith(f"{prio}:")]


def ensure_source_table(link: dict, gw: Optional[str], table: str, prio: str) -> None:
    """Traffic sourced from this uplink's address leaves through it."""
    dev, addr = link["iface"], link["addr"]
    net = str(ipaddress.ip_interface(f"{addr}/{link['prefix']}").network)
    _ip("route", "replace", net, "dev", dev, "src", addr, "table", table)
    if gw:
        _ip("route", "replace", "default", "via", gw, "dev", dev, "table", table)
    else:
        _ip("route", "replace", "default", "dev", dev, "table", table)
    lines = _rule_lines(prio)
    if any(f"from {addr} lookup {table}" in ln for ln in lines) and len(lines) == 1:
        return
    for _ in range(len(lines)):
        _ip("rule", "del", "priority", prio)
    _ip("rule", "add", "from", f"{addr}/32", "lookup", table, "priority", prio)


def drop_source_table(table: str, prio: str) -> None:
    for _ in range(len(_rule_lines(prio))):
        _ip("rule", "del", "priority", prio)
    _ip("route", "flush", "table", table)


def resolve(host: str) -> Optional[str]:
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        return infos[0][4][0] if infos else None
    except OSError:
        return None


def probe(src: Optional[str], host: str, port: int, timeout: float = 3.0) -> Tuple[bool, str, int]:
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        if src:
            s.bind((src, 0))
        s.connect((host, int(port)))
        return True, "", int((time.time() - t0) * 1000)
    except OSError as e:
        msg = "连接超时" if isinstance(e, socket.timeout) else (e.strerror or str(e))
        return False, msg, int((time.time() - t0) * 1000)
    finally:
        s.close()


def has_internet(src: str) -> bool:
    return any(probe(src, h, p, timeout=2.0)[0] for h, p in PUBLIC_PROBES)


def _in_net(ip: str, link: dict) -> bool:
    net = ipaddress.ip_interface(f"{link['addr']}/{link['prefix']}").network
    return ipaddress.IPv4Address(ip) in net


def route_dev(ip: str) -> str:
    _, out = _ip("route", "get", ip)
    m = re.search(r"\bdev (\S+)", out)
    return m.group(1) if m else ""


# ── egress selection ─────────────────────────────────────────────────

def _prepare_uplinks(state: dict) -> Dict[str, dict]:
    links = uplinks()
    gw_cache = state.setdefault("gw", {})
    for role, table, prio in (("wifi", WIFI_TABLE, WIFI_PRIO), ("wan", WAN_TABLE, WAN_PRIO)):
        link = links.get(role)
        if not link:
            drop_source_table(table, prio)
            continue
        gw = gateway(link["iface"], table, gw_cache)
        if gw:
            gw_cache[link["iface"]] = gw
        link["gw"] = gw
        ensure_source_table(link, gw, table, prio)
    return links


def _pin(ip: Optional[str], via: str, links: Dict[str, dict], state: dict) -> None:
    old = state.get("pinned")
    if old and old != ip:
        _ip("route", "del", f"{old}/32", "metric", PIN_METRIC)
    if not ip or via == "none":
        if ip:
            _ip("route", "del", f"{ip}/32", "metric", PIN_METRIC)
        state["pinned"] = None
        return
    link = links["wifi" if via == "wifi" else "wan"]
    cmd = ["route", "replace", f"{ip}/32"]
    if link.get("gw") and not _in_net(ip, link):
        cmd += ["via", link["gw"]]
    cmd += ["dev", link["iface"], "metric", PIN_METRIC]
    _ip(*cmd)
    state["pinned"] = ip


def select_route(force: bool = False) -> dict:
    s = load()
    state = read_json(ROUTE_STATE)
    links = _prepare_uplinks(state)
    now = time.time()
    wifi, wan = links.get("wifi"), links.get("wan")

    result = {
        "enabled": bool(s.get("enabled") and s.get("host")),
        "host": s.get("host", ""),
        "port": s.get("port", DEFAULT_PORT),
        "ip": None,
        "via": "none",
        "iface": "",
        "reason": "",
        "wifi": {"iface": wifi["iface"], "addr": wifi["addr"], "gw": wifi.get("gw")} if wifi else None,
        "wan": {"iface": wan["iface"], "addr": wan["addr"], "gw": wan.get("gw")} if wan else None,
        "checkedAt": now,
    }

    if not result["enabled"]:
        _pin(None, "none", links, state)
        result["reason"] = "未启用推送"
        state["decision"] = None
        _finish(result, state)
        return result

    ip = resolve(s["host"])
    result["ip"] = ip
    if not ip:
        _pin(None, "none", links, state)
        result["reason"] = f"无法解析 {s['host']}"
        _finish(result, state)
        return result

    prev = state.get("decision") or {}
    key = f"{ip}:{s['port']}"
    same_links = prev.get("links") == {r: (l["iface"], l["addr"]) for r, l in links.items()}
    if not force and prev.get("key") == key and same_links and now - prev.get("checkedAt", 0) < CHECK_S:
        via = prev.get("via", "none")
        if via != "none" and ("wifi" if via == "wifi" else "wan") not in links:
            via = "none"
        result.update({k: prev[k] for k in ("reason", "wifiReach", "wanInternet") if k in prev})
        if result["wifi"] is not None:
            result["wifi"]["reachable"] = prev.get("wifiReach")
            result["wifi"]["error"] = prev.get("wifiError", "")
        if result["wan"] is not None:
            result["wan"]["internet"] = prev.get("wanInternet")
    else:
        wifi_ok, wifi_err = False, "未连接 WiFi"
        if wifi:
            wifi_ok, wifi_err, _ = probe(wifi["addr"], ip, s["port"])
            result["wifi"]["reachable"] = wifi_ok
            result["wifi"]["error"] = wifi_err
        wan_net = bool(wan) and has_internet(wan["addr"])
        if wan:
            result["wan"]["internet"] = wan_net

        streak = state.setdefault("streak", {"ok": 0, "fail": 0})
        if wifi_ok:
            streak["ok"], streak["fail"] = streak.get("ok", 0) + 1, 0
        else:
            streak["ok"], streak["fail"] = 0, streak.get("fail", 0) + 1
        cur = prev.get("via") if prev.get("key") == key else None

        if wifi_ok and (cur != "4g" or streak["ok"] >= SWITCH_STREAK or not wan_net):
            via, reason = "wifi", "WiFi 可达 MyView"
        elif wifi_ok:
            via, reason = "4g", "WiFi 已恢复，再确认一次后切回 WiFi"
        elif wifi and cur == "wifi" and streak["fail"] < SWITCH_STREAK:
            via, reason = "wifi", f"WiFi 暂时连不上 MyView（{wifi_err}），再确认一次"
        elif wan_net:
            via = "4g"
            reason = "没有 WiFi，走 4G" if not wifi else f"WiFi 连不上 MyView（{wifi_err}），改走 4G"
        elif wifi and wifi.get("gw"):
            via, reason = "wifi", f"WiFi 连不上 MyView（{wifi_err}），4G 也不可用"
        else:
            via, reason = "none", "WiFi 和 4G 都不可用"

        prev = {
            "key": key,
            "via": via,
            "reason": reason,
            "wifiReach": wifi_ok if wifi else None,
            "wifiError": wifi_err if wifi else "",
            "wanInternet": wan_net if wan else None,
            "links": {r: (l["iface"], l["addr"]) for r, l in links.items()},
            "checkedAt": now,
        }
        state["decision"] = prev
        result["reason"] = reason

    _pin(ip, via, links, state)
    result["via"] = via
    if via != "none":
        result["iface"] = links["wifi" if via == "wifi" else "wan"]["iface"]
    result["checkedAt"] = prev.get("checkedAt", now)
    _finish(result, state)
    return result


def _finish(result: dict, state: dict) -> None:
    summary = f"{result['via']}@{result['iface'] or '-'} {result.get('ip') or '-'}:{result['port']} {result['reason']}"
    if state.get("lastLog") != summary:
        print(f"[myview-route] {summary}", flush=True)
        state["lastLog"] = summary
    try:
        write_json(ROUTE_STATE, state)
        write_json(ROUTE, result)
    except OSError as e:
        print(f"[myview-route] WARN: cannot write route state: {e}", file=sys.stderr)


def test_connection(s: dict) -> dict:
    """Probe the target over each uplink without changing any route."""
    out = {"host": s.get("host", ""), "port": s.get("port", DEFAULT_PORT), "ip": None,
           "wifi": None, "wan": None, "choice": "none"}
    if not s.get("host"):
        out["error"] = "先填 MyView IP"
        return out
    ip = resolve(s["host"])
    out["ip"] = ip
    if not ip:
        out["error"] = f"无法解析 {s['host']}"
        return out
    links = uplinks()
    if "wifi" in links:
        w = links["wifi"]
        ok, err, ms = probe(w["addr"], ip, s["port"])
        out["wifi"] = {"iface": w["iface"], "addr": w["addr"], "ok": ok, "error": err, "ms": ms}
    if "wan" in links:
        w = links["wan"]
        ok, err, ms = probe(w["addr"], ip, s["port"])
        out["wan"] = {"iface": w["iface"], "addr": w["addr"], "ok": ok, "error": err, "ms": ms,
                      "internet": has_internet(w["addr"])}
    if out["wifi"] and out["wifi"]["ok"]:
        out["choice"] = "wifi"
    elif out["wan"] and out["wan"]["internet"]:
        out["choice"] = "4g"
    return out


def relay_status(max_age: float = 8.0) -> dict:
    st = read_json(RELAY_STATUS)
    if not st:
        return {"running": False}
    st["running"] = time.time() - float(st.get("t", 0)) < max_age
    if not st["running"]:
        st["connected"] = False
    return st


def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "shell"
    if cmd == "shell":
        s = load()
        enabled = bool(s.get("enabled") and s.get("host"))
        for k, v in (
            ("MYVIEW_ENABLED", "true" if enabled else "false"),
            ("MYVIEW_HOST", s.get("host", "") if enabled else ""),
            ("MYVIEW_PORT", str(s.get("port", DEFAULT_PORT))),
            ("MYVIEW_SERVER_URL", server_url(s) if enabled else ""),
            ("LIDAR_IP", load_lidar()["lidarIp"]),
        ):
            print(f"{k}={shlex.quote(v)}")
        return 0
    if cmd == "lidar-ip":
        print(load_lidar()["lidarIp"])
        return 0
    if cmd == "gateway" and len(argv) > 2:
        gw = gateway(argv[2], "main", {})
        if gw:
            print(gw)
        return 0 if gw else 1
    if cmd == "signature":
        print(signature(load()))
        return 0
    if cmd == "route":
        r = select_route(force="--force" in argv)
        return 0 if (r["via"] != "none" or not r["enabled"]) else 1
    if cmd == "test":
        print(json.dumps(test_connection(load()), ensure_ascii=False))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
