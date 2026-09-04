/* LAN viewer: board holds the map; this page only filters what you see. */
(() => {
  const canvas = document.getElementById("c");
  const elConn = document.getElementById("conn");
  const elReady = document.getElementById("ready");
  const elMap = document.getElementById("nMap");
  const elVis = document.getElementById("nVis");
  const elTraj = document.getElementById("nTraj");
  const elSess = document.getElementById("session");
  const elFollow = document.getElementById("follow");
  const elInfo = document.getElementById("info");
  const elHint = document.getElementById("hint");
  const elFloorHint = document.getElementById("floorHint");
  const elPause = document.getElementById("pause");
  const layerBtn = document.getElementById("layerBtn");
  const floorBtn = document.getElementById("floorBtn");
  const floorMenu = document.getElementById("floorMenu");

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b0f14);
  scene.fog = new THREE.FogExp2(0x0b0f14, 0.012);

  const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 400);
  camera.up.set(0, 0, 1);
  camera.position.set(-8, -8, 6);

  const grid = new THREE.GridHelper(40, 40, 0x243040, 0x182028);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);
  scene.add(new THREE.AxesHelper(1.2));

  const pointMat = new THREE.PointsMaterial({
    size: 0.06,
    vertexColors: true,
    sizeAttenuation: true,
  });
  const pointGeo = new THREE.BufferGeometry();
  const points = new THREE.Points(pointGeo, pointMat);
  scene.add(points);

  const trajMat = new THREE.LineBasicMaterial({ color: 0x7ecbff });
  const trajGeo = new THREE.BufferGeometry();
  const trajLine = new THREE.Line(trajGeo, trajMat);
  scene.add(trajLine);

  const device = new THREE.Group();
  const body = new THREE.Mesh(
    new THREE.ConeGeometry(0.18, 0.55, 8),
    new THREE.MeshBasicMaterial({ color: 0xffc14d })
  );
  body.rotation.x = Math.PI / 2;
  device.add(body);
  scene.add(device);

  let persistPos = new Float32Array(0);
  let trajPos = new Float32Array(0);
  let liveChunks = [];
  let pose = [0, 0, 0];
  let follow = true;
  let session = "";
  let needFit = true;
  let paused = false;
  let viewDirty = true;

  const ui = {
    cloud: true,
    device: true,
    traj: true,
    info: true,
    mode: "persist",
    win: 0,
    horiz: false,
    horizBand: 0.6,
    section: false,
    floor: -1,
    floorMode: "auto",
    floorH: 3.0,
    floors: [],
  };

  const orbit = { theta: 0.8, phi: 0.9, r: 12, pan: new THREE.Vector3() };
  let drag = null;

  function heightColor(z, z0, z1, out, i) {
    const t = z1 > z0 ? Math.max(0, Math.min(1, (z - z0) / (z1 - z0))) : 0.5;
    out[i] = t < 0.5 ? 0.15 + t * 0.4 : 0.2 + (t - 0.5) * 1.6;
    out[i + 1] = t < 0.5 ? 0.35 + t * 1.2 : 1.1 - t;
    out[i + 2] = t < 0.4 ? 0.85 - t : 0.25;
  }

  function detectFloors(pos, specifiedH) {
    const zs = [];
    for (let i = 2; i < pos.length; i += 3) zs.push(pos[i]);
    if (!zs.length) return [];
    let zmin = Infinity;
    let zmax = -Infinity;
    for (const z of zs) {
      if (z < zmin) zmin = z;
      if (z > zmax) zmax = z;
    }
    if (specifiedH && specifiedH > 1.2) {
      const out = [];
      const top = zmax + 0.05;
      for (let z = zmin; z < top; z += specifiedH) out.push(z);
      return out;
    }
    const bin = 0.12;
    const n = Math.max(1, Math.ceil((zmax - zmin) / bin) + 1);
    const hist = new Array(n).fill(0);
    for (const z of zs) {
      let k = Math.floor((z - zmin) / bin);
      if (k < 0) k = 0;
      if (k >= n) k = n - 1;
      hist[k] += 1;
    }
    const avg = zs.length / n;
    const peaks = [];
    for (let i = 1; i < n - 1; i++) {
      if (hist[i] >= hist[i - 1] && hist[i] >= hist[i + 1] && hist[i] > avg * 1.4) {
        peaks.push({ z: zmin + (i + 0.5) * bin, c: hist[i] });
      }
    }
    peaks.sort((a, b) => b.c - a.c);
    const minSep = 2.0;
    const floors = [];
    for (const p of peaks) {
      if (floors.every((f) => Math.abs(f - p.z) >= minSep)) floors.push(p.z);
    }
    floors.sort((a, b) => a - b);
    return floors.length ? floors : [zmin];
  }

  function floorBand(idx) {
    const fl = ui.floors;
    if (idx < 0 || !fl.length) return null;
    const h = ui.floorMode === "manual" ? ui.floorH : (fl[1] - fl[0] || ui.floorH);
    const lo = fl[idx] - 0.25;
    const hi = (fl[idx + 1] != null ? fl[idx + 1] : fl[idx] + h) - 0.05;
    return [lo, hi];
  }

  function filterPoints(src) {
    if (!src || !src.length) return src;
    let lo = -Infinity;
    let hi = Infinity;
    if (ui.section && ui.floor >= 0) {
      const band = floorBand(ui.floor);
      if (band) {
        lo = band[0];
        hi = band[1];
      }
    }
    if (ui.horiz) {
      const mid = pose[2];
      const b = ui.horizBand;
      const hLo = mid - b;
      const hHi = mid + b;
      lo = Math.max(lo, hLo);
      hi = Math.min(hi, hHi);
    }
    if (!isFinite(lo) && !isFinite(hi)) return src;
    const kept = [];
    for (let i = 0; i < src.length; i += 3) {
      const z = src[i + 2];
      if (z < lo || z > hi) continue;
      kept.push(src[i], src[i + 1], z);
    }
    return new Float32Array(kept);
  }

  function livePoints() {
    if (!liveChunks.length) return new Float32Array(0);
    if (ui.win <= 0) return liveChunks[liveChunks.length - 1].xyz;
    const nowUnix = liveChunks[liveChunks.length - 1].t;
    const t0 = nowUnix - ui.win;
    const parts = [];
    let n = 0;
    for (const c of liveChunks) {
      if (c.t < t0 && c.t < nowUnix - ui.win) continue;
      parts.push(c.xyz);
      n += c.xyz.length;
    }
    if (!parts.length) return new Float32Array(0);
    const out = new Float32Array(n);
    let o = 0;
    for (const p of parts) {
      out.set(p, o);
      o += p.length;
    }
    return out;
  }

  function pruneLive() {
    const keep = 32;
    if (liveChunks.length <= keep) return;
    const now = liveChunks[liveChunks.length - 1].t;
    liveChunks = liveChunks.filter((c) => now - c.t <= 32);
  }

  function applyView() {
    const src = ui.mode === "persist" ? persistPos : livePoints();
    const vis = filterPoints(src);
    const n = vis.length / 3;
    pointGeo.setAttribute("position", new THREE.BufferAttribute(vis, 3));
    if (n) {
      let z0 = Infinity;
      let z1 = -Infinity;
      for (let i = 2; i < vis.length; i += 3) {
        if (vis[i] < z0) z0 = vis[i];
        if (vis[i] > z1) z1 = vis[i];
      }
      if (z1 - z0 < 0.05) {
        z0 -= 0.5;
        z1 += 0.5;
      }
      const col = new Float32Array(vis.length);
      for (let i = 0; i < n; i++) heightColor(vis[i * 3 + 2], z0, z1, col, i * 3);
      pointGeo.setAttribute("color", new THREE.BufferAttribute(col, 3));
      pointGeo.computeBoundingSphere();
    } else {
      pointGeo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(0), 3));
    }
    elMap.textContent = String(persistPos.length / 3);
    elVis.textContent = String(n);
    if (trajPos.length >= 6) {
      trajGeo.setAttribute("position", new THREE.BufferAttribute(trajPos, 3));
      trajGeo.computeBoundingSphere();
    }
    elTraj.textContent = String(trajPos.length / 3);
    points.visible = ui.cloud;
    device.visible = ui.device;
    trajLine.visible = ui.traj;
    elInfo.style.display = ui.info ? "flex" : "none";
    elHint.style.display = ui.info ? "block" : "none";
    viewDirty = false;
  }

  function setPersist(arr, replace) {
    if (replace) persistPos = new Float32Array(arr);
    else if (arr.length) {
      const next = new Float32Array(persistPos.length + arr.length);
      next.set(persistPos, 0);
      next.set(arr, persistPos.length);
      persistPos = next;
    }
    viewDirty = true;
  }

  function setTraj(arr, replace) {
    if (replace) trajPos = new Float32Array(arr);
    else if (arr.length) {
      const next = new Float32Array(trajPos.length + arr.length);
      next.set(trajPos, 0);
      next.set(arr, trajPos.length);
      trajPos = next;
    }
    viewDirty = true;
  }

  function appendTrajPoint(p) {
    const n = trajPos.length;
    if (n >= 3) {
      const dx = p[0] - trajPos[n - 3];
      const dy = p[1] - trajPos[n - 2];
      const dz = p[2] - trajPos[n - 1];
      if (dx * dx + dy * dy + dz * dz < 0.0004) return;
    }
    const next = new Float32Array(n + 3);
    next.set(trajPos, 0);
    next[n] = p[0];
    next[n + 1] = p[1];
    next[n + 2] = p[2];
    setTraj(next, true);
  }

  function parseLmap(buf) {
    const v = new DataView(buf);
    const magic = String.fromCharCode(v.getUint8(0), v.getUint8(1), v.getUint8(2), v.getUint8(3));
    if (magic !== "LMAP") return null;
    const kind = v.getUint8(4);
    const npts = v.getUint32(8, true);
    const ntraj = v.getUint32(12, true);
    if (kind === 3) {
      const stamp = v.getFloat64(16, true);
      const pts = new Float32Array(buf, 24, npts * 3);
      return { kind, stamp, pts: pts.slice(), traj: new Float32Array(0) };
    }
    const pts = new Float32Array(buf, 16, npts * 3);
    const traj = new Float32Array(buf, 16 + npts * 12, ntraj * 3);
    return { kind, pts: pts.slice(), traj: traj.slice() };
  }

  function refreshFloors(force) {
    const h = ui.floorMode === "manual" ? ui.floorH : 0;
    const found = detectFloors(persistPos, h);
    if (!force && found.length === ui.floors.length &&
        found.every((z, i) => Math.abs(z - (ui.floors[i] || 0)) < 0.15)) {
      return;
    }
    ui.floors = found;
    floorMenu.innerHTML = "";
    const add = (idx, label) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "menu-item" + (ui.floor === idx ? " on" : "");
      b.dataset.floor = String(idx);
      b.textContent = label;
      b.addEventListener("click", () => {
        ui.floor = idx;
        floorBtn.textContent = idx < 0 ? "楼层 全部" : `楼层 F${idx + 1}`;
        floorMenu.hidden = true;
        refreshFloorHint();
        viewDirty = true;
        [...floorMenu.querySelectorAll(".menu-item")].forEach((el) => {
          el.classList.toggle("on", Number(el.dataset.floor) === ui.floor);
        });
      });
      floorMenu.appendChild(b);
    };
    add(-1, "全部");
    found.forEach((z, i) => add(i, `F${i + 1}  (${z.toFixed(2)} m)`));
    if (ui.floor >= found.length) ui.floor = -1;
    floorBtn.textContent = ui.floor < 0 ? "楼层 全部" : `楼层 F${ui.floor + 1}`;
    refreshFloorHint();
  }

  function refreshFloorHint() {
    if (!ui.floors.length) {
      elFloorHint.textContent = "楼层 —";
      return;
    }
    const how = ui.floorMode === "manual" ? `指定 ${ui.floorH.toFixed(1)} m` : "自动";
    elFloorHint.textContent = `楼层 ${ui.floors.length} · ${how}`;
  }

  function fit() {
    const box = new THREE.Box3();
    if (pointGeo.getAttribute("position") && pointGeo.getAttribute("position").count) {
      pointGeo.computeBoundingBox();
      if (pointGeo.boundingBox) box.union(pointGeo.boundingBox);
    }
    box.expandByPoint(new THREE.Vector3(pose[0], pose[1], pose[2]));
    const size = box.getSize(new THREE.Vector3()).length();
    const center = box.getCenter(new THREE.Vector3());
    orbit.pan.copy(center);
    orbit.r = Math.max(4, size * 0.7 || 12);
    needFit = false;
  }

  function applySession(id, reason) {
    if (id && id !== session) {
      session = id;
      elSess.textContent = reason ? `${id} (${reason})` : id;
      persistPos = new Float32Array(0);
      trajPos = new Float32Array(0);
      liveChunks = [];
      ui.floors = [];
      ui.floor = -1;
      needFit = true;
      viewDirty = true;
    }
  }

  function onText(data) {
    let msg;
    try {
      msg = JSON.parse(data);
    } catch {
      return;
    }
    if (msg.session) applySession(msg.session, msg.reason);
    if (msg.type === "hello") {
      elReady.className = "pill " + (msg.ready ? "on" : "wait");
      elReady.textContent = msg.ready ? "里程计就绪" : "等待里程计";
    } else if (msg.type === "mapSession") {
      elReady.className = "pill wait";
      elReady.textContent = "坐标系已重置";
    } else if (msg.type === "pose" && msg.p) {
      pose = msg.p;
      device.position.set(msg.p[0], msg.p[1], msg.p[2]);
      if (msg.q) device.quaternion.set(msg.q[0], msg.q[1], msg.q[2], msg.q[3]);
      appendTrajPoint(msg.p);
      elReady.className = "pill on";
      elReady.textContent = "里程计就绪";
      if (ui.horiz) viewDirty = true;
    }
  }

  function onBin(buf) {
    const frame = parseLmap(buf);
    if (!frame) return;
    if (frame.kind === 1) {
      setPersist(frame.pts, true);
      setTraj(frame.traj, true);
      refreshFloors(true);
      needFit = true;
    } else if (frame.kind === 2) {
      setPersist(frame.pts, false);
    } else if (frame.kind === 3) {
      liveChunks.push({ t: frame.stamp || Date.now() / 1000, xyz: frame.pts });
      pruneLive();
      if (ui.mode === "live") viewDirty = true;
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      elConn.className = "pill on";
      elConn.textContent = "已连接";
    };
    ws.onclose = () => {
      elConn.className = "pill off";
      elConn.textContent = "已断开 · 重连中";
      setTimeout(connect, 1200);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      if (paused) return;
      if (typeof ev.data === "string") onText(ev.data);
      else onBin(ev.data);
    };
  }

  function resize() {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }

  function camTarget() {
    if (follow) return new THREE.Vector3(pose[0], pose[1], pose[2]);
    return orbit.pan.clone();
  }

  function placeCamera() {
    const t = camTarget();
    camera.position.set(
      t.x + orbit.r * Math.sin(orbit.phi) * Math.cos(orbit.theta),
      t.y + orbit.r * Math.sin(orbit.phi) * Math.sin(orbit.theta),
      t.z + orbit.r * Math.cos(orbit.phi)
    );
    camera.lookAt(t);
  }

  function setSeg(root, attr, value) {
    root.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("on", b.dataset[attr] === String(value));
    });
  }

  function syncWinEnabled() {
    document.getElementById("winSeg").querySelectorAll("button").forEach((b) => {
      b.disabled = ui.mode !== "live";
    });
  }

  function updateLayerLabel() {
    const n = [ui.cloud, ui.device, ui.traj, ui.info].filter(Boolean).length;
    layerBtn.textContent = `图层 ${n}`;
  }

  function bindDd(rootId, btnId) {
    const root = document.getElementById(rootId);
    const btn = document.getElementById(btnId);
    const menu = root.querySelector(".dd-menu");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    document.addEventListener("click", () => {
      menu.hidden = true;
    });
    menu.addEventListener("click", (e) => e.stopPropagation());
  }

  bindDd("layerDd", "layerBtn");
  bindDd("floorDd", "floorBtn");

  ["lyCloud", "lyDevice", "lyTraj", "lyInfo"].forEach((id) => {
    document.getElementById(id).addEventListener("change", () => {
      ui.cloud = document.getElementById("lyCloud").checked;
      ui.device = document.getElementById("lyDevice").checked;
      ui.traj = document.getElementById("lyTraj").checked;
      ui.info = document.getElementById("lyInfo").checked;
      updateLayerLabel();
      viewDirty = true;
    });
  });

  document.getElementById("modeSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (!b) return;
    ui.mode = b.dataset.mode;
    setSeg(document.getElementById("modeSeg"), "mode", ui.mode);
    syncWinEnabled();
    viewDirty = true;
  });
  document.getElementById("winSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-win]");
    if (!b || b.disabled) return;
    ui.win = Number(b.dataset.win);
    setSeg(document.getElementById("winSeg"), "win", ui.win);
    viewDirty = true;
  });

  document.getElementById("horiz").addEventListener("change", (e) => {
    ui.horiz = e.target.checked;
    viewDirty = true;
  });
  document.getElementById("horizBand").addEventListener("change", (e) => {
    ui.horizBand = Math.max(0.05, Number(e.target.value) || 0.6);
    viewDirty = true;
  });
  document.getElementById("section").addEventListener("change", (e) => {
    ui.section = e.target.checked;
    viewDirty = true;
  });
  document.getElementById("floorMode").addEventListener("change", (e) => {
    ui.floorMode = e.target.value;
    document.getElementById("floorHWrap").hidden = ui.floorMode !== "manual";
    refreshFloors(true);
    viewDirty = true;
  });
  document.getElementById("floorH").addEventListener("change", (e) => {
    ui.floorH = Math.max(1.5, Number(e.target.value) || 3);
    refreshFloors(true);
    viewDirty = true;
  });
  document.getElementById("detectFloors").addEventListener("click", () => {
    refreshFloors(true);
    viewDirty = true;
  });

  canvas.addEventListener("pointerdown", (e) => {
    canvas.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, btn: e.button };
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    if (drag.btn === 2 || e.shiftKey) {
      const t = camTarget();
      const right = new THREE.Vector3();
      camera.getWorldDirection(right);
      const up = camera.up.clone();
      right.cross(up).normalize();
      const fwd = new THREE.Vector3().crossVectors(up, right).normalize();
      const s = orbit.r * 0.002;
      orbit.pan.addScaledVector(right, -dx * s);
      orbit.pan.addScaledVector(fwd, dy * s);
      if (follow) {
        elFollow.checked = false;
        follow = false;
      }
    } else {
      orbit.theta -= dx * 0.008;
      orbit.phi = Math.max(0.12, Math.min(1.45, orbit.phi - dy * 0.008));
    }
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    orbit.r = Math.max(1.2, Math.min(180, orbit.r * (e.deltaY > 0 ? 1.08 : 0.92)));
  }, { passive: false });
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  elFollow.addEventListener("change", () => {
    follow = elFollow.checked;
    if (!follow) orbit.pan.set(pose[0], pose[1], pose[2]);
  });
  document.getElementById("fit").addEventListener("click", () => {
    follow = false;
    elFollow.checked = false;
    applyView();
    fit();
  });
  elPause.addEventListener("click", () => {
    paused = !paused;
    elPause.textContent = paused ? "继续" : "停止";
    document.querySelector(".bar").classList.toggle("paused", paused);
  });

  syncWinEnabled();
  updateLayerLabel();

  function tick() {
    resize();
    if (viewDirty) applyView();
    if (needFit && persistPos.length > 90) {
      applyView();
      fit();
    }
    placeCamera();
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  }

  connect();
  tick();
})();
