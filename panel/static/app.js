"use strict";

const byId = (id) => document.getElementById(id);
const message = byId("message");
let messageTimer = null;
let clientsCache = {wireguard: [], vless: []};
let selectedUriName = "";

function bytes(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function notify(text, error = false) {
  window.clearTimeout(messageTimer);
  message.textContent = text;
  message.classList.toggle("error", error);
  message.hidden = false;
  messageTimer = window.setTimeout(() => { message.hidden = true; }, error ? 8000 : 4500);
}

function renderService(name, state) {
  const active = state === "active";
  byId(`${name}-status`).textContent = active ? "Работает" : state;
  const dot = byId(`${name}-dot`);
  dot.classList.toggle("active", active);
  dot.classList.toggle("failed", !active);
}

function handshakeText(timestamp) {
  if (!timestamp) return "Handshake: нет";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000) - timestamp);
  if (seconds < 60) return `Handshake: ${seconds} с назад`;
  if (seconds < 3600) return `Handshake: ${Math.floor(seconds / 60)} мин назад`;
  if (seconds < 86400) return `Handshake: ${Math.floor(seconds / 3600)} ч назад`;
  return `Handshake: ${Math.floor(seconds / 86400)} дн назад`;
}

function actionButton(label, action, protocol, name, extraClass = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `action-button ${extraClass}`.trim();
  button.textContent = label;
  button.dataset.clientAction = action;
  button.dataset.protocol = protocol;
  button.dataset.name = name;
  return button;
}

function downloadLink(label, href) {
  const link = document.createElement("a");
  link.className = "action-button";
  link.textContent = label;
  link.href = href;
  return link;
}

function renderClients(clients) {
  clientsCache = clients;
  const list = byId("client-list");
  list.replaceChildren();
  let total = 0;
  for (const protocol of ["wireguard", "vless"]) {
    for (const client of clients[protocol]) {
      total += 1;
      const row = document.createElement("article");
      row.className = "client-row";
      const identity = document.createElement("div");
      identity.className = "client-identity";
      const title = document.createElement("strong");
      title.textContent = client.name;
      const meta = document.createElement("span");
      meta.textContent = protocol === "wireguard" ? `WireGuard · ${client.address}` : "VLESS · REALITY + Vision";
      identity.append(title, meta);

      const actions = document.createElement("div");
      actions.className = "client-actions";
      actions.append(downloadLink("Скачать", client.download));
      if (protocol === "vless") {
        actions.append(downloadLink("JSON", client.json));
        actions.append(actionButton("Копировать", "copy", protocol, client.name));
      }
      actions.append(actionButton("Перевыпустить", "regenerate", protocol, client.name));
      actions.append(actionButton("Удалить", "delete", protocol, client.name, "danger"));
      row.append(identity, actions);
      list.append(row);
    }
  }
  if (!total) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Клиенты не найдены.";
    list.append(empty);
  }
  if (selectedUriName) {
    const selected = clients.vless.find((client) => client.name === selectedUriName);
    byId("vless-uri").value = selected ? selected.uri : "";
    if (!selected) selectedUriName = "";
  } else if (!byId("vless-uri").value && clients.vless.length) {
    byId("vless-uri").value = clients.vless[0].uri;
  }
}

