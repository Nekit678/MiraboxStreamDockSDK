(() => {
  "use strict";

  const client = window.MiraBoxPropertyInspector;
  const reset = document.getElementById("reset");

  client.on("connected", () => {
    reset.disabled = false;
  });

  reset.addEventListener("click", () => {
    client.sendToPlugin({ event: "reset" });
  });
})();
