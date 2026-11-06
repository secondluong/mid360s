(() => {
  const $ = (id) => document.getElementById(id);
  const pill = $("mvPill");
  const modal = $("mvModal");
  const form = $("mvForm");
  const elStatus = $("mvStatus");
  const elMsg = $("mvMsg");
  const VIA = { wifi: "WiFi", "4g": "4G" };
  const MODE = { auto: "自动切换（优先 WiFi）", wifi: "只走 WiFi", "4g": "只走 4G" };
  let last = null;
  let busy = false;

  function viaText(route) {
    if (!route || !route.via || route.via === "none") return "无";
    return `${VIA[route.via] || route.via}（${route.iface}）`;
  }

  function pillState(st) {
    const s = st.settings || {};
    const r = st.route || {};
    const relay = st.relay || {};
    if (!s.enabled || !s.host) return ["MyView 未启用", "idle", "点“设置”填写 MyView IP 和端口"];
    if (relay.connected) {
      return [`MyView 已连接 · ${VIA[r.via] || "?"}`, "on", `${s.url}，出口 ${viaText(r)}`];
    }
    if (r.via === "none" && r.enabled) {
      return ["MyView 无可用网络", "off", r.reason || "WiFi 和 4G 都不可用"];
    }
    return ["MyView 连接中…", "wait", relay.lastError || r.reason || s.url];
  }

  function fmtAgo(t) {
    if (!t) return "—";
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 60) return `${s.toFixed(0)} 秒前`;
    if (s < 3600) return `${(s / 60).toFixed(0)} 分钟前`;
    return `${(s / 3600).toFixed(1)} 小时前`;
  }

  function linkLine(name, link, kind) {
    if (!link) return `${name}：未接入`;
    let txt = `${name}：${link.iface} ${link.addr}`;
    if (kind === "wifi" && link.reachable != null) {
      txt += link.reachable ? "，可连到 MyView" : `，连不上 MyView（${link.error || "失败"}）`;
    }
    if (kind === "wan" && link.internet != null) {
      txt += link.internet ? "，可以上公网" : "，上不了公网";
    }
    return txt;
  }

  function renderStatus(st) {
    const s = st.settings || {};
    const r = st.route || {};
    const relay = st.relay || {};
    const lines = [];
    if (!s.enabled || !s.host) {
      lines.push("推送未启用。");
    } else {
      lines.push(`目标：${s.url}${r.ip && r.ip !== s.host ? `（${r.ip}）` : ""}`);
      lines.push(`推送路由：${MODE[s.via || "auto"]}`);
      lines.push(`当前出口：${viaText(r)}${r.reason ? ` · ${r.reason}` : ""}`);
      if (relay.connected) {
        lines.push(
          `推送：已连接，已发点云 ${relay.cloudFramesSent || 0} 帧、轨迹点 ${relay.posesSent || 0} 个，` +
            `最近点云 ${fmtAgo(relay.lastCloudAt)}`
        );
      } else if (relay.running) {
        lines.push(`推送：未连接${relay.lastError ? `（${relay.lastError}）` : ""}`);
      } else {
        lines.push("推送：推送程序启动中…");
      }
    }
    lines.push(linkLine("WiFi", r.wifi, "wifi"));
    lines.push(linkLine("4G", r.wan, "wan"));
    elStatus.textContent = lines.join("\n");
  }

  function apply(st) {
    last = st;
    const [text, cls, title] = pillState(st);
    pill.textContent = text;
    pill.className = `pill ${cls}`;
    pill.title = title || "";
    if (!modal.hidden) {
      renderStatus(st);
      renderLidar(st.lidar);
    }
  }

  async function refresh() {
    try {
      const res = await fetch("/api/myview", { cache: "no-store" });
      if (res.ok) apply(await res.json());
    } catch (_) {
      // The board may be restarting; the next poll will catch up.
    }
  }

  function renderLidar(l) {
    if (!l) return;
    let txt = ` 当前运行：${l.runningIp || "—"}，${l.odomReady ? "里程计已就绪" : "等待雷达数据"}。`;
    if (l.runningIp && l.lidarIp !== l.runningIp) txt += ` 已保存 ${l.lidarIp}，重启后生效。`;
    $("lidarState").textContent = txt;
  }

  function fillLidar(l) {
    if (!l) return;
    $("lidarIp").value = l.lidarIp || "";
    $("lidarSn").value = "";
    renderLidar(l);
  }

  function lidarPrefix() {
    const l = (last && last.lidar) || {};
    const ip = l.pcIp || l.lidarIp || "192.168.1.50";
    return ip.split(".").slice(0, 3).join(".");
  }

  function fillForm(s) {
    $("mvEnabled").checked = !!s.enabled;
    $("mvHost").value = s.host || "";
    $("mvPort").value = s.port || 3000;
    $("mvVia").value = s.via || "auto";
    $("mvToken").value = s.token || "";
    $("mvTokenState").textContent = s.hasToken ? "" : " 当前未设置 Token。";
    $("mvSecure").checked = !!s.secure;
    $("mvDevice").value = s.deviceId || "";
    $("mvHz").value = s.cloudHz != null ? s.cloudHz : 5;
    $("mvPts").value = s.cloudMaxPoints != null ? s.cloudMaxPoints : 8000;
  }

  function formBody() {
    return {
      enabled: $("mvEnabled").checked,
      host: $("mvHost").value.trim(),
      port: $("mvPort").value.trim(),
      via: $("mvVia").value,
      token: $("mvToken").value.trim(),
      clearToken: !$("mvToken").value.trim(),
      secure: $("mvSecure").checked,
      deviceId: $("mvDevice").value.trim(),
      cloudHz: $("mvHz").value,
      cloudMaxPoints: $("mvPts").value,
    };
  }

  function showErrors(errors) {
    form.querySelectorAll(".mv-err").forEach((el) => {
      el.textContent = (errors && errors[el.dataset.for]) || "";
    });
    if (errors && (errors.deviceId || errors.cloudHz || errors.cloudMaxPoints)) {
      $("mvAdv").open = true;
    }
  }

  function setMsg(text, kind) {
    elMsg.textContent = text || "";
    elMsg.className = `mv-msg ${kind || ""}`;
  }

  function setBusy(on) {
    busy = on;
    ["mvTest", "mvSave"].forEach((id) => ($(id).disabled = on));
  }

  function open() {
    showErrors(null);
    setMsg("");
    if (last) {
      fillForm(last.settings || {});
      fillLidar(last.lidar);
      renderStatus(last);
    }
    modal.hidden = false;
  }

  function close() {
    if (busy) return;
    modal.hidden = true;
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = {};
    try {
      data = await res.json();
    } catch (_) {}
    return { ok: res.ok && data.ok !== false, data };
  }

  function testLine(name, r) {
    if (!r) return `${name}：未接入`;
    if (r.ok) return `${name}（${r.iface}）：能连上，${r.ms} ms`;
    let t = `${name}（${r.iface}）：连不上（${r.error || "失败"}）`;
    if (r.internet === false) t += "，4G 上不了公网";
    return t;
  }

  async function testConn() {
    showErrors(null);
    const body = formBody();
    if (!body.host) {
      showErrors({ host: "先填 MyView IP" });
      return;
    }
    setBusy(true);
    setMsg(`正在测试 ${body.host}:${body.port || 3000} …`);
    try {
      const { ok, data } = await post("/api/myview/test", body);
      if (!ok) {
        showErrors(data.errors);
        setMsg(data.error || "测试失败", "bad");
        return;
      }
      if (data.error) {
        setMsg(data.error, "bad");
        return;
      }
      const picked = data.choice === "wifi" ? data.wifi : data.choice === "4g" ? data.wan : null;
      let choice;
      if (body.via === "wifi") {
        choice = !data.wifi ? "只走 WiFi，但 WiFi 未连接。" : data.wifi.ok ? "将走 WiFi 推送。" : "只走 WiFi，但经 WiFi 连不上这个地址。";
      } else if (body.via === "4g") {
        choice = !data.wan ? "只走 4G，但没有接入 4G。" : data.wan.ok ? "将走 4G 推送。" : "只走 4G，但经 4G 连不上这个地址。";
      } else {
        choice =
          data.choice === "wifi" ? "将走 WiFi 推送。" : data.choice === "4g" ? "将走 4G 推送。" : "WiFi 和 4G 都连不上这个地址。";
      }
      const good = !!(picked && picked.ok);
      setMsg([testLine("WiFi", data.wifi), testLine("4G", data.wan), choice].join("\n"), good ? "good" : "bad");
    } catch (e) {
      setMsg(`测试失败：${e}`, "bad");
    } finally {
      setBusy(false);
    }
  }

  async function waitForRestart() {
    // The old process answers for about a second before it exits.
    await new Promise((r) => setTimeout(r, 4000));
    for (let i = 0; i < 90; i++) {
      try {
        const res = await fetch("/api/myview", { cache: "no-store" });
        if (res.ok) {
          const st = await res.json();
          apply(st);
          fillLidar(st.lidar);
          return true;
        }
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 2000));
    }
    return false;
  }

  async function save(ev) {
    ev.preventDefault();
    showErrors(null);
    const body = formBody();
    if (body.enabled && !body.host) {
      showErrors({ host: "启用推送前要先填 MyView IP" });
      return;
    }
    const lidarIp = $("lidarIp").value.trim();
    const running = ((last && last.lidar) || {}).runningIp || "";
    const lidarChanged = lidarIp !== "" && lidarIp !== running;
    if (
      lidarChanged &&
      !window.confirm(`雷达 IP 将从 ${running || "—"} 改为 ${lidarIp}。\n采集程序会重启（约 1 分钟），页面上已有的点云会清空。确定吗？`)
    ) {
      return;
    }
    setBusy(true);
    setMsg("正在保存…");
    try {
      const { ok, data } = await post("/api/myview", body);
      if (!ok) {
        showErrors(data.errors);
        setMsg("保存失败，请检查标红的项。", "bad");
        return;
      }
      apply(data);
      fillForm(data.settings || {});
      renderStatus(data);
      const s = data.settings || {};
      const pushMsg = s.enabled ? "MyView 推送设置已保存，几秒内用新设置重新连接。" : "MyView 推送已关闭。";

      if (!lidarChanged) {
        setMsg(`已保存。${pushMsg}`, "good");
        return;
      }
      const r = await post("/api/lidar", { lidarIp });
      if (!r.ok) {
        showErrors(r.data.errors);
        setMsg(`${pushMsg}\n雷达 IP 没有保存，请检查标红的项。`, "bad");
        return;
      }
      if (!r.data.restarting) {
        setMsg(`已保存。${pushMsg}`, "good");
        return;
      }
      setMsg(`${pushMsg}\n雷达 IP 已改为 ${lidarIp}，采集程序正在重启，约 1 分钟…`);
      const back = await waitForRestart();
      setMsg(
        back
          ? `重启完成，雷达 IP 现在是 ${lidarIp}。如果迟迟没有点云，请检查网线和雷达 IP 是否正确。`
          : "板子还没响应，请稍后刷新页面。",
        back ? "good" : "bad"
      );
    } catch (e) {
      setMsg(`保存失败：${e}`, "bad");
    } finally {
      setBusy(false);
    }
  }

  $("lidarSn").addEventListener("input", (e) => {
    const sn = e.target.value.replace(/\D/g, "").slice(0, 2);
    e.target.value = sn;
    if (sn.length === 2) $("lidarIp").value = `${lidarPrefix()}.1${sn}`;
  });
  $("mvBtn").addEventListener("click", open);
  $("mvCancel").addEventListener("click", close);
  $("mvTest").addEventListener("click", testConn);
  form.addEventListener("submit", save);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) close();
  });

  refresh();
  setInterval(refresh, 3000);
})();