function render(data) {
  byId("server-host").textContent = data.serverHost;
  byId("sync-time").textContent = new Date(data.timestamp * 1000).toLocaleString("ru-RU");
  renderService("wireguard", data.services.wireguard);
  renderService("vless", data.services.vless);
  byId("wireguard-meta").textContent = `UDP ${data.ports.wireguard} · ${handshakeText(data.wireguard.handshake)}`;
  byId("wireguard-traffic").textContent = `RX ${bytes(data.wireguard.rx)} / TX ${bytes(data.wireguard.tx)}`;
  byId("vless-meta").textContent = `TCP ${data.ports.vless} · REALITY + Vision`;
  byId("xray-version").textContent = data.xrayVersion;

  const diskPercent = data.disk.total ? Math.round(data.disk.used / data.disk.total * 100) : 0;
  byId("disk-value").textContent = `${diskPercent}%`;
  byId("disk-detail").textContent = `${bytes(data.disk.used)} занято · ${bytes(data.disk.free)} свободно`;
  byId("disk-meter").style.transform = `scaleX(${Math.min(1, Math.max(0, diskPercent / 100))})`;

  const memoryUsed = Math.max(0, data.memory.total - data.memory.available);
  const memoryPercent = data.memory.total ? Math.round(memoryUsed / data.memory.total * 100) : 0;
  byId("memory-value").textContent = `${memoryPercent}%`;
  byId("memory-detail").textContent = `${bytes(memoryUsed)} занято · ${bytes(data.memory.available)} доступно`;
  byId("memory-meter").style.transform = `scaleX(${Math.min(1, Math.max(0, memoryPercent / 100))})`;
  renderClients(data.clientLists);
}

async function refresh() {
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    notify(`Не удалось обновить состояние: ${error.message}`, true);
  }
}

async function restart(button) {
  const service = button.dataset.restart;
  const label = service === "wireguard" ? "WireGuard" : "VLESS";
  if (!window.confirm(`Перезапустить ${label}? Текущие подключения ненадолго прервутся.`)) return;
  button.disabled = true;
  const previous = button.textContent;
  button.textContent = "Перезапуск…";
  try {
    const response = await fetch("/api/restart", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({service}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.status || result.error || `HTTP ${response.status}`);
    notify(`${label}: сервис перезапущен (${result.status}).`);
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    await refresh();
  } catch (error) {
    notify(`Ошибка перезапуска ${label}: ${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

async function copyText(value) {
  if (!value) {
    notify("VLESS-ссылка ещё не загружена.", true);
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    notify("VLESS-ссылка скопирована.");
  } catch (_) {
    const field = byId("vless-uri");
    field.focus();
    field.select();
    notify("Ссылка выделена. Нажмите Ctrl+C или используйте меню браузера.");
  }
}

function setMutationButtons(disabled) {
  document.querySelectorAll("[data-client-action], #create-client").forEach((button) => {
    button.disabled = disabled;
  });
}

async function mutate(protocol, action, name) {
  setMutationButtons(true);
  try {
    const response = await fetch("/api/clients", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({protocol, action, name}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    renderClients(result.clients);
    const labels = {create: "создан", regenerate: "перевыпущен", delete: "удалён"};
    if (result.restart.ok) {
      notify(`${name}: клиент ${labels[action]}, сервис перезапущен.`);
    } else {
      notify(`${name}: изменения сохранены, но перезапуск не выполнен: ${result.restart.status}`, true);
    }
    await refresh();
  } catch (error) {
    notify(`Операция не выполнена: ${error.message}`, true);
  } finally {
    setMutationButtons(false);
  }
}

byId("create-client-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = byId("new-client-name").value.trim();
  const protocol = byId("new-client-protocol").value;
  mutate(protocol, "create", name).then(() => {
    byId("new-client-name").value = "";
  });
});

byId("client-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-client-action]");
  if (!button) return;
  const {clientAction: action, protocol, name} = button.dataset;
  if (action === "copy") {
    const client = clientsCache.vless.find((item) => item.name === name);
    selectedUriName = name;
    byId("vless-uri").value = client ? client.uri : "";
    copyText(client ? client.uri : "");
    return;
  }
  if (action === "regenerate") {
    if (!window.confirm(`Перевыпустить ключи ${name}? Старая конфигурация перестанет работать.`)) return;
  }
  if (action === "delete") {
    if (!window.confirm(`Удалить ${name}? Конфигурация и доступ этого клиента будут удалены.`)) return;
  }
  mutate(protocol, action, name);
});

document.querySelectorAll("[data-restart]").forEach((button) => {
  button.addEventListener("click", () => restart(button));
});
byId("copy-uri").addEventListener("click", () => copyText(byId("vless-uri").value));

refresh();
window.setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, 15000);
