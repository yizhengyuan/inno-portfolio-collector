const initialState = Object.freeze({
  ready: false,
  authenticated: false,
  recentJob: null,
  capabilities: [],
  preflightPassed: false,
  activeJob: "",
  error: "",
});

const sessionToken = document
  .querySelector('meta[name="inno-session-token"]')
  .getAttribute("content");

const createStore = (seed) => {
  let state = seed;
  const listeners = new Set();
  return Object.freeze({
    getState: () => state,
    setState: (next) => {
      state = Object.freeze({ ...state, ...next });
      listeners.forEach((listener) => listener(state));
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  });
};

const api = async (path, options = {}) => {
  const response = await fetch(path, options);
  const contentType = response.headers.get("Content-Type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) throw new Error(payload?.error?.message || "request failed");
  return payload;
};

const writeJson = (path, payload = {}) => api(path, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Inno-Session": sessionToken,
  },
  body: JSON.stringify(payload),
});

export const viewModel = (state) => Object.freeze({
  connection: state.ready ? "本机服务已连接" : "正在连接本机服务",
  login: state.authenticated ? "已登录" : "未登录",
  job: state.recentJob?.status || "暂无",
  error: state.error,
});

export const render = (state) => {
  const view = viewModel(state);
  document.querySelector("#connection-status").textContent = view.connection;
  document.querySelector("#login-state").textContent = view.login;
  document.querySelector("#job-state").textContent = view.job;
  const error = document.querySelector("#bootstrap-error");
  error.textContent = view.error;
  error.hidden = !view.error;
  document.querySelector("#login-start").disabled = !state.capabilities.includes("login");
  document.querySelector("#preflight-start").disabled = !state.capabilities.includes("preflight");
  document.querySelector("#collection-start").disabled = !(
    state.capabilities.includes("collection") && state.preflightPassed && !state.activeJob
  );
  document.querySelector("#collection-cancel").disabled = !state.activeJob;
};

const store = createStore(initialState);
store.subscribe(render);
render(store.getState());

const setLoginMessage = (message) => {
  document.querySelector("#login-message").textContent = message;
};

const completeLogin = async (loginId) => {
  await writeJson(`/api/login/${loginId}/complete`);
  setLoginMessage("登录已完成，只保存在这台 Mac。现在可以运行预检。");
  store.setState({ authenticated: true });
};

const pollLogin = async (loginId) => {
  try {
    const status = await api(`/api/login/${loginId}/status`);
    setLoginMessage(status.message_zh || "正在等待微信确认");
    if (status.status === "confirmed") {
      await completeLogin(loginId);
      return;
    }
    if (["expired", "failed", "cancelled", "account_not_bound_email"].includes(status.status)) return;
    setTimeout(() => pollLogin(loginId), 2000);
  } catch (error) {
    setLoginMessage(error.message || "登录状态读取失败，请重新开始。");
  }
};

const startLogin = async () => {
  const button = document.querySelector("#login-start");
  button.disabled = true;
  const session = document.querySelector("#login-session");
  session.hidden = false;
  setLoginMessage("正在生成二维码");
  try {
    const login = await writeJson("/api/login/start");
    document.querySelector("#login-qrcode").src = `/api/login/${login.login_id}/qrcode`;
    setLoginMessage("请使用微信扫描二维码。");
    setTimeout(() => pollLogin(login.login_id), 2000);
  } catch (error) {
    setLoginMessage(error.message || "无法开始登录，请稍后重试。");
    button.disabled = false;
  }
};

const renderPreflight = (rows) => {
  const body = document.querySelector("#preflight-rows");
  body.replaceChildren();
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    [
      row.project,
      row.account,
      row.mapping,
      row.login,
      String(row.catalog),
      row.date_filter,
      row.status,
      row.reason || "—",
    ].forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    });
    body.append(tr);
  });
};

const runPreflight = async () => {
  const state = document.querySelector("#preflight-state");
  state.textContent = "正在检查";
  try {
    const result = await writeJson("/api/preflight", { since: "2026-01-01" });
    renderPreflight(result.projects || []);
    state.textContent = result.ok ? "全部通过" : "存在需处理项目";
    store.setState({ preflightPassed: result.ok === true });
  } catch (error) {
    state.textContent = error.message || "预检失败";
  }
};

const pollCollection = async (jobId) => {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    document.querySelector("#job-state").textContent = job.status;
    if (["queued", "running"].includes(job.status)) {
      setTimeout(() => pollCollection(jobId), 1000);
      return;
    }
    store.setState({ activeJob: "" });
  } catch (error) {
    document.querySelector("#job-state").textContent = error.message || "任务状态不可用";
    store.setState({ activeJob: "" });
  }
};

const startCollection = async () => {
  try {
    const submitted = await writeJson("/api/collection", { since: "2026-01-01" });
    store.setState({ activeJob: submitted.job_id });
    pollCollection(submitted.job_id);
  } catch (error) {
    document.querySelector("#job-state").textContent = error.message || "无法开始采集";
  }
};

const cancelCollection = async () => {
  const jobId = store.getState().activeJob;
  if (!jobId) return;
  try {
    await writeJson(`/api/jobs/${jobId}/cancel`);
    document.querySelector("#job-state").textContent = "正在取消";
  } catch (error) {
    document.querySelector("#job-state").textContent = error.message || "无法取消任务";
  }
};

const bootstrap = async () => {
  try {
    const payload = await api("/api/bootstrap");
    store.setState({
      ready: true,
      authenticated: payload.authenticated === true,
      recentJob: payload.recent_job || null,
      capabilities: Array.isArray(payload.capabilities) ? payload.capabilities : [],
    });
  } catch (_error) {
    store.setState({
      ready: false,
      error: "无法连接本机服务，请关闭页面后重新打开应用。",
    });
  }
};

document.querySelector("#login-start").addEventListener("click", startLogin);
document.querySelector("#preflight-start").addEventListener("click", runPreflight);
document.querySelector("#collection-start").addEventListener("click", startCollection);
document.querySelector("#collection-cancel").addEventListener("click", cancelCollection);
bootstrap();
