const initialState = Object.freeze({
  ready: false,
  authenticated: false,
  recentJob: null,
  capabilities: [],
  error: "",
});

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
};

const store = createStore(initialState);
store.subscribe(render);
render(store.getState());

fetch("/api/bootstrap", { headers: { Accept: "application/json" } })
  .then((response) => {
    if (!response.ok) throw new Error("bootstrap failed");
    return response.json();
  })
  .then((payload) => store.setState({
    ready: true,
    authenticated: payload.authenticated === true,
    recentJob: payload.recent_job || null,
    capabilities: Array.isArray(payload.capabilities) ? payload.capabilities : [],
  }))
  .catch(() => store.setState({
    ready: false,
    error: "无法连接本机服务，请关闭页面后重新打开应用。",
  }));
