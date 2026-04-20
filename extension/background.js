chrome.runtime.onMessage.addListener(function (msg, _sender, sendResponse) {
  if (msg.type !== "fetchRating") return false;

  fetch(msg.url)
    .then(function (res) {
      if (!res.ok) {
        sendResponse({ ok: false, status: res.status });
        return;
      }
      return res.json().then(function (data) {
        sendResponse({ ok: true, data: data });
      });
    })
    .catch(function (err) {
      sendResponse({ ok: false, error: err.message });
    });

  // Return true to keep the message channel open for the async response
  return true;
});
