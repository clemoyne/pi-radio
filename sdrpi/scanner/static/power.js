(() => {
  let box = null;

  async function poll() {
    let status;
    try {
      status = await (await fetch("/api/power", {cache: "no-store"})).json();
    } catch (error) {
      return;
    }
    if (status.warning) {
      if (!box) {
        box = document.createElement("div");
        box.id = "power-warning";
        box.setAttribute("role", "alert");
        box.style.cssText = "flex:none;background:#c62828;color:#fff;border:2px solid #7f0000;font:700 18px/1.3 system-ui,sans-serif;text-align:center;padding:10px 16px";
        document.body.prepend(box);
      }
      box.textContent = status.text;
    } else if (box) {
      box.remove();
      box = null;
    }
  }

  poll();
  setInterval(poll, 5000);
})();
