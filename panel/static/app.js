"use strict";

const byId = (id) => document.getElementById(id);
const message = byId("message");
let messageTimer = null;

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
  messageTimer = window.setTimeout(() => { message.hidden = true; }, error ? 8000 : 4000);
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
  byId("vless-uri").value = data.vlessUri;
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

async function copyUri() {
  const value = byId("vless-uri").value;
  if (!value) {
    notify("VLESS-ссылка ещё не загружена.", true);
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    notify("VLESS-ссылка скопирована.");
  } catch (_) {
    byId("vless-uri").focus();
    byId("vless-uri").select();
    notify("Ссылка выделена. Нажмите Ctrl+C или используйте меню браузера.");
  }
}

document.querySelectorAll("[data-restart]").forEach((button) => {
  button.addEventListener("click", () => restart(button));
});
byId("copy-uri").addEventListener("click", copyUri);

refresh();
window.setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, 15000);
