// Runs in ISOLATED world — has access to chrome.runtime.
// Bridges custom DOM events from the MAIN-world content script to the background service worker.
console.log("[TRACE-BRIDGE] bridge.js loaded");

document.addEventListener("trace-fetch-request", function (e) {
  var id = e.detail.id;
  var url = e.detail.url;
  console.log("[TRACE-BRIDGE] received fetch request, url=", url);

  chrome.runtime.sendMessage({ type: "fetchRating", url: url }, function (resp) {
    if (chrome.runtime.lastError) {
      console.error("[TRACE-BRIDGE] sendMessage error:", chrome.runtime.lastError.message);
    } else {
      console.log("[TRACE-BRIDGE] got response from background:", resp);
    }

    document.dispatchEvent(
      new CustomEvent("trace-fetch-response", {
        detail: {
          id: id,
          ok: resp && resp.ok,
          data: resp && resp.data,
          status: resp && resp.status,
          error: (chrome.runtime.lastError && chrome.runtime.lastError.message) || null,
        },
      })
    );
  });
});
