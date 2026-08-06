(() => {
  "use strict";

  const processed = new Set();
  let initialized = false;
  let switchingChat = false;
  let processing = false;
  let pendingUnreadCount = 0;
  let scanTimer = null;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function hash(text) {
    let value = 2166136261;
    for (let i = 0; i < text.length; i += 1) {
      value ^= text.charCodeAt(i);
      value = Math.imul(value, 16777619);
    }
    return (value >>> 0).toString(16);
  }

  function messageNodes() {
    const rows = [...document.querySelectorAll("#main [data-testid^='conv-msg-']")];
    const incoming = [];
    let direction = "";
    for (const row of rows) {
      if (row.querySelector("[data-testid='tail-in']")) direction = "in";
      else if (row.querySelector("[data-testid='tail-out']")) direction = "out";
      if (direction === "in") incoming.push(row);
    }
    return incoming;
  }

  function getChatTitle() {
    const candidates = [
      "#main header span[title]",
      "#main header [data-testid='conversation-info-header-chat-title']",
      "#main header [dir='auto']"
    ];
    for (const selector of candidates) {
      const element = document.querySelector(selector);
      const value = element?.getAttribute("title") || element?.textContent;
      if (value?.trim()) return value.trim();
    }
    return "WhatsApp chat";
  }

  function parseNode(node) {
    const copyable = node.querySelector("[data-pre-plain-text]");
    const pre = copyable?.getAttribute("data-pre-plain-text") || "";
    const senderMatch = pre.match(/\]\s*(.*?)\s*:\s*$/u);
    const sender = senderMatch ? senderMatch[1].trim() : "";
    const textParts = [...node.querySelectorAll("span.selectable-text, [data-testid='msg-text']")]
      .map((element) => (element.innerText || element.textContent || "").trim())
      .filter(Boolean);
    const text = [...new Set(textParts)].join("\n").trim();
    let mediaType = "";
    if (node.querySelector("video")) mediaType = "video";
    else if (node.querySelector("audio")) mediaType = "audio";
    else if (node.querySelector("img[src^='blob:'], img[src^='data:']")) mediaType = "image";
    else if (node.querySelector("[data-testid*='sticker']")) mediaType = "sticker";
    else if (node.querySelector("[data-testid*='document'], [aria-label*='document' i]")) mediaType = "document";
    const rawId = node.getAttribute("data-id") || node.querySelector("[data-id]")?.getAttribute("data-id");
    const id = rawId || hash(`${pre}\n${text}\n${mediaType}`);
    return { id, sender, text, mediaType, chatTitle: getChatTitle() };
  }

  async function bridgePort() {
    const data = await chrome.storage.local.get({ bridgePort: 8765 });
    return Number(data.bridgePort) || 8765;
  }

  async function requestReply(payload) {
    const port = await bridgePort();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 40000);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/v1/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`bridge ${response.status}`);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function reportReady() {
    try {
      const port = await bridgePort();
      await fetch(`http://127.0.0.1:${port}/v1/status`);
    } catch (_) {
      // The desktop service may be started after WhatsApp Web; the timer retries.
    }
  }

  async function sendMessage(reply) {
    const box = document.querySelector("#main footer [contenteditable='true'][role='textbox']") ||
      document.querySelector("#main footer [contenteditable='true']") ||
      document.querySelector("#main [contenteditable='true'][role='textbox']") ||
      document.querySelector("[contenteditable='true'][role='textbox'][data-tab]");
    if (!box) throw new Error("WhatsApp input box not found");
    box.focus();
    document.execCommand("insertText", false, reply);
    box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: reply }));
    await sleep(250);
    const sendButton = document.querySelector("#main footer [data-testid='compose-btn-send']") ||
      document.querySelector("#main [data-testid='compose-btn-send']") ||
      document.querySelector("#main button[aria-label='Send'], #main button[aria-label='发送'], #main button[aria-label='إرسال']");
    if (sendButton) {
      sendButton.click();
      return;
    }
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    box.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
  }

  async function processBatch(nodes) {
    const items = nodes.map(parseNode).filter((item) => (item.text || item.mediaType) && !processed.has(item.id));
    if (!items.length || processing) return;
    processing = true;
    const payload = { ...items[items.length - 1] };
    payload.text = items.map((item) => item.text || `[${item.mediaType}]`).join("\n");
    payload.mediaType = items.map((item) => item.mediaType).filter(Boolean).join(",");
    try {
      const result = await requestReply(payload);
      if (result.ok) {
        items.forEach((item) => processed.add(item.id));
        if (result.send && result.reply) await sendMessage(result.reply);
      }
    } catch (error) {
      console.debug("AutoReply bridge retry pending:", error.message);
    } finally {
      processing = false;
    }
  }

  function unreadChatRow() {
    const badge = document.querySelector("[role='grid'] [data-testid='icon-unread-count']");
    return badge?.closest("[role='row']") || null;
  }

  async function openNextUnreadChat() {
    if (switchingChat || processing) return;
    const row = unreadChatRow();
    if (!row) return;
    const badge = row.querySelector("[data-testid='icon-unread-count']");
    pendingUnreadCount = Math.max(1, Math.min(20, Number((badge?.textContent || "1").trim()) || 1));
    const target = row.querySelector("[data-testid='cell-frame-container']") || row.querySelector("[role='gridcell']") || row;
    switchingChat = true;
    target.scrollIntoView({ block: "nearest" });
    target.click();
    await sleep(900);
    switchingChat = false;
    scheduleScan(100);
  }

  async function scan() {
    if (switchingChat || processing) return;
    const nodes = messageNodes();
    if (!initialized) {
      nodes.forEach((node) => processed.add(parseNode(node).id));
      initialized = true;
      console.info("AutoReply WhatsApp Bridge v2.1.0 ready; existing messages ignored.");
      openNextUnreadChat();
      return;
    }
    if (pendingUnreadCount) {
      const count = pendingUnreadCount;
      pendingUnreadCount = 0;
      await processBatch(nodes.slice(-count));
      openNextUnreadChat();
      return;
    }
    await processBatch(nodes.slice(-8));
    openNextUnreadChat();
  }

  function scheduleScan(delay = 500) {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scan, delay);
  }

  scheduleScan(2000);
  setTimeout(reportReady, 1000);
  new MutationObserver(() => scheduleScan()).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(() => scheduleScan(0), 4000);
  setInterval(reportReady, 10000);
})();
