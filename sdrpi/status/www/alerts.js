// Shows a banner at the top of the page while this Pi, or the other one, has an active alert. Included by every page.
(() => {
  const STALE_SECONDS = 300;
  let box = null;

  function render(items) {
    if (!items.length) {
      if (box) { box.remove(); box = null; }
      return;
    }
    const critical = items.some(i => i.level === "critical");
    if (!box) {
      box = document.createElement("div");
      box.id = "pistatus-alert";
      box.setAttribute("role", "alert");
      const power = document.getElementById("power-warning");
      if (power) power.after(box); else document.body.prepend(box);
    }
    box.style.cssText = "flex:none;color:#fff;font:600 15px/1.35 system-ui,sans-serif;padding:9px 16px;text-align:center;"
      + (critical ? "background:#c62828;border:2px solid #7f0000" : "background:#9a5b00;border:2px solid #5c3700");
    box.replaceChildren();
    box.append(document.createTextNode(`${items.length === 1 ? "Alert" : items.length + " alerts"}: ${items.map(i => i.text).join(" · ")} · `));
    const link = document.createElement("a");
    link.href = "/status/";
    link.textContent = "Status page";
    link.style.color = "#fff";
    box.append(link);
  }

  async function poll() {
    let data;
    try {
      data = await (await fetch("/status/data/status.json", {cache: "no-store"})).json();
    } catch (error) {
      return;
    }
    // power.js already shows undervoltage on sdrpi pages, every 5 seconds
    const power = document.getElementById("power-warning");
    const items = (data.alerts || []).filter(a => !(power && a.id === "power-0")).map(a => ({level: a.level, text: a.text}));
    if (Date.now() / 1000 - data.generated > STALE_SECONDS) items.push({level: "warning", text: `${data.name} status checks stopped updating`});
    if (data.peer && data.peer.reachable) (data.peer.alerts || []).forEach(a => items.push({level: a.level, text: `${data.peer.name}: ${a.text}`}));
    render(items);
  }

  poll();
  setInterval(poll, 30000);
})();
