/* LAN viewer: this page keeps points it has already drawn and only appends new ones. */
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
  const elPoseInfo = document.getElementById("poseInfo");
  const layerBtn = document.getElementById("layerBtn");
  const floorBtn = document.getElementById("floorBtn");
  const floorMenu = document.getElementById("floorMenu");

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b0f14);
  scene.fog = new THREE.FogExp2(0x0b0f14, 0.006);

  const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 400);
  camera.up.set(0, 0, 1);
  camera.position.set(-8, -8, 6);

  const grid = new THREE.GridHelper(40, 40, 0x243040, 0x182028);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);
  scene.add(new THREE.AxesHelper(1.2));

  // Match ~0.05 m LIO leaf / 0.03 m viewer voxel — keep dots small to avoid square blocks.
  const pointMat = new THREE.PointsMaterial({
    size: 0.028,
    vertexColors: true,
    sizeAttenuation: true,
  });
  const pointGeo = new THREE.BufferGeometry();
  const points = new THREE.Points(pointGeo, pointMat);
  scene.add(points);

  // LineBasicMaterial.linewidth is ignored on most WebGL, so the path is a ribbon.
  const TRAJ_HALF_W_MIN = 0.025;
  const trajMat = new THREE.MeshBasicMaterial({
    color: 0xe53935,
    side: THREE.DoubleSide,
    depthTest: false,
  });
  const trajGeo = new THREE.BufferGeometry();
  const trajLine = new THREE.Mesh(trajGeo, trajMat);
  trajLine.renderOrder = 9;
  trajLine.frustumCulled = false;
  scene.add(trajLine);

  const ICON_H = 0.85;
  const ICON_BASE = "/fireman/";
  const GIFT_STAND_FBX = ["消防灭火防护服（站立）.FBX"];
  const GIFT_RUN_FBX = ["消防灭火防护服（跑步）.FBX"];
  const GIFT_SUIT_TEX = ["消防灭火防护服.png"];
  const CARD_TEX = ["/firefighter.png"];

  function report() {}

  function punchGreen(img) {
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0);
    const pix = g.getImageData(0, 0, c.width, c.height);
    const d = pix.data;
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i];
      const gv = d[i + 1];
      const b = d[i + 2];
      if (gv > 90 && gv > r * 1.35 && gv > b * 1.35) d[i + 3] = 0;
    }
    g.putImageData(pix, 0, 0);
    const tex = new THREE.CanvasTexture(c);
    tex.needsUpdate = true;
    return tex;
  }

  function giftUrl(name) {
    // MyView only forwards a few extensions. .FBX is dropped; .bin is kept.
    return ICON_BASE + encodeURIComponent(name) + ".bin";
  }

  function styleIconMesh(root, suitMap) {
    root.traverse((obj) => {
      if (obj.isLight) obj.visible = false;
      if (!obj.isMesh) return;
      const src = obj.material;
      const mats = Array.isArray(src) ? src : [src];
      const next = mats.map((m) => {
        const map = suitMap || (m && m.map) || null;
        return new THREE.MeshBasicMaterial({
          map,
          color: map ? 0xffffff : (m && m.color ? m.color.clone() : new THREE.Color(0xd84315)),
          transparent: true,
          alphaTest: 0.02,
          depthTest: false,
          depthWrite: false,
          side: THREE.DoubleSide,
        });
      });
      obj.material = Array.isArray(src) ? next : next[0];
      obj.frustumCulled = false;
      obj.renderOrder = 10;
    });
  }

  const _faceRight = new THREE.Vector3();
  const _faceFwd = new THREE.Vector3();
  const _faceUp = new THREE.Vector3(0, 0, 1);
  const _worldZ = new THREE.Vector3(0, 0, 1);

  function centerIcon(wrap) {
    wrap.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(wrap);
    const c = box.getCenter(new THREE.Vector3());
    wrap.position.x -= c.x;
    wrap.position.y -= c.y;
    wrap.position.z -= box.min.z;
  }

  function faceIconForward(wrap) {
    wrap.updateMatrixWorld(true);
    const l = wrap.getObjectByName("Bip002_L_Clavicle");
    const r = wrap.getObjectByName("Bip002_R_Clavicle");
    if (!l || !r) return;
    r.getWorldPosition(_faceRight);
    l.getWorldPosition(_faceFwd);
    _faceRight.sub(_faceFwd);
    _faceRight.z = 0;
    if (_faceRight.lengthSq() < 1e-8) return;
    _faceRight.normalize();
    _faceFwd.copy(_faceUp).cross(_faceRight);
    if (_faceFwd.lengthSq() < 1e-8) return;
    const yaw = Math.atan2(_faceFwd.y, _faceFwd.x);
    wrap.rotateOnWorldAxis(_worldZ, -yaw);
  }

  function fitIcon(model) {
    const wrap = new THREE.Group();
    wrap.add(model);
    wrap.rotation.set(Math.PI / 2, Math.PI / 2, 0);
    wrap.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(wrap);
    const size = box.getSize(new THREE.Vector3());
    const h = Math.max(size.z, 1e-4);
    wrap.scale.setScalar(ICON_H / h);
    faceIconForward(wrap);
    centerIcon(wrap);
    return wrap;
  }

  function stripRootMotion(clip) {
    clip.tracks = clip.tracks.filter((t) => (
      t.name !== "Bip002.position" && t.name !== "Bip002_Footsteps.position"
    ));
  }

  function pinBipRoot(model) {
    const bip = model && model.getObjectByName("Bip002");
    if (!bip || !bip.userData.bindPos) return;
    bip.position.copy(bip.userData.bindPos);
  }

  function suitTexUrl(name) {
    return ICON_BASE + encodeURIComponent(name || GIFT_SUIT_TEX[0]);
  }

  function rewriteGiftTexUrl(url) {
    try {
      const path = decodeURIComponent(String(url).split("?")[0]);
      if (GIFT_SUIT_TEX.some((n) => path.includes(n))) return url;
      if (/\.(psd|tga|tiff?|bmp)$/i.test(path) || /uvw|贴图/i.test(path)) return suitTexUrl();
    } catch (_) { /* keep the original url */ }
    return url;
  }

  async function ensureLoaders() {
    if (ensureLoaders.FBXLoader) return;
    const [fbx, tga] = await Promise.all([
      import("/jsm/loaders/FBXLoader.js"),
      import("/jsm/loaders/TGALoader.js"),
    ]);
    ensureLoaders.FBXLoader = fbx.FBXLoader;
    ensureLoaders.TGALoader = tga.TGALoader;
  }

  function loadOneSuitTex(name) {
    return new Promise((resolve, reject) => {
      const Loader = /\.tga$/i.test(name) ? ensureLoaders.TGALoader : THREE.TextureLoader;
      const loader = new Loader();
      loader.load(suitTexUrl(name), (tex) => {
        if (THREE.SRGBColorSpace) tex.colorSpace = THREE.SRGBColorSpace;
        tex.wrapS = THREE.RepeatWrapping;
        tex.wrapT = THREE.RepeatWrapping;
        tex.needsUpdate = true;
        resolve(tex);
      }, undefined, reject);
    });
  }

  async function loadSuitTexture() {
    await ensureLoaders();
    let last = null;
    for (const name of GIFT_SUIT_TEX) {
      try {
        return await loadOneSuitTex(name);
      } catch (err) {
        last = err;
      }
    }
    throw last || new Error("no suit texture");
  }

  function loadFbx(name) {
    const manager = new THREE.LoadingManager();
    if (ensureLoaders.TGALoader) manager.addHandler(/\.tga$/i, new ensureLoaders.TGALoader());
    manager.setURLModifier(rewriteGiftTexUrl);
    const loader = new ensureLoaders.FBXLoader(manager);
    return new Promise((resolve, reject) => {
      loader.load(giftUrl(name), resolve, undefined, reject);
    });
  }

  async function loadFbxModel(names) {
    await ensureLoaders();
    for (const name of names) {
      try {
        const obj = await loadFbx(name);
        return { scene: obj, animations: obj.animations || [] };
      } catch (err) {
        console.warn("fbx failed", name, err);
      }
    }
    return null;
  }

  function makeCardFallback() {
    const card = new THREE.Group();
    card.rotation.set(Math.PI / 2, Math.PI / 2, 0);
    const mat = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      alphaTest: 0.2,
      side: THREE.DoubleSide,
      depthTest: false,
      depthWrite: false,
    });
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(0.62, 0.82), mat);
    plane.position.y = 0.08;
    card.add(plane);
    card.userData.load = () => {
      if (card.userData.loading) return;
      card.userData.loading = true;
      let i = 0;
      const tryNext = () => {
        if (i >= CARD_TEX.length) return;
        const url = CARD_TEX[i++];
        const img = new Image();
        img.onload = () => {
          const tex = /\.png$/i.test(url) ? punchGreen(img) : new THREE.Texture(img);
          if (THREE.SRGBColorSpace) tex.colorSpace = THREE.SRGBColorSpace;
          tex.needsUpdate = true;
          mat.map = tex;
          mat.needsUpdate = true;
          card.visible = true;
        };
        img.onerror = tryNext;
        img.src = url;
      };
      tryNext();
    };
    card.visible = false;
    return card;
  }

  function makeRescuer() {
    const root = new THREE.Group();
    const body = new THREE.Group();
    body.position.z = 0.32;
    root.add(body);

    const card = makeCardFallback();
    body.add(card);

    const disc = new THREE.Mesh(
      new THREE.CircleGeometry(0.1, 24),
      new THREE.MeshBasicMaterial({
        color: 0xb71c1c,
        transparent: true,
        opacity: 0.4,
        side: THREE.DoubleSide,
        depthTest: false,
      })
    );
    disc.position.z = 0.015;
    root.add(disc);

    const nose = new THREE.Mesh(
      new THREE.ConeGeometry(0.03, 0.06, 3),
      new THREE.MeshBasicMaterial({ color: 0xffe082, depthTest: false })
    );
    nose.rotation.z = -Math.PI / 2;
    nose.position.set(0.08, 0, 0.02);
    root.add(nose);

    const anim = {
      body, card, stand: null, run: null, mixer: null,
      disc, nose, speed: 0, yaw: 0, lie: 0, lieTarget: 0,
      quat: new THREE.Quaternion(), hasQuat: false, lastPos: null, lastAt: 0,
    };
    root.userData.anim = anim;

    Promise.allSettled([
      loadFbxModel(GIFT_STAND_FBX),
      loadFbxModel(GIFT_RUN_FBX),
      loadSuitTexture(),
    ]).then((results) => {
      const standModel = results[0].status === "fulfilled" ? results[0].value : null;
      const runModel = results[1].status === "fulfilled" ? results[1].value : null;
      const suitMap = results[2].status === "fulfilled" ? results[2].value : null;
      if (!standModel && !runModel) {
        if (card.userData.load) card.userData.load();
        return;
      }
      card.visible = false;
      body.position.z = 0;
      if (standModel && standModel.scene) {
        const bip = standModel.scene.getObjectByName("Bip002");
        if (bip) bip.userData.bindPos = bip.position.clone();
        styleIconMesh(standModel.scene, suitMap);
        anim.stand = fitIcon(standModel.scene);
        body.add(anim.stand);
      }
      if (runModel && runModel.scene) {
        const bip = runModel.scene.getObjectByName("Bip002");
        if (bip) bip.userData.bindPos = bip.position.clone();
        styleIconMesh(runModel.scene, suitMap);
        anim.run = fitIcon(runModel.scene);
        body.add(anim.run);
        if (runModel.animations && runModel.animations.length) {
          const clip = runModel.animations[0];
          stripRootMotion(clip);
          anim.mixer = new THREE.AnimationMixer(runModel.scene);
          const action = anim.mixer.clipAction(clip);
          action.setEffectiveTimeScale(0.45);
          action.play();
          anim.runRoot = runModel.scene;
        }
        anim.run.visible = false;
      }
      if (anim.stand) anim.stand.visible = true;
      else if (anim.run) anim.run.visible = true;
    }).catch(() => {
      if (card.userData.load) card.userData.load();
    });
    return root;
  }

  const _up = new THREE.Vector3();
  const _noseBase = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), -Math.PI / 2);

  function updateWalkAnim(now) {
    const a = device.userData.anim;
    if (!a) return;
    a.lie += (a.lieTarget - a.lie) * 0.18;
    const lie = a.lie;
    const walkMix = 1 - lie;
    const dt = Math.min(0.05, (now - (a._tickAt || now)) / 1000);
    a._tickAt = now;
    const moving = a.speed > 0.08 && walkMix > 0.35;
    if (moving) a.speed *= 0.985;
    else a.speed = 0;
    if (a.stand && a.run) {
      a.stand.visible = !moving;
      a.run.visible = moving;
    } else if (a.stand) {
      a.stand.visible = true;
    } else if (a.run) {
      a.run.visible = true;
    }
    if (a.mixer) {
      if (moving) {
        a.mixer.update(dt);
        pinBipRoot(a.runRoot);
      } else {
        a.mixer.setTime(0);
        pinBipRoot(a.runRoot);
      }
    }
    if (a.card && (a.stand || a.run)) a.card.visible = false;
    if (!a.stand && !a.run && a.body) a.body.position.z = 0.32 * walkMix + 0.08 * lie;
    if (a.disc) {
      a.disc.quaternion.copy(device.quaternion).invert();
      a.disc.material.opacity = 0.4 * Math.max(walkMix, 0.15);
      a.disc.visible = true;
    }
    if (a.nose) {
      a.nose.quaternion.copy(device.quaternion).invert().multiply(_noseBase);
      a.nose.visible = walkMix > 0.2;
    }
  }

  function applyDeviceTilt() {
    const a = device.userData.anim;
    if (!a) return;
    if (a.hasQuat) {
      _up.set(0, 0, 1).applyQuaternion(a.quat);
      a.lieTarget = Math.max(0, Math.min(1, (0.7 - _up.z) / 0.7));
    }
    if (a.hasQuat && a.lieTarget >= 0.5) device.quaternion.copy(a.quat);
    else device.rotation.set(0, 0, a.yaw);
  }

  function onDevicePose(p, q) {
    const now = performance.now();
    const a = device.userData.anim;
    device.position.set(p[0], p[1], p[2]);
    if (elPoseInfo) {
      const n = (v) => (Number.isFinite(v) ? v.toFixed(2) : "—");
      elPoseInfo.textContent = `X ${n(p[0])}   Y ${n(p[1])}   高 ${n(p[2])} m`;
    }
    if (!a) return;
    if (q && q.length >= 4) {
      a.quat.set(q[0], q[1], q[2], q[3]);
      a.hasQuat = true;
    }
    if (a.lastPos) {
      const dx = p[0] - a.lastPos[0];
      const dy = p[1] - a.lastPos[1];
      const dist = Math.hypot(dx, dy);
      const dt = Math.max(0.001, (now - a.lastAt) / 1000);
      if (dist > 0.015) {
        const targetYaw = Math.atan2(dy, dx);
        let dYaw = targetYaw - a.yaw;
        while (dYaw > Math.PI) dYaw -= Math.PI * 2;
        while (dYaw < -Math.PI) dYaw += Math.PI * 2;
        a.yaw += dYaw * Math.min(1, dist * 4);
        if (a.lieTarget < 0.5) a.speed = Math.max(a.speed, dist / dt);
      }
    } else if (a.hasQuat) {
      const fwd = _up.set(1, 0, 0).applyQuaternion(a.quat);
      if (Math.hypot(fwd.x, fwd.y) > 0.15) a.yaw = Math.atan2(fwd.y, fwd.x);
    }
    a.lastPos = [p[0], p[1], p[2]];
    a.lastAt = now;
    applyDeviceTilt();
  }

  const device = makeRescuer();
  device.renderOrder = 10;
  device.traverse((obj) => {
    if (obj.material) {
      obj.material.depthTest = false;
      obj.material.depthWrite = false;
    }
  });
  scene.add(device);

  let persistPos = new Float32Array(0);
  let trajPos = new Float32Array(0);
  let mapVoxel = 0.05;
  let seenKeys = new Set();
  // How many persist points are already on the GPU. New points append past this.
  let gpuPts = 0;
  let cloudBounds = new THREE.Box3();
  let liveChunks = [];
  let pose = [0, 0, 0];
  let follow = true;
  let session = "";
  let needFit = true;
  let paused = false;
  let viewDirty = true;
  let replay = null;

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
    // Saturated cyan→green→yellow→red; bright enough to read as solid structure.
    let t = z1 > z0 ? (z - z0) / (z1 - z0) : 0.5;
    t = Math.max(0, Math.min(1, t));
    if (t < 0.25) {
      const u = t / 0.25;
      out[i] = 0.1;
      out[i + 1] = 0.45 + 0.4 * u;
      out[i + 2] = 0.95;
    } else if (t < 0.5) {
      const u = (t - 0.25) / 0.25;
      out[i] = 0.1 + 0.15 * u;
      out[i + 1] = 0.85 + 0.15 * u;
      out[i + 2] = 0.95 - 0.7 * u;
    } else if (t < 0.75) {
      const u = (t - 0.5) / 0.25;
      out[i] = 0.25 + 0.75 * u;
      out[i + 1] = 1.0;
      out[i + 2] = 0.25 * (1 - u);
    } else {
      const u = (t - 0.75) / 0.25;
      out[i] = 1.0;
      out[i + 1] = 1.0 - 0.75 * u;
      out[i + 2] = 0.05;
    }
  }

  function heightRange(vis, n) {
    if (!n) return [0, 1];
    if (n < 24) {
      let z0 = Infinity;
      let z1 = -Infinity;
      for (let i = 0; i < n; i++) {
        const z = vis[i * 3 + 2];
        if (z < z0) z0 = z;
        if (z > z1) z1 = z;
      }
      if (z1 - z0 < 0.4) {
        z0 -= 0.4;
        z1 += 0.4;
      }
      return [z0, z1];
    }
    // Percentile range avoids a few ceiling/noise points washing out color.
    const step = Math.max(1, Math.floor(n / 4000));
    const zs = [];
    for (let i = 0; i < n; i += step) zs.push(vis[i * 3 + 2]);
    zs.sort((a, b) => a - b);
    const lo = zs[Math.floor(zs.length * 0.02)];
    const hi = zs[Math.floor(zs.length * 0.98)];
    if (hi - lo < 0.5) return [lo - 0.4, hi + 0.4];
    return [lo, hi];
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

  function trajHalfW() {
    return Math.max(TRAJ_HALF_W_MIN, Math.min(0.18, (orbit.r || 12) * 0.0032));
  }

  function trajWithTip(pos) {
    const src = pos && pos.length ? pos : trajPos;
    if (!pose || pose.length < 3) return src;
    const n = src.length;
    if (n < 3) {
      if (pose[0] === 0 && pose[1] === 0 && pose[2] === 0) return src;
      return new Float32Array([pose[0], pose[1], pose[2]]);
    }
    const dx = pose[0] - src[n - 3];
    const dy = pose[1] - src[n - 2];
    const dz = pose[2] - src[n - 1];
    if (dx * dx + dy * dy + dz * dz < 1e-8) return src;
    const out = new Float32Array(n + 3);
    out.set(src);
    out[n] = pose[0];
    out[n + 1] = pose[1];
    out[n + 2] = pose[2];
    return out;
  }

  function rebuildTrajRibbon(pos) {
    pos = trajWithTip(pos);
    let nPts = (pos && pos.length ? pos.length : 0) / 3;
    if (nPts < 2) {
      if (nPts === 1 && pos && pos.length >= 3) {
        pos = new Float32Array([pos[0], pos[1], pos[2], pos[0] + 0.05, pos[1], pos[2]]);
        nPts = 2;
      } else {
        trajGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(0), 3));
        trajGeo.setIndex(null);
        return;
      }
    }
    const verts = new Float32Array(nPts * 6);
    const indices = nPts > 32767
      ? new Uint32Array((nPts - 1) * 6)
      : new Uint16Array((nPts - 1) * 6);
    for (let i = 0; i < nPts; i++) {
      const i3 = i * 3;
      let dx;
      let dy;
      if (i === 0) {
        dx = pos[3] - pos[0];
        dy = pos[4] - pos[1];
      } else if (i === nPts - 1) {
        dx = pos[i3] - pos[i3 - 3];
        dy = pos[i3 + 1] - pos[i3 - 2];
      } else {
        dx = pos[i3 + 3] - pos[i3 - 3];
        dy = pos[i3 + 4] - pos[i3 - 2];
      }
      let sx = -dy;
      let sy = dx;
      const len = Math.hypot(sx, sy);
      const half = trajHalfW();
      if (len < 1e-8) {
        sx = half;
        sy = 0;
      } else {
        sx = (sx / len) * half;
        sy = (sy / len) * half;
      }
      const v = i * 6;
      verts[v] = pos[i3] - sx;
      verts[v + 1] = pos[i3 + 1] - sy;
      verts[v + 2] = pos[i3 + 2];
      verts[v + 3] = pos[i3] + sx;
      verts[v + 4] = pos[i3 + 1] + sy;
      verts[v + 5] = pos[i3 + 2];
    }
    for (let i = 0; i < nPts - 1; i++) {
      const a = i * 2;
      const o = i * 6;
      indices[o] = a;
      indices[o + 1] = a + 2;
      indices[o + 2] = a + 1;
      indices[o + 3] = a + 1;
      indices[o + 4] = a + 2;
      indices[o + 5] = a + 3;
    }
    trajGeo.setAttribute("position", new THREE.BufferAttribute(verts, 3));
    trajGeo.setIndex(new THREE.BufferAttribute(indices, 1));
    trajGeo.computeBoundingSphere();
  }

  function voxelKey(x, y, z) {
    const inv = 1 / mapVoxel;
    return Math.floor(x * inv + 1e-3) + "," + Math.floor(y * inv + 1e-3) + "," + Math.floor(z * inv + 1e-3);
  }

  function rememberPoints(arr) {
    for (let i = 0; i < arr.length; i += 3) seenKeys.add(voxelKey(arr[i], arr[i + 1], arr[i + 2]));
  }

  function unseenPoints(arr) {
    if (!arr || !arr.length || !seenKeys.size) return arr || new Float32Array(0);
    const kept = [];
    for (let i = 0; i < arr.length; i += 3) {
      const k = voxelKey(arr[i], arr[i + 1], arr[i + 2]);
      if (seenKeys.has(k)) continue;
      kept.push(arr[i], arr[i + 1], arr[i + 2]);
    }
    return new Float32Array(kept);
  }

  function expandBounds(arr, i0, nPts) {
    let minx, miny, minz, maxx, maxy, maxz;
    if (cloudBounds.isEmpty()) {
      minx = miny = minz = Infinity;
      maxx = maxy = maxz = -Infinity;
    } else {
      minx = cloudBounds.min.x;
      miny = cloudBounds.min.y;
      minz = cloudBounds.min.z;
      maxx = cloudBounds.max.x;
      maxy = cloudBounds.max.y;
      maxz = cloudBounds.max.z;
    }
    for (let i = 0; i < nPts; i++) {
      const o = (i0 + i) * 3;
      const x = arr[o];
      const y = arr[o + 1];
      const z = arr[o + 2];
      if (x < minx) minx = x;
      if (y < miny) miny = y;
      if (z < minz) minz = z;
      if (x > maxx) maxx = x;
      if (y > maxy) maxy = y;
      if (z > maxz) maxz = z;
    }
    if (!Number.isFinite(minx)) return;
    cloudBounds.min.set(minx, miny, minz);
    cloudBounds.max.set(maxx, maxy, maxz);
    const cx = (minx + maxx) * 0.5;
    const cy = (miny + maxy) * 0.5;
    const cz = (minz + maxz) * 0.5;
    const dx = maxx - minx;
    const dy = maxy - miny;
    const dz = maxz - minz;
    const r = Math.sqrt(dx * dx + dy * dy + dz * dz) * 0.5;
    pointGeo.boundingSphere = new THREE.Sphere(new THREE.Vector3(cx, cy, cz), Math.max(r, 0.5));
    pointGeo.boundingBox = cloudBounds.clone();
  }

  function markRange(attr, pointOffset, pointCount) {
    attr.updateRange.offset = pointOffset * 3;
    attr.updateRange.count = pointCount * 3;
    attr.needsUpdate = true;
  }

  function uploadPersist(fromPt) {
    const nPts = persistPos.length / 3;
    if (nPts <= fromPt) {
      pointGeo.setDrawRange(0, nPts);
      return;
    }
    const [z0, z1] = heightRange(persistPos, nPts);
    let posAttr = pointGeo.getAttribute("position");
    let colAttr = pointGeo.getAttribute("color");
    const have = posAttr && colAttr && posAttr.array.length / 3 >= nPts && gpuPts > 0 && fromPt > 0;
    if (!have) {
      const prevCap = posAttr && posAttr.array ? posAttr.array.length / 3 : 0;
      const cap = Math.max(nPts, prevCap ? prevCap * 2 : 4096);
      const pos = new Float32Array(cap * 3);
      const col = new Float32Array(cap * 3);
      pos.set(persistPos);
      for (let i = 0; i < nPts; i++) heightColor(pos[i * 3 + 2], z0, z1, col, i * 3);
      posAttr = new THREE.BufferAttribute(pos, 3);
      colAttr = new THREE.BufferAttribute(col, 3);
      posAttr.setUsage(THREE.DynamicDrawUsage);
      colAttr.setUsage(THREE.DynamicDrawUsage);
      pointGeo.setAttribute("position", posAttr);
      pointGeo.setAttribute("color", colAttr);
      gpuPts = nPts;
      cloudBounds.makeEmpty();
      expandBounds(pos, 0, nPts);
      pointGeo.setDrawRange(0, nPts);
      return;
    }
    const pos = posAttr.array;
    const col = colAttr.array;
    if (pos.length / 3 < nPts) {
      gpuPts = 0;
      uploadPersist(0);
      return;
    }
    for (let i = fromPt; i < nPts; i++) {
      pos[i * 3] = persistPos[i * 3];
      pos[i * 3 + 1] = persistPos[i * 3 + 1];
      pos[i * 3 + 2] = persistPos[i * 3 + 2];
      heightColor(pos[i * 3 + 2], z0, z1, col, i * 3);
    }
    markRange(posAttr, fromPt, nPts - fromPt);
    markRange(colAttr, fromPt, nPts - fromPt);
    gpuPts = nPts;
    expandBounds(pos, fromPt, nPts - fromPt);
    pointGeo.setDrawRange(0, nPts);
  }

  function uploadFiltered(src) {
    const vis = filterPoints(src);
    const n = vis.length / 3;
    gpuPts = 0;
    if (!n) {
      pointGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(0), 3));
      pointGeo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(0), 3));
      pointGeo.setDrawRange(0, 0);
      return n;
    }
    const [z0, z1] = heightRange(vis, n);
    const col = new Float32Array(vis.length);
    for (let i = 0; i < n; i++) heightColor(vis[i * 3 + 2], z0, z1, col, i * 3);
    pointGeo.setAttribute("position", new THREE.BufferAttribute(vis, 3));
    pointGeo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    pointGeo.computeBoundingSphere();
    pointGeo.computeBoundingBox();
    cloudBounds.copy(pointGeo.boundingBox);
    pointGeo.setDrawRange(0, n);
    return n;
  }

  function applyView() {
    const filtering = ui.horiz || (ui.section && ui.floor >= 0);
    const replaySrc = replay ? (replay.cloud || new Float32Array(0)) : null;
    let n;
    if (replaySrc) {
      n = uploadFiltered(replaySrc);
    } else if (ui.mode !== "persist") {
      n = uploadFiltered(livePoints());
    } else if (filtering) {
      n = uploadFiltered(persistPos);
    } else {
      const from = gpuPts > 0 && gpuPts <= persistPos.length / 3 ? gpuPts : 0;
      uploadPersist(from);
      n = persistPos.length / 3;
    }
    elMap.textContent = String((replay ? replay.pts.length : persistPos.length) / 3);
    elVis.textContent = String(n);
    const trajDraw = replay ? (replay.trajShown || new Float32Array(0)) : trajPos;
    rebuildTrajRibbon(trajDraw);
    elTraj.textContent = String(trajDraw.length / 3);
    points.visible = ui.cloud;
    device.visible = ui.device;
    trajLine.visible = ui.traj;
    elInfo.style.display = ui.info ? "flex" : "none";
    elHint.style.display = ui.info ? "block" : "none";
    viewDirty = false;
  }

  function setPersist(arr, replace) {
    const incoming = replace ? (arr || new Float32Array(0)) : unseenPoints(arr);
    if (replace) {
      persistPos = new Float32Array(incoming);
      seenKeys = new Set();
      gpuPts = 0;
      rememberPoints(persistPos);
    } else if (incoming.length) {
      rememberPoints(incoming);
      const next = new Float32Array(persistPos.length + incoming.length);
      next.set(persistPos, 0);
      next.set(incoming, persistPos.length);
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
    if (!cloudBounds.isEmpty()) box.union(cloudBounds);
    else if (pointGeo.boundingBox) box.union(pointGeo.boundingBox);
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
      seenKeys = new Set();
      gpuPts = 0;
      cloudBounds.makeEmpty();
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
      if (msg.voxel > 0) mapVoxel = msg.voxel;
      if (!replay) {
        elReady.className = "pill " + (msg.ready ? "on" : "wait");
        elReady.textContent = msg.ready ? "里程计就绪" : "等待里程计";
      }
    } else if (msg.type === "mapSession") {
      elReady.className = "pill wait";
      elReady.textContent = "坐标系已重置";
    } else if (msg.type === "pose" && msg.p) {
      appendTrajPoint(msg.p);
      if (replay) return;
      pose = msg.p;
      onDevicePose(msg.p, msg.q);
      elReady.className = "pill on";
      elReady.textContent = "里程计就绪";
      if (ui.horiz) viewDirty = true;
    }
  }

  function onBin(buf) {
    const frame = parseLmap(buf);
    if (!frame) return;
    if (frame.kind === 1) {
      // Reconnect: the board only still holds points this page has not drawn.
      // Appending them keeps the cloud already on screen.
      const fresh = persistPos.length === 0;
      setPersist(frame.pts, fresh);
      if (frame.traj && frame.traj.length) setTraj(frame.traj, true);
      if (!replay) {
        refreshFloors(true);
        if (fresh) needFit = true;
      }
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

  const REPLAY_WALK_MPS = 0.8;
  const REPLAY_SEE_M = 12;

  function fmtClock(s) {
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  }

  function upperBound(times, t) {
    let lo = 0;
    let hi = times.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (times[mid] <= t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  function strideXYZ(src, maxPoints) {
    const n = src.length / 3;
    if (n <= maxPoints) return src.slice();
    const out = new Float32Array(maxPoints * 3);
    for (let i = 0; i < maxPoints; i++) {
      const j = Math.round(i * (n - 1) / (maxPoints - 1));
      out[i * 3] = src[j * 3];
      out[i * 3 + 1] = src[j * 3 + 1];
      out[i * 3 + 2] = src[j * 3 + 2];
    }
    return out;
  }

  function timelineFor(pts, traj) {
    const nTr = traj.length / 3;
    const trT = new Float32Array(nTr);
    let acc = 0;
    for (let i = 1; i < nTr; i++) {
      acc += Math.hypot(
        traj[i * 3] - traj[(i - 1) * 3],
        traj[i * 3 + 1] - traj[(i - 1) * 3 + 1],
        traj[i * 3 + 2] - traj[(i - 1) * 3 + 2]
      );
      trT[i] = acc / REPLAY_WALK_MPS;
    }
    const nPts = pts.length / 3;
    const ptT = new Float32Array(nPts);
    if (!nTr || !nPts) return { pts, ptT, traj, trT, dur: nTr ? trT[nTr - 1] : 0 };
    const cell = 4;
    const buckets = new Map();
    for (let i = 0; i < nTr; i++) {
      const key = `${Math.floor(traj[i * 3] / cell)},${Math.floor(traj[i * 3 + 1] / cell)},${Math.floor(traj[i * 3 + 2] / cell)}`;
      let list = buckets.get(key);
      if (!list) {
        list = [];
        buckets.set(key, list);
      }
      list.push(i);
    }
    const see2 = REPLAY_SEE_M * REPLAY_SEE_M;
    for (let p = 0; p < nPts; p++) {
      const x = pts[p * 3];
      const y = pts[p * 3 + 1];
      const z = pts[p * 3 + 2];
      const cx = Math.floor(x / cell);
      const cy = Math.floor(y / cell);
      const cz = Math.floor(z / cell);
      let first = -1;
      let near = 0;
      let nearD = Infinity;
      for (let r = 1; r <= 3 && first < 0; r++) {
        for (let dx = -r; dx <= r; dx++) {
          for (let dy = -r; dy <= r; dy++) {
            for (let dz = -r; dz <= r; dz++) {
              const list = buckets.get(`${cx + dx},${cy + dy},${cz + dz}`);
              if (!list) continue;
              for (let k = 0; k < list.length; k++) {
                const i = list[k];
                const ddx = traj[i * 3] - x;
                const ddy = traj[i * 3 + 1] - y;
                const ddz = traj[i * 3 + 2] - z;
                const d2 = ddx * ddx + ddy * ddy + ddz * ddz;
                if (d2 <= see2 && (first < 0 || i < first)) first = i;
                if (d2 < nearD) {
                  nearD = d2;
                  near = i;
                }
              }
            }
          }
        }
      }
      ptT[p] = trT[first >= 0 ? first : near];
    }
    const order = Array.from({ length: nPts }, (_, i) => i);
    order.sort((a, b) => ptT[a] - ptT[b]);
    const ptsS = new Float32Array(nPts * 3);
    const tS = new Float32Array(nPts);
    for (let k = 0; k < nPts; k++) {
      const i = order[k];
      ptsS[k * 3] = pts[i * 3];
      ptsS[k * 3 + 1] = pts[i * 3 + 1];
      ptsS[k * 3 + 2] = pts[i * 3 + 2];
      tS[k] = ptT[i];
    }
    return { pts: ptsS, ptT: tS, traj, trT, dur: trT[nTr - 1] || 0 };
  }

  function packRecording(rec) {
    const npts = rec.pts.length / 3;
    const ntraj = rec.traj.length / 3;
    const buf = new ArrayBuffer(32 + npts * 16 + ntraj * 16);
    const view = new DataView(buf);
    view.setUint8(0, 82);
    view.setUint8(1, 80);
    view.setUint8(2, 76);
    view.setUint8(3, 89);
    view.setUint32(4, 1, true);
    view.setUint32(8, npts, true);
    view.setUint32(12, ntraj, true);
    view.setFloat64(16, 0, true);
    view.setFloat64(24, rec.dur, true);
    let off = 32;
    new Float32Array(buf, off, npts * 3).set(rec.pts);
    off += npts * 12;
    new Float32Array(buf, off, npts).set(rec.ptT);
    off += npts * 4;
    new Float32Array(buf, off, ntraj * 3).set(rec.traj);
    off += ntraj * 12;
    new Float32Array(buf, off, ntraj).set(rec.trT);
    return buf;
  }

  function fileStamp() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  }

  async function writeLocalFile(bytes, name) {
    if (window.showSaveFilePicker) {
      const handle = await window.showSaveFilePicker({
        suggestedName: name,
        types: [{ description: "点云轨迹", accept: { "application/octet-stream": [".bin"] } }],
      });
      const writable = await handle.createWritable();
      await writable.write(bytes);
      await writable.close();
      return;
    }
    const a = document.createElement("a");
    const url = URL.createObjectURL(new Blob([bytes]));
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  async function saveRecording() {
    const btn = document.getElementById("saveRec");
    if (!persistPos.length && !trajPos.length) {
      elReady.className = "pill wait";
      elReady.textContent = "没有可保存的内容";
      return;
    }
    btn.disabled = true;
    btn.textContent = "保存中";
    try {
      const pts = strideXYZ(persistPos, 200000);
      const traj = strideXYZ(trajPos, 20000);
      const rec = timelineFor(pts, traj);
      const name = `lidar-${fileStamp()}.bin`;
      await writeLocalFile(packRecording(rec), name);
      elReady.className = "pill on";
      elReady.textContent = "已保存到本机";
    } catch (err) {
      if (err && err.name === "AbortError") return;
      elReady.className = "pill off";
      elReady.textContent = "保存失败";
    } finally {
      btn.disabled = false;
      btn.textContent = "保存";
    }
  }

  function exitReplay() {
    replay = null;
    document.getElementById("replayBar").hidden = true;
    const btn = document.getElementById("replayBtn");
    btn.textContent = "回放";
    btn.classList.remove("on");
    viewDirty = true;
  }

  function startReplay(buf) {
    const view = new DataView(buf);
    const magic = String.fromCharCode(
      view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
    if (buf.byteLength < 32 || magic !== "RPLY") throw new Error("bad file");
    const npts = view.getUint32(8, true);
    const ntraj = view.getUint32(12, true);
    const t0 = view.getFloat64(16, true);
    const t1 = view.getFloat64(24, true);
    let off = 32;
    const pts = new Float32Array(buf.slice(off, off + npts * 12));
    off += npts * 12;
    const ptT = new Float32Array(buf.slice(off, off + npts * 4));
    off += npts * 4;
    const traj = new Float32Array(buf.slice(off, off + ntraj * 12));
    off += ntraj * 12;
    const trT = new Float32Array(buf.slice(off, off + ntraj * 4));
    replay = {
      pts, ptT, traj, trT,
      dur: Math.max(0, t1 - t0),
      cursor: 0,
      playing: true,
      speed: 4,
      last: performance.now(),
      shownPts: -1,
      shownTr: -1,
      cloud: new Float32Array(0),
      trajShown: new Float32Array(0),
    };
    document.getElementById("replayBar").hidden = false;
    document.getElementById("replayPlay").textContent = "暂停";
    document.getElementById("replaySpeed").textContent = "4×";
    document.getElementById("replayScrub").value = "0";
    const btn = document.getElementById("replayBtn");
    btn.textContent = "回放中";
    btn.classList.add("on");
    elReady.className = "pill on";
    elReady.textContent = "回放";
    needFit = true;
    viewDirty = true;
  }

  function paintReplay() {
    const now = performance.now();
    if (replay.playing) {
      const dt = replay.last ? (now - replay.last) / 1000 : 0;
      replay.cursor = Math.min(replay.dur, replay.cursor + dt * replay.speed);
      if (replay.cursor >= replay.dur - 1e-3) {
        replay.cursor = replay.dur;
        replay.playing = false;
        document.getElementById("replayPlay").textContent = "播放";
      }
      const scrub = document.getElementById("replayScrub");
      if (scrub && replay.dur > 0) scrub.value = String(Math.round(1000 * replay.cursor / replay.dur));
    }
    replay.last = now;
    document.getElementById("replayClock").textContent =
      `${fmtClock(replay.cursor)} / ${fmtClock(replay.dur)}`;
    const nPts = upperBound(replay.ptT, replay.cursor);
    if (nPts !== replay.shownPts) {
      replay.shownPts = nPts;
      replay.cloud = replay.pts.subarray(0, nPts * 3);
      viewDirty = true;
    }
    const nTr = upperBound(replay.trT, replay.cursor);
    if (nTr !== replay.shownTr) {
      replay.shownTr = nTr;
      replay.trajShown = replay.traj.subarray(0, nTr * 3);
      elTraj.textContent = String(nTr);
      viewDirty = true;
    }
    if (nTr > 0) {
      const i = nTr - 1;
      pose = [replay.traj[i * 3], replay.traj[i * 3 + 1], replay.traj[i * 3 + 2]];
      onDevicePose(pose);
    }
  }

  document.getElementById("saveRec").addEventListener("click", () => { saveRecording(); });
  document.getElementById("replayBtn").addEventListener("click", () => {
    if (replay) exitReplay();
    else document.getElementById("replayFile").click();
  });
  document.getElementById("replayFile").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        startReplay(reader.result);
      } catch (err) {
        elReady.className = "pill off";
        elReady.textContent = "文件无法回放";
      }
    };
    reader.readAsArrayBuffer(file);
  });
  document.getElementById("replayExit").addEventListener("click", exitReplay);
  document.getElementById("replayPlay").addEventListener("click", () => {
    if (!replay) return;
    if (!replay.playing && replay.cursor >= replay.dur - 1e-3) replay.cursor = 0;
    replay.playing = !replay.playing;
    replay.last = performance.now();
    document.getElementById("replayPlay").textContent = replay.playing ? "暂停" : "播放";
  });
  document.getElementById("replaySpeed").addEventListener("click", () => {
    if (!replay) return;
    replay.speed = replay.speed === 1 ? 4 : replay.speed === 4 ? 16 : 1;
    document.getElementById("replaySpeed").textContent = `${replay.speed}×`;
  });
  const replayScrub = document.getElementById("replayScrub");
  replayScrub.addEventListener("input", () => {
    if (!replay) return;
    replay.playing = false;
    replay.cursor = replay.dur * Number(replayScrub.value) / 1000;
    document.getElementById("replayPlay").textContent = "播放";
  });

  updateLayerLabel();

  function tick() {
    resize();
    if (replay) paintReplay();
    if (viewDirty) applyView();
    if (needFit && persistPos.length > 90) {
      applyView();
      fit();
    }
    updateWalkAnim(performance.now());
    placeCamera();
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  }

  connect();
  tick();
})();
