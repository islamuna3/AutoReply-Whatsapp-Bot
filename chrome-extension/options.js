const port = document.querySelector("#port");
const status = document.querySelector("#status");

chrome.storage.local.get({ bridgePort: 8765 }, (data) => {
  port.value = data.bridgePort;
});

document.querySelector("#save").addEventListener("click", async () => {
  const value = Number(port.value);
  if (value < 1024 || value > 65535) {
    status.textContent = "端口无效";
    return;
  }
  await chrome.storage.local.set({ bridgePort: value });
  status.textContent = "已保存";
  setTimeout(() => { status.textContent = ""; }, 1500);
});
